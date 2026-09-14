"""Integration tests for POST /collections/{name}/export and import endpoints (Tasks 4.2, 5.2)."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION
from archon_search.jobs.store import JobStore
from archon_search.paths import get_data_dir
from archon_search.server.app import create_app
from archon_search.server.routes_export import INVALID_ARCHIVE_DETAIL
from archon_search.types import JobStatus


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _make_mock_search_store(collection_meta: CollectionMeta | None = None) -> MagicMock:
    """Return a fully-mocked SearchStore that satisfies the lifespan startup."""
    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock.migrate_namespace = AsyncMock()
    mock.migrate_description_embedding = AsyncMock()
    mock.migrate_acl = AsyncMock()
    mock.migrate_centroid_sum = AsyncMock()
    mock.migrate_per_collection_model = AsyncMock()
    mock.get_all_collections_meta = AsyncMock(return_value=[])
    mock.get_collection_meta = AsyncMock(return_value=collection_meta)
    return mock


@pytest.fixture
def meta() -> CollectionMeta:
    return CollectionMeta(
        name="my-collection",
        namespace=DEFAULT_NAMESPACE,
        active_embedding_model="BAAI/bge-small-en-v1.5",
        description="Test collection",
    )


@pytest.fixture
def client(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
    meta: CollectionMeta,
) -> TestClient:
    """Client with a mocked SearchStore that reports 'my-collection' exists."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(meta)
    return TestClient(app, headers=auth_headers)


@pytest.fixture
def client_no_collection(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
) -> TestClient:
    """Client with a mocked SearchStore that reports no collection exists."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)
    return TestClient(app, headers=auth_headers)


# ---------------------------------------------------------------------------
# POST /collections/{name}/export
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_export_returns_202_and_job_id(client: TestClient) -> None:
    """Valid collection with valid output_path returns 202 with a job_id."""
    output_dir = str(get_data_dir() / "exports")
    response = client.post(
        "/collections/my-collection/export",
        json={"output_path": output_dir},
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.QUEUED.value


@pytest.mark.integration
def test_post_export_default_output_path(client: TestClient) -> None:
    """Empty output_path defaults to get_data_dir() / 'exports' and returns 202."""
    response = client.post(
        "/collections/my-collection/export",
        json={},
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.QUEUED.value


@pytest.mark.integration
def test_post_export_collection_not_found(client_no_collection: TestClient) -> None:
    """Unknown collection returns 404."""
    output_dir = str(get_data_dir() / "exports")
    response = client_no_collection.post(
        "/collections/unknown-collection/export",
        json={"output_path": output_dir},
    )
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "not_found"


@pytest.mark.integration
def test_post_export_path_outside_allowed(client: TestClient) -> None:
    """Path outside get_data_dir() returns 400 with error='path_unsafe'."""
    response = client.post(
        "/collections/my-collection/export",
        json={"output_path": "/tmp/evil-export-dir"},
    )
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "path_unsafe"


@pytest.mark.integration
def test_post_export_unauthenticated(tmp_path: Path, tmp_store: JobStore) -> None:
    """Request without Bearer token returns 401."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store()
    # No auth headers
    no_auth_client = TestClient(app)
    output_dir = str(get_data_dir() / "exports")
    response = no_auth_client.post(
        "/collections/my-collection/export",
        json={"output_path": output_dir},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /collections/{name}/import — helpers
# ---------------------------------------------------------------------------

_DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def _make_valid_archive(
    dest_dir: Path,
    *,
    schema_version: int = EXPORT_SCHEMA_VERSION,
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    collection: str = "my-collection",
    doc_count: int = 0,
) -> Path:
    """Create a minimal valid .tar.gz export archive inside *dest_dir* and return its path.

    *dest_dir* must be within ``get_data_dir()`` so that ``validate_export_path`` accepts
    the resulting path.
    """
    manifest = {
        "schema_version": schema_version,
        "collection": collection,
        "exported_at": "2024-01-01T00:00:00+00:00",
        "doc_count": doc_count,
        "active_embedding_model": embedding_model,
        "description": "",
        "archon_search_version": "dev",
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode()
    docs_bytes = b""  # empty documents.jsonl

    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_path = dest_dir / f"{collection}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        # Add manifest.json
        m_info = tarfile.TarInfo(name="manifest.json")
        m_info.size = len(manifest_bytes)
        tf.addfile(m_info, io.BytesIO(manifest_bytes))
        # Add documents.jsonl
        d_info = tarfile.TarInfo(name="documents.jsonl")
        d_info.size = len(docs_bytes)
        tf.addfile(d_info, io.BytesIO(docs_bytes))

    return archive_path


def _make_unsafe_archive(tmp_path: Path) -> Path:
    """Create a tar.gz with a traversal member (zip-slip)."""
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        evil_info = tarfile.TarInfo(name="../../etc/passwd")
        evil_info.size = 0
        tf.addfile(evil_info, io.BytesIO(b""))
    return archive_path


# ---------------------------------------------------------------------------
# POST /collections/{name}/import — tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_import_returns_202(
    tmp_store: JobStore,
    auth_headers: dict[str, str],
    meta: CollectionMeta,
    tmp_path: Path,
) -> None:
    """Valid archive against a non-existent collection returns 202 with QUEUED job."""
    # Archive must live inside get_data_dir() to pass validate_export_path
    archive = _make_valid_archive(get_data_dir() / "exports" / "test_import_202")

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = _DEFAULT_EMBEDDING_MODEL
    app = create_app(config, tmp_store)
    # Collection does NOT exist yet — get_collection_meta returns None
    app.state.search_store = _make_mock_search_store(collection_meta=None)

    c = TestClient(app, headers=auth_headers)
    response = c.post(
        "/collections/my-collection/import",
        json={"path": str(archive)},
    )
    assert response.status_code == 202, response.text
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.QUEUED.value


@pytest.mark.integration
def test_post_import_path_outside_allowed(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
) -> None:
    """Path outside get_data_dir() returns 400 with error='path_unsafe'."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)

    c = TestClient(app, headers=auth_headers)
    response = c.post(
        "/collections/my-collection/import",
        json={"path": "/tmp/evil.tar.gz"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "path_unsafe"


@pytest.mark.integration
def test_post_import_path_outside_configured_backup_output_dir(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
) -> None:
    """S291: widening the allow-list to [backup].output_dir must not allow anything else.

    With an off-tree ``[backup].output_dir`` configured, a path that is inside neither
    ``get_data_dir()`` nor that backup root is still rejected with 400 ``path_unsafe``.
    """
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.backup.output_dir = str(tmp_path / "offsite" / "backups")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)

    c = TestClient(app, headers=auth_headers)
    response = c.post(
        "/collections/my-collection/import",
        json={"path": str(tmp_path / "elsewhere" / "evil.tar.gz")},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "path_unsafe"
    assert response.json()["reason"] == "outside_allowed_dirs"


@pytest.mark.integration
def test_post_import_archive_not_found(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
) -> None:
    """Non-existent archive path returns 422."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)

    missing = get_data_dir() / "exports" / "does-not-exist.tar.gz"
    c = TestClient(app, headers=auth_headers)
    response = c.post(
        "/collections/my-collection/import",
        json={"path": str(missing)},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "archive_not_found"


@pytest.mark.integration
def test_post_import_corrupt_archive_sanitizes_wire_detail(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt archive returns a sanitized 422 detail; the raw exception is logged, not echoed.

    Regression for the CLAUDE.md invariant: never put str(exc) in a wire-facing detail
    for a caught exception whose message can embed internals (here: tarfile/gzip
    internals derived from the archive's own bytes).
    """
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)

    corrupt = get_data_dir() / "exports" / "corrupt.tar.gz"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not a real gzip archive")

    c = TestClient(app, headers=auth_headers)
    with caplog.at_level("WARNING", logger="archon_search.server.routes_export"):
        response = c.post(
            "/collections/my-collection/import",
            json={"path": str(corrupt)},
        )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "invalid_archive"
    # The wire detail must be a fixed, sanitized message — never the raw exception text.
    assert body["detail"] == INVALID_ARCHIVE_DETAIL
    assert "gzip" not in body["detail"].lower()
    # The real exception is still logged server-side for diagnosis.
    assert any("corrupt" in record.getMessage().lower() for record in caplog.records) or any(
        record.exc_info for record in caplog.records
    )


@pytest.mark.integration
def test_post_import_uses_validated_resolved_path_not_raw_body_path(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route must operate on the path returned by validate_export_path, not on raw body.path.

    Regression: previously the handler re-derived ``Path(body.path)`` after validation
    instead of reusing the already-resolved, already-checked Path — a TOCTOU-shaped
    inconsistency between the path that was validated and the path that was opened.
    """
    real_archive = _make_valid_archive(tmp_path / "real-exports" / "test_import_resolved")
    # A path that does NOT exist on disk — if the handler used this raw value instead of
    # the validated return value, the request would fail with 422 archive_not_found.
    nonexistent_raw_path = tmp_path / "does-not-exist-raw" / "phantom.tar.gz"

    def _fake_validate_export_path(raw: str, allowed_base_dirs: list[Path]) -> Path:
        assert raw == str(nonexistent_raw_path)
        return real_archive

    monkeypatch.setattr(
        "archon_search.server.routes_export.validate_export_path",
        _fake_validate_export_path,
    )

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = _DEFAULT_EMBEDDING_MODEL
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)

    c = TestClient(app, headers=auth_headers)
    response = c.post(
        "/collections/my-collection/import",
        json={"path": str(nonexistent_raw_path)},
    )
    assert response.status_code == 202, response.text


@pytest.mark.integration
def test_post_import_collection_exists_no_force(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
    meta: CollectionMeta,
) -> None:
    """Importing into an existing collection without force_overwrite returns 409."""
    archive = _make_valid_archive(get_data_dir() / "exports" / "test_import_exists_no_force")

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = _DEFAULT_EMBEDDING_MODEL
    app = create_app(config, tmp_store)
    # Collection EXISTS
    app.state.search_store = _make_mock_search_store(collection_meta=meta)

    c = TestClient(app, headers=auth_headers)
    response = c.post(
        "/collections/my-collection/import",
        json={"path": str(archive), "force_overwrite": False},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "collection_exists"


@pytest.mark.integration
def test_post_import_schema_version_mismatch_no_flag(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
) -> None:
    """Archive with wrong schema_version returns 422 without ignore_schema_version flag."""
    archive = _make_valid_archive(
        get_data_dir() / "exports" / "test_import_schema_mismatch",
        schema_version=99,
    )

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = _DEFAULT_EMBEDDING_MODEL
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)

    c = TestClient(app, headers=auth_headers)
    response = c.post(
        "/collections/my-collection/import",
        json={"path": str(archive)},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "schema_version_mismatch"


@pytest.mark.integration
def test_post_import_invalid_on_error(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
) -> None:
    """on_error value other than 'fail' or 'skip' returns 422 (Pydantic validation)."""
    # The Pydantic validator fires before path/archive validation, so the archive
    # path can be any string (validation fails first).
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)

    c = TestClient(app, headers=auth_headers)
    response = c.post(
        "/collections/my-collection/import",
        json={"path": str(get_data_dir() / "exports" / "any.tar.gz"), "on_error": "invalid"},
    )
    assert response.status_code == 422
