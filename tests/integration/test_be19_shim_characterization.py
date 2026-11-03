"""BE-19: characterization of the OpenAI shim's behaviour as it is TODAY (S60, S78), before BE-20 rewires it.

Each test records the old result on the real app so the rewrite changes it deliberately: BE-20 replaces these
expectations (plain 500 -> 503 envelope, bounded embedder wait, real 404 for an empty name; embedder-not-ready stays a 503, now through the service) instead of re-pinning them.

Run with:
    uv run pytest tests/integration/test_be19_shim_characterization.py --no-cov
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.pipeline import SearchPipelineResult
from archon_search.services.search_budget import EMBEDDER_WARMUP_WAIT_SECONDS
from archon_search.services.service_error import RETRY_AFTER_SECONDS
from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("openai_shim")]

_CHAT_PATH = "/v1/chat/completions"
# Stands in for the shim's whole-search budget, shrunk so a slow embedder load clearly outlasts it.
_SHIM_BUDGET_SECONDS = 0.05
# A cold embedder load longer than the 0.5 s handoff bound BE-20 introduces, so only an unbounded wait passes.
_EMBEDDER_WAIT_SECONDS = 2 * EMBEDDER_WARMUP_WAIT_SECONDS
# The wire value of the service's ``RETRY_AFTER_SECONDS`` constant, pinned independently of it.
_RETRY_AFTER_WIRE = "30"
_SERVICE_UNAVAILABLE_BODY = {
    "error": {"message": "Service temporarily unavailable.", "type": "server_error", "param": None, "code": None}
}


def _chat_body(model: str) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "find the answer"}]}


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _pipeline_result() -> SearchPipelineResult:
    return SearchPipelineResult(results=[], acl_filtered=False)


def test_shim_collection_list_failure_is_a_plain_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``archon-search`` fan-out: the all-collections list lookup is unguarded, so its failure escapes as a bare 500."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True, raise_server_exceptions=False) as (
        client,
        _cfg,
        api_key,
    ):
        client.app.state.pipeline.store.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("store is down"))

        response = client.post(_CHAT_PATH, json=_chat_body("archon-search"), headers=_headers(api_key))

        assert response.status_code == 500
        assert response.text == "Internal Server Error"
        assert response.headers["content-type"].startswith("text/plain")


def test_shim_embedder_not_ready_is_503_with_retry_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``archon-search/col``: an embedder that is not ready is answered by the app handler (503, ``Retry-After``, OpenAI envelope)."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, _cfg, api_key):
        client.app.state.pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col"))
        client.app.state.embedder_cache.get_or_load = AsyncMock(side_effect=EmbedderNotReadyError("still loading"))

        response = client.post(_CHAT_PATH, json=_chat_body("archon-search/col"), headers=_headers(api_key))

        assert response.status_code == 503
        assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS) == _RETRY_AFTER_WIRE
        assert response.json() == _SERVICE_UNAVAILABLE_BODY


def test_shim_single_path_embedder_wait_is_unbounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``archon-search/col``: the embedder lookup runs outside the search budget and has no bound of its own, so a slow load is waited out."""
    monkeypatch.setattr("archon_search.server.routes_openai_shim._SEARCH_TIMEOUT_SECONDS", _SHIM_BUDGET_SECONDS)
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, _cfg, api_key):
        pipeline = client.app.state.pipeline
        pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col"))
        pipeline.warmup_models = AsyncMock(return_value=True)
        pipeline.search = AsyncMock(return_value=_pipeline_result())
        embedder = MagicMock()
        embedder.is_warm = True

        async def slow_get_or_load(_model_name: str) -> MagicMock:
            await asyncio.sleep(_EMBEDDER_WAIT_SECONDS)
            return embedder

        client.app.state.embedder_cache.get_or_load = slow_get_or_load

        started = time.monotonic()
        response = client.post(_CHAT_PATH, json=_chat_body("archon-search/col"), headers=_headers(api_key))
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        assert elapsed >= _EMBEDDER_WAIT_SECONDS
        pipeline.search.assert_awaited_once()
        assert pipeline.search.call_args.kwargs["fts_only"] is False


def test_shim_empty_collection_name_against_a_real_store_is_a_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``archon-search/``: the real store raises ``ValueError`` for the empty name and the shim answers 503, not 404 (S78)."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, _cfg, api_key):
        unknown = client.post(_CHAT_PATH, json=_chat_body("archon-search/missing-col"), headers=_headers(api_key))
        response = client.post(_CHAT_PATH, json=_chat_body("archon-search/"), headers=_headers(api_key))

        assert unknown.status_code == 404  # control: the store itself works, only the empty name is refused
        assert response.status_code == 503
        assert response.json() == _SERVICE_UNAVAILABLE_BODY
