"""The 503 ``Retry-After`` of an embedder that is not ready comes from the one service constant."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.services.service_error import RETRY_AFTER_SECONDS

_RETRY_AFTER_HEADER = "Retry-After"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col", namespace="default"))
    pipeline.warmup_models = AsyncMock(return_value=True)
    app.state.pipeline = pipeline
    cache = MagicMock()
    cache.get_or_load = AsyncMock(side_effect=EmbedderNotReadyError("still loading"))
    app.state.embedder_cache = cache
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def test_embedder_not_ready_503_carries_retry_after_from_the_single_constant(client: TestClient) -> None:
    """The REST route (``/search``) and the app-level handler (``/explain``) emit the same header value."""
    search = client.post("/search", json={"collection": "col", "query": "test"})
    explain = client.post("/explain", json={"collection": "col", "query": "test"})

    assert search.status_code == explain.status_code == 503
    assert search.headers[_RETRY_AFTER_HEADER] == explain.headers[_RETRY_AFTER_HEADER] == str(RETRY_AFTER_SECONDS)
