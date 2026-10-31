"""T-6 (collection-descriptions plan, S42, S73) — a store written before this feature opens at startup.

The metadata table is built with the OLD schema (an Arrow table without the new columns) and written to LanceDB before
the app starts; ``make_real_app`` boots the real lifespan on it with the legacy collections configured, so the startup
sync runs over them. What a consumer sees is asserted through REST: a described legacy collection reads source
``generated`` / status ``ok``, an empty one reads null, a metadata write (``PATCH``) succeeds and lands, and a later
boot on the migrated store changes nothing. ``test_old_store_gets_columns_and_default_ttl_at_startup`` in
``test_pre_d3_startup_migration_e2e.py`` (BE-18) already covers a ``POST`` write on a store seeded the same way; this
file adds the REST reads (detail and list), the empty row, ``PATCH``, the configured-collections boot and the restarts.

Run with:
    uv run pytest tests/integration/test_lcd_t6_old_store_e2e.py --no-cov -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_GENERATED,
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_STATUS_OK,
)
from archon_search.server.schemas import StartupSyncResult, WarmupResult
from archon_search.store import SearchStore
from archon_search.sync import path_to_collection_name
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_META_TABLE = "_archon_collection_meta"
_DESCRIPTION_COLUMNS = ("description_source", "description_status")
_TTL_COLUMN = "default_ttl_seconds"
_LEGACY_DESCRIPTION = "notes about cats"
_WRITTEN_TTL_SECONDS = 3600
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01


def _old_row(name: str, description: str) -> dict[str, Any]:
    """A metadata row exactly as the pre-feature store wrote it."""
    return {
        "name": name,
        "description": description,
        "centroid_json": "",
        "description_embedding_json": "",
        "doc_count": 0,
        "chunk_count": 0,
        "active_embedding_model": "",
        "pending_embedding_model": None,
        "needs_reindex": False,
        "reindex_job_id": None,
        "community_rebuild_job_id": None,
        "metadata_reindex_job_id": None,
        "last_indexed": "",
        "last_described": "",
        "described_at_doc_count": 0,
        "namespace": "default",
        "centroid_sum_json": None,
        "mutations_since_recompute": 0,
        "needs_recompute": False,
        "schema_version": 0,
    }


def _seed_old_store(db_path: str, missing: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    """Write the metadata table with the current schema minus ``missing`` columns, before any app starts."""
    old_schema = pa.schema([f for f in SearchStore._meta_schema() if f.name not in missing])

    async def _seed() -> None:
        db = await lancedb.connect_async(db_path)
        await db.create_table(_META_TABLE, data=rows, schema=old_schema)

    asyncio.run(_seed())


def _read_table(db_path: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Column names and rows by name, read straight from LanceDB."""

    async def _read() -> tuple[list[str], dict[str, dict[str, Any]]]:
        db = await lancedb.connect_async(db_path)
        table = await db.open_table(_META_TABLE)
        return (await table.schema()).names, {r["name"]: r for r in await table.query().to_list()}

    return asyncio.run(_read())


def _await_warmup_done(client: TestClient) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while getattr(client.app.state, "warmup_result", None) in (None, WarmupResult.PENDING):
        assert time.monotonic() < deadline, f"warm-up did not finish within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)
    assert client.app.state.warmup_result == WarmupResult.DONE


def _make_directories(tmp_path: Path) -> tuple[Path, str, Path, str]:
    described = tmp_path / "described_docs"
    empty = tmp_path / "empty_docs"
    described.mkdir()
    empty.mkdir()
    return described, path_to_collection_name(str(described)), empty, path_to_collection_name(str(empty))


def _toml_with_collections(*directories: Path) -> str:
    return f"[collections]\ncollections = {json.dumps([str(d) for d in directories])}\n"


@contextlib.contextmanager
def _boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *directories: Path
) -> Iterator[tuple[TestClient, dict[str, str]]]:
    """The real app on the seeded store, ready, with ``directories`` configured as collections."""
    with make_real_app(tmp_path, monkeypatch, toml_content=_toml_with_collections(*directories)) as (
        client,
        _cfg,
        api_key,
    ):
        _await_warmup_done(client)
        assert client.app.state.sync_result == StartupSyncResult.DONE, "the startup sync ran over the legacy rows"
        yield client, {"Authorization": f"Bearer {api_key}"}


def _get(client: TestClient, headers: dict[str, str], name: str) -> dict[str, Any]:
    resp = client.get(f"/collections/{name}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _state(detail: dict[str, Any]) -> tuple[str | None, str | None]:
    return detail["description_source"], detail["description_status"]


def test_e2e_old_store_migrates_columns_and_default_ttl_at_startup(tmp_path, monkeypatch) -> None:
    """S42 + S73: both description columns and ``default_ttl_seconds`` appear; reads, a write and restarts all work."""
    db_path = str(tmp_path / "db")
    described_dir, described, empty_dir, empty = _make_directories(tmp_path)
    _seed_old_store(
        db_path,
        (_TTL_COLUMN, *_DESCRIPTION_COLUMNS),
        [_old_row(described, _LEGACY_DESCRIPTION), _old_row(empty, "")],
    )

    # Boot 1, read only: the migrated columns are still NULL on the legacy rows and the reader gives the defaults.
    with _boot(tmp_path, monkeypatch, described_dir, empty_dir) as (client, headers):
        legacy = _get(client, headers, described)
        assert legacy["description"] == _LEGACY_DESCRIPTION
        assert _state(legacy) == (DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        assert legacy["default_ttl_seconds"] is None

        blank = _get(client, headers, empty)
        assert blank["description"] == ""
        assert _state(blank) == (None, None)

        listed = {c["name"]: c for c in client.get("/collections/", headers=headers).json()}
        assert _state(listed[described]) == (DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        assert _state(listed[empty]) == (None, None)

    names, rows_read_only = _read_table(db_path)
    assert rows_read_only[described]["description_source"] is None, "a read-only boot must not rewrite legacy rows"
    for column in (_TTL_COLUMN, *_DESCRIPTION_COLUMNS):
        assert column in names, f"{column} must have been added at startup"

    # Boot 2: the metadata write succeeds and lands.
    with _boot(tmp_path, monkeypatch, described_dir, empty_dir) as (client, headers):
        written = client.patch(
            f"/collections/{described}", json={"default_ttl_seconds": _WRITTEN_TTL_SECONDS}, headers=headers
        )
        assert written.status_code == 200, written.text
        assert written.json()["default_ttl_seconds"] == _WRITTEN_TTL_SECONDS
        assert written.json()["description"] == _LEGACY_DESCRIPTION

    names_written, rows_written = _read_table(db_path)
    assert rows_written[described]["default_ttl_seconds"] == _WRITTEN_TTL_SECONDS, "the write must have landed"
    assert rows_written[described]["description"] == _LEGACY_DESCRIPTION
    assert (rows_written[described]["description_source"], rows_written[described]["description_status"]) == (
        DESCRIPTION_SOURCE_GENERATED,
        DESCRIPTION_STATUS_OK,
    ), "the write stores the legacy defaults the reader showed"

    # Boot 3 on the migrated store: nothing is added again and no row changes.
    with _boot(tmp_path, monkeypatch, described_dir, empty_dir) as (client, headers):
        again = _get(client, headers, described)
        assert _state(again) == (DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        assert again["default_ttl_seconds"] == _WRITTEN_TTL_SECONDS
        assert _state(_get(client, headers, empty)) == (None, None)

    names_after, rows_after = _read_table(db_path)
    assert names_after == names_written
    assert rows_after == rows_written


def test_e2e_old_store_lacking_only_default_ttl_keeps_description_state(tmp_path, monkeypatch) -> None:
    """S73: a table that has the description columns but no ``default_ttl_seconds`` gains it and keeps its values."""
    db_path = str(tmp_path / "db")
    described_dir, described, _empty_dir, _empty = _make_directories(tmp_path)
    row = _old_row(described, _LEGACY_DESCRIPTION) | {
        "description_source": DESCRIPTION_SOURCE_MANUAL,
        "description_status": DESCRIPTION_STATUS_OK,
    }
    _seed_old_store(db_path, (_TTL_COLUMN,), [row])

    with _boot(tmp_path, monkeypatch, described_dir) as (client, headers):
        detail = _get(client, headers, described)
        assert _state(detail) == (DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK)
        assert detail["default_ttl_seconds"] is None

        written = client.patch(
            f"/collections/{described}", json={"default_ttl_seconds": _WRITTEN_TTL_SECONDS}, headers=headers
        )
        assert written.status_code == 200, written.text

    names, rows = _read_table(db_path)
    assert _TTL_COLUMN in names
    assert rows[described]["default_ttl_seconds"] == _WRITTEN_TTL_SECONDS
    assert (rows[described]["description_source"], rows[described]["description_status"]) == (
        DESCRIPTION_SOURCE_MANUAL,
        DESCRIPTION_STATUS_OK,
    )
