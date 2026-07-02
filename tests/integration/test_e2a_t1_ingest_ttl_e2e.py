"""E2a T-1: E2e tests for TTL ingest, /expiring endpoint, and migration idempotency.

Scenarios covered:
- S1+S5: Ingest with chunk_ttl_seconds; chunk appears in GET /expiring with correct expires_at
         (request-level TTL wins over collection default).
- S3:    Ingest without any TTL; chunk absent from GET /expiring.
- S4:    PATCH collection default_ttl_seconds; new ingest picks it up; chunk appears in /expiring.
- S14:   POST /collections/{name}/migrate is idempotent; calling it twice produces no error.
- S15:   chunk_ttl_seconds=0 → 422 (validation).
- S16:   chunk_scopes with 101 items → 422 (validation).

Uses make_real_app + TestClient (integration label; exercises full HTTP stack).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_search.sync import path_to_collection_name
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Named constants — avoid magic numbers
# ---------------------------------------------------------------------------

_TTL_1H: int = 3600          # 1 hour in seconds
_TTL_2H: int = 7200          # 2 hours in seconds
_WITHIN_HOURS_2: int = 2     # query window for /expiring
_WITHIN_HOURS_24: int = 24   # query window for /expiring (24h)
_SCOPES_OVER_LIMIT: int = 101  # one over the 100-item limit → triggers 422
_POLL_TIMEOUT_S: float = 10.0
_POLL_INTERVAL_S: float = 0.1
_EXPIRES_AT_TOLERANCE_S: float = 30.0  # allowable clock skew between test and server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_job_done(client, job_id: str, api_key: str) -> None:
    """Poll GET /jobs/{job_id} until DONE; fail on FAILED or timeout."""
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=_auth(api_key))
        assert r.status_code == 200, f"GET /jobs/{job_id} returned {r.status_code}"
        status = r.json()["status"]
        if status == "DONE":
            return
        if status == "FAILED":
            pytest.fail(f"ingest job FAILED (job_id={job_id}): {r.json()}")
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"ingest job did not complete within {_POLL_TIMEOUT_S}s (job_id={job_id})")


async def _read_schema_names(db_path: str, col: str) -> list[str]:
    """Return schema column names via fresh LanceDB connection (no shared locks with server)."""
    import lancedb  # noqa: PLC0415
    db = await lancedb.connect_async(str(db_path))
    table = await db.open_table(col)
    schema = await table.schema()
    return schema.names


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ingest_with_ttl_appears_in_expiring_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingest with chunk_ttl_seconds=3600; chunk appears in /expiring within 2h window.

    PATCH first sets default_ttl_seconds=7200 on the collection; the ingest
    uses chunk_ttl_seconds=3600 (request-level).  The resulting expires_at must
    be ~now+3600s (not ~now+7200s) — proving request-level TTL wins over
    collection default (S1, S5).
    """
    col_path = tmp_path / "col_ttl_win"
    col_path.mkdir()
    col = path_to_collection_name(str(col_path))

    doc_file = col_path / "doc.txt"
    doc_file.write_text("hello world content for e2a t1 ttl precedence test", encoding="utf-8")

    toml = f'[collections]\ncollections = ["{col_path!s}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        headers = _auth(api_key)

        # Prime the collection meta via a real ingest (creates meta row safely through HTTP).
        r = client.post("/ingest", json={"collection": col, "path": str(doc_file)}, headers=headers)
        assert r.status_code == 202
        _poll_job_done(client, r.json()["job_id"], api_key)

        # PATCH: set collection default TTL to 7200s.
        resp = client.patch(
            f"/collections/{col}",
            json={"default_ttl_seconds": _TTL_2H},
            headers=headers,
        )
        assert resp.status_code == 200, f"PATCH failed: {resp.status_code} {resp.text}"

        # Record now BEFORE the ingest call (tolerance window: ±30s).
        now = datetime.now(UTC)

        # POST /ingest with request-level chunk_ttl_seconds=3600.
        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc_file), "chunk_ttl_seconds": _TTL_1H},
            headers=headers,
        )
        assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
        _poll_job_done(client, resp.json()["job_id"], api_key)

        # GET /expiring?within_hours=2 — chunk must appear.
        resp = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": _WITHIN_HOURS_2},
            headers=headers,
        )
        assert resp.status_code == 200, f"GET /expiring failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert len(data["items"]) > 0, (
            "Expected at least one expiring chunk after ingest with chunk_ttl_seconds=3600"
        )

        # Verify expires_at ≈ now + 3600s (request-level wins, NOT collection default 7200s).
        raw_expires_at = data["items"][0]["expires_at"]
        actual_expires_at = datetime.fromisoformat(raw_expires_at)
        if actual_expires_at.tzinfo is None:
            actual_expires_at = actual_expires_at.replace(tzinfo=UTC)
        assert actual_expires_at > now, "expires_at must be in the future"
        expected = now + timedelta(seconds=_TTL_1H)
        delta_s = abs((actual_expires_at - expected).total_seconds())
        assert delta_s < _EXPIRES_AT_TOLERANCE_S, (
            f"expires_at should be ~now+{_TTL_1H}s (request-level); "
            f"actual delta={delta_s:.1f}s "
            f"(actual={raw_expires_at}, expected≈{expected.isoformat()}). "
            "If delta is ~3600s, collection default was applied instead of request-level."
        )


def test_patch_collection_default_ttl_applies_to_new_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH default_ttl_seconds then ingest (no chunk_ttl_seconds) → chunk appears in /expiring (S4).

    Proves the collection default TTL is picked up when no per-request TTL is supplied.
    Also verifies default_ttl_seconds persists through subsequent ingests (regression for
    store.py bug where CollectionMeta constructors omitted default_ttl_seconds).
    """
    col_path = tmp_path / "col_default_ttl"
    col_path.mkdir()
    col = path_to_collection_name(str(col_path))

    doc_file = col_path / "doc.txt"
    doc_file.write_text("hello world for e2a t1 collection default ttl test", encoding="utf-8")

    setup_file = col_path / "setup.txt"
    setup_file.write_text("setup content for collection meta priming", encoding="utf-8")

    toml = f'[collections]\ncollections = ["{col_path!s}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        headers = _auth(api_key)

        # Prime the collection meta via a real ingest of a separate setup file.
        r = client.post("/ingest", json={"collection": col, "path": str(setup_file)}, headers=headers)
        assert r.status_code == 202
        _poll_job_done(client, r.json()["job_id"], api_key)

        # PATCH: set collection default TTL to 1 hour.
        resp = client.patch(
            f"/collections/{col}",
            json={"default_ttl_seconds": _TTL_1H},
            headers=headers,
        )
        assert resp.status_code == 200, f"PATCH failed: {resp.status_code} {resp.text}"

        # Record now BEFORE the ingest call (tolerance window: ±30s).
        now = datetime.now(UTC)

        # Ingest WITHOUT specifying chunk_ttl_seconds — pipeline must use collection default.
        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc_file)},
            headers=headers,
        )
        assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
        _poll_job_done(client, resp.json()["job_id"], api_key)

        # GET /expiring?within_hours=2 — chunk must appear (default TTL = 1h, within 2h window).
        resp = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": _WITHIN_HOURS_2},
            headers=headers,
        )
        assert resp.status_code == 200, f"GET /expiring failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert len(data["items"]) > 0, (
            "Expected at least one expiring chunk after ingest using collection default_ttl_seconds=3600"
        )

        # Verify expires_at ≈ now + 3600s.
        raw_expires_at = data["items"][0]["expires_at"]
        actual_expires_at = datetime.fromisoformat(raw_expires_at)
        if actual_expires_at.tzinfo is None:
            actual_expires_at = actual_expires_at.replace(tzinfo=UTC)
        assert actual_expires_at > now, "expires_at must be in the future"
        expected = now + timedelta(seconds=_TTL_1H)
        delta_s = abs((actual_expires_at - expected).total_seconds())
        assert delta_s < _EXPIRES_AT_TOLERANCE_S, (
            f"expires_at should be ~now+{_TTL_1H}s (collection default); "
            f"actual delta={delta_s:.1f}s "
            f"(actual={raw_expires_at}, expected≈{expected.isoformat()})."
        )

        # Verify default_ttl_seconds persists through ingest (regression: store.py wipes it otherwise).
        r2 = client.post("/ingest", json={"collection": col, "path": str(setup_file)}, headers=headers)
        assert r2.status_code == 202
        _poll_job_done(client, r2.json()["job_id"], api_key)

        resp = client.get(f"/collections/{col}/expiring", params={"within_hours": _WITHIN_HOURS_2}, headers=headers)
        assert resp.status_code == 200
        # Both doc.txt and setup.txt chunks must appear — proves default_ttl_seconds was not wiped by first ingest.
        assert len(resp.json()["items"]) >= 2, (
            "Both ingested files must appear in /expiring — if only 1, default_ttl_seconds was wiped after first ingest"
        )


def test_ingest_null_ttl_not_in_expiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingest without TTL (null on all three sources) → chunk absent from GET /expiring (S3)."""
    col_path = tmp_path / "col_no_ttl"
    col_path.mkdir()
    col = path_to_collection_name(str(col_path))

    doc_file = col_path / "doc.txt"
    doc_file.write_text("hello world for e2a t1 null ttl test", encoding="utf-8")

    toml = f'[collections]\ncollections = ["{col_path!s}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        headers = _auth(api_key)

        # Ingest without any TTL (no chunk_ttl_seconds, no collection default).
        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc_file)},
            headers=headers,
        )
        assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
        _poll_job_done(client, resp.json()["job_id"], api_key)

        # GET /expiring?within_hours=24 — chunk must NOT appear (no expiry set).
        resp = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": _WITHIN_HOURS_24},
            headers=headers,
        )
        assert resp.status_code == 200, f"GET /expiring failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert data["items"] == [], (
            f"Expected no expiring chunks (null TTL should not appear in /expiring); "
            f"got {data['items']}"
        )


def test_ingest_invalid_ttl_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /ingest with chunk_ttl_seconds=0 → 422 (S15).

    Validation is enforced by Pydantic at the route layer; no job is created.
    """
    col_path = tmp_path / "col_bad_ttl"
    col_path.mkdir()
    col = path_to_collection_name(str(col_path))

    doc_file = col_path / "doc.txt"
    doc_file.write_text("content", encoding="utf-8")

    toml = f'[collections]\ncollections = ["{col_path!s}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        headers = _auth(api_key)

        # chunk_ttl_seconds=0 is outside the valid range [1, 2^31-1] → 422.
        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc_file), "chunk_ttl_seconds": 0},
            headers=headers,
        )
        assert resp.status_code == 422, (
            f"Expected 422 for chunk_ttl_seconds=0, got {resp.status_code}: {resp.text}"
        )


def test_ingest_invalid_scopes_list_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /ingest with 101-item chunk_scopes list → 422 (S16).

    Validation is enforced by Pydantic at the route layer; no job is created.
    """
    col_path = tmp_path / "col_bad_scopes"
    col_path.mkdir()
    col = path_to_collection_name(str(col_path))

    doc_file = col_path / "doc.txt"
    doc_file.write_text("content", encoding="utf-8")

    toml = f'[collections]\ncollections = ["{col_path!s}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        headers = _auth(api_key)

        too_many_scopes = [f"scope-{i}" for i in range(_SCOPES_OVER_LIMIT)]  # 101 items

        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc_file), "chunk_scopes": too_many_scopes},
            headers=headers,
        )
        assert resp.status_code == 422, (
            f"Expected 422 for chunk_scopes with 101 items, got {resp.status_code}: {resp.text}"
        )


def test_post_migrate_idempotency_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /collections/{name}/migrate is idempotent: two calls both return 200.

    New collections get schema_version=STORE_SCHEMA_VERSION=1 from the first
    ingest (via _do_update_meta_on_add), so both calls have no pending migrations
    and return migrations_applied=[].  This verifies:
    1. Both calls return 200 (no crash, no 500).
    2. Response includes migrations_applied list.
    3. Chunk table already has expires_at and scopes columns (from _schema()).
    4. Second call returns the same result (idempotent — S14).
    """
    col_path = tmp_path / "col_migrate"
    col_path.mkdir()
    col = path_to_collection_name(str(col_path))

    doc_file = col_path / "doc.txt"
    doc_file.write_text(
        "content for e2a t1 migration idempotency test hello world", encoding="utf-8"
    )

    toml = f'[collections]\ncollections = ["{col_path!s}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        headers = _auth(api_key)

        # Ingest first to create the chunk table and the collection meta row.
        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc_file)},
            headers=headers,
        )
        assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
        _poll_job_done(client, resp.json()["job_id"], api_key)

        # First call: both calls are idempotent (schema_version=STORE_SCHEMA_VERSION=1 after ingest).
        resp1 = client.post(
            f"/collections/{col}/migrate",
            json={},
            headers=headers,
        )
        assert resp1.status_code == 200, (
            f"First POST /migrate failed: {resp1.status_code} {resp1.text}"
        )
        result1 = resp1.json()
        assert "migrations_applied" in result1, (
            f"Response missing migrations_applied: {result1}"
        )
        # New collections already have schema_version=1; no pending migrations.
        assert isinstance(result1["migrations_applied"], list), (
            f"migrations_applied should be a list; got {type(result1['migrations_applied'])}"
        )

        # Verify chunk table schema has expires_at and scopes columns (from _schema()).
        schema_names = asyncio.run(_read_schema_names(cfg.db_path, col))
        assert "expires_at" in schema_names, (
            f"expires_at column absent from chunk table schema: {schema_names}"
        )
        assert "scopes" in schema_names, (
            f"scopes column absent from chunk table schema: {schema_names}"
        )

        # Second call: must also return 200 with no error (idempotent).
        resp2 = client.post(
            f"/collections/{col}/migrate",
            json={},
            headers=headers,
        )
        assert resp2.status_code == 200, (
            f"Second POST /migrate failed: {resp2.status_code} {resp2.text}"
        )
        result2 = resp2.json()
        assert "migrations_applied" in result2, (
            f"Response missing migrations_applied on second call: {result2}"
        )
        # Both calls must return the same list (idempotent).
        assert result2["migrations_applied"] == result1["migrations_applied"], (
            f"Second migrate call returned a different result than first; "
            f"first={result1['migrations_applied']}, second={result2['migrations_applied']}"
        )
