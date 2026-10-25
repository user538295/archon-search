"""Unit tests for BE-2: GET /graph/{collection}/view route handler.

Unit tests (no real app, mock pipeline/config):
  - test_view_graph_disabled_returns_422
  - test_view_no_auth_checked_before_graph_disabled
  - test_view_unknown_collection_returns_404
  - test_view_collection_name_is_json_encoded
  - TestMiddlewareGraphViewAuth / TestMiddlewareExemptionEndToEnd (Bearer header or view token; ?token= is gone)

Run with:
    uv run pytest tests/server/test_routes_graph_view.py -n0 -v --no-cov
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from archon_search.server.middleware_auth import APIKeyMiddleware

VALID_KEY = "a" * 64
BAD_KEY = "z" * 64


# ---------------------------------------------------------------------------
# Shared stub setup
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _inject_graph_stub(graph_engine_stub):
    """Module-scoped alias for the shared fixture in ``conftest.py``."""
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
    app.state.graph_view_secret = b"s" * 32
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
        """Invalid ?view_token= + graph disabled → 401, NOT 422. Auth is checked first."""
        app = _make_stub_app(graph_enabled=False)
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=b"<canvas __ARCHON_COLLECTION__ __ARCHON_TOKEN__ __ARCHON_MAX_NODES__ __ARCHON_MAX_EDGES__>"):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/graph/test-col/view?view_token=badtoken")
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


class TestMiddlewareGraphViewAuth:
    """The viewer page needs a Bearer header or a view token."""

    def test_middleware_graph_view_without_credentials_is_401(self) -> None:
        """GET /graph/test-col/view with no header and no view token is NOT served (requires Bearer)."""
        app = _make_stub_app()
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=b"<canvas __ARCHON_COLLECTION__ __ARCHON_TOKEN__ __ARCHON_MAX_NODES__ __ARCHON_MAX_EDGES__>"):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/graph/test-col/view")  # no token, no header
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# End-to-end middleware exemption tests — real HTTP through APIKeyMiddleware
# ---------------------------------------------------------------------------


class TestMiddlewareExemptionEndToEnd:
    """Authentication of the viewer page through the real APIKeyMiddleware: header or view token, never the API key in the URL."""

    _STUB_HTML = b"<canvas __ARCHON_COLLECTION__ __ARCHON_TOKEN__ __ARCHON_MAX_NODES__ __ARCHON_MAX_EDGES__>"

    def _client(self, app: FastAPI) -> TestClient:
        return TestClient(app, raise_server_exceptions=False)

    def test_api_key_in_the_query_string_is_not_accepted(self) -> None:
        """?token=<valid api key> + no Authorization header → 401: the key never authenticates from a URL."""
        app = _make_stub_app()
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=self._STUB_HTML):
            resp = self._client(app).get(f"/graph/test-col/view?token={VALID_KEY}")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_view_token_in_the_query_string_opens_the_page_and_the_page_embeds_a_view_token(self) -> None:
        from archon_search.server.graph_view_token import mint_view_token

        app = _make_stub_app()
        view_token = mint_view_token(app.state.graph_view_secret, "default", "test-col")
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=self._STUB_HTML):
            resp = self._client(app).get(f"/graph/test-col/view?view_token={view_token}")
        assert resp.status_code == 200
        assert VALID_KEY not in resp.text
        assert '"gv1.' in resp.text

    def test_authorization_header_takes_priority_over_the_query_string(self) -> None:
        """Valid Authorization header + a bogus query token → the header authenticates; the query is ignored."""
        app = _make_stub_app()
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=self._STUB_HTML):
            resp = self._client(app).get(
                "/graph/test-col/view?token=invalid",
                headers={"Authorization": f"Bearer {VALID_KEY}"},
            )
        assert resp.status_code == 200
        assert VALID_KEY not in resp.text, "the page must embed a view token, never the caller's API key"

    def test_invalid_header_with_a_query_token_is_rejected(self) -> None:
        """Invalid Authorization header + ?token= → 401."""
        app = _make_stub_app()
        with patch("archon_search.server.routes_graph._load_viewer_html", return_value=self._STUB_HTML):
            resp = self._client(app).get(
                f"/graph/test-col/view?token={VALID_KEY}",
                headers={"Authorization": "Bearer invalid-token"},
            )
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"
