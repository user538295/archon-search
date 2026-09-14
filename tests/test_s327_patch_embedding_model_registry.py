"""S327: ``PATCH /collections/{name}`` must not 422 a documented, shipped model.

``UserManual/50_ingestion_and_collections.md`` (:200) documents "indexed data +
different model" as *setting* ``pending_embedding_model`` with
``needs_reindex = true`` — the only documented 422 on this surface is an unknown
model name at creation (:189). ``BAAI/bge-base-en-v1.5`` is a shipped profile
model (``UserManual/10_installation.md:106``), so the branch must apply to it.

``validate_embedding_model`` resolves a known model's dimension from
``TextEmbedding.list_supported_models()`` without downloading anything. These
tests feed it the descriptor shape the *real* fastembed emits (key ``"model"``,
verified against the installed fastembed registry), so a PATCH on a host whose
model cache is cold still takes the registry fast path instead of an
unreachable download probe.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.model_validation import validate_embedding_model
from archon_search.server.app import create_app
from archon_search.sync import path_to_collection_name

ACTIVE_MODEL = "BAAI/bge-small-en-v1.5"
REQUESTED_MODEL = "BAAI/bge-base-en-v1.5"
REQUESTED_MODEL_DIM = 768

# Descriptor shape emitted by the installed fastembed: the model name lives
# under "model" (keys: model, sources, model_file, description, license,
# size_in_GB, additional_files, dim, tasks).
REAL_REGISTRY = [
    {"model": ACTIVE_MODEL, "dim": 384, "size_in_GB": 0.067, "tasks": {}},
    {"model": REQUESTED_MODEL, "dim": REQUESTED_MODEL_DIM, "size_in_GB": 0.21, "tasks": {}},
]


def _uncached_model_embedder() -> MagicMock:
    """An embedder for a model that is not in the local cache (cold host)."""
    embedder = MagicMock()
    # AsyncMock raises TimeoutError when awaited — mirrors the real wait_for
    # timeout without leaking an inner coroutine (RuntimeWarning).
    embedder.embed = AsyncMock(side_effect=asyncio.TimeoutError)
    return embedder


@pytest.mark.asyncio
async def test_known_model_dim_comes_from_registry_without_download() -> None:
    """A registry-listed model resolves to its dim with no download probe."""
    with patch("archon_search.model_validation.TextEmbedding") as mock_te, patch(
        "archon_search.model_validation.make_embedder",
        side_effect=AssertionError("must not probe a model the registry already describes"),
    ):
        mock_te.list_supported_models.return_value = REAL_REGISTRY
        dim = await validate_embedding_model(REQUESTED_MODEL)

    assert dim == REQUESTED_MODEL_DIM


def test_patch_indexed_collection_to_shipped_model_sets_pending(
    tmp_path: Path,
) -> None:
    """PATCH to a shipped model on an indexed collection is branch 1, not 422."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model=ACTIVE_MODEL)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.count_documents = AsyncMock(return_value=1)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch("archon_search.model_validation.TextEmbedding") as mock_te, patch(
        "archon_search.model_validation.make_embedder",
        return_value=_uncached_model_embedder(),
    ):
        mock_te.list_supported_models.return_value = REAL_REGISTRY
        response = client.patch(f"/collections/{name}", json={"embedding_model": REQUESTED_MODEL})

    assert response.status_code == 200, (
        f"PATCH to the documented model {REQUESTED_MODEL} was rejected "
        f"{response.status_code}: {response.json()}"
    )
    body = response.json()
    assert body["pending_embedding_model"] == REQUESTED_MODEL
    assert body["needs_reindex"] is True
    assert body["active_embedding_model"] == ACTIVE_MODEL
    assert body["reindex_job_id"] is None
