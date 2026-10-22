"""HyDE on a collection served by its own (non-global) embedding model.

Regression for: ``HyDEGenerator.generate`` embeds the hypothetical document with the
pipeline's GLOBAL embedder, and the single-collection search/explain paths hand that
vector to a collection whose chunks were embedded by a different model. A different
dimension makes the vector leg raise; the same dimension silently ranks in the wrong
embedding space. The documented contract ("HyDE never degrades availability",
Documentation/UserManual/60_searching.md) requires HyDE on such a collection to either
embed the hypothesis with the collection's embedder or fall back to plain query
embedding with ``hyde_applied: false`` — never to error.

The fake fastembed backend installed here maps each model name to its own output
dimension and records every ``(model_name, text)`` it embeds, so a test can tell which
model embedded the hypothesis.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.store import STORE_SCHEMA_VERSION
from tests.integration.conftest import (
    ingest_doc,
    make_real_app,
    mcp_initialize,
    mcp_tool_call,
)

pytestmark = pytest.mark.integration

_GLOBAL_MODEL = SearchConfig().embedding_model
_GLOBAL_DIM = 384
_OTHER_DIM_MODEL = "test/collection-model-768"
_OTHER_DIM = 768
_SAME_DIM_MODEL = "test/collection-model-384"
_MODEL_DIMS = {_GLOBAL_MODEL: _GLOBAL_DIM, _OTHER_DIM_MODEL: _OTHER_DIM, _SAME_DIM_MODEL: _GLOBAL_DIM}

_COLLECTION = "percolhyde"
_DOC_TEXT = "Widgets are calibrated with the torque gauge before shipping."
_QUERY = "how are widgets calibrated"
_HYPOTHESIS = "HYPOTHETICAL: widgets get calibrated using a torque gauge."


class _StubHypothesisProvider:
    """QueryExpansionProvider stand-in: always returns a fixed hypothetical document."""

    async def generate_hypothetical_doc(self, query: str, *, max_tokens: int, timeout_seconds: float) -> str:
        return _HYPOTHESIS


@pytest.fixture(autouse=True)
def _stub_hyde_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand ``create_app``'s provider factory the stub, so HyDEGenerator is built with it."""
    import archon_search.server.app as app_module

    monkeypatch.setattr(app_module, "_build_query_expansion_provider", lambda *_args: _StubHypothesisProvider())


@pytest.fixture
def embed_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Install a model-aware fake ``fastembed.TextEmbedding``; return its call log."""
    calls: list[tuple[str, str]] = []

    class _ModelAwareTextEmbedding:
        def __init__(self, model_name: str, **kwargs: object) -> None:
            self._model_name = model_name
            self._dim = _MODEL_DIMS[model_name]

        def embed(self, texts: list[str]) -> Iterator[np.ndarray]:
            for text in texts:
                calls.append((self._model_name, text))
                yield np.full(self._dim, 1.0 / np.sqrt(self._dim), dtype=np.float32)

    monkeypatch.setattr(sys.modules["fastembed"], "TextEmbedding", _ModelAwareTextEmbedding)
    return calls


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _setup_collection_with_model(
    client, api_key: str, model: str, calls: list[tuple[str, str]], name: str = _COLLECTION
) -> None:
    """Pin *model* on a fresh collection, then ingest one doc through REST (embedded by *model*)."""
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model=model,
        schema_version=STORE_SCHEMA_VERSION,
    )
    client.portal.call(client.app.state.search_store.update_collection_meta, meta)
    ingest_doc(client, name, _DOC_TEXT, f"/docs/{name}/widgets.md", api_key=api_key)
    # The pin took effect: the ingested chunk was embedded by *model*, not the global one.
    assert any(m == model and _DOC_TEXT in text for m, text in calls), f"{model!r} never embedded the doc: {calls}"


def _hypothesis_embedders(calls: list[tuple[str, str]]) -> set[str]:
    return {model for model, text in calls if text == _HYPOTHESIS}


def _assert_no_error_and_right_space(status: int, data: dict, calls: list[tuple[str, str]], model: str) -> None:
    assert status == 200, f"HyDE on a per-collection-model collection must not error: {status} {data}"
    assert data["results"], f"expected the ingested doc to be returned: {data}"
    assert data["hyde_applied"] is True, f"HyDE must run on a per-collection-model collection: {data}"
    assert _hypothesis_embedders(calls) == {model}, (
        f"the hypothesis was embedded by {_hypothesis_embedders(calls)}, not the collection's model {model!r}"
    )


# ---------------------------------------------------------------------------
# REST POST /search — single collection
# ---------------------------------------------------------------------------


def test_rest_search_hyde_on_collection_with_other_dim_model_does_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed_calls: list[tuple[str, str]]
) -> None:
    with make_real_app(tmp_path, monkeypatch, hyde_enabled=True) as (client, _cfg, api_key):
        _setup_collection_with_model(client, api_key, _OTHER_DIM_MODEL, embed_calls)

        resp = client.post(
            "/search", json={"query": _QUERY, "collection": _COLLECTION, "hyde": True}, headers=_auth(api_key)
        )

    _assert_no_error_and_right_space(resp.status_code, resp.json(), embed_calls, _OTHER_DIM_MODEL)


def test_rest_search_hyde_embeds_hypothesis_with_collection_model_same_dim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed_calls: list[tuple[str, str]]
) -> None:
    """Same dimension, different model: no error is raised, so the wrong space is silent."""
    with make_real_app(tmp_path, monkeypatch, hyde_enabled=True) as (client, _cfg, api_key):
        _setup_collection_with_model(client, api_key, _SAME_DIM_MODEL, embed_calls)

        resp = client.post(
            "/search", json={"query": _QUERY, "collection": _COLLECTION, "hyde": True}, headers=_auth(api_key)
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["hyde_applied"] is True, resp.json()
    assert _hypothesis_embedders(embed_calls) == {_SAME_DIM_MODEL}, (
        f"hypothesis must be embedded by the collection's model {_SAME_DIM_MODEL!r}, "
        f"got {_hypothesis_embedders(embed_calls)}"
    )


# ---------------------------------------------------------------------------
# REST POST /explain — single collection
# ---------------------------------------------------------------------------


def test_rest_explain_hyde_on_collection_with_other_dim_model_does_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed_calls: list[tuple[str, str]]
) -> None:
    with make_real_app(tmp_path, monkeypatch, hyde_enabled=True) as (client, _cfg, api_key):
        _setup_collection_with_model(client, api_key, _OTHER_DIM_MODEL, embed_calls)

        resp = client.post(
            "/explain", json={"query": _QUERY, "collection": _COLLECTION, "hyde": True}, headers=_auth(api_key)
        )

    assert resp.status_code == 200, f"HyDE explain must not error: {resp.status_code} {resp.text}"
    data = resp.json()
    assert data["results"], f"expected the ingested doc in the explain trace: {data}"
    assert data["hyde_applied"] is True, data
    assert _hypothesis_embedders(embed_calls) == {_OTHER_DIM_MODEL}, _hypothesis_embedders(embed_calls)


# ---------------------------------------------------------------------------
# MCP search / search_with_context — single collection
# ---------------------------------------------------------------------------


def _mcp_call_tool(client, token: str, tool_name: str, arguments: dict) -> dict:
    """Initialize an MCP session, call *tool_name*, return the tool's parsed JSON payload."""
    return mcp_tool_call(client, token, mcp_initialize(client, token), tool_name, arguments)


@pytest.mark.parametrize("tool_name", ["search", "search_with_context", "explain"])
def test_mcp_hyde_on_collection_with_other_dim_model_does_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed_calls: list[tuple[str, str]], tool_name: str
) -> None:
    with make_real_app(tmp_path, monkeypatch, hyde_enabled=True, mcp_enabled=True) as (client, _cfg, api_key):
        _setup_collection_with_model(client, api_key, _OTHER_DIM_MODEL, embed_calls)

        payload = _mcp_call_tool(client, api_key, tool_name, {"query": _QUERY, "collection": _COLLECTION, "hyde": True})

    assert "error" not in payload, f"MCP {tool_name} with HyDE must not error: {payload}"
    assert payload["results"], f"expected the ingested doc: {payload}"
    assert payload["hyde_applied"] is True, payload
    assert _hypothesis_embedders(embed_calls) == {_OTHER_DIM_MODEL}, _hypothesis_embedders(embed_calls)


# ---------------------------------------------------------------------------
# Fan-out across collections on different models
# ---------------------------------------------------------------------------

_GLOBAL_COLLECTION = "globalhyde"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_hyde_fanout_across_models_embeds_with_the_global_model_and_excludes_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed_calls: list[tuple[str, str]], transport: str
) -> None:
    """A fan-out embeds with the global model, so its HyDE vector must too.

    The collection on another model is excluded as ``embedding_model_mismatch``; the one
    on the global model still answers with the HyDE vector.

    A guard on current behaviour, not a regression repro: it passes before the fix too,
    since the fan-out already embedded HyDE with the global model (C2-T-8).
    """
    body = {"query": _QUERY, "collections": [_GLOBAL_COLLECTION, _COLLECTION], "hyde": True}
    with make_real_app(tmp_path, monkeypatch, hyde_enabled=True, mcp_enabled=True) as (client, _cfg, api_key):
        _setup_collection_with_model(client, api_key, _GLOBAL_MODEL, embed_calls, name=_GLOBAL_COLLECTION)
        _setup_collection_with_model(client, api_key, _OTHER_DIM_MODEL, embed_calls)

        if transport == "rest":
            resp = client.post("/search", json=body, headers=_auth(api_key))
            assert resp.status_code == 200, resp.text
            data = resp.json()
        else:
            data = _mcp_call_tool(client, api_key, "search", body)

    assert "error" not in data, data
    assert data["hyde_applied"] is True, data
    assert data["results"] and {r["collection"] for r in data["results"]} == {_GLOBAL_COLLECTION}, data
    assert data["excluded_collections"] == [{"name": _COLLECTION, "reason": "embedding_model_mismatch"}]
    assert _hypothesis_embedders(embed_calls) == {_GLOBAL_MODEL}, _hypothesis_embedders(embed_calls)
