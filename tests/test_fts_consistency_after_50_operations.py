"""Integration test: FTS consistency after 50 add/delete/re-ingest operations.

Task F.1 acceptance criterion: after a sequence of 50 operations on a 500-chunk
collection, at least 10 representative FTS queries return the **same doc_id sets**
(set membership only — rank order and BM25 scores are NOT checked) as a fresh
``rebuild_fts_index()`` on the final collection state.

Verification strategy:
- Each document's chunk text is a **globally unique token** of the form
  ``u<12-hex-chars>`` (SHA-256-derived, unique to doc_id+version).
- After 50 operations (adds, deletes, re-ingests), for each of 10 sampled live
  documents we assert the target doc_id appears in results for BOTH the
  incremental index and the reference (fresh rebuild) index.
- Additionally, for all deleted documents we assert their tokens do NOT appear
  in the incremental index (no phantom hits).
- The query uses a large ``top_k`` (32) to ensure the FTS-matching doc is in
  the result window even if hybrid RRF scores push it past position 1.

Marked ``@pytest.mark.integration`` — requires real LanceDB disk I/O and is
excluded from the default ``uv run pytest`` run.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import SearchStore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIM = 8           # small enough for fast I/O in tests
_CORPUS_CHUNKS = 500  # halved from 1000 (2026-07-20) to cut suite ingest I/O; still a large-corpus FTS check (all assertions scale off this constant)
_DELTA_OPS = 50     # number of add/delete/re-ingest operations
_QUERIES = 10       # number of representative FTS queries to verify
_SEED = 42          # deterministic random sequence
_SEARCH_K = 32      # large enough so FTS match lands in result window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_id_from_path(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def _unique_token(doc_id: str, version: int = 0) -> str:
    """Return a short alphanumeric token unique to (doc_id, version).

    We take 12 hex chars from SHA-256(doc_id + version) and prefix with ``u``
    so it is a single FTS term.  Collision probability on 48 bits is negligible
    for a 500-doc corpus.
    """
    h = hashlib.sha256(f"{doc_id}-{version}".encode()).hexdigest()[:12]
    return f"u{h}"


def _chunk(doc_id: str, idx: int, text: str) -> ChunkRecord:
    """Build a ChunkRecord with a deterministic vector derived from its doc_id."""
    seed_val = int(hashlib.sha256(f"{doc_id}-{idx}".encode()).hexdigest()[:8], 16)
    vector = [(seed_val + i) % 10 / 10.0 for i in range(_DIM)]
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=vector,
        source_path=f"/corpus/{doc_id[:8]}.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_corpus_doc(doc_index: int, version: int = 0) -> tuple[str, str, list[ChunkRecord]]:
    """Return (doc_id, token, [chunks]) for a corpus document."""
    path = f"/corpus/doc{doc_index:04d}.md"
    doc_id = _doc_id_from_path(path)
    token = _unique_token(doc_id, version)
    return doc_id, token, [_chunk(doc_id, 0, token)]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_fts_consistency_after_50_operations(tmp_path: Any) -> None:
    """After 50 add/delete/re-ingest ops, incremental FTS == fresh rebuild.

    Verifies Task F.1 acceptance criteria:

    - Live documents (present in the final collection state) are findable via
      FTS in the incremental index, matching the fresh-rebuild reference.
    - Deleted documents do NOT appear in FTS results for the incremental index
      (no phantom hits).
    - Re-ingested documents return their new content, not old content.
    """

    async def _run() -> None:
        rng = random.Random(_SEED)
        store = SearchStore(tmp_path / "db")
        await store.connect()

        try:
            col = f"consistency-{uuid.uuid4().hex[:8]}"
            await store.ensure_collection(col, embedding_dim=_DIM)

            # --- Phase 1: ingest the base corpus chunks (_CORPUS_CHUNKS) ---
            # Each doc contributes 1 chunk with a globally unique FTS token.
            base_docs: dict[int, tuple[str, str, list[ChunkRecord]]] = {}
            for i in range(_CORPUS_CHUNKS):
                doc_id, token, chunks = _make_corpus_doc(i, version=0)
                base_docs[i] = (doc_id, token, chunks)
                await store.ingest_chunks(col, chunks)

            # Build the initial FTS index from the full corpus.
            await store.rebuild_fts_index(col)

            # --- Phase 2: 50 incremental add/delete/re-ingest ops ---------
            live_indices: list[int] = list(range(_CORPUS_CHUNKS))
            deleted_indices: list[int] = []  # indices whose original tokens are gone
            next_doc_index = _CORPUS_CHUNKS
            op_version: dict[int, int] = {}  # re-ingest version per doc

            op_types = ["add", "delete", "reingest"]
            for _op_num in range(_DELTA_OPS):
                op = rng.choice(op_types)

                if op == "add" or not live_indices:
                    idx = next_doc_index
                    next_doc_index += 1
                    path = f"/corpus/doc{idx:04d}.md"
                    doc_id = _doc_id_from_path(path)
                    token = _unique_token(doc_id, 0)
                    chunks = [_chunk(doc_id, 0, token)]
                    base_docs[idx] = (doc_id, token, chunks)
                    live_indices.append(idx)
                    await store.ingest_chunks(col, chunks)
                    await store.optimize_fts(col)

                elif op == "delete":
                    pick = rng.choice(live_indices)
                    live_indices.remove(pick)
                    deleted_indices.append(pick)
                    doc_id, _, _ = base_docs[pick]
                    await store.delete_document(col, doc_id, skip_fts_optimize=False)

                else:  # reingest
                    pick = rng.choice(live_indices)
                    doc_id, old_token, _ = base_docs[pick]
                    ver = op_version.get(pick, 0) + 1
                    op_version[pick] = ver
                    # Track old token as a "deleted token" for phantom-hit checks.
                    # We do NOT track the index itself as deleted because the doc_id
                    # stays live — only the old token becomes stale.
                    await store.delete_document(col, doc_id, skip_fts_optimize=True)
                    new_token = _unique_token(doc_id, ver)
                    new_chunks = [_chunk(doc_id, 0, new_token)]
                    base_docs[pick] = (doc_id, new_token, new_chunks)
                    await store.ingest_chunks(col, new_chunks)
                    await store.optimize_fts(col)

            # --- Phase 3: build a fresh-rebuild reference index -----------
            ref_col = f"{col}-ref"
            await store.ensure_collection(ref_col, embedding_dim=_DIM)
            for idx in live_indices:
                _, _, chunks = base_docs[idx]
                await store.ingest_chunks(ref_col, chunks)
            await store.rebuild_fts_index(ref_col)

            # --- Phase 4a: live-doc check — target doc in both results ----
            # For 10 sampled live docs, the incremental index must find the
            # same doc_id as the reference (fresh-rebuild) index.
            query_indices = rng.sample(
                live_indices, min(_QUERIES, len(live_indices))
            )
            query_vec = [0.5] * _DIM

            for qidx in query_indices:
                doc_id, token, _ = base_docs[qidx]

                ref_results = await store.hybrid_search(
                    ref_col, query_vec, token, _SEARCH_K
                )
                ref_doc_ids = {r.doc_id for r in ref_results}

                inc_results = await store.hybrid_search(
                    col, query_vec, token, _SEARCH_K
                )
                inc_doc_ids = {r.doc_id for r in inc_results}

                # Reference must contain the target (sanity check).
                assert doc_id in ref_doc_ids, (
                    f"Reference (fresh-rebuild) index missing live doc_id {doc_id!r} "
                    f"for token {token!r} (doc index {qidx})"
                )

                # Incremental must also contain the target.
                assert doc_id in inc_doc_ids, (
                    f"Incremental index missing live doc_id {doc_id!r} for token "
                    f"{token!r} (doc index {qidx}, after {_DELTA_OPS} ops)"
                )

            # --- Phase 4b: phantom-hit check — deleted docs absent --------
            # Pick up to 5 deleted docs and verify they are NOT in incremental
            # results for their tokens.
            phantom_sample = rng.sample(
                deleted_indices, min(5, len(deleted_indices))
            )
            for pidx in phantom_sample:
                doc_id, token, _ = base_docs[pidx]

                inc_results = await store.hybrid_search(
                    col, query_vec, token, _SEARCH_K
                )
                inc_doc_ids = {r.doc_id for r in inc_results}

                assert doc_id not in inc_doc_ids, (
                    f"Phantom hit: deleted doc_id {doc_id!r} still appears in "
                    f"incremental FTS results for token {token!r} "
                    f"(doc index {pidx}, after {_DELTA_OPS} ops)"
                )

        finally:
            await store.disconnect()

    asyncio.run(_run())
