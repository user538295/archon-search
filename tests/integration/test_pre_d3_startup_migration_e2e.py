"""e2e test: pre-D3 collection startup migration through full lifespan (D3 T-4).

Scenarios covered:
  S3 — _archon_collection_meta has no schema_version column (pre-D3 DB).
       A pre-existing collection row (written before the column was added) gets
       schema_version=0 from the add_columns default.  After startup migration,
       GET /migrations/pending returns {pending: [], schema_version: 0}; no error.
  S6 (partial) — Server starts with a pre-D3 meta table.  Lifespan runs
       _run_startup_migrations() which adds schema_version column silently; no
       operator action needed.  Note: with STORE_SCHEMA_VERSION=0 and all
       MigrationSpecs at introduced_at=0, no per-collection apply_in_place_migrations
       call is triggered (0 > 0 is False).  Full S6 coverage (per-collection apply
       path) requires STORE_SCHEMA_VERSION > 0 with at least one spec at
       introduced_at > 0.

Test flow:
  1. Seed a pre-D3 LanceDB meta table (no schema_version column) with one real
     collection row using the pre-D3 schema fields.
  2. Drive make_real_app lifespan — _run_startup_migrations() runs
     _migrate_schema_version() which adds the column via add_columns with
     cast(0 as bigint) as the default for all existing rows.
  3. Assert schema_version column is present in _archon_collection_meta.
  4. Read the pre-seeded row directly via LanceDB and assert schema_version == 0
     (proving the migration's cast(0 as bigint) was applied to the pre-existing row,
     not just to newly-created rows via the CollectionMeta dataclass default).
  5. Add the collection path to cfg.collections so GET /migrations/pending can pass
     the two-gate 404 check without calling POST /collections/ (which would 409
     because the meta row already exists from step 1).
  6. Assert GET /collections/{name}/migrations/pending returns pending=[] and
     schema_version=0; no error.

Why seeding a row matters:
  add_columns({"schema_version": "cast(0 as bigint)"}) applies the default to all
  existing rows in the table.  An empty table has no rows to receive this default.
  Without a pre-existing row, the test cannot distinguish "migration applied the
  cast(0 as bigint) default correctly" from "new CollectionMeta dataclass default
  of 0 was used by POST /collections/".

Uses make_real_app (real LanceDB in tmp_path, real TestClient over ASGI
transport).  No additional ML patching — global stubs from tests/conftest.py
are sufficient for the empty-directory registration path.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def test_pre_d3_startup_applies_in_place_migrations_e2e(
    tmp_path, monkeypatch, caplog
) -> None:
    """pre-D3 DB with a real collection row → startup migration → pending=[].

    Covers S3 (pre-existing row without schema_version column gets default 0
    from add_columns migration) and S6-partial (startup silently migrates the
    table structure; per-collection apply_in_place_migrations is not exercised
    because STORE_SCHEMA_VERSION=0 means no specs are pending at startup).

    Key invariant: schema_version=0 must come from _migrate_schema_version()'s
    add_columns({"schema_version": "cast(0 as bigint)"}) applied to the pre-
    existing row — NOT from the CollectionMeta dataclass default written by
    POST /collections/.  The test verifies this via direct LanceDB read BEFORE
    any POST /collections/ call.
    """
    import lancedb
    import pyarrow as pa

    from archon_search.store import STORE_SCHEMA_VERSION, SearchStore
    from archon_search.sync import path_to_collection_name

    db_path = str(tmp_path / "db")

    # Step 1: seed a pre-D3 meta table (no schema_version column) with one row.
    # The schema mirrors _meta_schema() minus the schema_version field — this is
    # what a real pre-D3 LanceDB instance looks like.
    pre_d3_schema = pa.schema(
        [f for f in SearchStore._meta_schema() if f.name != "schema_version"]
    )

    col_path = tmp_path / "pre_d3_docs"
    col_path.mkdir()
    col_name = path_to_collection_name(str(col_path))

    async def _seed_pre_d3() -> None:
        db = await lancedb.connect_async(db_path)
        # One real collection row written using the pre-D3 schema (no schema_version
        # column).  The migration's cast(0 as bigint) must fill it in.
        pre_d3_row = {
            "name": col_name,
            "description": "",
            "centroid_json": "",
            "description_embedding_json": "",
            "doc_count": 0,
            "chunk_count": 0,
            "active_embedding_model": "",
            "pending_embedding_model": None,
            "needs_reindex": False,
            "reindex_job_id": None,
            "last_indexed": "",
            "last_described": "",
            "described_at_doc_count": 0,
            "namespace": "default",
            "centroid_sum_json": None,
            "mutations_since_recompute": 0,
            "needs_recompute": False,
        }
        table = await db.create_table(
            "_archon_collection_meta",
            data=[pre_d3_row],
            schema=pre_d3_schema,
        )
        schema_names = (await table.schema()).names
        assert "schema_version" not in schema_names, (
            "pre-condition: schema_version column must NOT be present in seeded table"
        )

    asyncio.run(_seed_pre_d3())

    # Step 2: boot the app — lifespan calls _run_startup_migrations() which
    # calls _migrate_schema_version() → add_columns({"schema_version": "cast(0 as bigint)"}).
    # This applies the default to ALL existing rows, including the seeded row.
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
            headers = {"Authorization": f"Bearer {api_key}"}

            # Step 3: verify schema_version column is now present.
            async def _read_meta_rows() -> tuple[list[str], list[dict]]:
                db = await lancedb.connect_async(db_path)
                tbl = await db.open_table("_archon_collection_meta")
                schema = await tbl.schema()
                rows = await tbl.query().to_list()
                return schema.names, rows

            column_names, rows = asyncio.run(_read_meta_rows())
            assert "schema_version" in column_names, (
                f"schema_version column missing after startup migration; "
                f"columns present: {column_names}"
            )

            # Step 4: verify the pre-existing row got schema_version=0 from the
            # add_columns migration (cast(0 as bigint)).  This is the core S3 assertion:
            # a row that existed BEFORE the column was added receives the correct default.
            seeded_row = next(
                (r for r in rows if r.get("name") == col_name), None
            )
            assert seeded_row is not None, (
                f"pre-seeded row for {col_name!r} not found after startup migration; "
                f"rows present: {[r.get('name') for r in rows]}"
            )
            seeded_schema_version = seeded_row.get("schema_version")
            assert seeded_schema_version == 0, (
                f"pre-existing row must have schema_version=0 after add_columns migration; "
                f"got schema_version={seeded_schema_version!r}"
            )

            # Step 5: add the collection path to cfg.collections so GET /migrations/pending
            # can pass the config-path gate (gate 1 of the two-gate 404 check) without
            # going through POST /collections/ (which would 409 because the meta row
            # already exists from the seed).
            cfg.collections.append(str(col_path))

            # Step 6: GET /collections/{name}/migrations/pending must return pending=[].
            # All five pre-D3 migration specs have introduced_at=0; the pre-existing row
            # now has schema_version=0 after startup migration.  0 > 0 is False → pending=[].
            resp = client.get(
                f"/collections/{col_name}/migrations/pending",
                headers=headers,
            )
            assert resp.status_code == 200, (
                f"GET /migrations/pending failed: {resp.status_code} {resp.text}"
            )

            body = resp.json()
            assert body["collection"] == col_name, (
                f"expected collection={col_name!r}, got {body['collection']!r}"
            )
            assert body["pending"] == [], (
                f"expected pending=[] for a pre-D3 collection after startup migration; "
                f"got: {body['pending']}"
            )
            # schema_version must be 0: all pre-D3 specs have introduced_at=0 and
            # STORE_SCHEMA_VERSION=0, so a migrated pre-D3 collection is fully up-to-date.
            # NOTE: this assertion uses the literal 0, not STORE_SCHEMA_VERSION, to
            # document that a pre-D3 collection is ALWAYS at version 0 after migration.
            # If STORE_SCHEMA_VERSION is bumped to 1+ in the future, this assertion should
            # remain 0 (the collection was not explicitly migrated to 1).
            assert body["schema_version"] == 0, (
                f"expected schema_version=0 for pre-D3 collection; got {body['schema_version']}"
            )

    # Step 7: assert no WARNING+ log messages from archon_search during the
    # full lifespan + migration path.  Per the test spec: "assert no error logged."
    all_archon_warnings = [
        r for r in caplog.records
        if r.name.startswith("archon_search") and r.levelno >= logging.WARNING
    ]
    # "Concurrent migration" warnings indicate a race between two simultaneous
    # add_columns calls.  This must NOT appear in a single-app test — if it does,
    # it indicates a bug (migration ran twice unexpectedly).
    concurrent_migration_warnings = [
        r for r in all_archon_warnings if "Concurrent migration" in r.message
    ]
    assert concurrent_migration_warnings == [], (
        f"unexpected 'Concurrent migration' warning in single-app test "
        f"(migration ran twice?): "
        f"{[(r.name, r.levelname, r.message) for r in concurrent_migration_warnings]}"
    )
    # All other WARNING+ records are also unexpected during a clean startup migration.
    other_warnings = [
        r for r in all_archon_warnings if "Concurrent migration" not in r.message
    ]
    assert other_warnings == [], (
        f"unexpected WARNING+ log messages from archon_search during startup migration: "
        f"{[(r.name, r.levelname, r.message) for r in other_warnings]}"
    )
    assert STORE_SCHEMA_VERSION == 0, (
        "When STORE_SCHEMA_VERSION is bumped, update this test: seed the row with "
        "schema_version=0, verify it's returned as pending by GET /migrations/pending, "
        "then apply via POST /migrate, and re-verify pending=[]."
    )
