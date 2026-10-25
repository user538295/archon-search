"""Archives made before the ownership change carry no ``namespace`` in their manifest (SecReview #4 follow-up).

A pre-upgrade backup in ``<backup dir>/<ns>/`` stays restorable by ``<ns>`` (disaster recovery); a pre-upgrade flat
``<data>/exports/*.tar.gz`` is importable only by the default namespace; an archive outside the caller's own
directories is still refused with the same 400, and an archive whose manifest names another namespace still 422s.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.export_archive import (
    ARCHIVE_NAMESPACE_MISMATCH_DETAIL,
    EXPORT_SCHEMA_VERSION,
    ArchiveRejectedError,
    exports_dir,
)
from archon_search.jobs.store import JobStore
from archon_search.paths import get_data_dir
from archon_search.server.app import create_app
from archon_search.server.routes_export import _validate_manifest
from archon_search.types import ImportJob
from tests._fastmcp_stub import stub_mcp_tool

_MODEL = "BAAI/bge-small-en-v1.5"
_DEFAULT_KEY = "0" * 64
_TEAM = "team-a"
_TEAM_KEY = "a" * 64
_OTHER = "team-b"
_ACCEPTED = 202
_PATH_UNSAFE = 400
_MISMATCH = 422


def _legacy_manifest(**extra: object) -> dict:
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "collection": "my-collection",
        "exported_at": "2024-01-01T00:00:00+00:00",
        "doc_count": 0,
        "active_embedding_model": _MODEL,
        **extra,
    }


def _archive(directory: Path, manifest: dict, name: str = "legacy.tar.gz") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with tarfile.open(path, "w:gz") as tf:
        for member, payload in (("manifest.json", json.dumps(manifest).encode()), ("documents.jsonl", b"")):
            info = tarfile.TarInfo(name=member)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return path


def _client(tmp_path: Path, *, backup_dir: Path | None = None) -> tuple[TestClient, JobStore]:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = _MODEL
    config.namespaces = {_TEAM_KEY: _TEAM}
    if backup_dir is not None:
        config.backup.output_dir = str(backup_dir)
    store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, store)
    search_store = MagicMock()
    search_store.get_collection_meta = AsyncMock(return_value=None)
    app.state.search_store = search_store
    return TestClient(app), store


def _import(client: TestClient, archive: Path, key: str = _DEFAULT_KEY):
    return client.post(
        "/collections/my-collection/import",
        json={"path": str(archive)},
        headers={"Authorization": f"Bearer {key}"},
    )


def _flat_exports() -> Path:
    return get_data_dir() / "exports"


# --- backups made before the upgrade ----------------------------------------------------------------------


def test_pre_upgrade_backup_in_the_callers_backup_directory_is_restorable(tmp_path: Path) -> None:
    backups = tmp_path / "offsite"
    archive = _archive(backups / _TEAM, _legacy_manifest())
    client, store = _client(tmp_path, backup_dir=backups)

    response = _import(client, archive, _TEAM_KEY)

    assert response.status_code == _ACCEPTED, response.text
    assert [job.namespace for job in store.list()] == [_TEAM]


def test_pre_upgrade_default_namespace_backup_is_restorable(tmp_path: Path) -> None:
    backups = tmp_path / "offsite"
    archive = _archive(backups / DEFAULT_NAMESPACE, _legacy_manifest())
    client, _ = _client(tmp_path, backup_dir=backups)

    assert _import(client, archive).status_code == _ACCEPTED


def test_a_manifestless_archive_in_another_namespaces_backup_directory_stays_refused(tmp_path: Path) -> None:
    backups = tmp_path / "offsite"
    archive = _archive(backups / _OTHER, _legacy_manifest())
    client, store = _client(tmp_path, backup_dir=backups)

    response = _import(client, archive, _TEAM_KEY)

    assert response.status_code == _PATH_UNSAFE
    assert response.json() == {"error": "path_unsafe", "reason": "outside_allowed_dirs"}
    assert store.list() == []


def test_a_manifest_naming_another_namespace_is_refused_even_in_the_callers_directory(tmp_path: Path) -> None:
    backups = tmp_path / "offsite"
    archive = _archive(backups / _TEAM, _legacy_manifest(namespace=_OTHER))
    client, store = _client(tmp_path, backup_dir=backups)

    response = _import(client, archive, _TEAM_KEY)

    assert response.status_code == _MISMATCH
    assert response.json()["error"] == "archive_namespace_mismatch"
    assert ARCHIVE_NAMESPACE_MISMATCH_DETAIL in response.text
    assert _OTHER not in response.text
    assert store.list() == []


# --- flat <data>/exports/*.tar.gz -------------------------------------------------------------------------


def test_pre_upgrade_flat_export_is_importable_by_the_default_namespace(tmp_path: Path) -> None:
    archive = _archive(_flat_exports(), _legacy_manifest(), "flat-default.tar.gz")
    client, _ = _client(tmp_path)

    assert _import(client, archive).status_code == _ACCEPTED


def test_pre_upgrade_flat_export_is_not_importable_by_another_namespace(tmp_path: Path) -> None:
    archive = _archive(_flat_exports(), _legacy_manifest(), "flat-other.tar.gz")
    client, store = _client(tmp_path)

    response = _import(client, archive, _TEAM_KEY)

    assert response.status_code == _PATH_UNSAFE
    assert response.json() == {"error": "path_unsafe", "reason": "outside_allowed_dirs"}
    assert store.list() == []


def test_the_flat_exception_does_not_reach_into_another_namespaces_export_directory(tmp_path: Path) -> None:
    foreign = _archive(exports_dir(get_data_dir(), _OTHER), _legacy_manifest(), "foreign.tar.gz")
    client, store = _client(tmp_path)

    response = _import(client, foreign)

    assert response.status_code == _PATH_UNSAFE
    assert store.list() == []


def test_a_flat_archive_naming_another_namespace_is_refused_for_the_default_namespace(tmp_path: Path) -> None:
    archive = _archive(_flat_exports(), _legacy_manifest(namespace=_OTHER), "flat-named.tar.gz")
    client, _ = _client(tmp_path)

    assert _import(client, archive).status_code == _MISMATCH


# --- the job re-checks at run time ------------------------------------------------------------------------


def _job(namespace: str, archive: Path) -> ImportJob:
    store = JobStore(path=archive.parent / "jobs.json")
    return store.create_import(
        collection="my-collection",
        archive_path=str(archive),
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
        namespace=namespace,
    )


def test_the_import_job_accepts_a_manifestless_archive_in_its_namespaces_directory(tmp_path: Path) -> None:
    backups = tmp_path / "offsite"
    archive = _archive(backups / _TEAM, _legacy_manifest())
    config = SearchConfig()
    config.embedding_model = _MODEL
    config.backup.output_dir = str(backups)

    _validate_manifest(_legacy_manifest(), _job(_TEAM, archive), config)


def test_the_import_job_refuses_a_manifestless_archive_outside_its_namespaces_directories(tmp_path: Path) -> None:
    backups = tmp_path / "offsite"
    archive = _archive(backups / _OTHER, _legacy_manifest())
    config = SearchConfig()
    config.embedding_model = _MODEL
    config.backup.output_dir = str(backups)

    with pytest.raises(ArchiveRejectedError, match=ARCHIVE_NAMESPACE_MISMATCH_DETAIL):
        _validate_manifest(_legacy_manifest(), _job(_TEAM, archive), config)


# --- MCP --------------------------------------------------------------------------------------------------


def _mcp_import_tool(backups: Path, store: JobStore):
    config = SearchConfig()
    config.embedding_model = _MODEL
    config.backup.output_dir = str(backups)
    pipeline = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)
    return stub_mcp_tool("import_collection", pipeline, "col", config=config, job_store=store)


@pytest.mark.asyncio
async def test_mcp_import_accepts_a_pre_upgrade_backup_of_the_default_namespace(tmp_path: Path) -> None:
    backups = tmp_path / "offsite"
    archive = _archive(backups / DEFAULT_NAMESPACE, _legacy_manifest())
    store = JobStore(path=tmp_path / "jobs.json")

    result = await _mcp_import_tool(backups, store)(collection="my-collection", path=str(archive))

    assert result.get("status") == "QUEUED", result


@pytest.mark.asyncio
async def test_mcp_import_accepts_a_pre_upgrade_flat_export_for_the_default_namespace(tmp_path: Path) -> None:
    archive = _archive(_flat_exports(), _legacy_manifest(), "flat-mcp.tar.gz")
    store = JobStore(path=tmp_path / "jobs.json")

    result = await _mcp_import_tool(tmp_path / "offsite", store)(collection="my-collection", path=str(archive))

    assert result.get("status") == "QUEUED", result
