"""e2e test: schema health status reflects live migration state (D3 T-5).

Scenario S17:
  Given collections exist with schema_version tracked.
  When GET /status.
  Then response includes store_schema_version: int and collections_schema_behind: int
  (count of collections with schema_version < STORE_SCHEMA_VERSION).

Test flow:
  1. Register a collection via POST /collections/.
  2. GET /status → collections_schema_behind == 0 (collection at schema_version=0 == STORE_SCHEMA_VERSION=0).
  3. Seed schema_version=-1 directly in LanceDB to simulate a behind collection.
  4. GET /status → collections_schema_behind == 1.
  5. POST /collections/{name}/migrate (in-place apply) → 200.
  6. GET /status → collections_schema_behind == 0 (decremented after migration).
  7. Assert store_schema_version equals STORE_SCHEMA_VERSION throughout all GET /status calls.

Uses make_real_app (real LanceDB in tmp_path, real JobScheduler, TestClient over
ASGI transport). No ML patching beyond the global stubs from tests/conftest.py.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def test_schema_status_reflects_migration_state_e2e(tmp_path, monkeypatch) -> None:
    """GET /status collections_schema_behind reflects live migration state.

    Covers S17 end-to-end:
    - Fresh collection: collections_schema_behind == 0.
    - After seeding schema_version=-1: collections_schema_behind == 1.
    - After POST /migrate (in-place apply): collections_schema_behind == 0.
    - store_schema_version equals STORE_SCHEMA_VERSION throughout.
    """
    import lancedb

    from archon_search.store import STORE_SCHEMA_VERSION
    from archon_search.sync import path_to_collection_name

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # Step 1: Register a collection.
        col_path = tmp_path / "schema_status_docs"
        col_path.mkdir()
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=headers,
        )
        assert resp.status_code == 202, (
            f"POST /collections/ failed: {resp.status_code} {resp.text}"
        )

        # Poll until the registration ingest job reaches a terminal state.
        # Without this, a race exists: the background job calls update_collection_meta
        # (schema_version=STORE_SCHEMA_VERSION) AFTER we seed -1, overwriting our value.
        ingest_job_id = resp.json()["job_id"]
        deadline = time.monotonic() + 10.0
        status = None
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{ingest_job_id}", headers=headers)
            assert r.status_code == 200
            status = r.json()["status"]
            if status in ("DONE", "FAILED"):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"ingest job did not reach terminal state in 10s (job_id={ingest_job_id})")
        assert status == "DONE", (
            f"ingest job must reach DONE before seeding; reached {status!r} instead"
        )

        # Step 2: GET /status — freshly registered collection is at schema_version=0
        # which equals STORE_SCHEMA_VERSION=0; collections_schema_behind must be 0.
        # NOTE: with STORE_SCHEMA_VERSION=0, this assertion is tautological (no
        # production code path sets schema_version < 0). Step 4 is the substantive
        # proof that the route reads live DB data.
        resp = client.get("/status", headers=headers)
        assert resp.status_code == 200, (
            f"GET /status failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()
        assert body["store_schema_version"] == STORE_SCHEMA_VERSION, (
            f"expected store_schema_version={STORE_SCHEMA_VERSION}, "
            f"got {body['store_schema_version']}"
        )
        assert body["collections_schema_behind"] == 0, (
            f"expected collections_schema_behind=0 for fresh collection, "
            f"got {body['collections_schema_behind']}"
        )

        # Step 3: Seed schema_version=-1 to make the collection appear behind.
        # Direct LanceDB write — bypasses the asyncio.Lock (which belongs to the app's
        # event loop); same pattern as test_migrate_dry_run_in_place_e2e.py.
        async def _seed_schema_version_behind() -> None:
            db = await lancedb.connect_async(cfg.db_path)
            tbl = await db.open_table("_archon_collection_meta")
            rows = await tbl.query().to_list()
            target = next((r for r in rows if r["name"] == col_name), None)
            assert target is not None, (
                f"collection {col_name!r} not found in meta table"
            )
            await tbl.delete(f"name = '{col_name}'")
            target = dict(target)
            target["schema_version"] = -1
            await tbl.add([target])

        asyncio.run(_seed_schema_version_behind())

        # Step 4: GET /status — collection is now behind; collections_schema_behind == 1.
        resp = client.get("/status", headers=headers)
        assert resp.status_code == 200, (
            f"GET /status (after seed) failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()
        assert body["store_schema_version"] == STORE_SCHEMA_VERSION, (
            f"expected store_schema_version={STORE_SCHEMA_VERSION}, "
            f"got {body['store_schema_version']}"
        )
        assert body["collections_schema_behind"] == 1, (
            f"expected collections_schema_behind=1 after seeding schema_version=-1, "
            f"got {body['collections_schema_behind']}"
        )

        # Step 5: POST /collections/{name}/migrate applies in-place migrations
        # synchronously (200, no MigrationJob created).
        resp = client.post(
            f"/collections/{col_name}/migrate",
            json={},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"POST /migrate in-place failed: {resp.status_code} {resp.text}"
        )
        migrate_body = resp.json()
        assert "migrations_applied" in migrate_body, (
            f"expected 'migrations_applied' in POST /migrate response: {migrate_body}"
        )
        assert len(migrate_body["migrations_applied"]) > 0, (
            f"expected non-empty migrations_applied after seeding schema_version=-1; "
            f"got: {migrate_body['migrations_applied']}"
        )

        # Step 6: GET /status — count decrements back to 0 after migration.
        resp = client.get("/status", headers=headers)
        assert resp.status_code == 200, (
            f"GET /status (after migrate) failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()
        assert body["store_schema_version"] == STORE_SCHEMA_VERSION, (
            f"expected store_schema_version={STORE_SCHEMA_VERSION} after migration, "
            f"got {body['store_schema_version']}"
        )
        assert body["collections_schema_behind"] == 0, (
            f"expected collections_schema_behind=0 after in-place migration, "
            f"got {body['collections_schema_behind']}"
        )

        # Guard: when STORE_SCHEMA_VERSION bumps to 1+, Step 2's assertion
        # (collections_schema_behind == 0) becomes non-tautological (fresh
        # collections still start at schema_version=STORE_SCHEMA_VERSION, so
        # they won't be behind). The seed step (schema_version=-1) will still
        # exercise the < comparison. Review and update this test when bumping.
        assert STORE_SCHEMA_VERSION == 0, (
            "STORE_SCHEMA_VERSION was bumped; review this test — Step 2 may now "
            "need a fresh collection seed path, and Step 3 may need adjustment."
        )
