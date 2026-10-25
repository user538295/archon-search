"""H4: a non-admin key may ingest a server path only when the operator set ``[ingest].allowed_roots``.

With no roots configured the whole filesystem is in scope, so only an admin key (the operator's) may name a path;
a namespace token or a non-admin managed key sends its content inline. The server says so once at startup when
that situation exists. Real files in tmp dirs, real keys, both caller types, REST and MCP.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search._path_safety import ALLOWED_ROOTS_REQUIRED_DETAIL
from archon_search.config import SearchConfig
from archon_search.ingest_path_policy import log_if_non_admin_path_ingest_is_refused
from archon_search.jobs.store import JobStore
from archon_search.key_manager import KeyStore
from archon_search.server.app import create_app
from archon_search.server.authz import AuthIdentity
from archon_search.store import SearchStore
from tests._collection_access_stub import allow_collection_access_everywhere
from tests._fastmcp_stub import StubFastMCP, stubbed_fastmcp
from tests.test_app import wait_for_models_check_to_settle, wait_for_warmup

_TENANT_KEY = "7" * 64
_WARNING_MARKER = "[ingest].allowed_roots"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_collection_access_everywhere(monkeypatch)
    monkeypatch.setattr(SearchStore, "connect", AsyncMock())
    monkeypatch.setattr(SearchStore, "_run_startup_migrations", AsyncMock())
    monkeypatch.setattr(SearchStore, "disconnect", AsyncMock())
    monkeypatch.setattr(SearchStore, "ping", AsyncMock(return_value=True))


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "corpus" / "note.md"
    path.parent.mkdir()
    path.write_text("hello", encoding="utf-8")
    return path


def _config(tmp_path: Path, *, namespaces: bool = True, allowed_roots: list[str] | None = None) -> SearchConfig:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    if namespaces:
        config.namespaces = {_TENANT_KEY: "tenantA"}
    if allowed_roots is not None:
        config.ingest.allowed_roots = allowed_roots
    return config


def _app(tmp_path: Path, config: SearchConfig):
    return create_app(config, JobStore(path=tmp_path / "jobs.json"))


# ---------------------------------------------------------------------------
# REST POST /ingest
# ---------------------------------------------------------------------------


def test_a_namespace_token_cannot_ingest_a_path_without_allowed_roots(tmp_path: Path, document: Path) -> None:
    client = TestClient(_app(tmp_path, _config(tmp_path)), headers={"Authorization": f"Bearer {_TENANT_KEY}"})

    response = client.post("/ingest", json={"collection": "docs", "path": str(document)})

    assert response.status_code == 400
    assert response.json()["detail"] == ALLOWED_ROOTS_REQUIRED_DETAIL
    assert _WARNING_MARKER in response.json()["detail"]
    assert str(document) not in response.text


def test_a_namespace_token_cannot_ingest_etc_passwd_either_way(tmp_path: Path) -> None:
    """``/etc/passwd`` is denied for every caller; the non-admin refusal never reveals which rule fired."""
    client = TestClient(_app(tmp_path, _config(tmp_path)), headers={"Authorization": f"Bearer {_TENANT_KEY}"})

    response = client.post("/ingest", json={"collection": "docs", "path": "/etc/passwd"})

    assert response.status_code == 400


def test_a_namespace_token_can_still_ingest_inline_documents(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path, _config(tmp_path)), headers={"Authorization": f"Bearer {_TENANT_KEY}"})

    response = client.post(
        "/ingest",
        json={"collection": "docs", "documents": [{"source_path": "/virtual/a.md", "text": "inline text"}]},
    )

    assert response.status_code == 202


def test_a_namespace_token_can_ingest_a_path_inside_allowed_roots(tmp_path: Path, document: Path) -> None:
    config = _config(tmp_path, allowed_roots=[str(document.parent)])
    client = TestClient(_app(tmp_path, config), headers={"Authorization": f"Bearer {_TENANT_KEY}"})

    response = client.post("/ingest", json={"collection": "docs", "path": str(document)})

    assert response.status_code == 202


def test_the_admin_key_can_ingest_a_path_without_allowed_roots(
    tmp_path: Path, document: Path, auth_headers: dict[str, str]
) -> None:
    client = TestClient(_app(tmp_path, _config(tmp_path)), headers=auth_headers)

    response = client.post("/ingest", json={"collection": "docs", "path": str(document)})

    assert response.status_code == 202


def test_a_single_key_deployment_ingests_paths_as_before(
    tmp_path: Path, document: Path, auth_headers: dict[str, str]
) -> None:
    client = TestClient(_app(tmp_path, _config(tmp_path, namespaces=False)), headers=auth_headers)

    response = client.post("/ingest", json={"collection": "docs", "path": str(document)})

    assert response.status_code == 202


# ---------------------------------------------------------------------------
# MCP ingest_file / ingest_directory
# ---------------------------------------------------------------------------


def _mcp_app(allowed_roots: tuple[Path, ...] = ()) -> StubFastMCP:
    from archon_search._path_safety import IngestPathPolicy

    with stubbed_fastmcp():
        from archon_search.server import mcp as mcp_module

        return mcp_module.create_app(
            MagicMock(), "default", writer=None, ingest_policy=IngestPathPolicy(allowed_roots=allowed_roots)
        )  # type: ignore[call-arg]


def _as(namespace: str, *, admin: bool):
    return patch(
        "archon_search.server.mcp._get_request_identity",
        return_value=AuthIdentity(namespace=namespace, admin=admin),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["ingest_file", "ingest_directory"])
async def test_a_non_admin_mcp_caller_is_refused_a_path_without_allowed_roots(tool: str, document: Path) -> None:
    app = _mcp_app()
    target = document if tool == "ingest_file" else document.parent

    with _as("tenantA", admin=False), patch("archon_search.server.mcp._get_request_namespace", return_value="tenantA"):
        result = await app.tools[tool](path=str(target), collection=None)

    assert result["code"] == "path_unsafe"
    assert _WARNING_MARKER in result["error"]
    assert str(document) not in result["error"]


@pytest.mark.asyncio
async def test_a_non_admin_mcp_caller_may_ingest_a_path_inside_allowed_roots(document: Path) -> None:
    app = _mcp_app(allowed_roots=(document.parent,))

    with _as("tenantA", admin=False), patch("archon_search.server.mcp._get_request_namespace", return_value="tenantA"):
        result = await app.tools["ingest_file"](path=str(document), collection=None)

    assert result.get("code") != "path_unsafe"


@pytest.mark.asyncio
async def test_an_admin_mcp_caller_may_ingest_a_path_without_allowed_roots(document: Path) -> None:
    app = _mcp_app()

    with _as("default", admin=True):
        result = await app.tools["ingest_file"](path=str(document), collection=None)

    assert result.get("code") != "path_unsafe"


# ---------------------------------------------------------------------------
# The startup WARNING
# ---------------------------------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and _WARNING_MARKER in r.getMessage()]


@pytest.mark.asyncio
async def test_the_warning_is_logged_once_for_toml_namespace_tokens(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = _config(tmp_path)

    with caplog.at_level(logging.WARNING):
        await log_if_non_admin_path_ingest_is_refused(config, KeyStore(tmp_path / "keys.json"))

    assert len(_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_the_warning_is_logged_for_a_non_admin_managed_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = KeyStore(tmp_path / "keys.json")
    await store.create("tenantB", "ci", None)

    with caplog.at_level(logging.WARNING):
        await log_if_non_admin_path_ingest_is_refused(_config(tmp_path, namespaces=False), store)

    assert len(_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_no_warning_when_only_admin_keys_exist(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = KeyStore(tmp_path / "keys.json")
    await store.create("default", "ops", None, admin=True)

    with caplog.at_level(logging.WARNING):
        await log_if_non_admin_path_ingest_is_refused(_config(tmp_path, namespaces=False), store)

    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_no_warning_when_allowed_roots_is_set(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = _config(tmp_path, allowed_roots=[str(tmp_path)])

    with caplog.at_level(logging.WARNING):
        await log_if_non_admin_path_ingest_is_refused(config, KeyStore(tmp_path / "keys.json"))

    assert _warnings(caplog) == []


def test_the_server_logs_the_warning_at_startup(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = _app(tmp_path, _config(tmp_path))

    with caplog.at_level(logging.WARNING), TestClient(app):
        wait_for_warmup(app)
        wait_for_models_check_to_settle(app)

    assert len(_warnings(caplog)) == 1


def test_the_server_logs_no_warning_for_a_single_key_deployment(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = _app(tmp_path, _config(tmp_path, namespaces=False))

    with caplog.at_level(logging.WARNING), TestClient(app):
        wait_for_warmup(app)
        wait_for_models_check_to_settle(app)

    assert _warnings(caplog) == []


def test_rest_ingest_validates_the_path_off_the_event_loop(tmp_path: Path, document: Path) -> None:
    """Resolving the path and probing it hits the filesystem: it must not run on the event loop thread."""
    import asyncio

    from archon_search.server import routes_jobs

    real = routes_jobs.validate_ingest_source
    on_loop: list[bool] = []

    def recording(*args: object, **kwargs: object):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    client = TestClient(
        _app(tmp_path, _config(tmp_path)), headers={"Authorization": f"Bearer {'0' * 64}"}
    )
    with patch.object(routes_jobs, "validate_ingest_source", recording):
        client.post("/ingest", json={"collection": "docs", "path": str(document)})

    assert on_loop == [False]
