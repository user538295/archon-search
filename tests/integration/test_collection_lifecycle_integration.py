"""Task 6.5 — Collection lifecycle integration tests.

Verifies three collection deletion scenarios via the full HTTP stack:

1. Successful DELETE drops the LanceDB table and removes the meta row,
   verified both via store API and on-disk directory absence.

2. Attempting to DELETE a collection owned by namespace-A while authenticated
   as namespace-B returns 403 or 404 (namespace ownership check).

3. Attempting to DELETE a pinned-only collection (in pinned_collections but NOT
   in collections) returns 409 (conflict).

Run with:
    uv run pytest tests/integration/test_collection_lifecycle_integration.py -v --no-cov
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

# Embedding dimension used by the stub fastembed backend (384-dim zeros).
_EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _doc_id(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def _make_chunk(doc_id: str, idx: int, text: str, source_path: str):
    """Build a ChunkRecord for direct store injection."""
    from archon_search._types import ChunkRecord, normalize_iso_utc

    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=source_path,
        indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
        acl=None,
    )


async def _inject_collection(store, col: str, text: str, path: str, *, namespace: str, embedding_model: str) -> None:
    """Ensure col exists, inject one chunk, write meta so HTTP routes see it."""
    from archon_search.collection_meta import CollectionMeta

    doc_id = _doc_id(path)
    chunks = [_make_chunk(doc_id, 0, text, path)]

    await store.ensure_collection(col, _EMBEDDING_DIM)
    await store.ingest_chunks(col, chunks, namespace=namespace)
    await store.rebuild_fts_index(col)
    meta = CollectionMeta(
        name=col,
        active_embedding_model=embedding_model,
        doc_count=1,
        chunk_count=1,
        namespace=namespace,
    )
    await store.update_collection_meta(meta)


# ---------------------------------------------------------------------------
# Test 1 — Successful DELETE drops table and removes meta row
# ---------------------------------------------------------------------------


def test_delete_collection_drops_table_and_removes_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE /collections/{name} removes both LanceDB table and meta row.

    Flow:
    1. Create a real collection directory and inject data so the store and
       HTTP layer know about it.
    2. Register the directory in config.collections so the DELETE route can
       resolve the name → path mapping.
    3. DELETE /collections/{col_name}. Assert 200.
    4. GET /collections — assert col_name is absent.
    5. Store.list_collections() — assert col_name absent (no stale lock/meta).
    6. Assert the LanceDB collection directory is absent on disk.

    Both checks (4 & 5) are needed: drop_collection could fail silently while
    the meta row is deleted, causing false-positive on the HTTP list alone.

    Note: path_to_collection_name sanitizes hyphens to underscores, so the
    directory name must use underscores to produce a matching collection name.
    """
    # Use underscores — path_to_collection_name replaces hyphens with underscores.
    col_name = "lifecycle_delete_test"
    col_dir = tmp_path / col_name
    col_dir.mkdir()

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store
        db_path = Path(cfg.db_path)

        # Register directory so the DELETE route can resolve col_name → path.
        cfg.collections.append(str(col_dir))

        # Inject data via store directly (no HTTP ingest needed here).
        asyncio.run(
            _inject_collection(
                store,
                col_name,
                "lifecycle delete test document",
                f"/data/{col_name}/doc.md",
                namespace="default",
                embedding_model=cfg.embedding_model,
            )
        )

        # Verify it appears in GET /collections before delete.
        list_resp = client.get("/collections", headers=_auth(api_key))
        assert list_resp.status_code == 200
        names_before = {c["name"] for c in list_resp.json()}
        assert col_name in names_before, (
            f"Collection {col_name!r} not listed before delete; got {names_before}"
        )

        # DELETE the collection.
        del_resp = client.delete(f"/collections/{col_name}", headers=_auth(api_key))
        assert del_resp.status_code == 200, (
            f"DELETE /collections/{col_name} expected 200, "
            f"got {del_resp.status_code}: {del_resp.text}"
        )
        assert del_resp.json().get("deleted") is True

        # POST-delete: collection must be absent from HTTP listing.
        list_resp2 = client.get("/collections", headers=_auth(api_key))
        assert list_resp2.status_code == 200
        names_after = {c["name"] for c in list_resp2.json()}
        assert col_name not in names_after, (
            f"Collection {col_name!r} still present in GET /collections after delete; "
            f"got {names_after}"
        )

        # POST-delete: store.list_collections() must not include the dropped table.
        store_collections = asyncio.run(store.list_collections())
        store_names = {c.name for c in store_collections}
        assert col_name not in store_names, (
            f"Collection {col_name!r} still in store.list_collections() after delete; "
            f"got {store_names}"
        )

        # POST-delete: LanceDB directory on disk must be gone.
        # LanceDB stores each table as "{col_name}.lance/" inside db_path.
        lance_dir = db_path / f"{col_name}.lance"
        assert not lance_dir.exists(), (
            f"LanceDB directory {lance_dir} still present on disk after DELETE"
        )


# ---------------------------------------------------------------------------
# Test 2 — DELETE from wrong namespace returns 403 or 404
# ---------------------------------------------------------------------------


def test_delete_collection_wrong_namespace_returns_403_or_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A namespace-B token cannot delete a collection owned by namespace-A.

    Flow:
    1. Create a two-namespace app: key_a → ns-a, key_b → ns-b.
    2. Register a collection directory and inject data under ns-a.
    3. Attempt DELETE /collections/{name} with namespace-B bearer token.
    4. Assert status is 403 or 404 (namespace ownership check at
       routes_collections.py:272-275 returns 404 when get_collection_meta
       returns None for the requesting namespace).

    A 200 response here would be a security regression: namespace-B could
    destroy data owned by namespace-A.
    """
    key_a = secrets.token_hex(32)
    key_b = secrets.token_hex(32)
    namespaces = {key_a: "ns-a", key_b: "ns-b"}

    # Use underscores — path_to_collection_name replaces hyphens with underscores.
    col_name = "lifecycle_ns_delete_test"
    col_dir = tmp_path / col_name
    col_dir.mkdir()

    with make_real_app(tmp_path, monkeypatch, namespaces=namespaces) as (client, cfg, _default_key):
        store = client.app.state.search_store

        # Register path so the route can resolve col_name → path.
        cfg.collections.append(str(col_dir))

        # Inject collection under namespace-A.
        asyncio.run(
            _inject_collection(
                store,
                col_name,
                "namespace-a owned document",
                f"/data/ns-a/{col_name}/doc.md",
                namespace="ns-a",
                embedding_model=cfg.embedding_model,
            )
        )

        # Confirm collection is visible to namespace-A.
        list_a = client.get("/collections", headers=_auth(key_a))
        assert list_a.status_code == 200
        assert any(c["name"] == col_name for c in list_a.json()), (
            f"Collection {col_name!r} not visible to ns-a before cross-ns delete attempt"
        )

        # Attempt DELETE as namespace-B — must be rejected.
        del_resp = client.delete(f"/collections/{col_name}", headers=_auth(key_b))
        assert del_resp.status_code in {403, 404}, (
            f"Expected 403 or 404 when namespace-B attempts to delete a namespace-A "
            f"collection; got {del_resp.status_code}: {del_resp.text}"
        )

        # Verify the collection still exists for namespace-A (no destructive side-effect).
        list_a_after = client.get("/collections", headers=_auth(key_a))
        assert list_a_after.status_code == 200
        assert any(c["name"] == col_name for c in list_a_after.json()), (
            f"Collection {col_name!r} was removed from ns-a after a rejected "
            f"cross-namespace delete attempt — data-loss bug"
        )


# ---------------------------------------------------------------------------
# Test 3 — DELETE pinned-only collection returns 409
# ---------------------------------------------------------------------------


def test_delete_pinned_only_collection_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE /collections/{name} returns 409 when the collection is pinned-only.

    A pinned-only collection has its path in config.pinned_collections but NOT
    in config.collections.  The route checks this at routes_collections.py:286-294
    and raises HTTPException(409).

    Flow:
    1. Create a collection directory and inject data so the LanceDB table and
       meta row exist (required: the route checks collection existence first at
       lines 272-275, then the pinned-only check at 286-294).
    2. Register the path in config.pinned_collections ONLY — not in
       config.collections.
    3. DELETE /collections/{col_name}. Assert 409.
    4. Verify the collection is still present (no destructive side-effect).
    """
    # Use underscores — path_to_collection_name replaces hyphens with underscores.
    col_name = "lifecycle_pinned_test"
    col_dir = tmp_path / col_name
    col_dir.mkdir()

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store

        # Inject data first — the route checks existence before checking pinned-only.
        asyncio.run(
            _inject_collection(
                store,
                col_name,
                "pinned-only collection document",
                f"/data/{col_name}/doc.md",
                namespace="default",
                embedding_model=cfg.embedding_model,
            )
        )

        # Register ONLY in pinned_collections — NOT in collections.
        cfg.pinned_collections.append(str(col_dir))
        # Ensure it is NOT in collections (exact basename match, not substring).
        cfg.collections = [p for p in cfg.collections if Path(p).name != col_name]

        # Attempt DELETE — must return 409 (pinned-only protection).
        del_resp = client.delete(f"/collections/{col_name}", headers=_auth(api_key))
        assert del_resp.status_code == 409, (
            f"Expected 409 for pinned-only collection delete attempt; "
            f"got {del_resp.status_code}: {del_resp.text}"
        )
        assert "pinned" in del_resp.json().get("detail", "").lower(), (
            f"Expected 'pinned' in error detail; got: {del_resp.json()}"
        )

        # Collection must still exist in the store after the rejected delete.
        store_collections = asyncio.run(store.list_collections())
        store_names = {c.name for c in store_collections}
        assert col_name in store_names, (
            f"Pinned collection {col_name!r} was removed from the store despite "
            f"the 409 rejection — data-loss bug"
        )
