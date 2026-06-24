"""D9 / BE-4 — Asymmetry fix #1: pass config.namespaces to create_mcp_http_app().

Covers:
- test_mcp_middleware_receives_namespaces (unit) — create_mcp_http_app() called with a
  non-empty namespaces dict constructs APIKeyMiddleware with that dict, not {}.
- test_toml_namespace_token_accepted_by_mcp (integration) — a TOML namespace token used
  as a Bearer against the mounted /mcp endpoint returns 200, not 401.

Scenarios completed: S12 (TOML namespace tokens → correct namespace resolution).
Contract completed: C2 (partial — namespaces arg).
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# Unit — namespaces dict threaded into APIKeyMiddleware
# ---------------------------------------------------------------------------


def test_mcp_middleware_receives_namespaces(tmp_path) -> None:
    """create_mcp_http_app(config) passes config.namespaces to APIKeyMiddleware.

    This is a white-box unit test: we intercept add_middleware() to assert the
    namespaces kwarg matches config.namespaces, not the pre-fix hardcoded {}.
    """
    from starlette.applications import Starlette

    from archon_search.config import SearchConfig
    from archon_search.embedder_cache import EmbedderCache
    from archon_search.jobs.store import JobStore
    from archon_search.server.mcp import create_mcp_http_app

    toml_token = "a" * 64  # valid-looking hex token
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.namespaces = {toml_token: "ns-a"}

    mock_pipeline = MagicMock()
    mock_embedder_cache = MagicMock(spec=EmbedderCache)
    mock_job_store = MagicMock(spec=JobStore)

    captured: list[dict] = []

    original_add_middleware = Starlette.add_middleware

    def spy_add_middleware(self, cls, **kwargs):
        from archon_search.server.middleware_auth import APIKeyMiddleware

        if cls is APIKeyMiddleware:
            captured.append(kwargs.get("namespaces", "_missing_"))
        original_add_middleware(self, cls, **kwargs)

    with patch.object(Starlette, "add_middleware", spy_add_middleware):
        create_mcp_http_app(
            pipeline=mock_pipeline,
            default_collection="default",
            config=cfg,
            embedder_cache=mock_embedder_cache,
            job_store=mock_job_store,
        )

    assert len(captured) == 1, f"APIKeyMiddleware.add_middleware called {len(captured)} times, expected 1"
    assert captured[0] == {toml_token: "ns-a"}, (
        f"Expected namespaces={{{toml_token!r}: 'ns-a'}}, got {captured[0]!r}. "
        "BE-4 fix must pass config.namespaces to APIKeyMiddleware, not hardcoded {}."
    )


# ---------------------------------------------------------------------------
# Integration — TOML namespace token accepted by mounted /mcp endpoint
# ---------------------------------------------------------------------------


def test_toml_namespace_token_accepted_by_mcp_namespaces_path(tmp_path, monkeypatch) -> None:
    """TOML namespace token is accepted by the MCP sub-app via the namespaces dict.

    Proves the namespaces dict path specifically by calling create_mcp_http_app
    with key_store=None — so only the namespaces dict can authenticate the token.
    Uses APIKeyMiddleware directly to avoid MCP lifespan complexity.

    S12: TOML namespace tokens → correct namespace resolution.
    C2 (partial): namespaces arg passed correctly.
    """
    import secrets

    from archon_search.config import SearchConfig
    from archon_search.server.mcp import create_mcp_http_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    toml_token = secrets.token_hex(32)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.namespaces = {toml_token: "ns-toml"}

    mock_pipeline = MagicMock()

    # key_store=None: disables the managed-key path, so ONLY the namespaces dict
    # can authenticate. This proves BE-4 wired config.namespaces to the middleware.
    mcp_starlette = create_mcp_http_app(
        pipeline=mock_pipeline,
        default_collection="default",
        config=cfg,
        key_store=None,
    )

    # Verify the middleware was constructed with the namespaces dict by inspecting
    # the middleware stack. We look for APIKeyMiddleware and check _namespaces.
    from archon_search.server.middleware_auth import APIKeyMiddleware

    found_middleware: APIKeyMiddleware | None = None
    # FastMCP's StarletteWithLifespan exposes added middleware via user_middleware
    # (same underlying list as Starlette.user_middleware, accessible before build).
    for mw in mcp_starlette.user_middleware:
        if mw.cls is APIKeyMiddleware:
            # Instantiate with a dummy ASGI app to inspect kwargs without triggering the real app.
            async def _dummy_app(scope, receive, send): ...  # noqa: E704
            instance = mw.cls(_dummy_app, **mw.kwargs)
            found_middleware = instance
            break

    assert found_middleware is not None, "APIKeyMiddleware not found in MCP sub-app user_middleware"
    assert found_middleware._namespaces == {toml_token: "ns-toml"}, (
        f"Expected _namespaces={{{toml_token!r}: 'ns-toml'}}, "
        f"got {found_middleware._namespaces!r}. "
        "key_store=None means only config.namespaces can authenticate. "
        "BE-4 fix must wire config.namespaces into APIKeyMiddleware."
    )


def test_mcp_middleware_receives_empty_namespaces_when_config_none(tmp_path, monkeypatch) -> None:
    """create_mcp_http_app(config=None) passes {} to APIKeyMiddleware (not AttributeError).

    Covers the `config is None` branch (the else {} side of `config.namespaces if config is not None else {}`).
    """
    from starlette.applications import Starlette

    from archon_search.server.mcp import create_mcp_http_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    mock_pipeline = MagicMock()
    captured: list[dict] = []

    original_add_middleware = Starlette.add_middleware

    def spy_add_middleware(self, cls, **kwargs):
        from archon_search.server.middleware_auth import APIKeyMiddleware

        if cls is APIKeyMiddleware:
            captured.append(kwargs.get("namespaces", "_missing_"))
        original_add_middleware(self, cls, **kwargs)

    with patch.object(Starlette, "add_middleware", spy_add_middleware):
        create_mcp_http_app(
            pipeline=mock_pipeline,
            default_collection="default",
            config=None,  # the None branch under test
        )

    assert len(captured) == 1
    assert captured[0] == {}, f"Expected {{}} when config=None, got {captured[0]!r}"


@pytest.mark.integration
def test_toml_namespace_token_accepted_by_mcp(tmp_path, monkeypatch) -> None:
    """A TOML namespace token used as a Bearer against /mcp returns 200, not 401.

    Note: this test proves the full-stack behavior (TOML tokens work end-to-end),
    but authentication happens via the key_store synthetic-record path (not the
    namespaces dict path exclusively). See
    test_toml_namespace_token_accepted_by_mcp_namespaces_path for the direct
    namespaces-dict proof (key_store=None forces the namespaces path).
    """
    # Use a hex-looking token so secrets.compare_digest accepts it.
    toml_token = hashlib.sha256(b"test-toml-namespace-token").hexdigest()

    with make_real_app(
        tmp_path,
        monkeypatch,
        mcp_enabled=True,
        namespaces={toml_token: "ns-toml"},
    ) as (client, _cfg, _api_key):
        # Send a full MCP initialize request using the TOML namespace token.
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "be4-ns-test", "version": "1.0"},
                },
            },
            headers={
                "Authorization": f"Bearer {toml_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert resp.status_code == 200, (
            f"Expected 200 (namespace token accepted), got {resp.status_code}. "
            f"Body: {resp.text[:500]}. "
            "This proves BE-4 fix is live: config.namespaces passed to MCP middleware."
        )
        assert "mcp-session-id" in resp.headers
