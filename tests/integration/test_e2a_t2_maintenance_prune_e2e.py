"""E2a T-2: E2e tests for maintenance prune + /status maintenance fields.

Scenarios covered:
- S6: Maintenance loop prunes expired chunks; non-expired chunks are preserved.
- S7: GET /status shows last_expired_pruned_at set; expired_chunk_count is an integer.

Strategy:
  Use make_real_app(maintenance_enabled=True) to start a real FastAPI app with
  an in-process MaintenanceLoop.  Expired/alive chunks are seeded by writing
  directly to LanceDB via the real store (bypasses the HTTP ingest pipeline and
  avoids the short-TTL + sleep approach).  POST /maintenance/trigger fires an
  immediate maintenance pass; the test polls GET /status until last_run_at
  transitions from None to a timestamp, confirming the pass completed.

References: plan task T-2 in e2a-ttl-scoping-team-plan.md.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_search._types import normalize_iso_utc
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Named constants — avoid magic numbers
# ---------------------------------------------------------------------------

_EMBEDDING_DIM: int = 384          # must match the stub fastembed backend
_POLL_TIMEOUT_S: float = 10.0      # maximum time to wait for a maintenance pass
_POLL_INTERVAL_S: float = 0.1      # polling frequency
_PAST_SECONDS: int = 120           # seed chunks expired 2 min ago
_FUTURE_HOURS: int = 2             # seed alive chunks expiring 2 h from now


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def _seed_chunks(
    db_path: str,
    col: str,
    *,
    expired_count: int = 0,
    alive_count: int = 0,
    null_expiry_count: int = 0,
) -> tuple[list[str], list[str], list[str]]:
    """Create the collection (if absent) and insert expired/alive/null-expiry chunks
    directly via a fresh SearchStore connection — never via the server's loop-bound
    store instance.  See learnings.md: never call async store methods via asyncio.run()
    inside a make_real_app TestClient context using the server's own store object.

    Returns ``(expired_doc_ids, alive_doc_ids, null_expiry_doc_ids)``.
    """
    from archon_search.store import SearchStore

    # Safety invariant: open a fresh connection independent of the server's event loop.
    fresh_store = SearchStore(db_path)
    await fresh_store.connect()
    try:
        await fresh_store.ensure_collection(col, _EMBEDDING_DIM)

        # Apply E2a schema migrations so expires_at/scopes columns are present.
        pending = await fresh_store.pending_migrations(col, "default")
        if pending:
            await fresh_store.apply_in_place_migrations(col, "default", pending)

        # fresh_store._require_connected() is safe here: this is OUR fresh_store,
        # not the server's store. See learnings.md pattern.
        db = fresh_store._require_connected()
        table = await db.open_table(col)

        now = datetime.now(UTC)
        past_iso = normalize_iso_utc(now - timedelta(seconds=_PAST_SECONDS))
        future_iso = normalize_iso_utc(now + timedelta(hours=_FUTURE_HOURS))

        expired_doc_ids: list[str] = []
        alive_doc_ids: list[str] = []
        null_expiry_doc_ids: list[str] = []
        rows: list[dict] = []

        # Seed expired chunks: expires_at < now.
        for i in range(expired_count):
            doc_id = f"t2-expired-{i:04d}"
            expired_doc_ids.append(doc_id)
            rows.append({
                "doc_id": doc_id,
                "chunk_id": f"{doc_id}-{i:06d}",
                "text": f"expired chunk {i} for t2 test",
                "vector": [0.0] * _EMBEDDING_DIM,
                "source_path": f"/fake/t2/expired_{i}.txt",
                "indexed_at": past_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": past_iso,   # in the past → will be pruned
                "scopes": None,
            })

        # Seed alive chunks: expires_at > now.
        for i in range(alive_count):
            doc_id = f"t2-alive-{i:04d}"
            alive_doc_ids.append(doc_id)
            rows.append({
                "doc_id": doc_id,
                "chunk_id": f"{doc_id}-{i:06d}",
                "text": f"alive chunk {i} for t2 test",
                "vector": [0.0] * _EMBEDDING_DIM,
                "source_path": f"/fake/t2/alive_{i}.txt",
                "indexed_at": past_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": future_iso,   # in the future → must survive
                "scopes": None,
            })

        # Seed null-expiry chunks: expires_at is None → must survive (S6 invariant).
        for i in range(null_expiry_count):
            doc_id = f"t2-null-{i:04d}"
            null_expiry_doc_ids.append(doc_id)
            rows.append({
                "doc_id": doc_id,
                "chunk_id": f"{doc_id}-{i:06d}",
                "text": f"null-expiry chunk {i} for t2 test",
                "vector": [0.0] * _EMBEDDING_DIM,
                "source_path": f"/fake/t2/null_{i}.txt",
                "indexed_at": past_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": None,   # null → must never be pruned
                "scopes": None,
            })

        if rows:
            await table.add(rows)

    finally:
        await fresh_store.disconnect()

    return expired_doc_ids, alive_doc_ids, null_expiry_doc_ids


async def _count_chunks_fresh(db_path: str, col: str) -> int:
    """Count rows in a collection via a fresh lancedb connection.

    Safety invariant: never reuse the server's loop-bound connection inside
    asyncio.run(). Open a dedicated connection that belongs to THIS call.
    """
    import lancedb

    db = await lancedb.connect_async(db_path)
    try:
        table = await db.open_table(col)
        return await table.count_rows()
    except ValueError:
        return 0
    finally:
        db.close()  # no-op for local; not a coroutine


async def _get_doc_ids_fresh(db_path: str, col: str) -> set[str]:
    """Return the set of doc_ids in a collection via a fresh lancedb connection."""
    import lancedb

    db = await lancedb.connect_async(db_path)
    try:
        table = await db.open_table(col)
        rows = await table.query().select(["doc_id"]).to_list()
        return {r["doc_id"] for r in rows}
    except ValueError:
        return set()
    finally:
        db.close()  # no-op for local; not a coroutine


def _trigger_and_poll_maintenance(client, api_key: str) -> dict:
    """POST /maintenance/trigger then poll GET /status until last_run_at is non-null.

    Returns the maintenance block from the final /status response.
    Fails the test on timeout.
    """
    resp = client.post("/maintenance/trigger", headers=_auth(api_key))
    assert resp.status_code == 202, (
        f"POST /maintenance/trigger failed: {resp.status_code} {resp.text}"
    )

    deadline = time.monotonic() + _POLL_TIMEOUT_S
    maintenance_block: dict | None = None
    while time.monotonic() < deadline:
        r = client.get("/status", headers=_auth(api_key))
        assert r.status_code == 200, f"GET /status failed: {r.status_code} {r.text}"
        body = r.json()
        maintenance_block = body.get("maintenance")
        if maintenance_block and maintenance_block.get("last_run_at") is not None:
            return maintenance_block
        time.sleep(_POLL_INTERVAL_S)

    pytest.fail(
        f"maintenance pass did not complete within {_POLL_TIMEOUT_S}s; "
        f"last maintenance block: {maintenance_block}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_maintenance_prune_deletes_expired_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed expired chunk; POST /maintenance/trigger; verify chunk absent from store (S6).

    Flow:
    1. Start real app with maintenance_enabled=True.
    2. Seed 1 expired chunk directly into LanceDB (expires_at < now).
    3. Assert the chunk exists before trigger.
    4. POST /maintenance/trigger; poll until pass completes.
    5. Assert chunk is gone (count == 0).
    """
    col = "t2-prune-del"

    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 2: seed one expired chunk via a fresh connection (not the server's store).
        asyncio.run(_seed_chunks(cfg.db_path, col, expired_count=1))

        # Step 3: verify the chunk is present before the pass.
        count_before = asyncio.run(_count_chunks_fresh(cfg.db_path, col))
        assert count_before == 1, (
            f"Expected 1 chunk in store before maintenance pass; got {count_before}"
        )

        # Step 4: trigger maintenance and wait for completion.
        _trigger_and_poll_maintenance(client, api_key)

        # Step 5: expired chunk must be deleted.
        count_after = asyncio.run(_count_chunks_fresh(cfg.db_path, col))
        assert count_after == 0, (
            f"Expected 0 chunks after maintenance pass (expired chunk pruned); "
            f"got {count_after}. "
            "If count is still 1, the pruning policy did not run or the predicate was wrong."
        )


def test_maintenance_prune_preserves_non_expired_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed expired + alive + null-expiry chunks; trigger maintenance; only expired deleted (S6).

    Flow:
    1. Start real app with maintenance_enabled=True.
    2. Seed 1 expired chunk (expires_at < now) + 1 alive chunk (expires_at > now)
       + 1 null-expiry chunk (expires_at = None).
    3. Assert 3 chunks exist before trigger.
    4. POST /maintenance/trigger; poll until pass completes.
    5. Assert exactly 2 chunks remain (alive + null-expiry).
    6. Assert the correct doc_ids survived and the expired one is gone.
    """
    col = "t2-prune-keep"

    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 2: seed via a fresh connection (not the server's store).
        expired_ids, alive_ids, null_ids = asyncio.run(
            _seed_chunks(cfg.db_path, col, expired_count=1, alive_count=1, null_expiry_count=1)
        )

        # Step 3: verify all 3 chunks are present before the pass.
        count_before = asyncio.run(_count_chunks_fresh(cfg.db_path, col))
        assert count_before == 3, (
            f"Expected 3 chunks before maintenance pass; got {count_before}"
        )

        # Step 4: trigger and wait.
        _trigger_and_poll_maintenance(client, api_key)

        # Step 5: alive + null-expiry chunks must remain; expired must be gone.
        count_after = asyncio.run(_count_chunks_fresh(cfg.db_path, col))
        assert count_after == 2, (
            f"Expected 2 chunks after maintenance pass (alive + null-expiry preserved, "
            f"expired deleted); got {count_after}. "
            f"expired_doc_id={expired_ids[0]!r}, alive_doc_id={alive_ids[0]!r}, "
            f"null_doc_id={null_ids[0]!r}"
        )

        # Step 6: verify the CORRECT chunks survived.
        surviving_ids = asyncio.run(_get_doc_ids_fresh(cfg.db_path, col))
        assert alive_ids[0] in surviving_ids, (
            f"Alive chunk {alive_ids[0]!r} should have survived but is missing. "
            f"Surviving ids: {surviving_ids}"
        )
        assert null_ids[0] in surviving_ids, (
            f"Null-expiry chunk {null_ids[0]!r} should have survived but is missing "
            "(S6: chunks with expires_at=null must never be pruned). "
            f"Surviving ids: {surviving_ids}"
        )
        assert expired_ids[0] not in surviving_ids, (
            f"Expired chunk {expired_ids[0]!r} should have been pruned but still exists. "
            f"Surviving ids: {surviving_ids}"
        )


def test_status_maintenance_fields_after_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After maintenance trigger, GET /status shows last_expired_pruned_at set and
    expired_chunk_count is an integer (S7).

    Flow:
    1. Start real app with maintenance_enabled=True.
    2. Assert last_expired_pruned_at is None BEFORE any trigger (S7 starts-null).
    3. Seed 1 expired chunk so the prune policy has work to do.
    4. POST /maintenance/trigger; poll until pass completes.
    5. GET /status; assert:
       - maintenance.last_expired_pruned_at is a non-null ISO-8601 string.
       - maintenance.expired_chunk_count is an int (0 or more, never null).
    6. Verify expired_chunk_count is live: after prune, expired count is 0 (chunk deleted).
    """
    col = "t2-status-fields"

    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 2: assert last_expired_pruned_at is None BEFORE any trigger.
        pre_resp = client.get("/status", headers=_auth(api_key))
        assert pre_resp.status_code == 200, (
            f"GET /status pre-trigger failed: {pre_resp.status_code} {pre_resp.text}"
        )
        pre_body = pre_resp.json()
        pre_maintenance = pre_body.get("maintenance") or {}
        assert pre_maintenance.get("last_expired_pruned_at") is None, (
            "maintenance.last_expired_pruned_at must be None before any maintenance pass (S7); "
            f"got: {pre_maintenance.get('last_expired_pruned_at')!r}"
        )

        # Step 3: seed one expired chunk via a fresh connection.
        asyncio.run(_seed_chunks(cfg.db_path, col, expired_count=1))

        # Step 4: trigger and wait for pass to complete.
        maintenance_block = _trigger_and_poll_maintenance(client, api_key)

        # Step 5a: last_expired_pruned_at must be non-null and ISO-8601 parseable.
        pruned_at = maintenance_block.get("last_expired_pruned_at")
        assert pruned_at is not None, (
            "maintenance.last_expired_pruned_at must be non-null after a prune pass (S7); "
            f"full maintenance block: {maintenance_block}"
        )
        # Parse to verify ISO-8601 format (raises ValueError on bad format).
        datetime.fromisoformat(pruned_at)

        # Step 5b: expired_chunk_count must be an integer (never null).
        expired_count = maintenance_block.get("expired_chunk_count")
        assert expired_count is not None, (
            "maintenance.expired_chunk_count must not be null (S7); "
            f"full maintenance block: {maintenance_block}"
        )
        assert isinstance(expired_count, int), (
            f"maintenance.expired_chunk_count must be int, got {type(expired_count)} "
            f"(value={expired_count!r})"
        )

        # Step 6: after the prune, the expired chunk was deleted, so the live count is 0.
        # Re-fetch /status to get a fresh point-in-time count.
        status_resp = client.get("/status", headers=_auth(api_key))
        assert status_resp.status_code == 200
        fresh_body = status_resp.json()
        fresh_count = fresh_body["maintenance"]["expired_chunk_count"]
        assert isinstance(fresh_count, int), (
            f"Fresh expired_chunk_count is not int: {fresh_count!r}"
        )
        assert fresh_count == 0, (
            f"After pruning the single expired chunk, expired_chunk_count should be 0; "
            f"got {fresh_count}. "
            "This field is a live point-in-time count, not the prune-run delta."
        )
