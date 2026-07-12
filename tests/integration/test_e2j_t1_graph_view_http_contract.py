"""Integration tests for T-1: HTTP contract for GET /graph/{collection}/view — E2j.

Covers scenarios S1, S7, S8, S10, S11, S12, S13.

Tests:
  - test_e2j_view_happy_path               (S1, S12)  200 + text/html + api_key in body + graph structure
  - test_e2j_view_graph_disabled_422       (S7)        422 with exact detail string
  - test_e2j_view_no_auth_401             (S8)        no Authorization header → 401 + WWW-Authenticate: Bearer
  - test_e2j_view_collection_not_found_404 (S10)       unknown collection → 404
  - test_e2j_view_no_external_urls         (S11, S13)  response body has no external URL patterns

Run with:
    uv run pytest tests/integration/test_e2j_t1_graph_view_http_contract.py -n0 -v --no-cov
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_STUB_HTML = (
    b"<html><head></head><body>"
    b"<div id='network-container'></div>"
    b"<script>"
    b"const collection = __ARCHON_COLLECTION__;"
    b"const token = __ARCHON_TOKEN__;"
    b"const maxNodes = __ARCHON_MAX_NODES__;"
    b"const maxEdges = __ARCHON_MAX_EDGES__;"
    b"</script></body></html>"
)

_STUB_EMBEDDING_DIM = 384


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal spaCy stub so graph-enabled apps can be created."""

    class _FakeDoc:
        ents: list = []

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: _FakeNLP()  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


async def _seed_collection(db_path: str, collection: str, ns: str = "default") -> None:
    """Seed a minimal collection record so get_collection_meta returns non-None."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection(collection, _STUB_EMBEDDING_DIM)
        meta = CollectionMeta(
            name=collection,
            active_embedding_model="stub-model",
            doc_count=0,
            chunk_count=0,
            namespace=ns,
        )
        await store.update_collection_meta(meta)
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# S1, S12 — happy path: 200 + text/html + token in body + graph structure
# ---------------------------------------------------------------------------


def test_e2j_view_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S1/S12: GET /graph/{col}/view with valid Bearer returns 200 text/html.

    Asserts:
    - HTTP 200
    - Content-Type: text/html
    - api_key appears (JSON-encoded) in the response body
    - id="network-container" appears in the response body (graph container placeholder)
    """
    _install_spacy_stub(monkeypatch)
    monkeypatch.setattr(
        "archon_search.server.routes_graph._load_viewer_html",
        lambda: _STUB_HTML,
    )
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))
        resp = client.get("/graph/testcol/view", headers=_auth(api_key))

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    # Token must be present JSON-encoded (json.dumps adds surrounding quotes)
    assert json.dumps(api_key) in resp.text, (
        f"Expected token {api_key!r} (JSON-encoded) in response body"
    )
    # Graph container area must be present
    assert "network-container" in resp.text, (
        "Expected network-container graph area in response body"
    )


# ---------------------------------------------------------------------------
# S7 — graph disabled → 422 with exact detail string
# ---------------------------------------------------------------------------


def test_e2j_view_graph_disabled_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S7: graph.enabled=false → 422 with exact detail string."""
    _install_spacy_stub(monkeypatch)
    monkeypatch.setattr(
        "archon_search.server.routes_graph._load_viewer_html",
        lambda: _STUB_HTML,
    )
    # graph_enabled=False is the default in make_real_app
    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, cfg, api_key):
        resp = client.get("/graph/testcol/view", headers=_auth(api_key))

    assert resp.status_code == 422
    detail = resp.json().get("detail", "")
    assert detail == "graph inspection requires [graph] enabled=true in server config", (
        f"Unexpected detail: {detail!r}"
    )


# ---------------------------------------------------------------------------
# S8 — no Authorization header → 401 + WWW-Authenticate: Bearer
# ---------------------------------------------------------------------------


def test_e2j_view_no_auth_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S8: No Authorization header and no ?token= → 401 + WWW-Authenticate: Bearer."""
    _install_spacy_stub(monkeypatch)
    monkeypatch.setattr(
        "archon_search.server.routes_graph._load_viewer_html",
        lambda: _STUB_HTML,
    )
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        resp = client.get("/graph/testcol/view")

    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer", (
        f"Expected 'WWW-Authenticate: Bearer' header, got: {resp.headers.get('WWW-Authenticate')!r}"
    )


# ---------------------------------------------------------------------------
# S10 — unknown collection → 404
# ---------------------------------------------------------------------------


def test_e2j_view_collection_not_found_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S10: Valid auth but unknown collection → 404."""
    _install_spacy_stub(monkeypatch)
    monkeypatch.setattr(
        "archon_search.server.routes_graph._load_viewer_html",
        lambda: _STUB_HTML,
    )
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # No collection seeded — "nonexistent-col" does not exist
        resp = client.get("/graph/nonexistent-col/view", headers=_auth(api_key))

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# S11, S13 — no external URLs in real HTML response body
# ---------------------------------------------------------------------------


def test_e2j_view_no_external_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S11/S13: Real graph_viewer.html must be self-contained — no external URLs.

    Uses the REAL HTML (not a stub) to validate the actual file content.

    Checks (after stripping the bundled vendor-vis-network script block):
    - No <script src="https://
    - No <link href="https://
    - No fetch("https:// or fetch('https://
    - No new XMLHttpRequest
    """
    _install_spacy_stub(monkeypatch)
    # Do NOT monkeypatch _load_viewer_html — use the real file.
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))
        resp = client.get("/graph/testcol/view", headers=_auth(api_key))

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"

    body = resp.text

    # S12 — token substitution verified against the real HTML template
    assert json.dumps(api_key) in body, (
        f"Expected token {api_key!r} (JSON-encoded via json.dumps) in real HTML body"
    )

    # Real HTML must contain the network-container placeholder (vis-network creates canvas in JS)
    assert 'id="network-container"' in body, (
        "graph_viewer.html must contain id=\"network-container\" as the vis-network mount point"
    )

    # Strip the bundled <script id="vendor-vis-network">…</script> block.
    # This block is a self-contained minified library — it may contain internal
    # URL-like strings from the bundled code. We only care about the surrounding HTML.
    stripped = re.sub(
        r'<script\s+id="vendor-vis-network"[^>]*>.*?</script>',
        "",
        body,
        flags=re.DOTALL,
    )

    # Assert no external script src
    assert '<script src="https://' not in stripped, (
        "graph_viewer.html loads an external <script> — must be self-contained"
    )
    assert "<script src='https://" not in stripped, (
        "graph_viewer.html loads an external <script> — must be self-contained"
    )

    # Assert no external stylesheet link
    assert '<link href="https://' not in stripped, (
        "graph_viewer.html loads an external <link> stylesheet — must be self-contained"
    )
    assert "<link href='https://" not in stripped, (
        "graph_viewer.html loads an external <link> stylesheet — must be self-contained"
    )

    # Assert no runtime fetch calls to external URLs
    assert 'fetch("https://' not in stripped, (
        "graph_viewer.html calls fetch() against an external URL — must be self-contained"
    )
    assert "fetch('https://" not in stripped, (
        "graph_viewer.html calls fetch() against an external URL — must be self-contained"
    )

    # Also check for non-TLS external URLs
    assert '<script src="http://' not in stripped, (
        "graph_viewer.html loads an external <script> via http:// — must be self-contained"
    )
    assert '<link href="http://' not in stripped, (
        "graph_viewer.html loads an external <link> via http:// — must be self-contained"
    )

    # Assert no XMLHttpRequest (legacy external requests)
    assert "new XMLHttpRequest" not in stripped, (
        "graph_viewer.html uses XMLHttpRequest — must be self-contained"
    )

    # S13 — HTMX compatibility: no Web Components, no Shadow DOM, no type="module"
    assert "shadowRoot" not in stripped, (
        "graph_viewer.html uses Shadow DOM — must be HTMX-compatible"
    )
    assert 'customElements.define(' not in stripped, (
        "graph_viewer.html registers custom elements — must be HTMX-compatible"
    )
    # Check <script> elements in stripped HTML have no type="module"
    # (Not the vendored library which is stripped — check remaining script tags)
    assert 'type="module"' not in stripped, (
        "graph_viewer.html uses ES module <script type='module'> — blocks HTMX swaps"
    )
    assert "<template" not in stripped, (
        "graph_viewer.html uses <template> element (shadow DOM slot pattern)"
    )
