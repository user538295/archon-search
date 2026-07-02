"""Tests for E2a BE-4: REST route changes for TTL, scoping, and expiry endpoint.

Unit tests (1–8) use a mock search_store; integration tests (9–13) use
make_real_app + direct store injection.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.server.routes_jobs import IngestRequest
from archon_search.server.schemas import (
    DocumentInfoItem,
    ExpiringChunkItem,
    ExpiringChunksResponse,
    PatchCollectionBody,
)
from archon_search.store import STORE_SCHEMA_VERSION
from archon_search.sync import path_to_collection_name

from tests.integration.conftest import make_real_app

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Named constants (avoid magic numbers)
# ---------------------------------------------------------------------------

INT32_MAX: int = 2**31 - 1
MAX_SCOPE_ITEM_LEN: int = 255
MAX_SCOPE_LIST_ITEMS: int = 100
SECONDS_PER_HOUR: int = 3_600
MIN_WITHIN_HOURS: int = 1
MAX_WITHIN_HOURS: int = 8_760
EMBEDDING_DIM: int = 4  # stub embedder uses 4-dim vectors

pytestmark = pytest.mark.integration  # integration marker for the file's slow tests

# ---------------------------------------------------------------------------
# Helpers for unit tests
# ---------------------------------------------------------------------------


def _make_be4_patch_app(
    tmp_path: Path,
    tmp_store: JobStore,
    *,
    meta: CollectionMeta,
    count_chunks: int = 5,
    stored_dim: int | None = None,
) -> tuple[TestClient, MagicMock]:
    """Create a TestClient backed by a mock search_store for PATCH /collections/{name} tests."""
    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=count_chunks)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=stored_dim)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.query_expiring_chunks = AsyncMock(return_value=([], None))
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return client, mock_store


def _make_expiring_app(
    tmp_path: Path,
    tmp_store: JobStore,
    *,
    meta: CollectionMeta | None = None,
    query_result: tuple[list[dict], str | None] = ([], None),
) -> tuple[TestClient, MagicMock]:
    """Create a TestClient for GET /collections/{name}/expiring tests."""
    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.query_expiring_chunks = AsyncMock(return_value=query_result)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return client, mock_store


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _make_chunk(
    doc_id: str,
    chunk_idx: int,
    text: str,
    source_path: str,
    *,
    expires_at: str | None = None,
    scopes: list[str] | None = None,
):
    from archon_search._types import ChunkRecord, normalize_iso_utc

    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{chunk_idx:06d}",
        text=text,
        vector=[0.1, 0.2, 0.3, 0.4],
        source_path=source_path,
        indexed_at=normalize_iso_utc(datetime.now(UTC)),
        expires_at=expires_at,
        scopes=scopes,
    )


async def _inject_chunks(
    store,
    col: str,
    chunks: list,
    *,
    namespace: str = "default",
) -> None:
    """Inject chunks directly into the store and create collection meta."""
    await store.ensure_collection(col, EMBEDDING_DIM)
    await store.ingest_chunks(col, chunks, namespace=namespace)
    meta = CollectionMeta(
        name=col,
        active_embedding_model="test-model",
        namespace=namespace,
    )
    await store.update_collection_meta(meta)


# ---------------------------------------------------------------------------
# Unit tests — 1. IngestRequest validators
# ---------------------------------------------------------------------------


def test_ingest_request_chunk_ttl_seconds_range() -> None:
    """chunk_ttl_seconds=0 and =-1 raise ValidationError (valid range is [1, INT32_MAX])."""
    from pydantic import ValidationError

    for bad_val in (0, -1):
        with pytest.raises(ValidationError):
            IngestRequest(collection="col", chunk_ttl_seconds=bad_val)


def test_ingest_request_scope_string_max_length() -> None:
    """A scope string of 256 characters raises ValidationError (max is 255)."""
    from pydantic import ValidationError

    too_long = "x" * (MAX_SCOPE_ITEM_LEN + 1)
    with pytest.raises(ValidationError):
        IngestRequest(collection="col", chunk_scopes=[too_long])


def test_ingest_request_scope_list_max_items() -> None:
    """A scope list of 101 items raises ValidationError (max is 100)."""
    from pydantic import ValidationError

    too_many = ["scope"] * (MAX_SCOPE_LIST_ITEMS + 1)
    with pytest.raises(ValidationError):
        IngestRequest(collection="col", chunk_scopes=too_many)


# ---------------------------------------------------------------------------
# Unit tests — 4. ExpiringChunksResponse schema
# ---------------------------------------------------------------------------


def test_expiring_chunks_response_schema() -> None:
    """ExpiringChunksResponse has items, next_cursor, and page_count fields."""
    item = ExpiringChunkItem(
        chunk_id="cid-001",
        doc_id="did-001",
        source_path="/tmp/doc.txt",
        expires_at="2030-01-01T00:00:00.000000Z",
    )
    resp = ExpiringChunksResponse(items=[item], next_cursor=None, page_count=1)
    assert resp.items[0].chunk_id == "cid-001"
    assert resp.next_cursor is None
    assert resp.page_count == 1


# ---------------------------------------------------------------------------
# Unit tests — 5. GET /collections/{name}/expiring validation
# ---------------------------------------------------------------------------


def test_get_expiring_within_hours_ge1(
    tmp_path: Path,
    tmp_store: JobStore,
) -> None:
    """within_hours=0 raises 422 (ge=1 constraint)."""
    meta = CollectionMeta(name="col", namespace="default")
    client, _ = _make_expiring_app(tmp_path, tmp_store, meta=meta)

    resp = client.get("/collections/col/expiring", params={"within_hours": 0})
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Unit tests — 6–8. PATCH /collections/{name} with default_ttl_seconds
# ---------------------------------------------------------------------------


def test_patch_collection_default_ttl_only_no_embedding_model_required(
    tmp_path: Path,
    tmp_store: JobStore,
) -> None:
    """PATCH with only default_ttl_seconds returns 200; no reindex; no validate_embedding_model call."""
    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    client, mock_store = _make_be4_patch_app(tmp_path, tmp_store, meta=meta, count_chunks=0)

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        side_effect=AssertionError("validate_embedding_model must NOT be called when embedding_model is absent"),
    ):
        resp = client.patch(f"/collections/{name}", json={"default_ttl_seconds": 3600})

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    mock_store.update_collection_meta.assert_called_once()
    saved = mock_store.update_collection_meta.call_args[0][0]
    assert saved.default_ttl_seconds == 3600
    # Embedding model must be untouched — no reindex
    assert saved.needs_reindex is False
    assert saved.pending_embedding_model is None


def test_patch_collection_embedding_model_only_still_works(
    tmp_path: Path,
    tmp_store: JobStore,
) -> None:
    """PATCH with only embedding_model still triggers reindex state machine correctly."""
    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    client, mock_store = _make_be4_patch_app(tmp_path, tmp_store, meta=meta, count_chunks=5)

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=768,
    ):
        resp = client.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-base-en-v1.5"})

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    mock_store.update_collection_meta.assert_called_once()
    saved = mock_store.update_collection_meta.call_args[0][0]
    assert saved.pending_embedding_model == "BAAI/bge-base-en-v1.5"
    assert saved.needs_reindex is True


def test_patch_collection_embedding_model_explicit_null_accepted(
    tmp_path: Path,
    tmp_store: JobStore,
) -> None:
    """PATCH with embedding_model=null and default_ttl_seconds: 200; TTL updated; no reindex."""
    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    client, mock_store = _make_be4_patch_app(tmp_path, tmp_store, meta=meta, count_chunks=5)

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        side_effect=AssertionError("validate_embedding_model must NOT be called when embedding_model is null"),
    ):
        resp = client.patch(
            f"/collections/{name}",
            json={"embedding_model": None, "default_ttl_seconds": 3600},
        )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    mock_store.update_collection_meta.assert_called_once()
    saved = mock_store.update_collection_meta.call_args[0][0]
    assert saved.default_ttl_seconds == 3600
    # No reindex triggered
    assert saved.needs_reindex is False
    assert saved.pending_embedding_model is None


# ---------------------------------------------------------------------------
# Fixtures reused by unit tests above
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


# ---------------------------------------------------------------------------
# Integration tests (9–13)
# ---------------------------------------------------------------------------


def test_patch_collection_default_ttl_forward_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH default_ttl_seconds does NOT retroactively apply TTL to existing chunks (S9)."""
    col_path = tmp_path / "col9"
    col_path.mkdir()
    col = path_to_collection_name(str(col_path))

    toml = f'[collections]\ncollections = ["{col_path!s}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        store = client.app.state.search_store

        # Inject 5 chunks with no expires_at (also creates collection meta)
        import hashlib

        chunks = []
        for i in range(5):
            path = f"/docs/file_{i}.txt"
            doc_id = hashlib.sha256(path.encode()).hexdigest()
            chunks.append(_make_chunk(doc_id, 0, f"text {i}", path))

        asyncio.run(_inject_chunks(store, col, chunks))

        # PATCH collection with default_ttl_seconds=3600
        resp = client.patch(
            f"/collections/{col}",
            json={"default_ttl_seconds": 3600},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"PATCH failed: {resp.status_code} {resp.text}"

        # Existing chunks must not have expires_at — GET /expiring returns 0 items
        resp2 = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": MIN_WITHIN_HOURS},
            headers=_auth(api_key),
        )
        assert resp2.status_code == 200, f"GET /expiring failed: {resp2.status_code} {resp2.text}"
        data = resp2.json()
        assert data["items"] == [], f"expected no expiring items, got {data['items']}"


def test_get_expiring_returns_correct_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunks with expires_at within 1 hour appear in GET /expiring (S10)."""
    from archon_search._types import normalize_iso_utc

    col = "be4-expiring-window"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store

        import hashlib

        soon_path = "/docs/soon.txt"
        soon_doc_id = hashlib.sha256(soon_path.encode()).hexdigest()
        # Expires 30 minutes from now — within 1-hour query window
        expires_soon = normalize_iso_utc(datetime.now(UTC) + timedelta(minutes=30))

        far_path = "/docs/far.txt"
        far_doc_id = hashlib.sha256(far_path.encode()).hexdigest()
        # Expires 3 hours from now — outside 1-hour query window
        expires_far = normalize_iso_utc(datetime.now(UTC) + timedelta(hours=3))

        chunks = [
            _make_chunk(soon_doc_id, 0, "expires soon", soon_path, expires_at=expires_soon),
            _make_chunk(far_doc_id, 0, "expires far", far_path, expires_at=expires_far),
        ]
        asyncio.run(_inject_chunks(store, col, chunks))

        resp = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": MIN_WITHIN_HOURS},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert len(data["items"]) == 1, f"expected 1 expiring item, got {len(data['items'])}: {data}"
        assert data["items"][0]["doc_id"] == soon_doc_id


def test_get_expiring_excludes_already_expired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-expired chunks (expires_at < now) are excluded from GET /expiring (S11)."""
    from archon_search._types import normalize_iso_utc

    col = "be4-already-expired"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store

        import hashlib

        # One chunk already expired 1 minute ago
        past_path = "/docs/past.txt"
        past_doc_id = hashlib.sha256(past_path.encode()).hexdigest()
        expired_at = normalize_iso_utc(datetime.now(UTC) - timedelta(minutes=1))

        # One chunk expiring in 30 minutes
        soon_path = "/docs/soon.txt"
        soon_doc_id = hashlib.sha256(soon_path.encode()).hexdigest()
        expires_soon = normalize_iso_utc(datetime.now(UTC) + timedelta(minutes=30))

        chunks = [
            _make_chunk(past_doc_id, 0, "already expired", past_path, expires_at=expired_at),
            _make_chunk(soon_doc_id, 0, "expires soon", soon_path, expires_at=expires_soon),
        ]
        asyncio.run(_inject_chunks(store, col, chunks))

        resp = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": MIN_WITHIN_HOURS},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        chunk_ids = {item["doc_id"] for item in data["items"]}
        assert past_doc_id not in chunk_ids, "already-expired chunk must not appear in /expiring"
        assert soon_doc_id in chunk_ids, "chunk expiring in 30 min must appear in /expiring"


def test_get_expiring_chunks_cursor_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor pagination: 3 expiring chunks with limit=1 yields 3 pages with correct items (S12)."""
    from archon_search._types import normalize_iso_utc

    col = "be4-expiring-pagination"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store

        import hashlib

        # 3 chunks expiring at different times within the next hour
        chunks = []
        doc_ids = []
        for i in range(3):
            path = f"/docs/page_{i}.txt"
            doc_id = hashlib.sha256(path.encode()).hexdigest()
            expires_at = normalize_iso_utc(datetime.now(UTC) + timedelta(minutes=10 + i * 10))
            chunks.append(_make_chunk(doc_id, 0, f"page {i}", path, expires_at=expires_at))
            doc_ids.append(doc_id)
        asyncio.run(_inject_chunks(store, col, chunks))

        # Page 1
        resp1 = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": MIN_WITHIN_HOURS, "limit": 1},
            headers=_auth(api_key),
        )
        assert resp1.status_code == 200, f"page 1 failed: {resp1.status_code} {resp1.text}"
        page1 = resp1.json()
        assert len(page1["items"]) == 1, f"expected 1 item on page 1, got {len(page1['items'])}"
        assert page1["next_cursor"] is not None, "page 1 must have a next_cursor"

        # Page 2
        resp2 = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": MIN_WITHIN_HOURS, "limit": 1, "cursor": page1["next_cursor"]},
            headers=_auth(api_key),
        )
        assert resp2.status_code == 200, f"page 2 failed: {resp2.status_code} {resp2.text}"
        page2 = resp2.json()
        assert len(page2["items"]) == 1, f"expected 1 item on page 2, got {len(page2['items'])}"
        assert page2["next_cursor"] is not None, "page 2 must have a next_cursor"

        # Page 3 (last page)
        resp3 = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": MIN_WITHIN_HOURS, "limit": 1, "cursor": page2["next_cursor"]},
            headers=_auth(api_key),
        )
        assert resp3.status_code == 200, f"page 3 failed: {resp3.status_code} {resp3.text}"
        page3 = resp3.json()
        assert len(page3["items"]) == 1, f"expected 1 item on page 3, got {len(page3['items'])}"
        assert page3["next_cursor"] is None, "page 3 (last) must have next_cursor=None"

        # All 3 doc_ids must appear exactly once across 3 pages
        seen = {page1["items"][0]["doc_id"], page2["items"][0]["doc_id"], page3["items"][0]["doc_id"]}
        assert seen == set(doc_ids), f"expected all 3 doc_ids across pages, got {seen}"


def test_patch_collection_default_ttl_new_ingest_picks_it_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After PATCH sets default_ttl_seconds, new ingested files get expires_at (S13)."""
    from archon_search._types import normalize_iso_utc

    col_path = tmp_path / "col13"
    col_path.mkdir()
    col = path_to_collection_name(str(col_path))

    # Text file to ingest
    doc_file = col_path / "hello.txt"
    doc_file.write_text("hello world for BE-4 TTL pickup test", encoding="utf-8")

    toml = f'[collections]\ncollections = ["{col_path!s}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        store = client.app.state.search_store
        # Create collection meta so PATCH can find it
        asyncio.run(
            store.update_collection_meta(
                CollectionMeta(name=col, namespace="default", active_embedding_model="")
            )
        )

        # PATCH: set default TTL to 1 hour
        resp_patch = client.patch(
            f"/collections/{col}",
            json={"default_ttl_seconds": SECONDS_PER_HOUR},
            headers=_auth(api_key),
        )
        assert resp_patch.status_code == 200, (
            f"PATCH failed: {resp_patch.status_code} {resp_patch.text}"
        )

        # Ingest the file (no explicit chunk_ttl_seconds — pipeline picks up default from meta)
        resp_ingest = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc_file)},
            headers=_auth(api_key),
        )
        assert resp_ingest.status_code == 202, (
            f"ingest POST failed: {resp_ingest.status_code} {resp_ingest.text}"
        )
        job_id = resp_ingest.json()["job_id"]

        import time

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=_auth(api_key))
            assert r.status_code == 200
            status = r.json()["status"]
            if status == "DONE":
                break
            if status == "FAILED":
                pytest.fail(f"ingest job failed: {r.json()}")
            time.sleep(0.1)
        else:
            pytest.fail("ingest job did not complete in 10s")

        # New chunk must appear in GET /expiring with within_hours=2 window
        resp_exp = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": 2},
            headers=_auth(api_key),
        )
        assert resp_exp.status_code == 200, (
            f"GET /expiring failed: {resp_exp.status_code} {resp_exp.text}"
        )
        data = resp_exp.json()
        assert len(data["items"]) > 0, (
            "Expected at least one expiring chunk after ingesting with collection default_ttl"
        )


# ---------------------------------------------------------------------------
# Unit tests — 2. PatchCollectionBody.default_ttl_seconds validation
# ---------------------------------------------------------------------------


def test_patch_collection_default_ttl_seconds_validation() -> None:
    """default_ttl_seconds=0 and default_ttl_seconds=-1 raise ValidationError."""
    from pydantic import ValidationError

    for bad_val in (0, -1, -999):
        with pytest.raises(ValidationError):
            PatchCollectionBody(default_ttl_seconds=bad_val)
    # None is allowed (no change semantics)
    body = PatchCollectionBody(default_ttl_seconds=None)
    assert body.default_ttl_seconds is None
    # Positive value is allowed
    body = PatchCollectionBody(default_ttl_seconds=3600)
    assert body.default_ttl_seconds == 3600
    # INT32_MAX is the upper bound
    body = PatchCollectionBody(default_ttl_seconds=INT32_MAX)
    assert body.default_ttl_seconds == INT32_MAX
    # INT32_MAX + 1 is rejected
    with pytest.raises(ValidationError):
        PatchCollectionBody(default_ttl_seconds=INT32_MAX + 1)


# ---------------------------------------------------------------------------
# Integration test — tiebreak pagination when chunks share the same expires_at
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_expiring_chunks_cursor_tiebreak_same_expires_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pagination uses chunk_id as tiebreaker when two chunks share the same expires_at (E2a BE-4)."""
    from archon_search._types import normalize_iso_utc

    col = "be4-tiebreak-col"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store

        import hashlib

        # Create two chunks with IDENTICAL expires_at (same second precision)
        shared_expires_at = normalize_iso_utc(datetime.now(UTC) + timedelta(hours=1))

        path_a = "/docs/tiebreak_a.txt"
        doc_id_a = hashlib.sha256(path_a.encode()).hexdigest()

        path_b = "/docs/tiebreak_b.txt"
        doc_id_b = hashlib.sha256(path_b.encode()).hexdigest()

        chunk_a = _make_chunk(doc_id_a, 0, "chunk a text", path_a, expires_at=shared_expires_at)
        chunk_b = _make_chunk(doc_id_b, 0, "chunk b text", path_b, expires_at=shared_expires_at)

        asyncio.run(_inject_chunks(store, col, [chunk_a, chunk_b]))

        # Page 1: limit=1, expect exactly one item
        resp1 = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": 2, "limit": 1},
            headers=_auth(api_key),
        )
        assert resp1.status_code == 200, f"page 1 failed: {resp1.status_code} {resp1.text}"
        page1 = resp1.json()
        assert len(page1["items"]) == 1, f"expected 1 item on page 1, got {page1['items']}"
        assert page1["next_cursor"] is not None, "page 1 must have a next_cursor (2nd chunk pending)"
        chunk_id_1 = page1["items"][0]["chunk_id"]

        # Page 2: follow cursor, expect the second item
        resp2 = client.get(
            f"/collections/{col}/expiring",
            params={"within_hours": 2, "limit": 1, "cursor": page1["next_cursor"]},
            headers=_auth(api_key),
        )
        assert resp2.status_code == 200, f"page 2 failed: {resp2.status_code} {resp2.text}"
        page2 = resp2.json()
        assert len(page2["items"]) == 1, f"expected 1 item on page 2, got {page2['items']}"
        assert page2["next_cursor"] is None, "page 2 (last) must have next_cursor=None"
        chunk_id_2 = page2["items"][0]["chunk_id"]

        # The two pages must return different chunks and together cover both chunks
        assert chunk_id_1 != chunk_id_2, "pages must return different chunks"
        # Ordering: chunk_id is the tiebreaker, so page 1 must have the lexicographically smaller id
        assert chunk_id_1 < chunk_id_2, (
            f"chunk_id tiebreak violated: page1={chunk_id_1!r} >= page2={chunk_id_2!r}"
        )
        # Both chunk_ids must correspond to one of our injected chunks
        expected_chunk_ids = {chunk_a.chunk_id, chunk_b.chunk_id}
        seen_chunk_ids = {chunk_id_1, chunk_id_2}
        assert seen_chunk_ids == expected_chunk_ids, (
            f"expected chunk_ids {expected_chunk_ids}, got {seen_chunk_ids}"
        )
