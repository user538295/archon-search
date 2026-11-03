"""BE-20: the OpenAI shim's ``chat_completions`` runs on ``SearchService`` (S7, S10, S11, S17, S26, S28-S30, S34, S43-S46, S78).

Everything runs against the real app (``make_real_app``) with the pipeline's search calls stubbed, except: the S78 test (a
real store), the S7 test (it swaps the whole service to read the command the shim builds) and the S17 test (it mounts the
router without the API-key middleware: a request with no identity can only reach the adapter that way).

Run with:
    uv run pytest tests/integration/test_be20_shim_search_service.py --no-cov
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from archon_search._types import SearchResult
from archon_search.collection_meta import CollectionMeta
from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.pipeline import SearchPipelineResult
from archon_search.server import routes_openai_shim
from archon_search.services.authz import AuthIdentity
from archon_search.services.search_budget import EMBEDDER_WARMUP_WAIT_SECONDS
from archon_search.services.search_service import SearchHit, SearchOutcome
from archon_search.services.service_error import RETRY_AFTER_SECONDS
from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("openai_shim")]

_CHAT_PATH = "/v1/chat/completions"
_SERVICE_FACTORY = "archon_search.server.routes_openai_shim.build_search_service_from_state"
_SERVICE_BUDGET_PATH = "archon_search.services.search_service._SEARCH_TIMEOUT_SECONDS"
# The service's whole-search budget, shrunk so a sleeping pipeline call clearly outlasts it.
_SHORT_BUDGET_SECONDS = 0.05
_PAST_BUDGET_SLEEP_SECONDS = 10 * _SHORT_BUDGET_SECONDS
# A cold embedder load that never finishes inside the test: only the service's own handoff bound ends the wait.
_COLD_LOAD_UPPER_BOUND_SECONDS = 10 * EMBEDDER_WARMUP_WAIT_SECONDS
# The wire value of the service's ``RETRY_AFTER_SECONDS`` constant, pinned independently of it.
_RETRY_AFTER_WIRE = "30"
_SERVICE_UNAVAILABLE_BODY = {
    "error": {"message": "Service temporarily unavailable.", "type": "server_error", "param": None, "code": None}
}
_TIMEOUT_BODY = {"error": {"message": "Request timeout.", "type": "server_error", "param": None, "code": None}}
_UNAUTHORIZED_BODY = {
    "error": {"message": "Incorrect API key.", "type": "authentication_error", "param": None, "code": None}
}
_SOURCE_MARKER = "[Source:"


def _chat_body(model: str, content: str = "find the answer", **extra: object) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": content}], **extra}


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _not_found_body(model: str) -> dict:
    return {
        "error": {
            "message": f"The model '{model}' does not exist.",
            "type": "invalid_request_error",
            "param": None,
            "code": None,
        }
    }


def _hit(name: str = "a") -> SearchResult:
    return SearchResult(doc_id=f"doc-{name}", chunk_id=f"chunk-{name}", text=f"text {name}", score=0.5, source_path=f"{name}.md")


def _pipeline_result(*hits: SearchResult) -> SearchPipelineResult:
    return SearchPipelineResult(results=list(hits), acl_filtered=False)


def _warm_embedder() -> MagicMock:
    embedder = MagicMock()
    embedder.is_warm = True
    return embedder


def _stub_single_collection(client: TestClient, *hits: SearchResult) -> MagicMock:
    """Make ``col`` exist with a warm embedder and a pipeline search that answers *hits*; return the pipeline."""
    pipeline = client.app.state.pipeline
    pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col"))
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.search = AsyncMock(return_value=_pipeline_result(*hits))
    client.app.state.embedder_cache.get_or_load = AsyncMock(return_value=_warm_embedder())
    return pipeline


def _stub_fanout(client: TestClient, names: list[str], *hits: SearchResult) -> MagicMock:
    """Make *names* the namespace's collections and the pipeline's fan-out search answer *hits*; return the pipeline."""
    pipeline = client.app.state.pipeline
    pipeline.get_all_collections_meta = AsyncMock(return_value=[CollectionMeta(name=name) for name in names])
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.search_many = AsyncMock(return_value=_pipeline_result(*hits))
    return pipeline


def _read_telemetry_entries(cfg: object) -> list[dict]:
    entries: list[dict] = []
    for jsonl_file in Path(cfg.telemetry.log_dir).glob("*.jsonl"):  # type: ignore[attr-defined]
        entries.extend(json.loads(line) for line in jsonl_file.read_text().splitlines() if line.strip())
    return entries


@pytest.mark.parametrize(
    "model",
    ["archon-search/", "archon-search/   ", "archon-search/bad name!"],
    ids=["empty", "whitespace-only", "invalid"],
)
def test_shim_empty_whitespace_and_invalid_collection_name_return_404_against_the_real_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """S78, S11: the real store raises ``ValueError`` for these names; the shim answers its 404, not a 503, and records nothing."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True, telemetry_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        unknown = client.post(_CHAT_PATH, json=_chat_body("archon-search/missing-col"), headers=_headers(api_key))
        response = client.post(_CHAT_PATH, json=_chat_body(model), headers=_headers(api_key))

        assert unknown.status_code == 404  # control: the store itself works, only the name differs
        assert response.status_code == 404
        assert response.json() == _not_found_body(model)

    assert [e for e in _read_telemetry_entries(cfg) if e.get("endpoint") in ("search", "search_multi")] == []


def test_shim_collection_list_failure_and_fanout_failure_are_the_503_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S29, S45: the list lookup failure (a plain 500 before) and an unexpected fan-out failure answer the 503 envelope."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True, raise_server_exceptions=False) as (
        client,
        _cfg,
        api_key,
    ):
        pipeline = client.app.state.pipeline
        pipeline.store.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("store is down"))
        listing = client.post(_CHAT_PATH, json=_chat_body("archon-search"), headers=_headers(api_key))

        pipeline = _stub_fanout(client, ["col"])
        pipeline.search_many = AsyncMock(side_effect=RuntimeError("fanout exploded /secret/path"))
        fanout = client.post(_CHAT_PATH, json=_chat_body("archon-search"), headers=_headers(api_key))

        for response in (listing, fanout):
            assert response.status_code == 503
            assert response.json() == _SERVICE_UNAVAILABLE_BODY
            assert "Retry-After" not in response.headers
        assert "/secret/path" not in fanout.text


def test_shim_embedder_not_ready_has_retry_after_through_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S28, S44: the service maps the cache's give-up to ``embedder_not_ready`` (both branches); the shim renders 503 + ``Retry-After``."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True, telemetry_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        pipeline = client.app.state.pipeline
        pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col"))
        client.app.state.embedder_cache.get_or_load = AsyncMock(side_effect=EmbedderNotReadyError("still loading"))

        single = client.post(_CHAT_PATH, json=_chat_body("archon-search/col"), headers=_headers(api_key))

        fanout_pipeline = _stub_fanout(client, ["col"])
        fanout_pipeline.search_many = AsyncMock(side_effect=EmbedderNotReadyError("still loading"))
        fanout = client.post(_CHAT_PATH, json=_chat_body("archon-search"), headers=_headers(api_key))

        for response in (single, fanout):
            assert response.status_code == 503
            assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS) == _RETRY_AFTER_WIRE
            assert response.json() == _SERVICE_UNAVAILABLE_BODY

    recorded = [e for e in _read_telemetry_entries(cfg) if e.get("endpoint") == "search"]
    assert [(e["status"], e.get("error_kind")) for e in recorded] == [("internal_error", "other")] * 2


def test_shim_cold_embedder_degrades_to_fts_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S26: a never-finishing embedder handoff answers full-text-only results after the 0.5 s bound; the fan-out is unchanged."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, _cfg, api_key):
        pipeline = _stub_single_collection(client, _hit())
        # The placeholder a cold handoff searches with is the global embedder: warm it, so ``fts_only`` can only come from
        # the cold handoff and not from the warm-up of an unwarmed global embedder.
        pipeline._global_embedder = _warm_embedder()
        cache = client.app.state.embedder_cache
        warm = client.post(_CHAT_PATH, json=_chat_body("archon-search/col"), headers=_headers(api_key))
        assert warm.status_code == 200
        assert pipeline.search.call_args.kwargs["fts_only"] is False  # control: a loaded embedder is searched with

        async def never_finishing_load(_model_name: str) -> MagicMock:
            await asyncio.Event().wait()
            return _warm_embedder()

        cache.get_or_load = AsyncMock(side_effect=never_finishing_load)
        pipeline.search.reset_mock()

        started = time.monotonic()
        response = client.post(_CHAT_PATH, json=_chat_body("archon-search/col"), headers=_headers(api_key))
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        assert _SOURCE_MARKER in response.json()["choices"][0]["message"]["content"]
        assert EMBEDDER_WARMUP_WAIT_SECONDS <= elapsed < _COLD_LOAD_UPPER_BOUND_SECONDS
        pipeline.search.assert_awaited_once()
        assert pipeline.search.call_args.kwargs["fts_only"] is True

        # The all-collections model never goes through the embedder cache: it answers at once, however slow that load is.
        _stub_fanout(client, ["col"], _hit())
        started = time.monotonic()
        fanout = client.post(_CHAT_PATH, json=_chat_body("archon-search"), headers=_headers(api_key))

        assert fanout.status_code == 200
        assert time.monotonic() - started < EMBEDDER_WARMUP_WAIT_SECONDS
        cache.get_or_load.assert_awaited_once()  # only the single-collection request asked the cache


@pytest.mark.parametrize("model", ["archon-search/col", "archon-search"], ids=["single", "fanout"])
def test_shim_search_timeout_answers_the_504_envelope_and_records_a_timeout_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """S30: a pipeline call past the budget answers 504 "Request timeout." and records one ``timeout`` entry labelled ``search``."""
    monkeypatch.setattr(_SERVICE_BUDGET_PATH, _SHORT_BUDGET_SECONDS)

    async def sleeps_past_the_budget(*_args: object, **_kwargs: object) -> SearchPipelineResult:
        await asyncio.sleep(_PAST_BUDGET_SLEEP_SECONDS)
        return _pipeline_result()

    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True, telemetry_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        pipeline = _stub_single_collection(client)
        _stub_fanout(client, ["col"])
        pipeline.search = sleeps_past_the_budget
        pipeline.search_many = sleeps_past_the_budget

        response = client.post(_CHAT_PATH, json=_chat_body(model), headers=_headers(api_key))

        assert response.status_code == 504
        assert response.json() == _TIMEOUT_BODY

    recorded = [e for e in _read_telemetry_entries(cfg) if e.get("endpoint") == "search"]
    assert [(e["status"], e.get("error_kind")) for e in recorded] == [("timeout", "timeout")]


def test_shim_search_is_recorded_in_telemetry_with_the_same_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S34: a successful shim search is written like any other: ``search`` for one collection, ``search_multi`` for the fan-out."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True, telemetry_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        _stub_single_collection(client, _hit())
        assert client.post(_CHAT_PATH, json=_chat_body("archon-search/col"), headers=_headers(api_key)).status_code == 200
        _stub_fanout(client, ["col", "other"], _hit())
        assert client.post(_CHAT_PATH, json=_chat_body("archon-search"), headers=_headers(api_key)).status_code == 200

    entries = _read_telemetry_entries(cfg)
    single = [e for e in entries if e.get("endpoint") == "search"]
    multi = [e for e in entries if e.get("endpoint") == "search_multi"]
    assert [(e["status"], e["collection"], e["result_doc_ids"]) for e in single] == [("ok", "col", ["doc-a"])]
    assert [(e["status"], e["collections"], e["result_count"]) for e in multi] == [("ok", ["col", "other"], 1)]


def test_shim_padded_name_and_query_reach_the_service_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7, S78: the command the shim hands the service carries the stripped collection name and the stripped query."""
    outcome = SearchOutcome(
        results=(SearchHit(result=_hit(), acl_gate=None),),
        acl_filtered=False,
        excluded_collections=(),
        embedding_model="m",
        hyde_applied=False,
        rag_fusion_applied=False,
        rag_fusion_queries_used=0,
        rag_fusion_attempted=False,
        graph_expansion_applied=False,
        expansion_used=False,
        expansion_warning=None,
        applied_filters=None,
        ppr_entities_matched=None,
        include_metadata=False,
    )
    service = MagicMock()
    service.search = AsyncMock(return_value=outcome)
    monkeypatch.setattr(_SERVICE_FACTORY, lambda _state: service)

    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, _cfg, api_key):
        padded_model = "archon-search/  col  "
        response = client.post(
            _CHAT_PATH, json=_chat_body(padded_model, "  find the answer \n"), headers=_headers(api_key)
        )

        assert response.status_code == 200
        assert response.json()["model"] == padded_model  # the model echoed is the one requested
        command, caller = service.search.call_args.args
        assert (command.collection, command.collections, command.query) == ("col", None, "find the answer")
        assert caller == AuthIdentity(namespace="default", admin=True)


def test_shim_without_an_identity_answers_the_openai_401_envelope_before_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S17: with the API-key middleware bypassed (no identity on the request) the shim answers 401 and never builds the service."""
    build = MagicMock()
    monkeypatch.setattr(_SERVICE_FACTORY, build)
    app = FastAPI()
    app.include_router(routes_openai_shim.router, prefix="/v1")
    app.state.pipeline = MagicMock()
    app.state.config = MagicMock()

    response = TestClient(app).post(_CHAT_PATH, json=_chat_body("archon-search/col"))

    assert response.status_code == 401
    assert response.json() == _UNAUTHORIZED_BODY
    assert response.headers["WWW-Authenticate"] == "Bearer"
    build.assert_not_called()


def test_max_fanout_is_read_from_config_by_the_service_and_the_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The one source is ``config.max_fanout``: REST rejects four collections, the shim truncates to three and warns."""
    with make_real_app(
        tmp_path, monkeypatch, openai_shim_enabled=True, toml_content="[search]\nmax_fanout = 3\n"
    ) as (client, _cfg, api_key):
        rest = client.post(
            "/search", json={"collections": ["a", "b", "c", "d"], "query": "hello"}, headers=_headers(api_key)
        )
        pipeline = _stub_fanout(client, ["a", "b", "c", "d"], _hit())
        with caplog.at_level(logging.WARNING):
            shim = client.post(_CHAT_PATH, json=_chat_body("archon-search"), headers=_headers(api_key))

        assert rest.status_code == 422
        assert "collections length exceeds maximum of 3" in rest.json()["detail"]
        assert shim.status_code == 200
        assert pipeline.search_many.call_args.args[1] == ["a", "b", "c"]
        assert "['d']" in " ".join(caplog.messages)


@pytest.mark.parametrize("top_k", [0, 1000, None], ids=["zero", "above-maximum", "null"])
def test_shim_accepts_a_request_top_k_of_zero_above_the_maximum_or_null_with_no_4xx_and_ignores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, top_k: int | None
) -> None:
    """S46: ``top_k`` 0, a value above ``top_k_max`` and ``null`` each answer 200; the pipeline gets no ``top_k`` argument."""
    with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, cfg, api_key):
        assert top_k is None or top_k > cfg.top_k_max or top_k == 0
        pipeline = _stub_single_collection(client, _hit("a"), _hit("b"))
        _stub_fanout(client, ["col"], _hit("a"), _hit("b"))

        single = client.post(_CHAT_PATH, json=_chat_body("archon-search/col", top_k=top_k), headers=_headers(api_key))
        fanout = client.post(_CHAT_PATH, json=_chat_body("archon-search", top_k=top_k), headers=_headers(api_key))

        for response in (single, fanout):
            assert response.status_code == 200
            assert response.json()["choices"][0]["message"]["content"].count(_SOURCE_MARKER) == 2
        assert "top_k" not in pipeline.search.call_args.kwargs
        assert "top_k" not in pipeline.search_many.call_args.kwargs
