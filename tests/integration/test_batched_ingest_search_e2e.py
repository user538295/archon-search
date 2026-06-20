"""E2e: batched ingest → both batches searchable (D4 T-1).

Scenario S12: POST /ingest a file that produces >512 chunks via HTTP.
Poll until DONE. Then search for tokens from batch 1 and batch 2.
Both must return results, proving both sections of the file are searchable after a multi-batch ingest.

The file is structured as:
  - 512 paragraphs each containing "alpha-batch-one" → becomes batch 1
  - 108 paragraphs each containing "beta-batch-two"  → becomes batch 2

Each paragraph is ~2100 characters of English-like filler text.  With the
GPT-2 tokenizer at 512-token chunk_size (the default), each paragraph produces
≥1 chunk.  620 paragraphs → ≥620 chunks, well above the 512-chunk batch boundary.

Uses make_real_app (real LanceDB, real pipeline, stub embedder from tests/conftest.py).
The stub embedder returns zero-vectors, so vector search produces uniform scores;
FTS provides the discriminating signal for matching distinct tokens.

Run with:
    uv run pytest tests/integration/test_batched_ingest_search_e2e.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app, search

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BATCH_1_TOKEN = "alpha-batch-one"
_BATCH_2_TOKEN = "beta-batch-two"

# Number of paragraphs in each section.
# 512 + 108 = 620 paragraphs → ≥620 chunks → triggers a second batch.
_BATCH_1_PARAGRAPHS = 512
_BATCH_2_PARAGRAPHS = 108

# Each paragraph must be large enough to exceed 512 GPT-2 tokens so the
# chunker does not merge adjacent paragraphs.  GPT-2 averages ~4 chars/token,
# so 512 tokens ≈ 2048 chars.  We use ~2100-char paragraphs to be safe.
_FILLER = (
    "The quick brown fox jumps over the lazy dog near the riverbank. "
    "A software system is characterized by the combination of its components, "
    "their interactions, and the environment in which it operates. "
    "Distributed computing enables workloads to be spread across many machines, "
    "improving both throughput and fault tolerance at the cost of added complexity. "
    "Data pipelines transform raw input into structured representations suitable "
    "for downstream consumption by analytics engines, machine learning models, "
    "and reporting dashboards. "
    "Indexing strategies determine how quickly a system can locate specific "
    "records among millions of stored documents, with trade-offs between "
    "write amplification, storage overhead, and query latency. "
    "Vector similarity search complements traditional keyword retrieval by "
    "capturing semantic relationships between queries and documents even when "
    "surface-level terms do not overlap. "
    "Hybrid retrieval systems combine dense embedding lookups with sparse BM25 "
    "or TF-IDF scoring, then merge results through reciprocal rank fusion or "
    "a learned combination model. "
    "Cross-encoder reranking refines a candidate set by jointly attending to "
    "the query and each passage, producing more accurate relevance scores than "
    "bi-encoder retrieval at the expense of higher inference cost. "
    "The engineering team invested significant effort in reducing tail latencies "
    "by profiling hot paths, batching small operations, and tuning garbage "
    "collection heuristics in the runtime environment. "
)  # ~1190 chars; repeated twice below to reach ~2380 chars per paragraph


def _build_large_corpus(tmp_path: Path) -> Path:
    """Write a plain-text file with 620 paragraphs totalling >512 chunks.

    Paragraphs 1-512 embed ``alpha-batch-one`` (batch 1).
    Paragraphs 513-620 embed ``beta-batch-two`` (batch 2).
    """
    filler_body = _FILLER + _FILLER  # ~2380 chars ≈ 595 GPT-2 tokens

    lines: list[str] = []
    for i in range(_BATCH_1_PARAGRAPHS):
        lines.append(f"{_BATCH_1_TOKEN} paragraph {i:04d}: {filler_body}")
        lines.append("")  # blank separator

    for i in range(_BATCH_2_PARAGRAPHS):
        lines.append(f"{_BATCH_2_TOKEN} paragraph {i:04d}: {filler_body}")
        lines.append("")  # blank separator

    corpus_file = tmp_path / "large_corpus.txt"
    corpus_file.write_text("\n".join(lines), encoding="utf-8")
    return corpus_file


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_batched_ingest_search_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /ingest a file whose first 512 chunks carry 'alpha-batch-one' and
    remaining chunks carry 'beta-batch-two'.  Poll until DONE.  Search for each
    token and assert at least one hit.

    This verifies that a file large enough to span multiple ingest batches results in all content being searchable — tokens from early paragraphs and tokens from later paragraphs both produce hits.
    """
    corpus_file = _build_large_corpus(tmp_path)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-batched-ingest"

        # Ingest with a generous timeout: 620 paragraphs × real LanceDB writes.
        ingest_file_via_path(
            client,
            col,
            str(corpus_file),
            api_key=api_key,
            timeout_s=120.0,
        )

        # Batch 1 token must be searchable.
        batch1_results = search(client, col, _BATCH_1_TOKEN, api_key=api_key)
        assert batch1_results, (
            f"Expected at least one result for '{_BATCH_1_TOKEN}' after batched ingest. "
            "Batch 1 chunks may not have been written to the store."
        )
        assert any(_BATCH_1_TOKEN in r["text"] for r in batch1_results), (
            f"Expected at least one result with '{_BATCH_1_TOKEN}' in text. "
            "Results returned but did not contain the expected token — FTS may be matching "
            "unrelated terms."
        )

        # Batch 2 token must be searchable.
        batch2_results = search(client, col, _BATCH_2_TOKEN, api_key=api_key)
        assert batch2_results, (
            f"Expected at least one result for '{_BATCH_2_TOKEN}' after batched ingest. "
            "Batch 2 chunks may not have been written to the store."
        )
        assert any(_BATCH_2_TOKEN in r["text"] for r in batch2_results), (
            f"Expected at least one result with '{_BATCH_2_TOKEN}' in text. "
            "If this fails but the non-empty check passes, batch 2 chunks may not have been "
            "written — FTS matched on shared sub-tokens ('batch') from batch 1."
        )
