"""``SearchService.search`` over a REAL pipeline and the REAL ``RAGFusionGenerator``: a failed RAG Fusion generation reaches
the outcome as a fixed ``expansion_warning`` on the single and the multi path, a good one does not, and the warning never
carries the query.

The model host is a scripted ``LlmClient`` (no network); everything between it and the outcome is the production code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from archon_search.config import SearchConfig
from archon_search.llm_client.protocol import LlmCallFailure, LlmCallTimeout
from archon_search.rag_fusion import RAGFusionGenerator
from archon_search.services.authz import AuthIdentity
from archon_search.services.search_command import SearchCommand
from archon_search.services.search_service import SearchOutcome, SearchService
from tests._hyde_stubs import ScriptedLlmClient
from tests.integration.conftest import make_real_pipeline

pytestmark = pytest.mark.integration

_CALLER = AuthIdentity(namespace="default", admin=False)
_COLLECTION = "rag-fusion-failure"
_QUERY = "zebra-quokka-7731 searchable document"
_FAILED = "RAG Fusion expansion failed"
_TIMED_OUT = "RAG Fusion timed out"
_VARIANTS = 2
_PATHS = pytest.mark.parametrize("multi", [False, True], ids=["single", "multi"])


async def _search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: ScriptedLlmClient, *, multi: bool, rag_fusion: bool = True
) -> SearchOutcome:
    _store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    doc = tmp_path / "doc.md"
    doc.write_text("# Search content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, _COLLECTION, embedder=pipeline._global_embedder)
    # A cold embedder makes the search lexical-only and skips RAG Fusion; the stub backend is what "warm" reads.
    pipeline._global_embedder._backend.is_warm = True
    config = SearchConfig()
    config.rag_fusion.enabled = True
    service = SearchService(pipeline, config, rag_fusion_generator=RAGFusionGenerator(config.rag_fusion, client))
    selection: dict[str, Any] = {"collections": (_COLLECTION,)} if multi else {"collection": _COLLECTION}
    return await service.search(SearchCommand(query=_QUERY, rag_fusion=rag_fusion, **selection), _CALLER)


@_PATHS
@pytest.mark.parametrize(
    ("failure", "warning"),
    [
        (LlmCallFailure("ollama", "cannot connect to the server"), _FAILED),
        (LlmCallTimeout("ollama", "no answer within 10s"), _TIMED_OUT),
    ],
    ids=["provider-failure", "timeout"],
)
async def test_a_failed_generation_is_reported_as_the_expansion_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool, failure: LlmCallFailure, warning: str
) -> None:
    client = ScriptedLlmClient(failure=failure)

    outcome = await _search(tmp_path, monkeypatch, client, multi=multi)

    assert len(client.requests) == 1
    assert outcome.expansion_warning == warning
    assert outcome.expansion_used is False
    assert (outcome.rag_fusion_attempted, outcome.rag_fusion_applied, outcome.rag_fusion_queries_used) == (True, False, 0)
    assert outcome.results, "the single query is still searched"


@_PATHS
async def test_an_answer_without_a_valid_variant_is_reported_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool
) -> None:
    outcome = await _search(tmp_path, monkeypatch, ScriptedLlmClient(reply="  \n  "), multi=multi)

    assert outcome.expansion_warning == _FAILED
    assert (outcome.rag_fusion_attempted, outcome.rag_fusion_applied) == (True, False)


@_PATHS
async def test_a_good_generation_has_no_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool) -> None:
    outcome = await _search(tmp_path, monkeypatch, ScriptedLlmClient(reply="variant one\nvariant two"), multi=multi)

    assert outcome.expansion_warning is None
    assert outcome.expansion_used is True
    assert (outcome.rag_fusion_attempted, outcome.rag_fusion_applied, outcome.rag_fusion_queries_used) == (
        True,
        True,
        _VARIANTS,
    )


@_PATHS
async def test_a_search_that_did_not_ask_for_rag_fusion_has_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool
) -> None:
    client = ScriptedLlmClient(failure=LlmCallFailure("ollama", "cannot connect to the server"))

    outcome = await _search(tmp_path, monkeypatch, client, multi=multi, rag_fusion=False)

    assert client.requests == []
    assert outcome.expansion_warning is None
    assert outcome.rag_fusion_attempted is False


@_PATHS
async def test_the_warning_never_carries_the_query_even_when_the_server_echoes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool
) -> None:
    client = ScriptedLlmClient(failure=LlmCallFailure("ollama", f"bad request: {_QUERY}"))

    outcome = await _search(tmp_path, monkeypatch, client, multi=multi)

    assert outcome.expansion_warning == _FAILED
    assert "zebra-quokka-7731" not in repr(outcome.expansion_warning)
