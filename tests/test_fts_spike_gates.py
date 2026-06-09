"""tests/test_fts_spike_gates.py — Integration tests verifying LanceDB table.optimize() gates.

All tests are marked @pytest.mark.integration — they exercise real LanceDB disk I/O
and are excluded from the default `uv run pytest` suite.

Run with:
    uv run pytest -m integration tests/test_fts_spike_gates.py -v --no-cov

These tests correspond to the spike gates in Documentation/Backlog/C6-spike-findings.md
and gate the C6 implementation plan (Plan A / B / C decision).
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import lancedb
import pytest
from lancedb.index import FTS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIM = 4  # tiny embedding dimension — faster, no real model needed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(text: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "text": text,
        "vector": [0.0] * _DIM,
    }


async def _fts_hits(table: Any, query: str) -> list[dict[str, Any]]:
    """Return all FTS hits for *query* (up to 100)."""
    q = await table.search(query, query_type="fts")
    return await q.limit(100).to_list()


async def _make_table(db: Any, rows: list[dict[str, Any]], *, index: bool = True) -> Any:
    """Create a uniquely named LanceDB table with FTS index."""
    name = f"spike_{uuid.uuid4().hex[:8]}"
    tbl = await db.create_table(name, data=rows)
    if index:
        await tbl.create_index("text", config=FTS(), replace=True)
    return tbl


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """Module-scoped LanceDB connection to a temp directory."""
    tmp = tmp_path_factory.mktemp("spike_lancedb")

    async def _connect() -> Any:
        return await lancedb.connect_async(str(tmp))

    return asyncio.run(_connect())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_optimize_fts_incorporates_new_rows(db: Any) -> None:
    """(b) Rows inserted AFTER create_index appear in FTS results after optimize().

    This tests the add path: index is created, a new row is added, optimize()
    is called, and the new row must be searchable via FTS.
    """
    tbl = await _make_table(db, [_row("seed row for gate b")])

    unique_token = f"gatebnewrow{uuid.uuid4().hex[:10]}"
    await tbl.add([_row(f"freshly added {unique_token}")])
    await tbl.optimize()

    hits = await _fts_hits(tbl, unique_token)
    matched = [r for r in hits if unique_token in r.get("text", "")]
    assert matched, (
        f"Expected newly inserted row with token {unique_token!r} "
        f"to appear in FTS after optimize(), got {hits}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_optimize_fts_removes_deleted_rows(db: Any) -> None:
    """(c) Deleted rows do NOT appear in FTS results after optimize() (no phantom hits).

    This is the critical gate: if this fails, Plan B applies (delete path falls
    back to rebuild_fts_index). Since this gate passes, Plan A is used throughout.
    """
    unique_token = f"gatecdel{uuid.uuid4().hex[:10]}"
    tbl = await _make_table(
        db,
        [
            _row(f"row to be deleted {unique_token}"),
            _row("this row survives"),
        ],
    )

    # Confirm presence before delete
    pre = await _fts_hits(tbl, unique_token)
    assert any(unique_token in r.get("text", "") for r in pre), (
        "Setup error: token not found before delete"
    )

    # Delete the row and run optimize
    await tbl.delete(f"text LIKE '%{unique_token}%'")
    await tbl.optimize()

    post = await _fts_hits(tbl, unique_token)
    phantom = [r for r in post if unique_token in r.get("text", "")]
    assert not phantom, (
        f"Phantom hit: deleted row still returned by FTS after optimize(); hits={post}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_optimize_fts_is_idempotent(db: Any) -> None:
    """Calling optimize() twice does not corrupt the index.

    Verifies that a double-optimize (e.g., from a retry) leaves FTS in a
    consistent, queryable state.
    """
    unique_token = f"idem{uuid.uuid4().hex[:10]}"
    tbl = await _make_table(db, [_row(f"idempotent row {unique_token}")])

    # Two optimize calls in sequence
    await tbl.optimize()
    await tbl.optimize()

    hits = await _fts_hits(tbl, unique_token)
    assert any(unique_token in r.get("text", "") for r in hits), (
        f"Row not found after two optimize() calls; hits={hits}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_optimize_fts_after_replace_true_index(db: Any) -> None:
    """(e) optimize() is effective after create_index(replace=True) + new row.

    Simulates the error-fallback path in ingest_file: rebuild_fts_index() calls
    create_index(replace=True), then a subsequent optimize() call must still work.
    """
    tbl = await _make_table(db, [_row("initial row for replace test")])
    # Simulate a full rebuild (replace=True)
    await tbl.create_index("text", config=FTS(), replace=True)

    unique_token = f"gateen{uuid.uuid4().hex[:10]}"
    await tbl.add([_row(f"post-replace row {unique_token}")])
    await tbl.optimize()

    hits = await _fts_hits(tbl, unique_token)
    assert any(unique_token in r.get("text", "") for r in hits), (
        f"Post-replace row not found via FTS after optimize(); hits={hits}"
    )
