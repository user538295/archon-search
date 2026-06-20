"""Integration test: directory ingest peak memory is O(batch_size), not O(corpus_size).

Scenario S11: ingest 10 files × 100 chunks via ``ingest_directory()``, measured
with ``tracemalloc``.  Peak Python-allocated memory when ingesting 10 files must
be less than 3× the peak when ingesting 1 file.

Background: Before D4, ``ingest_directory()`` accumulated chunk-text strings from
all files into memory before writing (O(corpus_size) in text-string allocations).
After D4, ``ingest_directory()`` processes one file at a time; the Python heap
peak is bounded by a single file's worth of chunk strings (O(batch_size)).
This test guards against reintroduction of such accumulators.

Note: ``tracemalloc`` measures Python-heap allocations only; LanceDB's Rust heap
is not captured.  The key signal is text-string allocation: 10 files × 100 chunks
× ~620 chars = ~620 KB text strings if accumulated; ~62 KB if processed one file
at a time.  That 10× ratio far exceeds the 3× threshold, making the test
discriminating despite stub-embedder vectors being tiny (4 floats = 32 bytes each).

Implementation notes
--------------------
- ``make_real_pipeline`` uses ``chunk_size=128``.  Paragraphs of ~620 chars
  reliably produce ≥1 chunk each at this chunk size.  100 paragraphs per file
  → ~100 chunks per file.
- ``tracemalloc.stop()`` is placed in the ``finally`` block so tracing always
  stops even if ``ingest_directory`` raises, preventing leaked tracing state
  from corrupting subsequent tests in the same xdist worker.
- Marked ``xdist_group("benchmark")`` so it runs on a single xdist worker,
  avoiding CPU starvation from concurrent workers skewing the measurement.
- ``gc.collect()`` is called between the two measurement runs to flush allocator
  arenas from the first run and make the measurements more comparable.

Run in isolation:
    uv run pytest tests/integration/test_directory_ingest_memory_bounds.py -v
"""
from __future__ import annotations

import gc
import tracemalloc
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_pipeline

pytestmark = pytest.mark.integration

# Number of paragraphs per file.  Each paragraph is ~620 chars, reliably
# producing ≥1 chunk at chunk_size=128.  100 paragraphs → ~100 chunks per file.
_PARAGRAPHS_PER_FILE = 100

# Paragraph body: ~620 characters.  Enough to exceed 128 tokeniser tokens so
# each paragraph produces at least one chunk in DocumentChunker(chunk_size=128).
_PARA_BODY = (
    "The quick brown fox jumps over the lazy dog near the riverbank every day. "
    "A software system is characterised by its components, their interactions, "
    "and the environment in which it operates.  Distributed computing enables "
    "workloads to be spread across many machines, improving throughput and fault "
    "tolerance at the cost of added complexity.  Data pipelines transform raw "
    "input into structured representations suitable for downstream analytics, "
    "machine-learning models, and reporting dashboards.  Indexing strategies "
    "determine how quickly a system can locate specific records among millions "
    "of stored documents, with trade-offs between write amplification, storage "
    "overhead, and query latency.  Vector similarity search complements keyword "
    "retrieval by capturing semantic relationships between queries and documents."
)  # ~620 chars

_FILES_SMALL = 1   # baseline: 1 file
_FILES_LARGE = 10  # load: 10 files

# Minimum chunks to be produced by the ingest — proves the measurement is
# not vacuously small due to parser failures.
_MIN_CHUNKS_PER_FILE = 50

# Allow at most this factor of peak-memory growth from 1 to 10 files.
# If corpus-wide accumulators exist, growth would be ~10×.
# Without accumulators, growth is bounded by LanceDB/asyncio overhead (~1–2×).
# 3× is a generous upper bound that still catches O(corpus) regressions.
_PEAK_RATIO_LIMIT = 3.0


def _build_files(directory: Path, n_files: int, token: str) -> Path:
    """Write ``n_files`` plain-text files into ``directory``.

    Each file contains ``_PARAGRAPHS_PER_FILE`` paragraphs of ~620 chars.
    ``token`` is embedded in each paragraph for debuggability.
    Returns ``directory`` for convenience.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        paragraphs = [
            f"{token}-file{i:02d}-para{j:03d}: {_PARA_BODY}"
            for j in range(_PARAGRAPHS_PER_FILE)
        ]
        (directory / f"doc_{i:02d}.txt").write_text(
            "\n\n".join(paragraphs), encoding="utf-8"
        )
    return directory


@pytest.mark.xdist_group("benchmark")
async def test_directory_ingest_peak_memory_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peak Python memory for 10-file ingest must be < 3× the 1-file baseline.

    If corpus-wide text-string accumulators are reintroduced into
    ``ingest_directory()``, the 10-file peak would be ≈10× the 1-file peak.
    After D4, only one file's data lives in memory at a time, keeping the
    ratio close to 1×.

    Two separate pipeline+store pairs are used so the second measurement does
    not inherit LanceDB state (open tables, cached metadata) from the first.
    """
    # -----------------------------------------------------------------
    # Build two corpora in separate subdirectories.
    # -----------------------------------------------------------------
    small_dir = _build_files(tmp_path / "small", _FILES_SMALL, "small")
    large_dir = _build_files(tmp_path / "large", _FILES_LARGE, "large")

    # -----------------------------------------------------------------
    # Baseline measurement: 1-file ingest.
    # -----------------------------------------------------------------
    store_s, pipeline_s = await make_real_pipeline(tmp_path / "db_small", monkeypatch)
    try:
        tracemalloc.start()
        try:
            results_small = await pipeline_s.ingest_directory(
                small_dir,
                "col-small",
                embedder=pipeline_s._global_embedder,
                rebuild_fts=False,
            )
            _, peak_small = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        await store_s.disconnect()

    # Verify the baseline ingest actually produced chunks — a zero-chunk result
    # would make peak_small trivially small and the ratio vacuously pass.
    total_small = sum(r.chunks_created for r in results_small)
    assert total_small >= _MIN_CHUNKS_PER_FILE, (
        f"1-file ingest produced only {total_small} chunks; "
        f"expected ≥ {_MIN_CHUNKS_PER_FILE}.  "
        "The file may be too small to exercise chunking — increase _PARAGRAPHS_PER_FILE."
    )

    # Flush allocator arenas from the first run before the second measurement.
    gc.collect()

    # -----------------------------------------------------------------
    # Load measurement: 10-file ingest.
    # -----------------------------------------------------------------
    store_l, pipeline_l = await make_real_pipeline(tmp_path / "db_large", monkeypatch)
    try:
        tracemalloc.start()
        try:
            results_large = await pipeline_l.ingest_directory(
                large_dir,
                "col-large",
                embedder=pipeline_l._global_embedder,
                rebuild_fts=False,
            )
            _, peak_large = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        await store_l.disconnect()

    # Verify the load ingest produced chunks proportional to the file count.
    total_large = sum(r.chunks_created for r in results_large)
    assert total_large >= _FILES_LARGE * _MIN_CHUNKS_PER_FILE, (
        f"10-file ingest produced only {total_large} chunks; "
        f"expected ≥ {_FILES_LARGE * _MIN_CHUNKS_PER_FILE}.  "
        "Files may be too small — increase _PARAGRAPHS_PER_FILE."
    )

    # -----------------------------------------------------------------
    # Assertions.
    # -----------------------------------------------------------------
    assert peak_small > 0, "tracemalloc reported zero peak for 1-file ingest — measurement error"
    assert peak_large > 0, "tracemalloc reported zero peak for 10-file ingest — measurement error"

    ratio = peak_large / peak_small
    assert ratio < _PEAK_RATIO_LIMIT, (
        f"Peak memory scaled by {ratio:.2f}× from {_FILES_SMALL} file(s) to "
        f"{_FILES_LARGE} file(s).  Expected < {_PEAK_RATIO_LIMIT}×.  "
        f"1-file peak: {peak_small / 1024:.1f} KiB ({total_small} chunks); "
        f"10-file peak: {peak_large / 1024:.1f} KiB ({total_large} chunks).  "
        "This suggests corpus-wide text accumulators were reintroduced into "
        "ingest_directory() — a D4 regression."
    )
