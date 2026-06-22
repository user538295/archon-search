"""T-1: E2e — issue a key via POST /keys, authenticate a search request, confirm 200 + namespace.

Covers S1 (POST /keys returns 201 with all required fields) and S2 (managed key auth works;
request.state.namespace = key's namespace).

The namespace stamp is verified indirectly via namespace isolation: a collection registered
under 'managed-ns' (created by the managed key's namespace) is visible when using the managed
token but NOT visible when using a different-namespace key.  This proves that
``request.state.namespace`` was resolved to 'managed-ns' for the managed token.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

# Embedding dimension used by the stub fastembed backend.
_EMBEDDING_DIM = 384
_COL = "test-d7-t1-managed-key"
_DOC_PATH = "/data/d7-t1/doc.md"
_DOC_TEXT = "managed key e2e test document content"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _doc_id(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


async def _inject_chunks_ns(store, col: str, embedding_model: str, namespace: str) -> None:
    """Create a collection + insert one chunk under *namespace* so /search can find it.

    NOTE: ``embedding_model`` is intentionally NOT passed to ``store.ingest_chunks``.
    ``store.ingest_chunks`` accepts a ``namespace`` kwarg but does NOT forward it to
    ``_do_update_meta_on_add``.  When ``embedding_model=None``, ``_do_update_meta_on_add``
    short-circuits and creates no meta row — so the explicit ``update_collection_meta``
    call below is the sole source of truth for the namespace.  If you ever add
    ``embedding_model=...`` to the ``ingest_chunks`` call without fixing that propagation
    gap, ``_do_update_meta_on_add`` will create a wrong-namespace meta row under namespace
    'default', breaking the isolation assertion at Step 4.
    """
    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.collection_meta import CollectionMeta

    doc_id = _doc_id(_DOC_PATH)
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=_DOC_TEXT,
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=_DOC_PATH,
        indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
        acl=None,
    )

    await store.ensure_collection(col, _EMBEDDING_DIM)
    await store.ingest_chunks(col, [chunk], namespace=namespace)
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
# T-1: e2e — issue key via REST, authenticate, assert 200 + correct namespace
# ---------------------------------------------------------------------------


def test_e2e_issue_key_and_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a managed key via POST /keys; use it to authenticate a search request (S1, S2).

    Namespace correctness is proved via isolation: the managed key's namespace
    owns the test collection, so:
    - search with managed token → 200 with non-empty results (namespace matches collection's)
    - search with default api key → 404 (default namespace cannot see managed-ns collection)
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, default_api_key):
        store = client.app.state.search_store

        # Step 1 — issue a managed key via the REST endpoint.
        resp = client.post(
            "/keys",
            headers=_auth(default_api_key),
            json={"namespace": "managed-ns", "label": "t1-test-key"},
        )
        assert resp.status_code == 201, f"POST /keys failed: {resp.status_code} {resp.text}"
        body = resp.json()

        # S1 assertions: response must include all required fields with correct types/values.
        assert isinstance(body.get("id"), str) and body["id"], (
            f"'id' must be a non-empty string, got: {body.get('id')!r}"
        )
        assert isinstance(body.get("token"), str) and body["token"], (
            f"'token' must be a non-empty string, got: {body.get('token')!r}"
        )
        assert body["namespace"] == "managed-ns", f"namespace mismatch: {body['namespace']}"
        assert body.get("label") == "t1-test-key", (
            f"'label' must be echoed back, got: {body.get('label')!r}"
        )
        # created_at must be a parseable ISO-8601 datetime
        created_at = datetime.fromisoformat(body["created_at"])
        assert created_at.tzinfo is not None, "created_at must be timezone-aware"
        assert body["status"] == "active", f"status should be 'active', got {body['status']}"
        assert body["expires_at"] is None, "expires_at should be null when not specified"

        managed_token = body["token"]

        # Step 2 — insert a collection under 'managed-ns' so we can exercise /search.
        asyncio.run(
            _inject_chunks_ns(store, _COL, cfg.embedding_model, namespace="managed-ns")
        )

        # Step 3 — authenticate a search request with the managed token (S2).
        search_resp = client.post(
            "/search",
            headers=_auth(managed_token),
            json={"collection": _COL, "query": "managed key e2e test", "top_k": 5},
        )
        assert search_resp.status_code == 200, (
            f"Managed key should authenticate the search request; "
            f"got {search_resp.status_code}: {search_resp.text}"
        )
        results = search_resp.json().get("results", [])
        assert len(results) >= 1, (
            "Search with managed key should return at least one result (non-empty confirms the "
            "collection is accessible). Step 4 below (default key gets 404 for the same "
            f"collection) provides the namespace-isolation proof. Got: {results}"
        )

        # Step 4 — verify namespace stamp: the default key (namespace='default')
        # must NOT be able to see the 'managed-ns' collection → proves the managed
        # token resolved to 'managed-ns', not 'default'.
        isolation_resp = client.post(
            "/search",
            headers=_auth(default_api_key),
            json={"collection": _COL, "query": "managed key e2e test", "top_k": 5},
        )
        assert isolation_resp.status_code == 404, (
            f"Default key (namespace='default') should not see 'managed-ns' collection; "
            f"got {isolation_resp.status_code}: {isolation_resp.text}"
        )
