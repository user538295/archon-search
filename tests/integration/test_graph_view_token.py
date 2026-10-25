"""The graph viewer no longer takes the API key in the URL (SecReview #8).

``?token=<api key>`` is gone. A browser opens the viewer with a short-lived, single-purpose *view token* minted
by ``POST /graph/{collection}/view-token``; the page embeds a view token, never the real key.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from pathlib import Path

import pytest

from archon_search.server.graph_view_token import mint_view_token
from tests._graph_engine_stub import install_graph_engine_stub
from tests.integration.conftest import make_real_app
from tests.integration.test_e2j_t1_graph_view_http_contract import _seed_collection

pytestmark = pytest.mark.integration

_OK = 200
_UNAUTHORIZED = 401
_NOT_FOUND = 404
_TOKEN_IN_PAGE_RE = re.compile(r'const token\s*=\s*"(?P<token>[^"]+)"')


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _embedded_token(html: str) -> str:
    match = _TOKEN_IN_PAGE_RE.search(html)
    assert match is not None, "the viewer page must define const token"
    return match.group("token")


@pytest.fixture
def graph_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install_graph_engine_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "docs"))
        yield client, cfg, api_key


def test_the_api_key_in_a_query_string_no_longer_authenticates(graph_app) -> None:
    client, _cfg, api_key = graph_app

    for param in ("token", "api_key", "view_token"):
        response = client.get(f"/graph/docs/view?{param}={api_key}")
        assert response.status_code == _UNAUTHORIZED, param
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_view_token_opens_the_viewer_without_an_authorization_header(graph_app) -> None:
    client, _cfg, api_key = graph_app

    minted = client.post("/graph/docs/view-token", headers=_bearer(api_key))
    assert minted.status_code == _OK, minted.text
    body = minted.json()
    assert body["view_url"] == f"/graph/docs/view?view_token={body['view_token']}"
    assert body["expires_in"] > 0

    page = client.get(body["view_url"])

    assert page.status_code == _OK
    assert "text/html" in page.headers["content-type"]
    assert page.headers["Cache-Control"] == "no-store"


def test_the_page_never_contains_the_real_api_key(graph_app) -> None:
    client, _cfg, api_key = graph_app
    view_token = client.post("/graph/docs/view-token", headers=_bearer(api_key)).json()["view_token"]

    via_view_token = client.get(f"/graph/docs/view?view_token={view_token}")
    via_header = client.get("/graph/docs/view", headers=_bearer(api_key))

    for page in (via_view_token, via_header):
        assert page.status_code == _OK
        assert api_key not in page.text
        assert _embedded_token(page.text).startswith("gv1.")


def test_the_embedded_token_fetches_the_graph_and_nothing_else(graph_app) -> None:
    client, _cfg, api_key = graph_app
    page = client.get("/graph/docs/view", headers=_bearer(api_key))
    embedded = _embedded_token(page.text)

    graph = client.get("/graph/docs", headers=_bearer(embedded))
    assert graph.status_code == _OK, graph.text
    assert graph.json()["nodes"] == []

    refused = {
        ("GET", "/graph/docs/impact/Foo"): client.get("/graph/docs/impact/Foo", headers=_bearer(embedded)),
        ("GET", "/graph/cross-collection"): client.get("/graph/cross-collection?collections=docs", headers=_bearer(embedded)),
        ("GET", "/status"): client.get("/status", headers=_bearer(embedded)),
        ("GET", "/keys"): client.get("/keys", headers=_bearer(embedded)),
        ("GET", "/collections/"): client.get("/collections/", headers=_bearer(embedded)),
        ("POST", "/search"): client.post("/search", json={"query": "x"}, headers=_bearer(embedded)),
        ("POST", "/keys"): client.post("/keys", json={"namespace": "default"}, headers=_bearer(embedded)),
        ("POST", "/graph/docs/view-token"): client.post("/graph/docs/view-token", headers=_bearer(embedded)),
        ("DELETE", "/graph/docs"): client.delete("/graph/docs", headers=_bearer(embedded)),
    }
    assert {key: r.status_code for key, r in refused.items() if r.status_code not in (_UNAUTHORIZED, 405)} == {}


def test_a_view_token_is_bound_to_its_collection(graph_app) -> None:
    client, cfg, api_key = graph_app
    asyncio.run(_seed_collection(cfg.db_path, "other"))
    token = client.post("/graph/docs/view-token", headers=_bearer(api_key)).json()["view_token"]

    assert client.get("/graph/docs", headers=_bearer(token)).status_code == _OK
    assert client.get("/graph/other", headers=_bearer(token)).status_code == _UNAUTHORIZED
    assert client.get(f"/graph/other/view?view_token={token}").status_code == _UNAUTHORIZED


def test_an_expired_view_token_is_rejected(graph_app) -> None:
    client, _cfg, _api_key = graph_app
    expired = mint_view_token(client.app.state.graph_view_secret, "default", "docs", now=0)

    assert client.get(f"/graph/docs/view?view_token={expired}").status_code == _UNAUTHORIZED
    assert client.get("/graph/docs", headers=_bearer(expired)).status_code == _UNAUTHORIZED


def test_a_view_token_from_another_server_instance_is_rejected(graph_app) -> None:
    client, _cfg, _api_key = graph_app
    foreign = mint_view_token(secrets.token_bytes(32), "default", "docs")

    assert client.get(f"/graph/docs/view?view_token={foreign}").status_code == _UNAUTHORIZED


def test_a_view_token_cannot_be_sent_with_a_non_get_method(graph_app) -> None:
    client, _cfg, api_key = graph_app
    token = client.post("/graph/docs/view-token", headers=_bearer(api_key)).json()["view_token"]

    response = client.post(f"/graph/docs/view?view_token={token}")

    assert response.status_code == _UNAUTHORIZED


def test_minting_requires_an_authenticated_caller_and_an_existing_collection(graph_app) -> None:
    client, _cfg, api_key = graph_app

    assert client.post("/graph/docs/view-token").status_code == _UNAUTHORIZED
    assert client.post("/graph/nope/view-token", headers=_bearer(api_key)).status_code == _NOT_FOUND


def test_a_view_token_never_crosses_namespaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token minted in ns-a cannot open a collection that lives in ns-b, even by name."""
    install_graph_engine_stub(monkeypatch)
    key_a, key_b = secrets.token_hex(32), secrets.token_hex(32)
    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, namespaces={key_a: "ns-a", key_b: "ns-b"}
    ) as (client, cfg, _default_key):
        asyncio.run(_seed_collection(cfg.db_path, "col-b", "ns-b"))

        assert client.post("/graph/col-b/view-token", headers=_bearer(key_a)).status_code == _NOT_FOUND
        token_b = client.post("/graph/col-b/view-token", headers=_bearer(key_b)).json()["view_token"]
        assert client.get(f"/graph/col-b/view?view_token={token_b}").status_code == _OK
        # The same collection name looked up through a token of the wrong namespace is not found.
        wrong_ns = mint_view_token(client.app.state.graph_view_secret, "ns-a", "col-b")
        assert client.get(f"/graph/col-b/view?view_token={wrong_ns}").status_code == _NOT_FOUND


def test_graph_disabled_still_answers_422_for_minting_and_viewing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_graph_engine_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, _cfg, api_key):
        assert client.post("/graph/docs/view-token", headers=_bearer(api_key)).status_code == 422
        assert client.get("/graph/docs/view", headers=_bearer(api_key)).status_code == 422
