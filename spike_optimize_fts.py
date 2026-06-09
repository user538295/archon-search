"""spike_optimize_fts.py — Verify `table.optimize()` semantics for C6 planning.

This script is a throwaway spike to validate LanceDB's incremental FTS
maintenance API before modifying the production store. Run it once and record
the results in Documentation/Backlog/C6-spike-findings.md.

Usage:
    uv run python spike_optimize_fts.py

Gates:
    (a) API availability  — table.optimize() exists and is callable
    (b) New-row indexing  — newly inserted rows appear after optimize()
    (c) Deleted-row cleanup — deleted rows disappear after optimize()
    (d) Concurrent safety — 3 concurrent optimize() calls don't corrupt
    (e) Compatibility     — optimize() works after create_index(replace=True)
    (f) Update-row indexing — updated text appears after optimize()
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from typing import Any

import lancedb
from lancedb.index import FTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 4  # tiny embedding dimension for spike


def _row(text: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "text": text,
        "vector": [0.0] * _DIM,
    }


async def _fts_search(table: Any, query: str) -> list[dict[str, Any]]:
    """Return all FTS hits for *query* (up to 50)."""
    q = await table.search(query, query_type="fts")
    return await q.limit(50).to_list()


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


async def gate_a_api_availability(db: Any) -> bool:
    """(a) table.optimize() exists on the async API."""
    tbl = await db.create_table(
        f"spike_a_{uuid.uuid4().hex[:6]}",
        data=[_row("initial content")],
    )
    await tbl.create_index("text", config=FTS(), replace=True)
    has_attr = hasattr(tbl, "optimize")
    if has_attr:
        # ensure it is callable (not just a property)
        has_attr = callable(tbl.optimize)
        # actually call it to confirm no AttributeError / NotImplementedError
        try:
            await tbl.optimize()
            print("  (a) PASS — table.optimize() exists and completed without error")
            return True
        except (AttributeError, NotImplementedError) as exc:
            print(f"  (a) FAIL — optimize() raised {type(exc).__name__}: {exc}")
            return False
    else:
        print("  (a) FAIL — table has no 'optimize' attribute")
        return False


async def gate_b_new_row_indexing(db: Any) -> bool:
    """(b) Rows inserted AFTER create_index appear in FTS results after optimize()."""
    tbl = await db.create_table(
        f"spike_b_{uuid.uuid4().hex[:6]}",
        data=[_row("seed row alpha")],
    )
    await tbl.create_index("text", config=FTS(), replace=True)

    # Insert a new row AFTER the index was created
    unique_token = f"xyztoken{uuid.uuid4().hex[:8]}"
    await tbl.add([_row(f"new content {unique_token}")])
    await tbl.optimize()

    hits = await _fts_search(tbl, unique_token)
    if any(unique_token in row.get("text", "") for row in hits):
        print("  (b) PASS — newly inserted row appears in FTS after optimize()")
        return True
    else:
        print(f"  (b) FAIL — unique token {unique_token!r} not found in FTS results after optimize(); hits={hits}")
        return False


async def gate_c_deleted_row_cleanup(db: Any) -> bool:
    """(c) Deleted rows NO LONGER appear in FTS results after optimize()."""
    unique_token = f"deletetoken{uuid.uuid4().hex[:8]}"
    tbl = await db.create_table(
        f"spike_c_{uuid.uuid4().hex[:6]}",
        data=[_row(f"this will be deleted {unique_token}"), _row("stays around")],
    )
    await tbl.create_index("text", config=FTS(), replace=True)

    # Confirm the row appears before deletion
    pre_hits = await _fts_search(tbl, unique_token)
    if not any(unique_token in row.get("text", "") for row in pre_hits):
        print(f"  (c) SETUP FAIL — token {unique_token!r} not found before delete; cannot test gate (c)")
        return False

    # Delete and optimize
    await tbl.delete(f"text LIKE '%{unique_token}%'")
    await tbl.optimize()

    post_hits = await _fts_search(tbl, unique_token)
    if not any(unique_token in row.get("text", "") for row in post_hits):
        print("  (c) PASS — deleted row no longer appears in FTS after optimize()")
        return True
    else:
        print(f"  (c) FAIL — deleted row still appears in FTS after optimize(); hits={post_hits}")
        return False


async def gate_d_concurrent_safety(db: Any) -> bool:
    """(d) 3 concurrent optimize() calls do not raise or corrupt the table."""
    tbl = await db.create_table(
        f"spike_d_{uuid.uuid4().hex[:6]}",
        data=[_row("concurrent test row one"), _row("concurrent test row two")],
    )
    await tbl.create_index("text", config=FTS(), replace=True)

    try:
        await asyncio.gather(tbl.optimize(), tbl.optimize(), tbl.optimize())
    except Exception as exc:  # noqa: BLE001
        print(f"  (d) FAIL — concurrent optimize() raised {type(exc).__name__}: {exc}")
        return False

    # Verify table is still queryable
    try:
        hits = await _fts_search(tbl, "concurrent test row")
        print(f"  (d) PASS — 3 concurrent optimize() calls completed; table queryable ({len(hits)} hits)")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  (d) FAIL — table not queryable after concurrent optimize(); error: {exc}")
        return False


async def gate_e_compatibility_after_replace(db: Any) -> bool:
    """(e) optimize() works after create_index(replace=True) with a new row added."""
    tbl = await db.create_table(
        f"spike_e_{uuid.uuid4().hex[:6]}",
        data=[_row("initial for replace test")],
    )
    await tbl.create_index("text", config=FTS(), replace=True)
    # Re-create the index with replace=True (simulates a manual rebuild)
    await tbl.create_index("text", config=FTS(), replace=True)

    # Add a new row and optimize
    unique_token = f"replacetoken{uuid.uuid4().hex[:8]}"
    await tbl.add([_row(f"post-replace {unique_token}")])
    await tbl.optimize()

    hits = await _fts_search(tbl, unique_token)
    if any(unique_token in row.get("text", "") for row in hits):
        print("  (e) PASS — optimize() works after create_index(replace=True)")
        return True
    else:
        print(f"  (e) FAIL — post-replace token not found in FTS; hits={hits}")
        return False


async def gate_f_update_row_indexing(db: Any) -> bool:
    """(f) Updated text in an existing row appears in FTS after optimize()."""
    old_token = f"oldtoken{uuid.uuid4().hex[:8]}"
    new_token = f"newtoken{uuid.uuid4().hex[:8]}"
    row = _row(f"original text {old_token}")
    row_id = row["id"]

    tbl = await db.create_table(
        f"spike_f_{uuid.uuid4().hex[:6]}",
        data=[row],
    )
    await tbl.create_index("text", config=FTS(), replace=True)

    # Update the text column
    await tbl.update(
        where=f"id = '{row_id}'",
        updates={"text": f"updated text {new_token}"},
    )
    await tbl.optimize()

    new_hits = await _fts_search(tbl, new_token)
    if any(new_token in r.get("text", "") for r in new_hits):
        print("  (f) PASS — updated text is searchable via FTS after optimize()")
        return True
    else:
        print(f"  (f) FAIL — updated text {new_token!r} not found in FTS; hits={new_hits}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    import lancedb as _lancedb

    print(f"\n=== FTS optimize() spike — LanceDB {_lancedb.__version__} ===\n")

    tmpdir = tempfile.mkdtemp(prefix="archon_spike_")
    try:
        db = await lancedb.connect_async(tmpdir)

        results: dict[str, bool] = {}
        results["a"] = await gate_a_api_availability(db)
        results["b"] = await gate_b_new_row_indexing(db)
        results["c"] = await gate_c_deleted_row_cleanup(db)
        results["d"] = await gate_d_concurrent_safety(db)
        results["e"] = await gate_e_compatibility_after_replace(db)
        results["f"] = await gate_f_update_row_indexing(db)

        print("\n--- Summary ---")
        all_pass = True
        for gate, passed in results.items():
            status = "PASS" if passed else "FAIL"
            print(f"  ({gate}) {status}")
            if not passed:
                all_pass = False

        print()
        if not results["a"] or not results["b"]:
            print("Go/no-go: PLAN C — gates (a) or (b) failed; C6 is DEFERRED.")
        elif not results["c"]:
            print("Go/no-go: PLAN B — gate (c) failed; delete path uses rebuild_fts_index.")
        else:
            # Gates (a), (b), (c) all pass → Plan A regardless of (d), (e), (f).
            # (d) FAIL is noted but non-blocking: concurrent optimize() calls conflict;
            # callers must NOT issue parallel optimize() on the same table.
            print("Go/no-go: PLAN A — critical gates (a)(b)(c) pass; full incremental FTS (optimize only).")
            if not results["d"]:
                print("  Note: gate (d) failed — concurrent optimize() conflict. Callers must serialize optimize() calls.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
