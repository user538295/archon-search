"""Integration tests for BE-2: GET /graph/{collection}/view route.

Tests (all integration-marked, real app via make_real_app):
  - test_view_token_injected_in_response
  - test_view_returns_html_content_type
  - test_view_invalid_token_returns_401
  - test_view_no_auth_returns_401_with_www_authenticate
  - test_view_query_param_token_happy_path
  - test_view_query_param_invalid_token_returns_401
  - test_view_query_param_keystore_revoked_token_returns_401
  - test_view_query_param_legacy_revoked_token_returns_401
  - test_view_query_param_wrong_namespace_returns_404
  - test_view_header_takes_priority_over_query_param

Run with:
    uv run pytest tests/integration/test_e2j_be2_graph_view_route.py -n0 -v --no-cov
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_STUB_HTML = (
    b"<html><head></head><body><canvas id='network-container'></canvas>"
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
# Integration tests
# ---------------------------------------------------------------------------


class TestViewHappyPath:
    @pytest.fixture(autouse=True)
    def _stub_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "archon_search.server.routes_graph._load_viewer_html",
            lambda: _STUB_HTML,
        )

    def test_view_returns_html_content_type(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /graph/{col}/view with valid Bearer returns 200 text/html."""
        _install_spacy_stub(monkeypatch)
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            asyncio.run(_seed_collection(cfg.db_path, "testcol"))
            resp = client.get("/graph/testcol/view", headers=_auth(api_key))
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_view_token_injected_in_response(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Bearer token literal appears verbatim (JSON-encoded) in the response body."""
        _install_spacy_stub(monkeypatch)
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            asyncio.run(_seed_collection(cfg.db_path, "testcol"))
            resp = client.get("/graph/testcol/view", headers=_auth(api_key))
        assert resp.status_code == 200
        assert json.dumps(api_key) in resp.text, (
            f"Expected token {api_key!r} (JSON-encoded) in body"
        )


class TestViewAuthErrors:
    @pytest.fixture(autouse=True)
    def _stub_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "archon_search.server.routes_graph._load_viewer_html",
            lambda: _STUB_HTML,
        )

    def test_view_invalid_token_returns_401(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid Authorization: Bearer <bad> → 401 with WWW-Authenticate: Bearer."""
        _install_spacy_stub(monkeypatch)
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            resp = client.get(
                "/graph/testcol/view",
                headers={"Authorization": "Bearer invalidtoken"},
            )
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_view_no_auth_returns_401_with_www_authenticate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No auth header and no ?token= → 401 with WWW-Authenticate: Bearer."""
        _install_spacy_stub(monkeypatch)
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            resp = client.get("/graph/testcol/view")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


class TestViewQueryParamToken:
    @pytest.fixture(autouse=True)
    def _stub_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "archon_search.server.routes_graph._load_viewer_html",
            lambda: _STUB_HTML,
        )

    def test_view_query_param_token_happy_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid ?token=<raw> query param → 200 text/html with token in body."""
        _install_spacy_stub(monkeypatch)
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            asyncio.run(_seed_collection(cfg.db_path, "testcol"))
            resp = client.get(f"/graph/testcol/view?token={api_key}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert json.dumps(api_key) in resp.text

    def test_view_query_param_invalid_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid ?token=<bad> → 401 with WWW-Authenticate: Bearer."""
        _install_spacy_stub(monkeypatch)
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            resp = client.get("/graph/testcol/view?token=badtoken")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_view_query_param_keystore_revoked_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KeyStore-managed token after revocation → 401."""
        _install_spacy_stub(monkeypatch)
        from archon_search.key_manager import KeyStore

        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            key_store = KeyStore(path=tmp_path / "keys.json")

            async def _create_and_revoke() -> str:
                result = await key_store.create(
                    ns="default",
                    label="test-key",
                    expires_at=None,
                )
                raw_token = result["token"]
                records = await key_store.load()
                assert records, "Expected at least one key record"
                await key_store.revoke(records[0].id)
                return raw_token

            raw_token = asyncio.run(_create_and_revoke())
            resp = client.get(f"/graph/testcol/view?token={raw_token}")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_view_query_param_legacy_revoked_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy api_key marked revoked in keys.json → 401 on ?token= path."""
        _install_spacy_stub(monkeypatch)

        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            # Write a revoked record matching api_key into keys.json
            token_hash = hashlib.sha256(api_key.encode()).hexdigest()
            revoked_record = {
                "id": "revoked-key-1",
                "token_hash": token_hash,
                "namespace": "default",
                "label": "revoked",
                "created_at": datetime.now(UTC).isoformat(),
                "expires_at": None,
                "status": "revoked",
            }
            keys_path = tmp_path / "keys.json"
            keys_path.write_bytes(json.dumps([revoked_record]).encode())

            resp = client.get(f"/graph/testcol/view?token={api_key}")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_view_query_param_wrong_namespace_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """?token= from namespace ns-a, collection only in ns-b → 404 (negative).
        ?token= from namespace ns-b, collection in ns-b → 200 (positive control).
        """
        _install_spacy_stub(monkeypatch)
        key_a = secrets.token_hex(32)
        key_b = secrets.token_hex(32)

        with make_real_app(
            tmp_path,
            monkeypatch,
            graph_enabled=True,
            namespaces={key_a: "ns-a", key_b: "ns-b"},
        ) as (client, cfg, api_key):
            # Seed a collection in namespace ns-b
            asyncio.run(_seed_collection(cfg.db_path, "col-b", ns="ns-b"))

            # Negative: token from ns-a cannot access col-b (in ns-b)
            resp_negative = client.get(f"/graph/col-b/view?token={key_a}")
            assert resp_negative.status_code == 404, "ns-a token must not see ns-b collection"

            # Positive: token from ns-b CAN access col-b
            resp_positive = client.get(f"/graph/col-b/view?token={key_b}")
            assert resp_positive.status_code == 200, "ns-b token must see ns-b collection"

    def test_view_header_takes_priority_over_query_param(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid Authorization header + invalid ?token= → 200 (header wins)."""
        _install_spacy_stub(monkeypatch)
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            asyncio.run(_seed_collection(cfg.db_path, "testcol"))
            # Valid header + invalid ?token= → header must win; middleware validates header
            resp = client.get(
                "/graph/testcol/view?token=invalidtoken",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        assert resp.status_code == 200
