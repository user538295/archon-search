"""``SearchService.search`` single versus multi path over a real store and pipeline (the embedder cache is a stub)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.services.authz import AuthIdentity
from archon_search.services.search_command import SearchCommand
from archon_search.services.search_service import SearchService
from tests.integration.conftest import make_real_pipeline

pytestmark = pytest.mark.integration

_COLLECTION = "svc-col"
_COLLECTION_MODEL = "collection-pinned-model"
_QUERY = "quick brown fox"
_EXCLUSION_REASON = "embedding_model_mismatch"
_DOC_TEXT = "The quick brown fox jumps over the lazy dog.\nA quick brown fox is quick, and the brown fox is a fox.\n"


async def test_same_collection_single_and_multi_path_may_differ_when_its_model_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    try:
        doc = tmp_path / "fox.md"
        doc.write_text(_DOC_TEXT, encoding="utf-8")
        assert (await pipeline.ingest_file(doc, _COLLECTION, embedder=pipeline.global_embedder)).status == "ok"
        meta = await pipeline.get_collection_meta(_COLLECTION, namespace=DEFAULT_NAMESPACE)
        assert meta is not None
        await store.update_collection_meta(replace(meta, active_embedding_model=_COLLECTION_MODEL))
        global_model = pipeline.global_embedder.model_name
        cache = MagicMock()
        cache.get_or_load = AsyncMock(return_value=pipeline.global_embedder)
        config = SearchConfig()
        config.embedding_model = global_model
        service = SearchService(pipeline, config, embedder_cache=cache)
        caller = AuthIdentity(namespace=DEFAULT_NAMESPACE, admin=True)

        single = await service.search(SearchCommand(query=_QUERY, collection=_COLLECTION), caller)
        multi = await service.search(SearchCommand(query=_QUERY, collection=None, collections=(_COLLECTION,)), caller)
    finally:
        await store.disconnect()

    assert single.results, "the single path searches the collection with its own embedder"
    assert single.embedding_model == _COLLECTION_MODEL
    assert multi.results == ()
    assert [(e.name, e.reason) for e in multi.excluded_collections] == [(_COLLECTION, _EXCLUSION_REASON)]
    assert multi.embedding_model == global_model
