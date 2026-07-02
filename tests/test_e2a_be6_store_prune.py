"""Tests for BE-6: store.prune_expired_chunks, store.count_expired_chunks,
and MaintenanceConfig.prune_expired_chunks.

Plan: Documentation/Backlog/e2a-ttl-scoping-team-plan.md Task BE-6.

TDD: tests are written first; implementation goes in store.py and config.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from archon_search._types import normalize_iso_utc


# ---------------------------------------------------------------------------
# Unit: MaintenanceConfig.prune_expired_chunks default and TOML round-trip
# ---------------------------------------------------------------------------


def test_maintenance_config_prune_expired_chunks_default_true(tmp_path: Path) -> None:
    """MaintenanceConfig.prune_expired_chunks defaults to True."""
    from archon_search.config import MaintenanceConfig

    cfg = MaintenanceConfig()
    assert cfg.prune_expired_chunks is True


def test_maintenance_config_prune_false_from_toml(tmp_path: Path) -> None:
    """prune_expired_chunks=false in TOML sets the field to False."""
    from archon_search.config import load_config

    toml_path = tmp_path / "archon-search.toml"
    toml_path.write_text("[maintenance]\nprune_expired_chunks = false\n", encoding="utf-8")
    config = load_config(path=toml_path)
    assert config.maintenance.prune_expired_chunks is False


def test_maintenance_config_prune_true_from_toml(tmp_path: Path) -> None:
    """prune_expired_chunks=true in TOML sets the field to True."""
    from archon_search.config import load_config

    toml_path = tmp_path / "archon-search.toml"
    toml_path.write_text("[maintenance]\nprune_expired_chunks = true\n", encoding="utf-8")
    config = load_config(path=toml_path)
    assert config.maintenance.prune_expired_chunks is True


# ---------------------------------------------------------------------------
# Unit: prune_expired_chunks behaviour (mocked via real small store)
# ---------------------------------------------------------------------------
# These use a real LanceDB store in tmp_path rather than MagicMock because
# LanceDB's async API is hard to mock precisely.  They are still unit-level
# in intent: controlled fixture, single assertion per test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_prune_expired_chunks_returns_doc_ids(tmp_path: Path) -> None:
    """prune_expired_chunks returns doc_ids of deleted (expired) chunks."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        past_iso = normalize_iso_utc(now - timedelta(seconds=10))
        now_iso = normalize_iso_utc(now)

        db = store._require_connected()
        table = await db.open_table("col")

        # Insert 3 expired chunks with distinct doc_ids
        rows = []
        doc_ids = []
        for i in range(3):
            doc_id = (chr(ord("a") + i) * 64)
            chunk_id = doc_id + "-000000"
            doc_ids.append(doc_id)
            rows.append({
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "text": f"expired chunk {i}",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": f"/tmp/f{i}.txt",
                "indexed_at": now_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "cli",
                "updated_at": now_iso,
                "acl": None,
                "expires_at": past_iso,
                "scopes": None,
            })
        await table.add(rows)

        returned_doc_ids = await store.prune_expired_chunks("col", "default")

        assert set(returned_doc_ids) == set(doc_ids)
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_prune_expired_chunks_does_not_delete_future_expires(tmp_path: Path) -> None:
    """prune_expired_chunks does not include chunks with future expires_at."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        future_iso = normalize_iso_utc(now + timedelta(hours=1))

        db = store._require_connected()
        table = await db.open_table("col")

        await table.add([{
            "doc_id": "a" * 64,
            "chunk_id": "a" * 64 + "-000000",
            "text": "future chunk",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "source_path": "/tmp/f.txt",
            "indexed_at": now_iso,
            "file_type": "",
            "language": "",
            "metadata": "{}",
            "custom_score": None,
            "ingested_by": "cli",
            "updated_at": now_iso,
            "acl": None,
            "expires_at": future_iso,
            "scopes": None,
        }])

        returned_doc_ids = await store.prune_expired_chunks("col", "default")

        assert returned_doc_ids == []
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_prune_expired_chunks_does_not_delete_null_expires(tmp_path: Path) -> None:
    """prune_expired_chunks does not include chunks with null expires_at."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)

        db = store._require_connected()
        table = await db.open_table("col")

        await table.add([{
            "doc_id": "b" * 64,
            "chunk_id": "b" * 64 + "-000000",
            "text": "no-expiry chunk",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "source_path": "/tmp/g.txt",
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

        returned_doc_ids = await store.prune_expired_chunks("col", "default")

        assert returned_doc_ids == []
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_prune_expired_chunks_real_store(tmp_path: Path) -> None:
    """After prune, expired rows are gone; non-expired rows survive.

    S6: chunk with expires_at < now is deleted; chunk with expires_at >= now is not.
    """
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        past_iso = normalize_iso_utc(now - timedelta(seconds=10))
        future_iso = normalize_iso_utc(now + timedelta(hours=1))

        db = store._require_connected()
        table = await db.open_table("col")

        expired_doc_id = "c" * 64
        alive_doc_id = "d" * 64

        await table.add([
            {
                "doc_id": expired_doc_id,
                "chunk_id": expired_doc_id + "-000000",
                "text": "expired",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/exp.txt",
                "indexed_at": now_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "cli",
                "updated_at": now_iso,
                "acl": None,
                "expires_at": past_iso,
                "scopes": None,
            },
            {
                "doc_id": alive_doc_id,
                "chunk_id": alive_doc_id + "-000000",
                "text": "alive",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/alive.txt",
                "indexed_at": now_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "cli",
                "updated_at": now_iso,
                "acl": None,
                "expires_at": future_iso,
                "scopes": None,
            },
        ])

        pruned = await store.prune_expired_chunks("col", "default")

        assert expired_doc_id in pruned
        assert alive_doc_id not in pruned

        # Verify the expired row is actually gone from the store
        total = await store.count_chunks("col", "default")
        assert total == 1
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# Unit: count_expired_chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_count_expired_chunks_returns_correct_count(tmp_path: Path) -> None:
    """count_expired_chunks returns the number of chunks with expires_at < now."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        past_iso = normalize_iso_utc(now - timedelta(seconds=10))
        future_iso = normalize_iso_utc(now + timedelta(hours=1))

        db = store._require_connected()
        table = await db.open_table("col")

        await table.add([
            {
                "doc_id": "e" * 64, "chunk_id": "e" * 64 + "-000000",
                "text": "expired", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/e1.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": past_iso, "scopes": None,
            },
            {
                "doc_id": "f" * 64, "chunk_id": "f" * 64 + "-000000",
                "text": "future", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/e2.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": future_iso, "scopes": None,
            },
            {
                "doc_id": "g" * 64, "chunk_id": "g" * 64 + "-000000",
                "text": "no expiry", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/e3.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": None, "scopes": None,
            },
        ])

        count = await store.count_expired_chunks("col", "default")
        assert count == 1
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_count_expired_chunks_returns_zero_on_pre_migration_table(tmp_path: Path) -> None:
    """count_expired_chunks returns 0 for pre-E2a collection (no expires_at column)."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        # Create collection WITHOUT applying E2a migrations
        await store.ensure_collection("col", embedding_dim=4)
        # DO NOT call apply_in_place_migrations — col is at schema_version 0

        count = await store.count_expired_chunks("col", "default")
        assert count == 0
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_prune_expired_chunks_mixed_all_categories(tmp_path: Path) -> None:
    """prune_expired_chunks only removes expired chunks; future and null survive.

    Inserts one expired chunk, one future chunk, and one null-expiry chunk.
    Only the expired doc_id is returned; count_chunks reports 2 remaining.
    """
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        past_iso = normalize_iso_utc(now - timedelta(seconds=10))
        future_iso = normalize_iso_utc(now + timedelta(hours=1))

        expired_doc_id = "x" * 64
        future_doc_id = "y" * 64
        null_doc_id = "z" * 64

        db = store._require_connected()
        table = await db.open_table("col")

        await table.add([
            {
                "doc_id": expired_doc_id,
                "chunk_id": expired_doc_id + "-000000",
                "text": "expired chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/expired.txt",
                "indexed_at": now_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "cli",
                "updated_at": now_iso,
                "acl": None,
                "expires_at": past_iso,
                "scopes": None,
            },
            {
                "doc_id": future_doc_id,
                "chunk_id": future_doc_id + "-000000",
                "text": "future chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/future.txt",
                "indexed_at": now_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "cli",
                "updated_at": now_iso,
                "acl": None,
                "expires_at": future_iso,
                "scopes": None,
            },
            {
                "doc_id": null_doc_id,
                "chunk_id": null_doc_id + "-000000",
                "text": "permanent chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/permanent.txt",
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
            },
        ])

        returned_doc_ids = await store.prune_expired_chunks("col", "default")

        assert returned_doc_ids == [expired_doc_id]
        assert future_doc_id not in returned_doc_ids
        assert null_doc_id not in returned_doc_ids

        remaining = await store.count_chunks("col", "default")
        assert remaining == 2
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_prune_expired_chunks_returns_empty_on_pre_migration_table(tmp_path: Path) -> None:
    """prune_expired_chunks returns [] for pre-E2a collection (no expires_at column)."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        # DO NOT apply E2a migrations

        result = await store.prune_expired_chunks("col", "default")
        assert result == []
    finally:
        await store.disconnect()
