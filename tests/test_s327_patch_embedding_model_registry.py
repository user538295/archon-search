"""S327: ``PATCH /collections/{name}`` must not 422 a documented, shipped model.

``UserManual/50_ingestion_and_collections.md`` §"Per-collection embedding model" documents
"indexed data + different model" as *setting* ``pending_embedding_model`` with
``needs_reindex = true`` — the only documented 422 on this surface is an unknown
model name at creation. ``BAAI/bge-base-en-v1.5`` is the ``balanced`` profile's
embedder (``UserManual/10_installation.md`` §"Install profiles (C0)";
``archon_search/profiles.py``), so the branch must apply to it.

``validate_embedding_model`` resolves a supported model's dimension from the spec registry
(``archon_search.model_specs``) without loading or downloading anything, so a PATCH on a host
whose model cache is cold never waits on a download.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.model_validation import validate_embedding_model
from archon_search.server.app import create_app
from archon_search.sync import path_to_collection_name
from tests._collection_lock_stub import with_locked_collection_meta

ACTIVE_MODEL = "BAAI/bge-small-en-v1.5"
REQUESTED_MODEL = "BAAI/bge-base-en-v1.5"
REQUESTED_MODEL_DIM = 768


@pytest.mark.asyncio
async def test_known_model_dim_comes_from_the_spec_registry() -> None:
    """A supported model resolves to its dim without loading or downloading anything."""
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
    with_locked_collection_meta(mock_store)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

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
