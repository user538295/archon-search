"""bug-080 e2e — collection responses report the live chunk count, not a stale cache.

Every collection response (``GET /collections``, ``GET /collections/{name}``,
``PATCH /collections/{name}``, ``GET /status``) historically returned a
hardcoded ``chunk_count: 0``.  The fix reads ``count_chunks()`` from the store
at request time.

These tests inject a real collection with THREE chunks but persist a
deliberately WRONG ``meta.chunk_count`` sentinel.  Asserting the API returns
``3`` (the true row count) — never the sentinel — proves the handlers read the
live store count and not the cached meta value.

Run with:
    uv run pytest tests/integration/test_chunk_count_live_e2e.py -v --no-cov
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_EMBEDDING_DIM = 384
_REAL_CHUNKS = 3
# A wrong-on-purpose cached value: if any handler reads meta.chunk_count instead
# of the live store count, the assertions below will surface this sentinel.
_STALE_META_CHUNK_COUNT = 999


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _make_chunk(doc_id: str, idx: int, source_path: str):
    from archon_search._types import ChunkRecord, normalize_iso_utc

    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=f"chunk number {idx}",
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=source_path,
        indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
        acl=None,
    )


async def _inject_with_stale_meta(store, col: str, path: str, *, namespace: str, embedding_model: str) -> None:
    """Inject _REAL_CHUNKS chunks but persist a wrong meta.chunk_count sentinel."""
    from archon_search.collection_meta import CollectionMeta

    doc_id = hashlib.sha256(path.encode()).hexdigest()
    chunks = [_make_chunk(doc_id, i, path) for i in range(_REAL_CHUNKS)]

    await store.ensure_collection(col, _EMBEDDING_DIM)
    await store.ingest_chunks(col, chunks, namespace=namespace)
    await store.rebuild_fts_index(col)
    meta = CollectionMeta(
        name=col,
        active_embedding_model=embedding_model,
        doc_count=1,
        chunk_count=_STALE_META_CHUNK_COUNT,  # deliberately wrong
        namespace=namespace,
    )
    await store.update_collection_meta(meta)


def test_chunk_count_is_live_across_all_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four collection responses report the true row count (3), never the stale sentinel."""
    from archon_search.sync import path_to_collection_name

    col_name = "chunkcount_live"
    col_dir = tmp_path / col_name
    col_dir.mkdir()

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store
        cfg.collections.append(str(col_dir))
        resolved_name = path_to_collection_name(str(col_dir))

        asyncio.run(
            _inject_with_stale_meta(
                store,
                resolved_name,
                f"/data/{col_name}/doc.md",
                namespace="default",
                embedding_model=cfg.embedding_model,
            )
        )

        # Sanity: the true store count really is _REAL_CHUNKS, and it differs
        # from the persisted meta sentinel — otherwise the test proves nothing.
        assert asyncio.run(store.count_chunks(resolved_name, namespace="default")) == _REAL_CHUNKS
        assert _REAL_CHUNKS != _STALE_META_CHUNK_COUNT

        # 1. GET /collections
        list_resp = client.get("/collections", headers=_auth(api_key))
        assert list_resp.status_code == 200
        entry = next(c for c in list_resp.json() if c["name"] == resolved_name)
        assert entry["chunk_count"] == _REAL_CHUNKS

        # 2. GET /collections/{name}
        info_resp = client.get(f"/collections/{resolved_name}", headers=_auth(api_key))
        assert info_resp.status_code == 200
        assert info_resp.json()["chunk_count"] == _REAL_CHUNKS

        # 3. PATCH /collections/{name} (ttl-only PATCH — no embedding-model validation)
        patch_resp = client.patch(
            f"/collections/{resolved_name}",
            json={"default_ttl_seconds": 3600},
            headers=_auth(api_key),
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["chunk_count"] == _REAL_CHUNKS

        # 4. GET /status
        status_resp = client.get("/status", headers=_auth(api_key))
        assert status_resp.status_code == 200
        status_col = next(c for c in status_resp.json()["collections"] if c["name"] == resolved_name)
        assert status_col["chunk_count"] == _REAL_CHUNKS


def test_empty_collection_reports_zero_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge case from the brief: an existing empty collection reports chunk_count=0, never raises."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.sync import path_to_collection_name

    col_name = "chunkcount_empty"
    col_dir = tmp_path / col_name
    col_dir.mkdir()

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store
        cfg.collections.append(str(col_dir))
        resolved_name = path_to_collection_name(str(col_dir))

        # Create an empty table + meta with a wrong-on-purpose sentinel.
        async def _seed() -> None:
            await store.ensure_collection(resolved_name, _EMBEDDING_DIM)
            await store.update_collection_meta(
                CollectionMeta(
                    name=resolved_name,
                    active_embedding_model=cfg.embedding_model,
                    chunk_count=_STALE_META_CHUNK_COUNT,
                    namespace="default",
                )
            )

        asyncio.run(_seed())

        info_resp = client.get(f"/collections/{resolved_name}", headers=_auth(api_key))
        assert info_resp.status_code == 200
        assert info_resp.json()["chunk_count"] == 0
