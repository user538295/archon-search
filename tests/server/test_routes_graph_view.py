"""Unit tests for BE-2: GET /graph/{collection}/view route handler.

Unit tests (no real app, mock pipeline/config):
  - test_view_graph_disabled_returns_422
  - test_view_no_auth_checked_before_graph_disabled
  - test_view_unknown_collection_returns_404
  - test_view_collection_name_is_json_encoded
  - test_middleware_graph_view_token_param_is_exempt
  - test_middleware_exact_path_scope

Run with:
    uv run pytest tests/server/test_routes_graph_view.py -n0 -v --no-cov
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.server.middleware_auth import APIKeyMiddleware

VALID_KEY = "a" * 64
BAD_KEY = "z" * 64


# ---------------------------------------------------------------------------
# Shared stub setup
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _inject_spacy_stub():
    """Inject a spaCy stub to allow graph-enabled app creation in tests."""
    if "spacy" not in sys.modules:
        stub = types.ModuleType("spacy")
        sys.modules["spacy"] = stub
    yield


def _make_stub_app(
    *,
    graph_enabled: bool = True,
    collection_exists: bool = True,
    api_key: str = VALID_KEY,
) -> FastAPI:
    """Build a minimal FastAPI app with the graph view route wired up.

    Replaces _load_viewer_html with a stable stub that has all four placeholders.
    """
    from archon_search.config import SearchConfig
    from archon_search.server.routes_graph import router

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware, api_key=api_key)

    config = MagicMock(spec=SearchConfig)
    config.graph = MagicMock()
    config.graph.enabled = graph_enabled
    config.graph.max_inspection_nodes = 500
    config.graph.max_inspection_edges = 2000

    pipeline = MagicMock()
    if collection_exists:
        pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    else:
        pipeline.get_collection_meta = AsyncMock(return_value=None)

    app.state.config = config
    app.state.pipeline = pipeline
    app.state.api_key = api_key
    app.state.namespaces = {}
    app.state.key_store = None

    app.include_router(router)

    return app


# ---------------------------------------------------------------------------
# Unit tests — guard order
# ---------------------------------------------------------------------------


class TestViewGuardOrder:
    """Auth checked before graph-enabled guard; graph-enabled before collection."""

    def test_view_graph_disabled_returns_422(self) -> None:
        """graph.enabled=False → 422 with explicit detail string."""
        app = _make_stub_app(graph_enabled=False)
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=b"<canvas __ARCHON_COLLECTION__ __ARCHON_TOKEN__ __ARCHON_MAX_NODES__ __ARCHON_MAX_EDGES__>"):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                "/graph/test-col/view",
                headers={"Authorization": f"Bearer {VALID_KEY}"},
            )
        assert resp.status_code == 422
        assert "graph" in resp.json()["detail"].lower()

    def test_view_no_auth_checked_before_graph_disabled(self) -> None:
        """Invalid ?token= + graph disabled → 401, NOT 422. Auth is checked first."""
        app = _make_stub_app(graph_enabled=False)
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=b"<canvas __ARCHON_COLLECTION__ __ARCHON_TOKEN__ __ARCHON_MAX_NODES__ __ARCHON_MAX_EDGES__>"):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/graph/test-col/view?token=badtoken")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_view_unknown_collection_returns_404(self) -> None:
        """Unknown collection → 404 after auth passes and graph is enabled."""
        app = _make_stub_app(collection_exists=False)
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=b"<canvas __ARCHON_COLLECTION__ __ARCHON_TOKEN__ __ARCHON_MAX_NODES__ __ARCHON_MAX_EDGES__>"):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                "/graph/unknown-col/view",
                headers={"Authorization": f"Bearer {VALID_KEY}"},
            )
        assert resp.status_code == 404
        assert "collection not found" in resp.json()["detail"]

    def test_view_collection_name_is_json_encoded(self) -> None:
        """Collection name with < and > is safely unicode-escaped — no script-breakout.

        The name must avoid URL path separators (/) to be a valid single path segment.
        We test < and > encoding, which is what prevents </script> breakout in HTML.
        """
        app = _make_stub_app()
        stub_html = b'const collection = __ARCHON_COLLECTION__; const token = __ARCHON_TOKEN__; const maxNodes = __ARCHON_MAX_NODES__; const maxEdges = __ARCHON_MAX_EDGES__;'
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=stub_html):
            client = TestClient(app, raise_server_exceptions=False)
            # Use a name with < and > — these are the characters that enable </script> breakout.
            # Avoid / because that splits the URL path into extra segments.
            dangerous_name = "col<script>alert(1)"
            app.state.pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
            resp = client.get(
                f"/graph/{dangerous_name}/view",
                headers={"Authorization": f"Bearer {VALID_KEY}"},
            )
        assert resp.status_code == 200
        body = resp.text
        # Raw < and > must NOT appear in the JS value — they must be unicode-escaped
        collection_js_value = body.split("const collection = ")[1].split(";")[0]
        assert "<" not in collection_js_value, (
            "XSS: raw < must not appear in the collection JS value (enables </script> breakout)"
        )
        assert ">" not in collection_js_value, (
            "XSS: raw > must not appear in the collection JS value"
        )
        # The encoded form must be present (literal \\u003c in the response text)
        assert "\\u003c" in collection_js_value, (
            "Expected \\u003c (literal unicode escape) in collection JS value"
        )


# ---------------------------------------------------------------------------
# Unit tests — middleware exemption
# ---------------------------------------------------------------------------


class TestMiddlewareGraphViewExemption:
    """Test that the middleware graph-view ?token= exemption works correctly."""

    def test_middleware_graph_view_token_param_is_exempt(self) -> None:
        """GET /graph/test-col/view?token=abc bypasses header-presence check."""
        from archon_search.server.middleware_auth import _GRAPH_VIEW_RE

        # Verify the regex matches the graph view path
        assert _GRAPH_VIEW_RE.match("/graph/test-col/view") is not None
        assert _GRAPH_VIEW_RE.match("/graph/test-col/view/") is None  # no trailing slash
        assert _GRAPH_VIEW_RE.match("/graph/col/view") is not None

    def test_middleware_graph_view_no_token_not_exempt(self) -> None:
        """GET /graph/test-col/view without ?token= is NOT exempt (requires Bearer)."""
        app = _make_stub_app()
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=b"<canvas __ARCHON_COLLECTION__ __ARCHON_TOKEN__ __ARCHON_MAX_NODES__ __ARCHON_MAX_EDGES__>"):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/graph/test-col/view")  # no token, no header
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_middleware_exact_path_scope(self) -> None:
        """A request to /other/view?token=abc is NOT exempt (path must be /graph/{col}/view)."""
        from archon_search.server.middleware_auth import _GRAPH_VIEW_RE

        assert _GRAPH_VIEW_RE.match("/other/view") is None
        assert _GRAPH_VIEW_RE.match("/graph/view") is None  # too short — missing collection segment
        assert _GRAPH_VIEW_RE.match("/graph/col1/col2/view") is None  # extra segment


# ---------------------------------------------------------------------------
# End-to-end middleware exemption tests — real HTTP through APIKeyMiddleware
# ---------------------------------------------------------------------------


class TestMiddlewareExemptionEndToEnd:
    """Test middleware exemption behavior with real HTTP requests through APIKeyMiddleware."""

    _STUB_HTML = b"<canvas __ARCHON_COLLECTION__ __ARCHON_TOKEN__ __ARCHON_MAX_NODES__ __ARCHON_MAX_EDGES__>"

    def test_graph_view_token_no_header_is_exempt(self) -> None:
        """?token= + no Authorization header → middleware passes through (exempt)."""
        app = _make_stub_app()
        with patch(
            "archon_search.server.routes_graph._load_viewer_html",
            return_value=self._STUB_HTML,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            # Valid token in ?token=, no Authorization header → middleware exempts, handler validates
            resp = client.get(f"/graph/test-col/view?token={VALID_KEY}")
        # Middleware does NOT return 401 (exempted) — handler runs and validates the token
        assert resp.status_code != 401 or resp.headers.get("WWW-Authenticate") is None, (
            "Middleware must not 401 an exempt ?token= path before the handler runs"
        )

    def test_graph_view_with_authorization_header_not_exempt(self) -> None:
        """Valid Authorization header + ?token=invalid → header takes priority, NOT exempt."""
        app = _make_stub_app()
        with patch(
            "archon_search.server.routes_graph._load_viewer_html",
            return_value=self._STUB_HTML,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            # Valid header present → middleware validates header, not exempt even with ?token=
            resp = client.get(
                f"/graph/test-col/view?token=invalid",
                headers={"Authorization": f"Bearer {VALID_KEY}"},
            )
        # Valid header → middleware accepts → handler runs → 200
        assert resp.status_code == 200

    def test_graph_view_invalid_header_with_token_not_exempt(self) -> None:
        """Invalid Authorization header + ?token= → middleware rejects (header present → not exempt)."""
        app = _make_stub_app()
        with patch(
            "archon_search.server.routes_graph._load_viewer_html",
            return_value=self._STUB_HTML,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            # Invalid header + valid ?token= → middleware rejects (header present, not exempt)
            resp = client.get(
                f"/graph/test-col/view?token={VALID_KEY}",
                headers={"Authorization": "Bearer invalid-token"},
            )
        # Middleware rejects invalid header → 401
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"
