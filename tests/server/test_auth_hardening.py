"""Auth hardening on the real ASGI app: non-ASCII bearer tokens, docs endpoints, ``/health`` (SecReview #6, #9d)."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from archon_search.config import ConfigError, SearchConfig, load_config
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app

_DEFAULT_KEY = "0" * 64  # tests/conftest.py ARCHON_SEARCH_API_KEY
_NON_ASCII_BEARER = b"Bearer \xe9"  # a latin-1 'é': Starlette decodes header bytes as latin-1
_NON_ASCII_UTF8_BEARER = "Bearer clé".encode()  # UTF-8 bytes arrive as two latin-1 characters
_UNAUTHORIZED = 401
_OK = 200
_NOT_FOUND = 404


def _app(tmp_path: Path, *, docs_enabled: bool = False, namespaces: dict[str, str] | None = None):
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.docs_enabled = docs_enabled
    if namespaces is not None:
        config.namespaces = namespaces
    return create_app(config, JobStore(path=tmp_path / "jobs.json"))


def _client(tmp_path: Path, **kwargs) -> TestClient:
    # raise_server_exceptions=True: a crash inside the app would surface as an exception, not a quiet 500.
    return TestClient(_app(tmp_path, **kwargs), raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# #6 — a non-ASCII bearer token is a 401, never a 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bearer", [_NON_ASCII_BEARER, _NON_ASCII_UTF8_BEARER], ids=["latin1", "utf8"])
@pytest.mark.parametrize("path", ["/status", "/search", "/keys"])
def test_non_ascii_bearer_token_is_401_without_a_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, path: str, bearer: bytes
) -> None:
    client = _client(tmp_path)

    with caplog.at_level(logging.DEBUG):
        response = client.request("GET", path, headers=[(b"authorization", bearer)])

    assert response.status_code == _UNAUTHORIZED
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert not [r for r in caplog.records if r.exc_info], "no exception may be logged for a bad token"


def test_non_ascii_bearer_token_is_401_against_toml_namespace_tokens(tmp_path: Path) -> None:
    """The TOML token comparison (a different code path from the default key) is non-ASCII safe too."""
    client = _client(tmp_path, namespaces={"t" * 40: "ns-a"})

    response = client.get("/keys", headers=[(b"authorization", _NON_ASCII_BEARER)])

    assert response.status_code == _UNAUTHORIZED
    assert client.get("/keys", headers={"Authorization": f"Bearer {'t' * 40}"}).status_code == _OK


def test_an_empty_bearer_token_never_authenticates_even_if_a_namespace_token_is_empty(tmp_path: Path) -> None:
    """Defence in depth behind the config check: a programmatically built config with an empty token."""
    client = _client(tmp_path, namespaces={"": "ns-a"})

    for header in ("Bearer ", "Bearer"):
        assert client.get("/keys", headers={"Authorization": header}).status_code == _UNAUTHORIZED


def test_non_ascii_bearer_token_is_401_on_the_mounted_mcp_endpoint(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from archon_search.server.mcp import create_mcp_http_app

    mcp_app = create_mcp_http_app(MagicMock(), "default", key_store=None)

    with TestClient(mcp_app, raise_server_exceptions=True) as client:
        response = client.post("/", json={}, headers=[(b"authorization", _NON_ASCII_BEARER)])

    assert response.status_code == _UNAUTHORIZED


def test_a_valid_ascii_token_still_authenticates_next_to_the_non_ascii_fix(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/keys", headers={"Authorization": f"Bearer {_DEFAULT_KEY}"}).status_code == _OK


# ---------------------------------------------------------------------------
# #9d — docs endpoints off by default, /openapi.json authenticated, /health minimal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/docs/oauth2-redirect"])
def test_interactive_docs_are_not_served_by_default(tmp_path: Path, path: str) -> None:
    client = _client(tmp_path)

    assert client.get(path).status_code == _UNAUTHORIZED
    assert client.get(path, headers={"Authorization": f"Bearer {_DEFAULT_KEY}"}).status_code == _NOT_FOUND


def test_openapi_json_requires_a_bearer_token_by_default(tmp_path: Path) -> None:
    client = _client(tmp_path)

    anonymous = client.get("/openapi.json")
    authenticated = client.get("/openapi.json", headers={"Authorization": f"Bearer {_DEFAULT_KEY}"})

    assert anonymous.status_code == _UNAUTHORIZED
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"
    assert authenticated.status_code == _OK
    assert "/search" in authenticated.json()["paths"]


def test_docs_pages_and_schema_are_public_when_the_operator_enables_them(tmp_path: Path) -> None:
    client = _client(tmp_path, docs_enabled=True)

    assert client.get("/docs").status_code == _OK
    assert client.get("/redoc").status_code == _OK
    assert client.get("/openapi.json").status_code == _OK
    # Enabling docs opens only those three paths.
    assert client.get("/status").status_code == _UNAUTHORIZED


def test_health_is_status_only(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/health")

    assert response.status_code == _OK
    assert response.json() == {"status": "running"}


def test_ready_probe_stays_unauthenticated_for_the_docker_healthcheck(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/ready").status_code != _UNAUTHORIZED


def test_authenticated_status_still_reports_version_and_mcp(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        body = client.get("/status", headers={"Authorization": f"Bearer {_DEFAULT_KEY}"}).json()

    assert body["version"]
    assert "mcp" in body


def test_only_health_and_ready_are_exempt_from_authentication(tmp_path: Path) -> None:
    from archon_search.server.middleware_auth import _EXEMPT_PATHS, DOCS_PATHS

    assert _EXEMPT_PATHS == frozenset({"/health", "/ready"})
    assert DOCS_PATHS == frozenset({"/docs", "/redoc", "/openapi.json"})


def test_docs_enabled_is_off_by_default_and_read_from_the_server_table(tmp_path: Path) -> None:
    assert SearchConfig().docs_enabled is False
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text("[server]\ndocs_enabled = true\n")

    assert load_config(config_file).docs_enabled is True


def test_docs_enabled_must_be_a_boolean(tmp_path: Path) -> None:
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text('[server]\ndocs_enabled = "yes"\n')

    with pytest.raises(ConfigError, match="docs_enabled"):
        load_config(config_file)


async def test_httpx_asgi_transport_carries_the_raw_header_bytes(tmp_path: Path) -> None:
    """Guards the premise of the non-ASCII tests: the raw latin-1 byte reaches the ASGI app unchanged."""
    seen: list[bytes] = []

    async def probe(scope, receive, send) -> None:
        seen.extend(value for name, value in scope["headers"] if name == b"authorization")
        await send({"type": "http.response.start", "status": _OK, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe), base_url="http://t") as client:
        await client.get("/", headers=[(b"authorization", _NON_ASCII_BEARER)])

    assert seen == [_NON_ASCII_BEARER]
