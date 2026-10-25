"""The lifespan's embedder cache serves the default model from the global embedder, not a second copy.

The pipeline's global embedder (centroid recompute, routing, fan-out) and the embedder cache
(single-collection search, ingest, MCP) both resolve the default ``embedding_model``. Warm-up used
to build each separately, so two copies of the default model sat in memory for the process
lifetime: ~130 MB for ``bge-small``, 1.3 GB for ``bge-large``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.embedder import Embedder
from archon_search.jobs.store import JobStore
from archon_search.model_validation import ModelValidationResult
from archon_search.server import app as app_module
from archon_search.server.app import create_app
from archon_search.store import SearchStore
from tests.test_app import wait_for_models_check_to_settle, wait_for_warmup


@pytest.fixture(autouse=True)
def _offline_store_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    validation_ok = ModelValidationResult(embedder_ok=True, reranker_ok=True)
    monkeypatch.setattr(app_module, "validate_models_async", AsyncMock(return_value=validation_ok))
    monkeypatch.setattr(SearchStore, "connect", AsyncMock())
    monkeypatch.setattr(SearchStore, "_run_startup_migrations", AsyncMock())
    monkeypatch.setattr(SearchStore, "disconnect", AsyncMock())
    monkeypatch.setattr(SearchStore, "ping", AsyncMock(return_value=True))


def test_cache_hands_out_the_global_embedder_for_the_default_model(tmp_path: Path) -> None:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))

    with TestClient(app) as client:
        wait_for_warmup(app)
        wait_for_models_check_to_settle(app)
        cache = app.state.embedder_cache
        served: Embedder = client.portal.call(cache.get_or_load, config.embedding_model)

    assert served is app.state.pipeline.global_embedder
    assert cache.cached_models() == [config.embedding_model]
