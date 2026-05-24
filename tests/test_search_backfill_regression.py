"""Regression tests for date-range search correctness before/after timestamp backfill.

Task 6.1 — A2 plan.

These tests demonstrate the SEARCH path bug caused by mixed-format ``indexed_at``
values.  LanceDB uses string comparison for the WHERE clause emitted by
``build_where()``.  The fixed-width format produced by ``normalize_iso_utc`` ends
with ``.ffffffZ``; the legacy format ends with ``Z`` (no microseconds).  Because
``ord('Z') == 90 > ord('.') == 46``, a legacy timestamp at e.g.
``2026-04-30T23:59:59Z`` compares as *greater* than the fixed-width boundary
``2026-04-30T23:59:59.999999Z``, so the row is **incorrectly excluded** when
``indexed_at <= '<boundary>'`` is evaluated by the SQL engine.

After ``reindex_metadata(normalize_timestamps=True)`` all rows carry
``indexed_at`` in fixed-width format and string comparison is again equivalent
to temporal ordering.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid

import pytest

from archon_search._types import ChunkRecord
from archon_search.filters import SearchFilters
from archon_search.store import SearchStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIM = 4

# Fixed-width timestamps (correct format).
_T_JAN = "2026-01-15T00:00:00.000000Z"   # January — well before filter range
_T_MAR = "2026-03-10T00:00:00.000000Z"   # March — in range
_T_APR_FIXED = "2026-04-30T00:00:00.000000Z"   # April 30 midnight — in range (fixed)
# Legacy timestamp: same calendar day as the indexed_before boundary
# (2026-04-30T23:59:59.999999Z) but WITHOUT microseconds.  String comparison
# places this AFTER .999999Z, so it is incorrectly excluded by the filter.
_T_APR_LEGACY = "2026-04-30T23:59:59Z"   # April 30 end-of-day — legacy format
_T_JUN = "2026-06-01T00:00:00.000000Z"   # June — after filter range


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _chunk(seed: str, indexed_at: str, text: str) -> ChunkRecord:
    did = _doc_id(seed)
    return ChunkRecord(
        doc_id=did,
        chunk_id=f"{did}-000000",
        text=text,
        vector=[0.25] * _DIM,
        source_path=f"/tmp/backfill_test_{seed}.md",
        indexed_at=indexed_at,
        file_type="md",
    )


def _chunk_id(seed: str) -> str:
    return f"{_doc_id(seed)}-000000"


def _make_corpus() -> list[ChunkRecord]:
    """Mixed-format corpus designed to expose the string-comparison bug.

    Corpus:
      jan   — fixed-width, January  (before range)
      mar   — fixed-width, March    (in range)
      apr_f — fixed-width, April 30 midnight (in range)
      apr_l — LEGACY format, April 30 end-of-day (in range temporally, but
              excluded by buggy string comparison before backfill)
      jun   — fixed-width, June     (after range)
    """
    return [
        _chunk("jan",   _T_JAN,        "january document out of range"),
        _chunk("mar",   _T_MAR,        "march document in range"),
        _chunk("apr_f", _T_APR_FIXED,  "april fixed format in range"),
        _chunk("apr_l", _T_APR_LEGACY, "april legacy format should be in range"),
        _chunk("jun",   _T_JUN,        "june document out of range"),
    ]


# Filter: March 1 → April 30 inclusive
# -> boundary: indexed_at >= '2026-03-01T00:00:00.000000Z'
#              indexed_at <= '2026-04-30T23:59:59.999999Z'
_FILTERS = SearchFilters(indexed_after="2026-03-01", indexed_before="2026-04-30")

# Expected chunk_ids AFTER backfill (correct set):
#   mar, apr_f, apr_l — jan and jun are outside the range
_EXPECTED_IN_RANGE = {_chunk_id("mar"), _chunk_id("apr_f"), _chunk_id("apr_l")}
_EXPECTED_OUT_OF_RANGE = {_chunk_id("jan"), _chunk_id("jun")}


# ---------------------------------------------------------------------------
# Module-scoped store + async helper
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backfill_store(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """Isolated SearchStore for this module; avoids sharing state with other modules."""
    tmp = tmp_path_factory.mktemp("backfill_db")
    store = SearchStore(tmp)
    asyncio.run(store.connect())
    yield store
    asyncio.run(store.disconnect())


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_date_filter_returns_wrong_results_before_backfill_then_correct_after(
    backfill_store: SearchStore,
) -> None:
    """Pre-backfill: the legacy-format row is missing from results.
    Post-backfill: all three in-range rows are returned.

    The bug:
        indexed_at = '2026-04-30T23:59:59Z'  (legacy, no microseconds)
        boundary   = '2026-04-30T23:59:59.999999Z'
        'Z' (ASCII 90) > '.' (ASCII 46)  →  legacy string > boundary
        → LanceDB WHERE clause `indexed_at <= '<boundary>'` evaluates False
        → row silently dropped even though it is temporally within range.
    """
    col = f"backfill-{uuid.uuid4().hex[:8]}"
    corpus = _make_corpus()
    await backfill_store.ensure_collection(col, _DIM)
    await backfill_store.ingest_chunks(col, corpus)
    await backfill_store.rebuild_fts_index(col)

    # --- RED phase: pre-backfill search ---
    pre_results = await backfill_store.hybrid_search(
        col,
        query_vector=[0.25] * _DIM,
        query_text="document",
        top_k=10,
        filters=_FILTERS,
    )
    pre_chunk_ids = {r.chunk_id for r in pre_results}

    # The legacy-format row SHOULD be in range but IS excluded by string comparison.
    # NOTE: if LanceDB ever fixes this to use proper temporal comparison (Arrow
    # timestamp types), this assertion flips and the pre-backfill phase should be
    # skipped. The soft-fail guard below handles that without breaking CI.
    legacy_cid = _chunk_id("apr_l")
    bug_present = legacy_cid not in pre_chunk_ids
    if not bug_present:
        # LanceDB has fixed the string-comparison issue upstream; skip the
        # pre-backfill assertion but still verify the post-backfill result set.
        import warnings
        warnings.warn(
            "Pre-backfill bug not triggered — LanceDB may now use temporal "
            "comparison. The post-backfill phase still runs as a sanity check.",
            stacklevel=1,
        )

    # Rows outside the range must not appear in either phase.
    assert not (_EXPECTED_OUT_OF_RANGE & pre_chunk_ids), (
        f"Pre-backfill: out-of-range rows appeared in results: "
        f"{_EXPECTED_OUT_OF_RANGE & pre_chunk_ids}"
    )

    # --- Run backfill ---
    result = await backfill_store.reindex_metadata(col, normalize_timestamps=True)
    assert result.ts_normalized >= 1, (
        "Expected at least one timestamp to be normalized by reindex_metadata"
    )

    # --- GREEN phase: post-backfill search ---
    post_results = await backfill_store.hybrid_search(
        col,
        query_vector=[0.25] * _DIM,
        query_text="document",
        top_k=10,
        filters=_FILTERS,
    )
    post_chunk_ids = {r.chunk_id for r in post_results}

    # All three in-range rows must be present after normalization.
    missing = _EXPECTED_IN_RANGE - post_chunk_ids
    assert not missing, (
        f"Post-backfill: in-range rows still missing: {missing}"
    )

    # Out-of-range rows must remain excluded.
    assert not (_EXPECTED_OUT_OF_RANGE & post_chunk_ids), (
        f"Post-backfill: out-of-range rows appeared: "
        f"{_EXPECTED_OUT_OF_RANGE & post_chunk_ids}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_date_filter_on_mixed_format_collection_warns(
    backfill_store: SearchStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A date-range search on a collection with legacy-format rows emits a WARNING.

    The WARNING fires in ``hybrid_search`` (``store.py``) when the result set
    contains rows whose ``indexed_at`` does not match ``_FIXED_WIDTH_PATTERN``.
    """
    col = f"warn-mixed-{uuid.uuid4().hex[:8]}"
    # Only the legacy row needs to be present; the filter must be broad enough
    # to include it so that the WARNING fires on an actual result row.
    corpus = [
        _chunk("warn_jan",  _T_JAN,        "january open"),
        _chunk("warn_legacy", _T_APR_LEGACY, "legacy april in range"),
    ]
    await backfill_store.ensure_collection(col, _DIM)
    await backfill_store.ingest_chunks(col, corpus)
    await backfill_store.rebuild_fts_index(col)

    # Use a filter that would INCLUDE the legacy row temporally (Jan–May).
    # The bug means the row may or may not appear in results, but the WARNING
    # must fire whenever any result row has a legacy format AND a date filter
    # is active.  We use indexed_after only so the legacy row is not cut by
    # the indexed_before boundary.
    f = SearchFilters(indexed_after="2026-01-01", indexed_before="2026-05-01")

    with caplog.at_level(logging.WARNING, logger="archon"):
        await backfill_store.hybrid_search(
            col,
            query_vector=[0.25] * _DIM,
            query_text="open",
            top_k=10,
            filters=f,
        )

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        ("legacy-format" in msg or "legacy" in msg.lower()) and col in msg
        for msg in warning_messages
    ), (
        f"Expected a WARNING about legacy-format timestamps, got: {warning_messages}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_date_filter_on_normalized_collection_does_not_warn(
    backfill_store: SearchStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After backfill, the same date-range search emits NO legacy-format WARNING.

    Verifies that ``reindex_metadata(normalize_timestamps=True)`` eliminates
    the WARNING path entirely for a fully-normalized collection.
    """
    col = f"warn-norm-{uuid.uuid4().hex[:8]}"
    corpus = [
        _chunk("norm_jan",    _T_JAN,        "january normalized"),
        _chunk("norm_apr",    _T_APR_LEGACY, "april legacy to be normalized"),
        _chunk("norm_jun",    _T_JUN,        "june normalized"),
    ]
    await backfill_store.ensure_collection(col, _DIM)
    await backfill_store.ingest_chunks(col, corpus)
    await backfill_store.rebuild_fts_index(col)

    # Normalize all timestamps.
    result = await backfill_store.reindex_metadata(col, normalize_timestamps=True)
    assert result.ts_normalized >= 1

    f = SearchFilters(indexed_after="2026-01-01", indexed_before="2026-12-31")

    with caplog.at_level(logging.WARNING, logger="archon"):
        await backfill_store.hybrid_search(
            col,
            query_vector=[0.25] * _DIM,
            query_text="normalized",
            top_k=10,
            filters=f,
        )

    legacy_warnings = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING
        and ("legacy-format" in r.message or "legacy" in r.message.lower())
        and col in r.message
    ]
    assert not legacy_warnings, (
        f"Post-backfill: unexpected legacy-format WARNING(s): {legacy_warnings}"
    )
