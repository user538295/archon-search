"""Task 6.4 — ACL and namespace isolation integration tests.

Verifies three security-critical properties:

1. Namespace isolation: a bearer token scoped to namespace-B cannot see data
   ingested under namespace-A, even when both collections share the same
   LanceDB instance.

2. Collection list isolation: GET /collections scoped to namespace-A returns
   only namespace-A collections; namespace-B collections are absent.

3. ACL field persistence: chunk-level ``acl`` values survive an
   export → import round-trip and appear verbatim in POST /search responses.

Run with:
    uv run pytest tests/integration/test_acl_namespace_integration.py -v --no-cov
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import archon_search.jobs.scheduler as _scheduler_module
from archon_search.types import JobStatus
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
    """SHA-256 hex digest of *path* — stable, collision-free doc identifier."""
    return hashlib.sha256(path.encode()).hexdigest()


def _make_chunk(doc_id: str, idx: int, text: str, source_path: str, *, acl: list[str] | None = None):
    """Build a ``ChunkRecord`` for direct store injection."""
    from archon_search._types import ChunkRecord, normalize_iso_utc

    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=source_path,
        indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
        acl=acl,
    )


async def _inject_chunks_ns(
    store,
    col: str,
    chunks,
    *,
    embedding_model: str,
    namespace: str,
) -> None:
    """Ensure *col* exists and inject *chunks* with a CollectionMeta row for *namespace*.

    ``store.ingest_chunks`` populates LanceDB rows but does not create a
    ``CollectionMeta`` row.  The search route validates that the collection exists
    via ``get_all_collections_meta``; without a meta row it raises
    ``CollectionNotFoundError`` (404).  We create the meta row here so HTTP search
    works against directly-injected data.
    """
    from archon_search.collection_meta import CollectionMeta

    await store.ensure_collection(col, _EMBEDDING_DIM)
    await store.ingest_chunks(col, chunks, namespace=namespace)
    await store.rebuild_fts_index(col)
    meta = CollectionMeta(
        name=col,
        active_embedding_model=embedding_model,
        doc_count=len({c.doc_id for c in chunks}),
        chunk_count=len(chunks),
        namespace=namespace,
    )
    await store.update_collection_meta(meta)


def _poll_job_store(job_store, job_id: str, *, timeout_s: float = 30.0):
    """Poll ``job_store.get(job_id)`` until a terminal status is reached.

    Uses the in-process job_store rather than GET /jobs/{id} to avoid the
    known Pydantic validation issue where ``result: dict`` serialises as 500
    through the REST endpoint.
    """
    terminal = {JobStatus.DONE, JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = job_store.get(job_id)
        if job is None:
            pytest.fail(f"job {job_id!r} not found in job_store")
        if job.status in terminal:
            return job
        time.sleep(0.1)
    pytest.fail(f"job {job_id!r} did not reach terminal state within {timeout_s}s")


# ---------------------------------------------------------------------------
# Test 1 — Namespace isolation: cross-namespace HTTP search returns 404
# ---------------------------------------------------------------------------


def test_cross_namespace_search_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection registered under namespace-A is not visible to namespace-B callers.

    The search route calls ``get_collection_meta(name, namespace=ns)`` and returns
    404 when the meta row does not belong to the requesting namespace.  This test
    verifies that the HTTP layer enforces that namespace boundary, preventing
    data-exposure via a cross-namespace search request.

    Note: namespace isolation here is enforced at the ``CollectionMeta.namespace``
    level (not at the chunk-level ACL field).  Chunks are injected with ``acl=None``
    (fail-open); the isolation property being tested is the collection-level namespace
    gate, not chunk-level ACL filtering.

    Flow:
    1. Create a two-namespace app: key_a → ns-a, key_b → ns-b.
    2. Inject chunks directly into a collection registered under ns-a.
    3. POST /search with the ns-b bearer token against the same collection name.
    4. Assert 404 — namespace-B cannot find a collection owned by namespace-A.

    A regression that changed 404 → 200+data would be a data-exposure bug.
    """
    key_a = secrets.token_hex(32)
    key_b = secrets.token_hex(32)
    namespaces = {key_a: "ns-a", key_b: "ns-b"}

    col = "test-ns-isolation"
    doc_path = "/data/ns-a/secret.md"
    doc_id = _doc_id(doc_path)

    with make_real_app(tmp_path, monkeypatch, namespaces=namespaces) as (client, cfg, _default_key):
        store = client.app.state.search_store

        # Inject data into ns-a.
        asyncio.run(
            _inject_chunks_ns(
                store,
                col,
                [_make_chunk(doc_id, 0, "namespace-a secret document content", doc_path)],
                embedding_model=cfg.embedding_model,
                namespace="ns-a",
            )
        )

        # Search as ns-b — the collection is invisible to ns-b.
        # The search route calls get_collection_meta(col, namespace="ns-b") which
        # returns None (meta row belongs to ns-a), so the route returns 404.
        resp = client.post(
            "/search",
            json={"collection": col, "query": "namespace-a secret document content", "top_k": 10},
            headers=_auth(key_b),
        )
        assert resp.status_code == 404, (
            f"expected 404 (namespace-B cannot access namespace-A collection); "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Test 2 — ACL field survives export → import round-trip
# ---------------------------------------------------------------------------


def test_acl_field_survives_export_import_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunk-level ``acl`` values are preserved through an export/import cycle.

    Flow:
    1. Create a two-namespace app: key_a → ns-a.  Inject chunks with
       ``acl=['ns-a']`` into a collection registered under ns-a.
    2. POST /collections/{col_src}/export as namespace-A.
       Poll until DONE; capture archive_path.
    3. POST /collections/{col_dst}/import with that archive_path as namespace-A.
       Poll until DONE.
    4. POST /search against the imported collection as namespace-A.
       ACL filter passes because the request namespace matches the chunk ACL.
    5. Assert all returned chunks have ``acl == ['ns-a']``.

    Verifies that ACL metadata is written into the export archive and faithfully
    restored on import, so that ACL enforcement decisions after import are based
    on the original ACL values.

    Note: chunks with ``acl=['ns-a']`` are filtered out for any namespace other
    than 'ns-a', so the test must search as namespace-A to get results.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    key_a = secrets.token_hex(32)
    namespaces = {key_a: "ns-a"}

    col_src = "test-acl-export-src"
    col_dst = "test-acl-import-dst"
    doc_path = "/data/acl/doc.md"
    chunk_doc_id = _doc_id(doc_path)
    acl_value = ["ns-a"]

    with make_real_app(tmp_path, monkeypatch, namespaces=namespaces) as (client, cfg, _default_key):
        store = client.app.state.search_store
        job_store = client.app.state.job_store

        # Step 1 — inject chunks with explicit ACL under namespace ns-a.
        asyncio.run(
            _inject_chunks_ns(
                store,
                col_src,
                [
                    _make_chunk(
                        chunk_doc_id,
                        0,
                        "acl round-trip test document content",
                        doc_path,
                        acl=acl_value,
                    )
                ],
                embedding_model=cfg.embedding_model,
                namespace="ns-a",
            )
        )

        # Step 2 — export as namespace-A.
        export_resp = client.post(
            f"/collections/{col_src}/export",
            json={},
            headers=_auth(key_a),
        )
        assert export_resp.status_code == 202, (
            f"POST /collections/{col_src}/export expected 202, "
            f"got {export_resp.status_code}: {export_resp.text}"
        )
        export_job_id = export_resp.json()["job_id"]
        export_job = _poll_job_store(job_store, export_job_id)
        assert export_job.status == JobStatus.DONE, (
            f"export job failed: {export_job.error!r}"
        )
        archive_path = export_job.result["archive_path"]

        # Step 3 — import into a new collection as namespace-A.
        import_resp = client.post(
            f"/collections/{col_dst}/import",
            json={"path": archive_path},
            headers=_auth(key_a),
        )
        assert import_resp.status_code == 202, (
            f"POST /collections/{col_dst}/import expected 202, "
            f"got {import_resp.status_code}: {import_resp.text}"
        )
        import_job_id = import_resp.json()["job_id"]
        import_job = _poll_job_store(job_store, import_job_id)
        assert import_job.status == JobStatus.DONE, (
            f"import job failed: {import_job.error!r}"
        )

        # Step 4 — search the imported collection as namespace-A.
        # ACL filter passes: chunk acl=['ns-a'] and request namespace='ns-a'.
        search_resp = client.post(
            "/search",
            json={"collection": col_dst, "query": "acl round-trip test document content", "top_k": 10},
            headers=_auth(key_a),
        )
        assert search_resp.status_code == 200, (
            f"search against imported collection failed: "
            f"{search_resp.status_code}: {search_resp.text}"
        )
        results = search_resp.json()["results"]
        assert results, "imported collection returned zero results; ACL round-trip cannot be verified"

        # Step 5 — verify ACL field on each returned chunk.
        for item in results:
            item_acl = item.get("acl")
            assert item_acl == acl_value, (
                f"ACL field mismatch after export/import round-trip: "
                f"expected {acl_value!r}, got {item_acl!r} (chunk: {item.get('chunk_id')!r})"
            )


# ---------------------------------------------------------------------------
# Test 3 — GET /collections scoped to ns-A does not expose ns-B collections
# ---------------------------------------------------------------------------


def test_namespace_a_cannot_list_namespace_b_collections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /collections with namespace-A auth must not return namespace-B collections.

    Flow:
    1. Create a two-namespace app: key_a → ns-a, key_b → ns-b.
    2. Create real directories for col-a (ns-a) and col-b (ns-b).
       Register both in cfg.collections and inject meta under their respective namespaces.
       The GET /collections route only lists paths registered in config.collections;
       without that registration a directly-injected collection is never returned.
    3. GET /collections as namespace-A.
    4. Assert the namespace-A collection name IS present.
    5. Assert the namespace-B collection name is absent.

    A broken namespace filter on the collection list endpoint would let a caller
    enumerate collections belonging to a different namespace — an information-
    disclosure regression.
    """
    key_a = secrets.token_hex(32)
    key_b = secrets.token_hex(32)
    namespaces = {key_a: "ns-a", key_b: "ns-b"}

    # Create real directories so path_to_collection_name can be derived from the last component.
    dir_a = tmp_path / "colnslista"   # → collection name "colnslista"
    dir_b = tmp_path / "colnslistb"   # → collection name "colnslistb"
    dir_a.mkdir()
    dir_b.mkdir()

    col_a = "colnslista"
    col_b = "colnslistb"

    doc_a_id = _doc_id("/data/ns-a/list-a.md")
    doc_b_id = _doc_id("/data/ns-b/list-b.md")

    with make_real_app(tmp_path, monkeypatch, namespaces=namespaces) as (client, cfg, _default_key):
        store = client.app.state.search_store

        # Register both paths in config.collections so they appear in GET /collections.
        cfg.collections.append(str(dir_a))
        cfg.collections.append(str(dir_b))

        # Inject meta rows under their respective namespaces.
        asyncio.run(
            _inject_chunks_ns(
                store,
                col_a,
                [_make_chunk(doc_a_id, 0, "namespace a listing test document", "/data/ns-a/list-a.md")],
                embedding_model=cfg.embedding_model,
                namespace="ns-a",
            )
        )
        asyncio.run(
            _inject_chunks_ns(
                store,
                col_b,
                [_make_chunk(doc_b_id, 0, "namespace b listing test document", "/data/ns-b/list-b.md")],
                embedding_model=cfg.embedding_model,
                namespace="ns-b",
            )
        )

        # GET /collections as namespace-A.
        resp = client.get("/collections", headers=_auth(key_a))
        assert resp.status_code == 200, (
            f"GET /collections expected 200, got {resp.status_code}: {resp.text}"
        )
        # The route returns a JSON array of CollectionSummary objects directly.
        collection_names = {c["name"] for c in resp.json()}

        assert col_a in collection_names, (
            f"Expected ns-a collection {col_a!r} to appear in namespace-A listing; "
            f"got: {collection_names}"
        )
        assert col_b not in collection_names, (
            f"Namespace isolation breach: ns-b collection {col_b!r} is visible to "
            f"namespace-A bearer token; collections listed: {collection_names}"
        )
