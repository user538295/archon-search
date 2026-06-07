"""C4 Task 6.2 — integration tests for HyDE query expansion over the full HTTP stack.

Uses the real ``create_app()`` + ``TestClient`` pattern (matching existing integration
tests in tests/server/test_routes_search.py) with a real LanceDB store but mocks
``HyDEGenerator.generate`` to return a fixed vector, avoiding real Anthropic API calls.

Run with:
    uv run pytest tests/test_integration_hyde.py -m integration --no-cov -q
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search._types import ChunkRecord
from archon_search.collection_meta import CollectionMeta
from archon_search.config import HyDEConfig, SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.store import SearchStore

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VECTOR_DIM = 384  # Must match the stub fastembed dimension (zeros(384))
_FIXED_HYDE_VECTOR: list[float] = [0.1] * _VECTOR_DIM
_COLLECTION = "hydecol"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ingest_chunk(tmp_path: Path) -> None:
    """Create a LanceDB store, ingest one chunk, and disconnect."""
    db_path = str(tmp_path / "search")
    chunk = ChunkRecord(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000000",
        text="hello world documentation",
        vector=[0.0] * _VECTOR_DIM,
        source_path="/docs/hello.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )
    store = SearchStore(db_path)
    await store.connect()
    await store.ensure_collection(_COLLECTION, _VECTOR_DIM)
    await store.ingest_chunks(_COLLECTION, [chunk])
    await store.update_collection_meta(
        CollectionMeta(
            name=_COLLECTION,
            active_embedding_model="BAAI/bge-small-en-v1.5",
            namespace="default",
        )
    )
    await store.disconnect()


def _make_app(tmp_path: Path, *, hyde_enabled: bool = True):  # type: ignore[return]
    """Return a create_app() instance with HyDE optionally enabled."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.hyde = HyDEConfig(enabled=hyde_enabled)
    job_store = JobStore(path=tmp_path / "jobs.json")
    return create_app(config, job_store)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_hyde_true_returns_200_and_hyde_applied_true(tmp_path: Path) -> None:
    """POST /search with hyde=true, enabled generator → 200 with hyde_applied=True and results present."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, hyde_enabled=True)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    with patch(
        "archon_search.hyde.HyDEGenerator.generate",
        new=AsyncMock(return_value=_FIXED_HYDE_VECTOR),
    ):
        with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
            response = client.post(
                "/search",
                json={"query": "hello world", "collection": _COLLECTION, "hyde": True},
            )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["hyde_applied"] is True
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_search_hyde_false_returns_200_and_hyde_applied_false(tmp_path: Path) -> None:
    """POST /search with hyde=false → 200 with hyde_applied=False (generator not invoked)."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, hyde_enabled=True)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
        response = client.post(
            "/search",
            json={"query": "hello world", "collection": _COLLECTION, "hyde": False},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["hyde_applied"] is False
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_explain_hyde_true_returns_200_and_hyde_applied_true(tmp_path: Path) -> None:
    """POST /explain with hyde=true, enabled generator → 200 with hyde_applied=True."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, hyde_enabled=True)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    with patch(
        "archon_search.hyde.HyDEGenerator.generate",
        new=AsyncMock(return_value=_FIXED_HYDE_VECTOR),
    ):
        with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
            response = client.post(
                "/explain",
                json={"query": "hello world", "collection": _COLLECTION, "hyde": True},
            )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["hyde_applied"] is True
