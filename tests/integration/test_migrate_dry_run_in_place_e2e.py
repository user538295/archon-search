"""e2e test: full dry-run → in-place apply → pending empty flow (D3 T-1).

Scenario:
  1. Register a collection and seed its schema_version to -1 so that all
     five formalised in-place migrations (introduced_at=0) appear pending.
  2. GET /collections/{name}/migrations/pending returns a non-empty list.
  3. POST /collections/{name}/migrate (no flags, defaults to in-place) returns 200
     with migrations_applied list; no MigrationJob created.
  4. GET /collections/{name}/migrations/pending returns {pending: []}.
  5. schema_version in _archon_collection_meta equals STORE_SCHEMA_VERSION.

Uses make_real_app (real LanceDB in tmp_path, real JobScheduler, TestClient
over ASGI transport). No patching — the full real code path is exercised.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def test_migrate_dry_run_then_in_place_apply_e2e(
    tmp_path, monkeypatch
) -> None:
    """Full e2e: dry-run view → in-place apply → empty pending list.

    Seeds schema_version=-1 on the collection so the five pre-D3 migrations
    (introduced_at=0) appear pending (0 > -1). After POST /migrate the route
    calls the real apply_in_place_migrations which runs the five idempotent
    migrate_*() methods and sets schema_version=STORE_SCHEMA_VERSION (0).
    Subsequent GET /migrations/pending returns pending=[].

    Covers scenarios S1 (non-empty pending), S2 (empty after apply), S5
    (in-place 200, no MigrationJob).
    """
    from archon_search.store import STORE_SCHEMA_VERSION, SearchStore
    from archon_search.sync import path_to_collection_name
    from archon_search.types import MigrationJob, MigrationKind

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # Register a collection.
        col_path = tmp_path / "dry_run_docs"
        col_path.mkdir()
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=headers,
        )
        assert resp.status_code == 202, (
            f"add_collection failed: {resp.status_code} {resp.text}"
        )

        # Poll the ingest job until it reaches a terminal state.
        # This prevents a race where the ingest job calls update_collection_meta
        # (which sets schema_version=STORE_SCHEMA_VERSION=0) AFTER we seed -1,
        # overwriting our seeded value back to 0.
        ingest_job_id = resp.json()["job_id"]
        deadline = time.monotonic() + 10.0
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

        # Seed schema_version=-1 so all pre-D3 migrations (introduced_at=0) appear pending.
        # 0 > -1 is True → all five specs are returned by pending_migrations().
        # Direct LanceDB write bypasses the asyncio.Lock (which belongs to the app's event
        # loop) — the same pattern used by test_startup_migrations.py and
        # test_collection_lifecycle_integration.py.
        import lancedb

        async def _seed_schema_version_behind() -> None:
            db = await lancedb.connect_async(cfg.db_path)
            tbl = await db.open_table("_archon_collection_meta")
            rows = await tbl.query().to_list()
            target = next((r for r in rows if r["name"] == col_name), None)
            assert target is not None, f"collection {col_name!r} not found in meta table"
            await tbl.delete(f"name = '{col_name}'")
            target = dict(target)
            target["schema_version"] = -1
            await tbl.add([target])

        asyncio.run(_seed_schema_version_behind())

        # Step 1: GET /migrations/pending must return a non-empty list.
        resp = client.get(
            f"/collections/{col_name}/migrations/pending",
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"GET /migrations/pending failed: {resp.status_code} {resp.text}"
        )
        pending_body = resp.json()
        assert pending_body["collection"] == col_name
        assert pending_body["schema_version"] == -1
        assert len(pending_body["pending"]) > 0, (
            "expected non-empty pending list when schema_version=-1"
        )
        # All pending specs must have kind="in_place" (the five pre-D3 migrations).
        for spec in pending_body["pending"]:
            assert spec["kind"] == "in_place", (
                f"expected all pending specs to be in_place, got {spec['kind']!r} for {spec['name']!r}"
            )

        # Step 2: dry_run=true returns the same pending list without applying anything.
        resp = client.post(
            f"/collections/{col_name}/migrate",
            json={"dry_run": True},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"POST /migrate dry_run failed: {resp.status_code} {resp.text}"
        )
        dry_run_body = resp.json()
        assert dry_run_body["collection"] == col_name
        assert len(dry_run_body["pending"]) > 0, (
            "dry_run=true must return same pending list without side effects"
        )
        # dry_run response must match the GET /migrations/pending response.
        assert dry_run_body["pending"] == pending_body["pending"], (
            "dry_run POST must return the same pending list as GET /migrations/pending"
        )
        # dry_run must report the unchanged schema_version.
        assert dry_run_body["schema_version"] == -1, (
            "dry_run response must report schema_version=-1 (no changes applied)"
        )
        # dry_run response must NOT contain migrations_applied (no apply occurred).
        assert "migrations_applied" not in dry_run_body, (
            "dry_run response must not contain 'migrations_applied'"
        )

        # Verify dry_run made no changes: schema_version still -1.
        async def _read_schema_version() -> int:
            db = await lancedb.connect_async(cfg.db_path)
            tbl = await db.open_table("_archon_collection_meta")
            rows = await tbl.query().to_list()
            row = next((r for r in rows if r["name"] == col_name), None)
            assert row is not None
            return int(row["schema_version"])

        assert asyncio.run(_read_schema_version()) == -1, (
            "schema_version must be unchanged after dry_run"
        )

        # Step 3: POST /migrate (no dry_run) applies in-place migrations synchronously.
        resp = client.post(
            f"/collections/{col_name}/migrate",
            json={},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"POST /migrate in-place failed: {resp.status_code} {resp.text}"
        )
        apply_body = resp.json()
        assert "migrations_applied" in apply_body, (
            f"expected 'migrations_applied' in response body: {apply_body}"
        )
        # Verify exact migration names from the canonical catalog (IN_PLACE only).
        # The route filters to in_place_specs before returning, so we mirror that filter.
        # Using _all_migrations() unfiltered would break when a REWRITE spec is added.
        expected_migration_names = [
            s.name for s in SearchStore._all_migrations()
            if s.kind == MigrationKind.IN_PLACE
        ]
        assert apply_body["migrations_applied"] == expected_migration_names, (
            f"expected migrations_applied={expected_migration_names!r}, "
            f"got {apply_body['migrations_applied']!r}"
        )

        # Step 4: No MigrationJob must have been created (in-place path, not rewrite).
        # Use direct job_store access to verify no MigrationJob was created for in-place-only
        # migrations (POST /migrate for in-place returns 200 and applies synchronously, no job).
        job_store = client.app.state.job_store
        migration_jobs = [j for j in job_store.list() if isinstance(j, MigrationJob)]
        assert len(migration_jobs) == 0, (
            "no MigrationJob should be created for in-place-only migrations"
        )

        # Step 5: schema_version in _archon_collection_meta must equal STORE_SCHEMA_VERSION.
        final_schema_version = asyncio.run(_read_schema_version())
        assert final_schema_version == STORE_SCHEMA_VERSION, (
            f"expected schema_version={STORE_SCHEMA_VERSION} after apply, "
            f"got {final_schema_version}"
        )

        # Step 6: GET /migrations/pending must now return an empty list.
        resp = client.get(
            f"/collections/{col_name}/migrations/pending",
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"GET /migrations/pending (after apply) failed: {resp.status_code} {resp.text}"
        )
        after_body = resp.json()
        assert after_body["pending"] == [], (
            f"expected empty pending list after apply, got: {after_body['pending']}"
        )
        assert after_body["schema_version"] == STORE_SCHEMA_VERSION
