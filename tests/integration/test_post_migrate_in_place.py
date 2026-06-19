"""Integration tests for POST /collections/{name}/migrate in-place path (D3 BE-7).

Uses make_real_app + TestClient to exercise the full stack with a real LanceDB.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import make_real_app


@pytest.mark.integration
def test_post_migrate_in_place_real_store(tmp_path, monkeypatch) -> None:
    """POST /collections/{name}/migrate returns 200 with migrations_applied; no MigrationJob created.

    With STORE_SCHEMA_VERSION=0 and all pre-D3 migrations having introduced_at=0,
    no migration is pending after startup (startup migrations bring schema_version
    to 0, matching STORE_SCHEMA_VERSION). The POST returns migrations_applied=[] —
    the "nothing to apply" path.

    schema_version update correctness is covered by store unit tests
    (test_apply_in_place_updates_schema_version) which directly call
    apply_in_place_migrations with real LanceDB.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # Register a collection.
        col_path = tmp_path / "my_docs"
        col_path.mkdir()

        from archon_search.sync import path_to_collection_name
        col_name = path_to_collection_name(str(col_path))

        resp = client.post("/collections/", json={"path": str(col_path)}, headers=headers)
        assert resp.status_code == 202, f"add_collection failed: {resp.status_code} {resp.text}"

        # POST migrate — no pending migrations → empty migrations_applied.
        resp = client.post(f"/collections/{col_name}/migrate", json={}, headers=headers)
        assert resp.status_code == 200, f"POST /migrate failed: {resp.status_code} {resp.text}"
        result = resp.json()
        assert "migrations_applied" in result
        # STORE_SCHEMA_VERSION=0: all pre-D3 migrations have introduced_at=0,
        # so they are not pending (applied at startup). migrations_applied is empty.
        assert result["migrations_applied"] == []

        # Verify no MigrationJob was created.
        resp = client.get("/jobs?kind=migration", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        # GET /migrations/pending must also be empty.
        resp = client.get(f"/collections/{col_name}/migrations/pending", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["pending"] == []
