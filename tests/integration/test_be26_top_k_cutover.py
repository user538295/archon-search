"""BE-26 (C3, C5, C6, C7; S19, S20, S22, S46, S72, S79): ``top_k`` is real on REST and MCP and the shim keeps ignoring it.

Everything runs against the real app (real store, pipeline and service wiring); the timeout test stubs the pipeline.

Run with:
    uv run pytest tests/integration/test_be26_top_k_cutover.py --no-cov
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.pipeline import SearchPipelineResult
from archon_search.services.service_error import (
    SEARCH_TIMEOUT_DETAIL,
    TIMEOUT_CODE,
    top_k_exceeded_detail,
)
from tests.integration.conftest import (
    ingest_doc,
    make_real_app,
    mcp_initialize,
    mcp_tool_call,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("be26_top_k")]

_COLLECTION_A = "tkcola"
_COLLECTION_B = "tkcolb"
_DOCS_PER_COLLECTION = 12
_QUERY = "aurora"
_REQUESTED_TOP_K = 7
_TOP_K_MAX = 20
_SHIM_TOP_K = 3
_SHIM_CHAT_PATH = "/v1/chat/completions"
_SOURCE_MARKER = "[Source:"
_SHORT_BUDGET_SECONDS = 0.05
_PAST_BUDGET_SLEEP_SECONDS = 10 * _SHORT_BUDGET_SECONDS
_REST_TIMEOUT_DETAIL = "Search timed out"
#: A request ``top_k`` the shim must accept and ignore: below 1, above the maximum, and a plain in-range value.
_SHIM_REQUEST_TOP_KS = (0, 10**6, 2, None)


def _ingest_corpus(client, api_key: str, *collections: str) -> None:  # type: ignore[no-untyped-def]
    for collection in collections:
        for i in range(_DOCS_PER_COLLECTION):
            ingest_doc(
                client,
                collection,
                f"Aurora borealis observation number {i}: the aurora glowed over the northern sky.",
                f"/corpus/{collection}_{i}.txt",
                api_key=api_key,
            )


def _rest(client, api_key: str, **body: object):  # type: ignore[no-untyped-def]
    return client.post("/search", json={"query": _QUERY, **body}, headers={"Authorization": f"Bearer {api_key}"})


def _shim(client, api_key: str, model: str, **extra: object):  # type: ignore[no-untyped-def]
    body = {"model": model, "messages": [{"role": "user", "content": _QUERY}], **extra}
    return client.post(_SHIM_CHAT_PATH, json=body, headers={"Authorization": f"Bearer {api_key}"})


def _mcp(client, api_key: str, **arguments: object) -> dict:  # type: ignore[no-untyped-def]
    session_id = mcp_initialize(client, api_key)
    return mcp_tool_call(client, api_key, session_id, "search", {"query": _QUERY, **arguments})


def _source_count(response) -> int:  # type: ignore[no-untyped-def]
    return response.json()["choices"][0]["message"]["content"].count(_SOURCE_MARKER)


def test_top_k_exact_n_on_rest_and_mcp_single_and_multi_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S19: ``top_k=N`` returns exactly N on the single and the multi path of REST and of MCP."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, cfg, api_key):
        _ingest_corpus(client, api_key, _COLLECTION_A, _COLLECTION_B)
        assert cfg.top_k_return != _REQUESTED_TOP_K, "N must differ from the configured count to prove the request wins"
        single = {"collection": _COLLECTION_A, "top_k": _REQUESTED_TOP_K}
        multi = {"collections": [_COLLECTION_A, _COLLECTION_B], "top_k": _REQUESTED_TOP_K}

        for selection in (single, multi):
            response = _rest(client, api_key, **selection)
            assert response.status_code == 200, response.text
            assert len(response.json()["results"]) == _REQUESTED_TOP_K, f"REST {sorted(selection)}"

            payload = _mcp(client, api_key, **selection)
            assert len(payload["results"]) == _REQUESTED_TOP_K, f"MCP {sorted(selection)}: {payload}"


def test_top_k_below_one_or_above_the_maximum_is_rejected_on_rest_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S20, S46: REST and MCP reject both bounds; the shim answers no 4xx for any request ``top_k``."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, openai_shim_enabled=True, top_k_max=_TOP_K_MAX) as (
        client,
        _cfg,
        api_key,
    ):
        _ingest_corpus(client, api_key, _COLLECTION_A)
        above = _TOP_K_MAX + 1

        assert _rest(client, api_key, collection=_COLLECTION_A, top_k=0).status_code == 422
        above_response = _rest(client, api_key, collection=_COLLECTION_A, top_k=above)
        assert (above_response.status_code, above_response.json()) == (
            422,
            {"detail": top_k_exceeded_detail(above, _TOP_K_MAX)},
        )

        below_mcp = _mcp(client, api_key, collection=_COLLECTION_A, top_k=0)
        above_mcp = _mcp(client, api_key, collection=_COLLECTION_A, top_k=above)
        assert below_mcp["code"] == "validation_error", below_mcp
        assert (above_mcp["code"], above_mcp["error"]) == ("validation_error", top_k_exceeded_detail(above, _TOP_K_MAX))

        for request_top_k in _SHIM_REQUEST_TOP_KS:
            response = _shim(client, api_key, f"archon-search/{_COLLECTION_A}", top_k=request_top_k)
            assert response.status_code == 200, f"shim top_k={request_top_k!r}: {response.text}"


def test_top_k_null_is_valid_on_rest_and_means_the_configured_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S22: ``top_k: null`` and an omitted ``top_k`` both give the configured ``top_k_return`` on every path."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, cfg, api_key):
        _ingest_corpus(client, api_key, _COLLECTION_A, _COLLECTION_B)
        assert cfg.top_k_return < _DOCS_PER_COLLECTION

        for selection in ({"collection": _COLLECTION_A}, {"collections": [_COLLECTION_A, _COLLECTION_B]}):
            for body in (selection, {**selection, "top_k": None}):
                response = _rest(client, api_key, **body)
                assert response.status_code == 200, response.text
                assert len(response.json()["results"]) == cfg.top_k_return, f"REST {body}"
            assert len(_mcp(client, api_key, **selection)["results"]) == cfg.top_k_return


def test_large_top_k_times_out_with_the_timeout_error_on_every_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S72: a pipeline that exceeds the (patched) budget on a large ``top_k`` is the timeout error on REST, MCP and the shim."""
    monkeypatch.setattr("archon_search.services.search_service._SEARCH_TIMEOUT_SECONDS", _SHORT_BUDGET_SECONDS)
    large = _TOP_K_MAX
    toml = f"[search]\ntop_k_max = {_TOP_K_MAX}\n[openai_shim]\nenabled = true\ntop_k = {large}\n"

    async def slow_when_large(*_args: object, top_k: int | None = None, **_kwargs: object) -> SearchPipelineResult:
        if top_k is not None and top_k >= large:
            await asyncio.sleep(_PAST_BUDGET_SLEEP_SECONDS)
        return SearchPipelineResult(results=[], acl_filtered=False)

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, toml_content=toml) as (client, _cfg, api_key):
        pipeline = client.app.state.pipeline
        pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name=_COLLECTION_A))
        pipeline.get_all_collections_meta = AsyncMock(return_value=[CollectionMeta(name=_COLLECTION_A)])
        pipeline.warmup_models = AsyncMock(return_value=True)
        pipeline.search = slow_when_large
        pipeline.search_many = slow_when_large

        for selection in ({"collection": _COLLECTION_A}, {"collections": [_COLLECTION_A]}):
            response = _rest(client, api_key, top_k=large, **selection)
            assert (response.status_code, response.json()) == (504, {"detail": _REST_TIMEOUT_DETAIL}), selection
            payload = _mcp(client, api_key, top_k=large, **selection)
            assert (payload["code"], payload["error"]) == (TIMEOUT_CODE, SEARCH_TIMEOUT_DETAIL), selection

        for model in (f"archon-search/{_COLLECTION_A}", "archon-search"):
            assert _shim(client, api_key, model).status_code == 504, model


def test_shim_count_follows_the_config_key_then_top_k_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S79: ``[openai_shim].top_k`` is the shim's count when set, a request ``top_k`` never is, and unset means ``top_k_return``."""
    with make_real_app(
        tmp_path, monkeypatch, toml_content=f"[openai_shim]\nenabled = true\ntop_k = {_SHIM_TOP_K}\n"
    ) as (client, cfg, api_key):
        _ingest_corpus(client, api_key, _COLLECTION_A)
        assert _SHIM_TOP_K != cfg.top_k_return
        for request_top_k in (None, 9):
            extra = {} if request_top_k is None else {"top_k": request_top_k}
            single = _shim(client, api_key, f"archon-search/{_COLLECTION_A}", **extra)
            assert single.status_code == 200, single.text
            assert _source_count(single) == _SHIM_TOP_K, f"request top_k={request_top_k!r}"
        fanout = _shim(client, api_key, "archon-search", top_k=9)
        assert fanout.status_code == 200, fanout.text
        assert _source_count(fanout) == _SHIM_TOP_K

    with make_real_app(tmp_path / "unset", monkeypatch, openai_shim_enabled=True) as (client, cfg, api_key):
        _ingest_corpus(client, api_key, _COLLECTION_A)
        response = _shim(client, api_key, f"archon-search/{_COLLECTION_A}", top_k=9)
        assert _source_count(response) == cfg.top_k_return
