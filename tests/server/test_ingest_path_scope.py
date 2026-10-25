"""A client-supplied ingest path cannot reach secrets, device files or anything outside ``allowed_roots`` (SecReview #3).

Drives the real ASGI app (REST) and the real MCP tool functions with real files in tmp dirs; before the fix any key
could ingest ``<data dir>/.search.env`` and read it back through search.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from tests._collection_access_stub import allow_collection_access
from tests._fastmcp_stub import stub_mcp_tool

_DEFAULT_KEY = "0" * 64  # tests/conftest.py ARCHON_SEARCH_API_KEY
_AUTH = {"Authorization": f"Bearer {_DEFAULT_KEY}"}
_ACCEPTED = 202
_BAD_REQUEST = 400
_DENIED_DETAIL = "path is unsafe: denied_location"
_OUTSIDE_DETAIL = "path is unsafe: outside_allowed_roots"


def _write(path: Path, text: str = "content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(directory))
    return directory


def _client(tmp_path: Path, **ingest_overrides: object) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "data" / "search")
    for name, value in ingest_overrides.items():
        setattr(config.ingest, name, value)
    # The app runs without its lifespan, so its store is not connected; ownership is not what is under test here.
    return TestClient(allow_collection_access(create_app(config, JobStore(path=tmp_path / "data" / "jobs.json"))))


def _post_ingest(client: TestClient, path: Path):
    return client.post("/ingest", json={"collection": "docs", "path": str(path)}, headers=_AUTH)


# ---------------------------------------------------------------------------
# REST POST /ingest
# ---------------------------------------------------------------------------


def test_rest_ingest_of_the_default_key_file_is_refused(tmp_path: Path, data_dir: Path) -> None:
    secret = _write(data_dir / ".search.env", "ARCHON_SEARCH_API_KEY=" + "d" * 64)

    response = _post_ingest(_client(tmp_path), secret)

    assert response.status_code == _BAD_REQUEST
    assert response.json() == {"detail": _DENIED_DETAIL}


@pytest.mark.parametrize("name", [".secrets.env", "keys.json", "archon-search.toml"])
def test_rest_ingest_of_any_data_dir_file_is_refused(tmp_path: Path, data_dir: Path, name: str) -> None:
    response = _post_ingest(_client(tmp_path), _write(data_dir / name))

    assert response.status_code == _BAD_REQUEST
    assert response.json()["detail"] == _DENIED_DETAIL


def test_rest_ingest_of_the_data_dir_as_a_directory_is_refused(tmp_path: Path, data_dir: Path) -> None:
    response = _post_ingest(_client(tmp_path), data_dir)

    assert response.status_code == _BAD_REQUEST
    assert response.json()["detail"] == _DENIED_DETAIL


def test_rest_ingest_through_a_symlink_to_a_secret_is_refused(tmp_path: Path, data_dir: Path) -> None:
    secret = _write(data_dir / ".search.env", "ARCHON_SEARCH_API_KEY=" + "e" * 64)
    link = tmp_path / "docs" / "harmless.md"
    link.parent.mkdir()
    link.symlink_to(secret)

    response = _post_ingest(_client(tmp_path), link)

    assert response.status_code == _BAD_REQUEST
    assert response.json()["detail"] == _DENIED_DETAIL


@pytest.mark.parametrize("relative", [".ssh/id_ed25519", ".aws/credentials", "tls/server.pem", "tls/server.key", ".env", ".kube/config"])
def test_rest_ingest_of_credential_files_is_refused(tmp_path: Path, data_dir: Path, relative: str) -> None:
    # A ``*.key`` file is a credential when it is a PEM; the others are denied by name or directory alone.
    response = _post_ingest(_client(tmp_path), _write(tmp_path / "home" / relative, "-----BEGIN PRIVATE KEY-----\nMIIE\n"))

    assert response.status_code == _BAD_REQUEST
    assert response.json()["detail"] == _DENIED_DETAIL


def test_rest_ingest_of_a_device_file_is_refused(tmp_path: Path, data_dir: Path) -> None:
    response = _post_ingest(_client(tmp_path), Path("/dev/zero"))

    assert response.status_code == _BAD_REQUEST
    assert response.json()["detail"] == _DENIED_DETAIL


def test_rest_ingest_of_an_ordinary_document_is_still_accepted(tmp_path: Path, data_dir: Path) -> None:
    response = _post_ingest(_client(tmp_path), _write(tmp_path / "docs" / "notes.md"))

    assert response.status_code == _ACCEPTED


def test_rest_ingest_outside_allowed_roots_is_refused(tmp_path: Path, data_dir: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    client = _client(tmp_path, allowed_roots=[str(corpus)])

    outside = _post_ingest(client, _write(tmp_path / "elsewhere" / "a.md"))
    inside = _post_ingest(client, _write(corpus / "a.md"))

    assert outside.status_code == _BAD_REQUEST
    assert outside.json()["detail"] == _OUTSIDE_DETAIL
    assert inside.status_code == _ACCEPTED


def test_rest_ingest_symlink_out_of_an_allowed_root_is_refused(tmp_path: Path, data_dir: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "escape.md").symlink_to(_write(tmp_path / "elsewhere" / "private.md"))

    response = _post_ingest(_client(tmp_path, allowed_roots=[str(corpus)]), corpus / "escape.md")

    assert response.status_code == _BAD_REQUEST
    assert response.json()["detail"] == _OUTSIDE_DETAIL


def test_inline_document_labels_are_not_subject_to_the_read_policy(tmp_path: Path, data_dir: Path) -> None:
    """An inline document names no file on this host, so its ``source_path`` is only a structural label."""
    response = _client(tmp_path, allowed_roots=[str(tmp_path / "corpus")]).post(
        "/ingest",
        json={"collection": "docs", "documents": [{"text": "hello", "source_path": "/virtual/notes/.env"}]},
        headers=_AUTH,
    )

    assert response.status_code == _ACCEPTED


def test_add_collection_refuses_a_secret_path(tmp_path: Path, data_dir: Path) -> None:
    secret_dir = data_dir / "search"
    secret_dir.mkdir()

    response = _client(tmp_path).post("/collections/", json={"path": str(secret_dir), "name": "stolen"}, headers=_AUTH)

    assert response.status_code == _BAD_REQUEST
    assert response.json()["detail"] == _DENIED_DETAIL


# ---------------------------------------------------------------------------
# MCP ingest tools
# ---------------------------------------------------------------------------


def _mcp_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock()
    pipeline.ingest_directory = AsyncMock(return_value=[])
    pipeline.store.collection_writer = MagicMock()
    return pipeline


@pytest.mark.parametrize("tool_name", ["ingest_file", "ingest_directory"])
def test_mcp_ingest_tools_refuse_a_secret_path(tool_name: str, data_dir: Path) -> None:
    import asyncio

    secret = _write(data_dir / ".search.env", "ARCHON_SEARCH_API_KEY=" + "f" * 64)
    pipeline = _mcp_pipeline()
    tool = stub_mcp_tool(tool_name, pipeline, "col", config=SearchConfig())

    result = asyncio.run(tool(path=str(secret)))

    assert result["code"] == "path_unsafe"
    assert "never ingested" in result["error"]
    pipeline.ingest_file.assert_not_awaited()
    pipeline.ingest_directory.assert_not_awaited()


def test_mcp_ingest_file_refuses_a_path_outside_allowed_roots(tmp_path: Path, data_dir: Path) -> None:
    import asyncio

    config = SearchConfig()
    config.ingest.allowed_roots = [str(tmp_path / "corpus")]
    pipeline = _mcp_pipeline()
    tool = stub_mcp_tool("ingest_file", pipeline, "col", config=config)

    result = asyncio.run(tool(path=str(_write(tmp_path / "elsewhere" / "a.md"))))

    assert result["code"] == "path_unsafe"
    assert "allowed" in result["error"]


@pytest.mark.parametrize("pattern", ["/etc/*", "../*", "**/../../*", "a\x00b", "x" * 400])
def test_mcp_ingest_directory_refuses_an_unsafe_glob(pattern: str, tmp_path: Path, data_dir: Path) -> None:
    import asyncio

    docs = tmp_path / "docs"
    docs.mkdir()
    pipeline = _mcp_pipeline()
    tool = stub_mcp_tool("ingest_directory", pipeline, "col", config=SearchConfig())

    result = asyncio.run(tool(path=str(docs), glob_pattern=pattern))

    assert result["code"] == "path_unsafe"
    pipeline.ingest_directory.assert_not_awaited()
