"""Integration tests for BE-3: ACL provenance migration and store behaviour (G15).

Covers:
- test_migrate_acl_provenance_idempotent (S10)
- test_do_ingest_guard_drops_provenance_on_unmigrated_table (S11)
- test_startup_migration_runs_on_server_start (S12)
- test_candidate_builder_reads_provenance_from_row
"""
from __future__ import annotations

import asyncio

import pytest

from tests.integration.conftest import make_real_app


@pytest.mark.asyncio
@pytest.mark.integration
async def test_migrate_acl_provenance_idempotent(tmp_path) -> None:
    """Running migrate_acl_provenance twice leaves columns present exactly once and does not raise (S10)."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("col1", embedding_dim=4)

        # First run
        await store.migrate_acl_provenance()
        # Second run — must be idempotent (no error, no duplicate columns)
        await store.migrate_acl_provenance()

        db = store._require_connected()
        table = await db.open_table("col1")
        schema = await table.schema()
        schema_names = schema.names

        assert "acl_source" in schema_names
        assert "acl_sidecar_path" in schema_names
        assert "acl_warning" in schema_names

        # No duplicate columns — each name appears exactly once
        assert schema_names.count("acl_source") == 1
        assert schema_names.count("acl_sidecar_path") == 1
        assert schema_names.count("acl_warning") == 1
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_do_ingest_guard_drops_provenance_on_unmigrated_table(tmp_path, caplog) -> None:
    """_do_ingest on a table lacking provenance columns logs WARNING and does not crash; ingest succeeds (S11)."""
    import logging

    import lancedb
    import pyarrow as pa

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.store import SearchStore

    # Build an old-schema table (no acl_source/acl_sidecar_path/acl_warning columns)
    old_schema = pa.schema([
        pa.field("doc_id", pa.utf8()),
        pa.field("chunk_id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("source_path", pa.utf8()),
        pa.field("indexed_at", pa.utf8()),
        pa.field("file_type", pa.utf8()),
        pa.field("language", pa.utf8()),
        pa.field("metadata", pa.utf8()),
        pa.field("custom_score", pa.float32(), nullable=True),
        pa.field("ingested_by", pa.utf8()),
        pa.field("updated_at", pa.utf8()),
        pa.field("acl", pa.list_(pa.utf8()), nullable=True),
        pa.field("expires_at", pa.utf8(), nullable=True),
        pa.field("scopes", pa.list_(pa.utf8()), nullable=True),
        # No acl_source, acl_sidecar_path, acl_warning
    ])

    db_path = tmp_path / "db"
    raw_db = await lancedb.connect_async(str(db_path))
    await raw_db.create_table("oldcol", schema=old_schema)
    raw_db.close()

    store = SearchStore(db_path)
    await store.connect()
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        doc_id = "a" * 64
        chunk = ChunkRecord(
            doc_id=doc_id,
            chunk_id=doc_id + "-000000",
            text="test chunk with provenance",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/test.txt",
            indexed_at=now_iso,
            acl_source="frontmatter",
            acl_sidecar_path=None,
            acl_warning=["some warning"],
        )

        db = store._require_connected()
        with caplog.at_level(logging.WARNING, logger="archon_search.store"):
            count = await store._do_ingest(db, "oldcol", [chunk])

        # Ingest must succeed
        assert count == 1

        # WARNING must have been logged
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("acl_provenance" in msg or "acl_source" in msg for msg in warning_messages), (
            f"Expected WARNING about missing ACL provenance columns, got: {warning_messages}"
        )

        # The row must exist in the table
        table = await db.open_table("oldcol")
        rows = await table.query().to_list()
        assert len(rows) == 1
        assert rows[0]["doc_id"] == doc_id

        # Schema must not have grown the provenance columns
        schema_names = (await table.schema()).names
        assert "acl_source" not in schema_names
        assert "acl_sidecar_path" not in schema_names
        assert "acl_warning" not in schema_names
    finally:
        await store.disconnect()


@pytest.mark.integration
def test_startup_migration_runs_on_server_start(tmp_path, monkeypatch) -> None:
    """make_real_app lifespan triggers migration; old rows survive with acl_source=null;
    new ingest populates columns (S12).
    """
    import lancedb
    import pyarrow as pa

    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore
    from archon_search._types import normalize_iso_utc

    db_path = str(tmp_path / "db")

    # Seed a pre-G15 table (without acl_source/acl_sidecar_path/acl_warning) with an existing row.
    pre_migration_schema = pa.schema([
        pa.field("doc_id", pa.utf8()),
        pa.field("chunk_id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("source_path", pa.utf8()),
        pa.field("indexed_at", pa.utf8()),
        pa.field("file_type", pa.utf8()),
        pa.field("language", pa.utf8()),
        pa.field("metadata", pa.utf8()),
        pa.field("custom_score", pa.float32(), nullable=True),
        pa.field("ingested_by", pa.utf8()),
        pa.field("updated_at", pa.utf8()),
        pa.field("acl", pa.list_(pa.utf8()), nullable=True),
        pa.field("expires_at", pa.utf8(), nullable=True),
        pa.field("scopes", pa.list_(pa.utf8()), nullable=True),
        # No acl_source, acl_sidecar_path, acl_warning
    ])

    old_doc_id = "b" * 64

    async def _seed() -> None:
        from datetime import UTC, datetime

        db = await lancedb.connect_async(db_path)
        # Create meta table
        meta_schema = SearchStore._meta_schema()
        # Remove acl_source/acl_sidecar_path/acl_warning from the meta_schema if present
        # (they're chunk-table columns, not meta — meta_schema is unchanged)
        meta_tbl = await db.create_table("_archon_collection_meta", schema=meta_schema)
        await meta_tbl.add([{
            "name": "pre_g15_col",
            "description": "",
            "centroid_json": "[]",
            "description_embedding_json": "[]",
            "doc_count": 0,
            "chunk_count": 0,
            "active_embedding_model": "test",
            "pending_embedding_model": None,
            "needs_reindex": None,
            "reindex_job_id": None,
            "community_rebuild_job_id": None,
            "metadata_reindex_job_id": None,
            "last_indexed": "2024-01-01T00:00:00.000000Z",
            "last_described": "2024-01-01T00:00:00.000000Z",
            "described_at_doc_count": 0,
            "namespace": "default",
            "centroid_sum_json": None,
            "mutations_since_recompute": None,
            "needs_recompute": None,
            "schema_version": None,
            "default_ttl_seconds": None,
        }])

        # Create the pre-G15 chunk table
        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        tbl = await db.create_table("pre_g15_col", schema=pre_migration_schema)
        await tbl.add([{
            "doc_id": old_doc_id,
            "chunk_id": old_doc_id + "-000000",
            "text": "old row without provenance",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "source_path": "/old/doc.txt",
            "indexed_at": now_iso,
            "file_type": "",
            "language": "",
            "metadata": "{}",
            "custom_score": None,
            "ingested_by": "cli",
            "updated_at": now_iso,
            "acl": None,
            "expires_at": None,
            "scopes": None,
        }])

    asyncio.run(_seed())

    # Boot the app — lifespan calls _run_startup_migrations() including migrate_acl_provenance
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        resp = client.get("/health")
        assert resp.status_code == 200

    # Verify columns were added after migration
    async def _check_migration() -> dict:
        db = await lancedb.connect_async(db_path)
        tbl = await db.open_table("pre_g15_col")
        schema_names = (await tbl.schema()).names
        rows = await tbl.query().to_list()
        return {"schema_names": schema_names, "rows": rows}

    result = asyncio.run(_check_migration())
    schema_names = result["schema_names"]
    rows = result["rows"]

    # Columns must be present after migration
    assert "acl_source" in schema_names, (
        f"acl_source missing after startup migration; columns: {schema_names}"
    )
    assert "acl_sidecar_path" in schema_names
    assert "acl_warning" in schema_names

    # Old row must survive with acl_source=null
    assert len(rows) == 1
    old_row = rows[0]
    assert old_row["doc_id"] == old_doc_id
    assert old_row.get("acl_source") is None, (
        f"Pre-G15 row should have acl_source=null, got: {old_row.get('acl_source')}"
    )

    # S12 — new ingest after migration populates the provenance columns (MAJOR-3 fix)
    new_doc_id = "e" * 64

    async def _ingest_after_migration() -> None:
        from datetime import UTC, datetime

        from archon_search._types import ChunkRecord, normalize_iso_utc
        from archon_search.store import SearchStore

        store = SearchStore(db_path)
        await store.connect()
        try:
            db_inner = store._require_connected()
            now = datetime.now(UTC)
            now_iso = normalize_iso_utc(now)
            chunk = ChunkRecord(
                doc_id=new_doc_id,
                chunk_id=new_doc_id + "-000000",
                text="new row ingested after migration",
                vector=[0.5, 0.6, 0.7, 0.8],
                source_path="/new/doc.txt",
                indexed_at=now_iso,
                acl_source="frontmatter",
                acl_sidecar_path=None,
                acl_warning=["migrated-warning"],
            )
            await store._do_ingest(db_inner, "pre_g15_col", [chunk])
        finally:
            await store.disconnect()

    asyncio.run(_ingest_after_migration())

    async def _check_new_row() -> dict:
        db = await lancedb.connect_async(db_path)
        tbl = await db.open_table("pre_g15_col")
        rows_all = await tbl.query().to_list()
        new_row = next((r for r in rows_all if r["doc_id"] == new_doc_id), None)
        return {"new_row": new_row}

    result2 = asyncio.run(_check_new_row())
    new_row = result2["new_row"]
    assert new_row is not None, "New chunk was not persisted after migration"
    assert new_row.get("acl_source") == "frontmatter", (
        f"Expected acl_source='frontmatter' on new row, got: {new_row.get('acl_source')}"
    )
    # acl_warning or None coercion: non-empty list stored as-is (not collapsed to null)
    raw_warning = new_row.get("acl_warning")
    assert raw_warning is not None and list(raw_warning) == ["migrated-warning"], (
        f"Expected acl_warning=['migrated-warning'] on new row, got: {raw_warning}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_candidate_builder_reads_provenance_from_row(tmp_path) -> None:
    """ScoredSearchCandidate built from a row with provenance values carries them;
    pre-migration null row yields None/[] without error.
    """
    import lancedb
    import pyarrow as pa

    from archon_search._types import normalize_iso_utc
    from archon_search.store import SearchStore

    db_path = tmp_path / "db"

    # Build a fully migrated schema (with acl_source/acl_sidecar_path/acl_warning)
    migrated_schema = SearchStore._schema(4)

    raw_db = await lancedb.connect_async(str(db_path))
    tbl = await raw_db.create_table("testcol", schema=migrated_schema)

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    now_iso = normalize_iso_utc(now)

    # Row with provenance values
    doc_with_provenance = "c" * 64
    await tbl.add([{
        "doc_id": doc_with_provenance,
        "chunk_id": doc_with_provenance + "-000000",
        "text": "chunk with sidecar acl",
        "vector": [0.1, 0.2, 0.3, 0.4],
        "source_path": "/docs/file.md",
        "indexed_at": now_iso,
        "file_type": "md",
        "language": "en",
        "metadata": "{}",
        "custom_score": None,
        "ingested_by": "cli",
        "updated_at": now_iso,
        "acl": ["ns-a"],
        "expires_at": None,
        "scopes": None,
        "acl_source": "sidecar",
        "acl_sidecar_path": "docs/file.md.acl",
        "acl_warning": [],
    }])

    # Row with null provenance (pre-G15 behaviour after migration adds nulls)
    doc_null_provenance = "d" * 64
    await tbl.add([{
        "doc_id": doc_null_provenance,
        "chunk_id": doc_null_provenance + "-000000",
        "text": "old chunk without provenance",
        "vector": [0.9, 0.8, 0.7, 0.6],
        "source_path": "/docs/old.md",
        "indexed_at": now_iso,
        "file_type": "md",
        "language": "en",
        "metadata": "{}",
        "custom_score": None,
        "ingested_by": "cli",
        "updated_at": now_iso,
        "acl": None,
        "expires_at": None,
        "scopes": None,
        "acl_source": None,
        "acl_sidecar_path": None,
        "acl_warning": None,
    }])

    raw_db.close()

    store = SearchStore(db_path)
    await store.connect()
    try:
        # Use hybrid_search_with_trace to exercise the candidate builder
        candidates = await store.hybrid_search_with_trace(
            collection="testcol",
            query_vector=[0.1, 0.2, 0.3, 0.4],
            query_text="acl",
            candidate_depth=10,
        )

        # Find the candidate built from the row with provenance
        with_prov = next(
            (c for c in candidates if c.doc_id == doc_with_provenance), None
        )
        assert with_prov is not None, "Candidate with provenance not found in results"
        assert with_prov.acl_source == "sidecar", (
            f"Expected acl_source='sidecar', got {with_prov.acl_source!r}"
        )
        assert with_prov.acl_sidecar_path == "docs/file.md.acl", (
            f"Expected acl_sidecar_path='docs/file.md.acl', got {with_prov.acl_sidecar_path!r}"
        )
        assert with_prov.acl_warning == [], (
            f"Expected acl_warning=[], got {with_prov.acl_warning!r}"
        )

        # Find the candidate built from the null-provenance row
        null_prov = next(
            (c for c in candidates if c.doc_id == doc_null_provenance), None
        )
        assert null_prov is not None, "Candidate with null provenance not found in results"
        assert null_prov.acl_source is None, (
            f"Expected acl_source=None, got {null_prov.acl_source!r}"
        )
        assert null_prov.acl_sidecar_path is None, (
            f"Expected acl_sidecar_path=None, got {null_prov.acl_sidecar_path!r}"
        )
        assert null_prov.acl_warning == [], (
            f"Expected acl_warning=[] (coerced from null), got {null_prov.acl_warning!r}"
        )
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_do_ingest_roundtrip_nonempty_acl_warning(tmp_path) -> None:
    """Non-empty acl_warning survives the production write path (_do_ingest → hybrid_search_with_trace) (MAJOR-1)."""
    from datetime import UTC, datetime

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.store import SearchStore

    db_path = tmp_path / "db"
    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection("warntest", embedding_dim=4)
        await store.migrate_acl_provenance()

        db = store._require_connected()
        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        doc_id = "f" * 64
        chunk = ChunkRecord(
            doc_id=doc_id,
            chunk_id=doc_id + "-000000",
            text="chunk with two warnings",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/warn.txt",
            indexed_at=now_iso,
            acl_source="frontmatter",
            acl_sidecar_path=None,
            acl_warning=["warning-one", "warning-two"],
        )
        count = await store._do_ingest(db, "warntest", [chunk])
        assert count == 1

        candidates = await store.hybrid_search_with_trace(
            collection="warntest",
            query_vector=[0.1, 0.2, 0.3, 0.4],
            query_text="warnings",
            candidate_depth=10,
        )

        candidate = next((c for c in candidates if c.doc_id == doc_id), None)
        assert candidate is not None, "Written chunk not found in search results"
        assert candidate.acl_source == "frontmatter", (
            f"Expected acl_source='frontmatter', got {candidate.acl_source!r}"
        )
        assert candidate.acl_warning == ["warning-one", "warning-two"], (
            f"Expected acl_warning=['warning-one', 'warning-two'], got {candidate.acl_warning!r}"
        )
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_do_ingest_guard_no_warning_for_null_provenance(tmp_path, caplog) -> None:
    """No WARNING is logged when all chunks have null/empty provenance on an unmigrated table (MAJOR-2)."""
    import logging

    import lancedb
    import pyarrow as pa

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.store import SearchStore

    # Build an old-schema table (no provenance columns) — same shape as MAJOR-2 existing test
    old_schema = pa.schema([
        pa.field("doc_id", pa.utf8()),
        pa.field("chunk_id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("source_path", pa.utf8()),
        pa.field("indexed_at", pa.utf8()),
        pa.field("file_type", pa.utf8()),
        pa.field("language", pa.utf8()),
        pa.field("metadata", pa.utf8()),
        pa.field("custom_score", pa.float32(), nullable=True),
        pa.field("ingested_by", pa.utf8()),
        pa.field("updated_at", pa.utf8()),
        pa.field("acl", pa.list_(pa.utf8()), nullable=True),
        pa.field("expires_at", pa.utf8(), nullable=True),
        pa.field("scopes", pa.list_(pa.utf8()), nullable=True),
        # No acl_source, acl_sidecar_path, acl_warning
    ])

    db_path = tmp_path / "db"
    raw_db = await lancedb.connect_async(str(db_path))
    await raw_db.create_table("nullprov", schema=old_schema)
    raw_db.close()

    store = SearchStore(db_path)
    await store.connect()
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        doc_id = "g" * 64
        # All provenance fields are null/empty — the guard should NOT fire
        chunk = ChunkRecord(
            doc_id=doc_id,
            chunk_id=doc_id + "-000000",
            text="chunk with no provenance",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/noprov.txt",
            indexed_at=now_iso,
            acl_source=None,
            acl_sidecar_path=None,
            acl_warning=[],
        )

        db = store._require_connected()
        with caplog.at_level(logging.WARNING, logger="archon_search.store"):
            count = await store._do_ingest(db, "nullprov", [chunk])

        assert count == 1

        # No G15-related warning should have been emitted
        warning_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING
            and ("G15" in r.message or "acl_source" in r.message or "acl_provenance" in r.message)
        ]
        assert not warning_messages, (
            f"Expected no G15/acl_provenance WARNING for null-provenance chunk, got: {warning_messages}"
        )

        # Discriminating positive control: a non-null chunk on the same unmigrated table MUST warn.
        # This proves the guard discriminates (not just "null provenance is structurally skipped").
        doc_id_nonnull = "h" * 64
        nonnull_chunk = ChunkRecord(
            doc_id=doc_id_nonnull,
            chunk_id=doc_id_nonnull + "-000000",
            text="chunk with non-null provenance",
            vector=[0.5, 0.6, 0.7, 0.8],
            source_path="/tmp/nonnull.txt",
            indexed_at=now_iso,
            acl_source="collection_default",
            acl_sidecar_path=None,
            acl_warning=[],
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="archon_search.store"):
            await store._do_ingest(db, "nullprov", [nonnull_chunk])

        positive_warnings = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING
            and ("G15" in r.message or "acl_source" in r.message or "acl_provenance" in r.message)
        ]
        assert positive_warnings, (
            "Expected a G15/acl_provenance WARNING when ingesting a non-null-provenance chunk "
            f"into an unmigrated table, but none were emitted. Guard may be broken."
        )
    finally:
        await store.disconnect()
