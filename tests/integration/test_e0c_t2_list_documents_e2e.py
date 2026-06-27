"""E0c / T-2 — E2e tests for L4 pagination: full ingest-then-paginate flow and performance.

Tests:
- Full pagination walk: ingest 155 docs, page through all 4 pages (3 full + 1 partial of 5)
  with limit=50, collect all doc_ids, assert all 155 retrieved and last page has null next_cursor.
- Deleted cursor: ingest 20 docs, delete one, use its doc_id directly as cursor — no 4xx,
  response resumes from first doc_id sorting after the deleted cursor.
- Large-collection performance (S17): inject 5 001 docs via batched direct store insert,
  time three consecutive page requests, assert each page responds within 5 s.

Scenarios covered (tested here): S1, S2, S3, S4, S17
S5, S6, S7 covered by tests/integration/test_e0c_be6_list_documents_endpoint.py
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

# Embedding dimension for the stub fastembed backend (384-dim zeros).
_EMBEDDING_DIM = 384

# Maximum time per page for the large-collection performance test (seconds).
_MAX_PAGE_LATENCY_S = 5.0

# Number of documents for the large-collection performance test.
_PERF_DOC_COUNT = 5_001


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _doc_id(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def _make_chunk(doc_id: str, chunk_idx: int, text: str, source_path: str):
    from archon_search._types import ChunkRecord, normalize_iso_utc

    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{chunk_idx:06d}",
        text=text,
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=source_path,
        indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
        acl=None,
    )


async def _inject_docs(store, col: str, n: int, *, namespace: str, prefix: str = "doc") -> list[str]:
    """Inject N documents (one chunk each) directly into the store.

    Returns the sorted list of doc_ids (sorted by doc_id ascending, matching store order).
    """
    from archon_search.collection_meta import CollectionMeta

    await store.ensure_collection(col, _EMBEDDING_DIM)

    chunks = []
    doc_ids = []
    for i in range(n):
        path = f"/fake/{prefix}_{i:06d}.txt"
        did = _doc_id(path)
        chunks.append(_make_chunk(did, 0, f"document {i}", path))
        doc_ids.append(did)

    # Single batch insert for efficiency.
    await store.ingest_chunks(col, chunks, namespace=namespace)
    await store.rebuild_fts_index(col)

    meta = CollectionMeta(
        name=col,
        active_embedding_model="test-model",
        doc_count=n,
        chunk_count=n,
        namespace=namespace,
    )
    await store.update_collection_meta(meta)

    return sorted(doc_ids)


async def _inject_docs_batched(
    store,
    col: str,
    n: int,
    *,
    namespace: str,
    batch_size: int = 500,
    prefix: str = "perf",
) -> list[str]:
    """Inject N documents (one chunk each) in batches — suitable for large n.

    Returns the sorted list of doc_ids.
    """
    from archon_search.collection_meta import CollectionMeta

    await store.ensure_collection(col, _EMBEDDING_DIM)

    doc_ids = []
    all_chunks = []
    for i in range(n):
        path = f"/fake/{prefix}_{i:07d}.txt"
        did = _doc_id(path)
        all_chunks.append(_make_chunk(did, 0, f"document {i}", path))
        doc_ids.append(did)

    for batch_start in range(0, n, batch_size):
        batch = all_chunks[batch_start : batch_start + batch_size]
        await store.ingest_chunks(col, batch, namespace=namespace)

    meta = CollectionMeta(
        name=col,
        active_embedding_model="test-model",
        doc_count=n,
        chunk_count=n,
        namespace=namespace,
    )
    await store.update_collection_meta(meta)

    return sorted(doc_ids)


def _list_page(client, col: str, api_key: str, *, limit: int, cursor: str | None = None) -> dict:
    """GET /collections/{col}/documents with optional cursor; assert 200 and return JSON."""
    params: dict = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    resp = client.get(
        f"/collections/{col}/documents",
        params=params,
        headers=_auth(api_key),
    )
    assert resp.status_code == 200, (
        f"GET /collections/{col}/documents?limit={limit}&cursor={cursor!r} "
        f"returned {resp.status_code}: {resp.text}"
    )
    return resp.json()


# ---------------------------------------------------------------------------
# S1–S3: Full pagination walk — 150 docs, limit=50, collect all pages
# ---------------------------------------------------------------------------


def test_e2e_list_documents_full_pagination_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest 155 docs, walk all 4 pages with limit=50, assert all 155 retrieved (S1–S3).

    Uses 155 (not a multiple of 50) to exercise the partial last-page case.

    Verifies:
    - Pages 1–3: 50 items each, next_cursor set, total=155.
    - Page 4 (last, partial): 5 items, next_cursor null, total=155.
    - No overlap between pages; items within each page are sorted by doc_id ascending.
    - All 155 doc_ids collected across pages match the injected set.
    """
    col = "t2-full-flow"
    n_docs = 155
    page_size = 50

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.search_store
        all_expected_ids = asyncio.run(
            _inject_docs(store, col, n_docs, namespace="default")
        )

        collected_ids: list[str] = []
        cursor: str | None = None
        page_num = 0

        while True:
            page_num += 1
            data = _list_page(client, col, api_key, limit=page_size, cursor=cursor)

            assert "items" in data, f"page {page_num}: 'items' missing"
            assert "next_cursor" in data, f"page {page_num}: 'next_cursor' missing"
            assert "total" in data, f"page {page_num}: 'total' missing"
            assert data["total"] == n_docs, (
                f"page {page_num}: expected total={n_docs}, got {data['total']}"
            )

            page_ids = [item["doc_id"] for item in data["items"]]
            assert len(page_ids) > 0, f"page {page_num}: must not be empty before last page"

            # No page may contain duplicate doc_ids within itself.
            assert len(page_ids) == len(set(page_ids)), (
                f"page {page_num}: duplicate doc_ids in page: {page_ids}"
            )
            # No page may overlap with already-collected doc_ids.
            overlap = set(page_ids) & set(collected_ids)
            assert not overlap, (
                f"page {page_num}: overlaps with previous pages: {overlap}"
            )
            assert page_ids == sorted(page_ids), (
                f"page {page_num}: items must be sorted by doc_id ascending, got: {page_ids}"
            )

            collected_ids.extend(page_ids)
            cursor = data["next_cursor"]

            if cursor is None:
                break  # Last page reached

        assert len(collected_ids) == n_docs, (
            f"Expected {n_docs} unique docs across all pages, got {len(collected_ids)}"
        )
        assert set(collected_ids) == set(all_expected_ids), (
            "Collected doc_ids do not match the full set of injected doc_ids"
        )
        assert collected_ids == sorted(collected_ids), (
            "doc_ids collected across all pages must be in ascending order (pagination contract)"
        )
        assert page_num == math.ceil(n_docs / page_size), (
            f"Expected {math.ceil(n_docs / page_size)} pages, walked {page_num}"
        )


# ---------------------------------------------------------------------------
# S4: Deleted cursor — use deleted doc's doc_id directly as cursor; no 4xx
# ---------------------------------------------------------------------------


def test_e2e_list_documents_deleted_cursor_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest 20 docs, delete one, use its doc_id as cursor directly — no 4xx (S4).

    Verifies that the cursor referencing a deleted document silently resumes
    from the first doc_id that sorts strictly after the deleted cursor value.
    The deleted document must not appear in the result.
    """
    col = "t2-deleted-cursor"
    n_docs = 20

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.search_store
        sorted_ids = asyncio.run(
            _inject_docs(store, col, n_docs, namespace="default")
        )

        # Pick a doc in the middle so there are docs sorting after it.
        target_idx = 9  # 10th doc in sorted order (0-indexed)
        deleted_doc_id = sorted_ids[target_idx]
        docs_expected_after_cursor = sorted_ids[target_idx + 1 :]

        # Delete the target document via the pipeline.
        pipeline_obj = client.app.state.pipeline
        asyncio.run(
            pipeline_obj.delete_document(deleted_doc_id, col, namespace="default")
        )

        # Use the deleted doc's doc_id directly as the cursor — must not 4xx.
        resp = client.get(
            f"/collections/{col}/documents",
            params={"cursor": deleted_doc_id, "limit": n_docs},  # large limit to get all remaining
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 with a stale cursor, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        returned_ids = [item["doc_id"] for item in data["items"]]

        # Deleted document must not appear in results.
        assert deleted_doc_id not in returned_ids, (
            "Deleted document must not appear when its doc_id is used as cursor"
        )

        # Results must match docs that sort strictly after the deleted cursor value.
        assert returned_ids == docs_expected_after_cursor, (
            f"Expected docs after cursor {deleted_doc_id!r}:\n"
            f"  expected: {docs_expected_after_cursor}\n"
            f"  got:      {returned_ids}"
        )


def test_e2e_list_documents_cursor_past_all_docs_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cursor sorting after all doc_ids returns empty items and null next_cursor (S4 edge case).

    SHA-256 hex doc_ids use only [0-9a-f]. A cursor of 'z' * 64 sorts after every
    possible hex string, so no documents can appear after it.
    """
    col = "t2-cursor-past-end"
    n_docs = 5

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_docs(store, col, n_docs, namespace="default")
        )

        # Use a cursor that sorts after all possible SHA-256 hex doc_ids.
        beyond_all_cursor = "z" * 64

        resp = client.get(
            f"/collections/{col}/documents",
            params={"cursor": beyond_all_cursor, "limit": 50},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 with beyond-range cursor, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["items"] == [], (
            f"Expected empty items when cursor sorts past all docs, got: {data['items']}"
        )
        assert data["next_cursor"] is None, (
            f"Expected null next_cursor for empty result, got: {data['next_cursor']!r}"
        )
        assert data["total"] == n_docs, (
            f"Expected total={n_docs} regardless of empty page, got: {data['total']}"
        )


# ---------------------------------------------------------------------------
# S17: Large-collection performance — 5 001 docs, each page < 5 s
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.xdist_group("benchmark")
def test_e2e_list_documents_large_collection_performance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 001-doc collection: time three page requests, assert each responds within 5 s (S17).

    Uses batched direct store insertion to avoid the ingest-pipeline overhead.
    Three pages are timed with limit=200 (25 pages total in the full collection):
    - page 1 (no cursor)
    - page 2 (cursor from page 1)
    - page 3 (cursor from page 2)

    Each page must respond in under _MAX_PAGE_LATENCY_S seconds.
    """
    col = "t2-perf-large"
    page_size = 200

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_docs_batched(store, col, _PERF_DOC_COUNT, namespace="default")
        )

        cursor: str | None = None
        for page_num in range(1, 4):
            t0 = time.perf_counter()
            data = _list_page(client, col, api_key, limit=page_size, cursor=cursor)
            elapsed = time.perf_counter() - t0

            assert elapsed < _MAX_PAGE_LATENCY_S, (
                f"Page {page_num} took {elapsed:.2f} s — exceeds {_MAX_PAGE_LATENCY_S} s limit"
            )
            assert len(data["items"]) == page_size, (
                f"Page {page_num}: expected {page_size} items, got {len(data['items'])}"
            )
            assert data["total"] == _PERF_DOC_COUNT, (
                f"Page {page_num}: expected total={_PERF_DOC_COUNT}, got {data['total']}"
            )

            cursor = data["next_cursor"]
            assert cursor is not None, (
                f"Page {page_num}: expected next_cursor for a non-last page, got null"
            )
