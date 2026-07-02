"""BE-3: Pipeline TTL computation + scopes assignment.

Tests cover:
- Request-level chunk_ttl_seconds wins over collection default_ttl_seconds (S1)
- Collection default_ttl_seconds applies when no request TTL is given (S2)
- No expiry when all TTL sources are None (S3)
- chunk_scopes are assigned to every chunk
- Empty scopes list [] is normalised to None
- Integration: stored expires_at matches expected TTL window
- Integration: collection default TTL path via pipeline
- Integration: watcher-mode ingest (no request TTL) respects collection default
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from archon_search._types import normalize_iso_utc
from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipeline(store):
    """Build a minimal SearchPipeline for ingest tests."""
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs):
            return [0.5] * len(pairs)

    return SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


async def _read_chunks(store, col_name: str) -> list[dict]:
    """Read all raw rows from a collection chunk table via public API."""
    rows = []
    async for row in store.list_chunks_raw(col_name, DEFAULT_NAMESPACE):
        rows.append(row)
    return rows


async def _set_collection_default_ttl(store, col_name: str, ttl_seconds: int | None) -> None:
    """Insert or update collection meta with given default_ttl_seconds.

    Safe to call before the chunk table exists — update_collection_meta only
    touches _archon_collection_meta, which is independent of the chunk table.
    """
    await store.update_collection_meta(
        CollectionMeta(
            name=col_name,
            namespace=DEFAULT_NAMESPACE,
            default_ttl_seconds=ttl_seconds,
            active_embedding_model="mock-embedder",
        )
    )


def _parse_iso(s: str) -> datetime:
    """Parse normalize_iso_utc output back to a UTC datetime.

    On Python 3.11+ fromisoformat handles the trailing Z natively; we
    strip it explicitly for safety and use removesuffix (not rstrip) to
    avoid accidentally stripping characters other than a trailing Z.
    """
    return datetime.fromisoformat(s.removesuffix("Z")).replace(tzinfo=UTC)


_TOLERANCE = timedelta(seconds=30)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_md_file(tmp_path: Path) -> Path:
    """Write a small markdown file with enough text to produce at least one chunk."""
    f = tmp_path / "doc.md"
    f.write_text(
        "# TTL Test Document\n\n"
        + ("This is a paragraph for TTL testing purposes. " * 20 + "\n\n") * 3
    )
    return f


# ---------------------------------------------------------------------------
# Unit tests — TTL precedence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_request_ttl_wins_over_collection_default(
    connected_store, col_name, sample_md_file
):
    """S1: request chunk_ttl_seconds=3600 overrides collection default_ttl_seconds=7200."""
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    # Pre-populate collection meta with default TTL of 7200s.
    await _set_collection_default_ttl(connected_store, col_name, ttl_seconds=7200)

    before = datetime.now(UTC)
    await pipeline.ingest_file(
        sample_md_file,
        col_name,
        embedder=embedder,
        chunk_ttl_seconds=3600,
    )
    after = datetime.now(UTC)

    rows = await _read_chunks(connected_store, col_name)
    assert rows, "Expected at least one chunk to be ingested"

    for row in rows:
        raw = row.get("expires_at")
        assert raw is not None, "expires_at should not be None when TTL=3600"
        expires = _parse_iso(raw)
        # Should be ≈ now+3600s, NOT now+7200s
        assert before + timedelta(seconds=3600) - _TOLERANCE <= expires <= after + timedelta(seconds=3600) + _TOLERANCE, (
            f"expires_at {expires} is not within ±30s of now+3600s"
        )


@pytest.mark.asyncio
async def test_ingest_collection_default_ttl_applies_when_no_request_ttl(
    connected_store, col_name, sample_md_file
):
    """S2: when no request TTL, collection default_ttl_seconds=3600 is used."""
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    # Pre-populate collection meta with default TTL of 3600s.
    await _set_collection_default_ttl(connected_store, col_name, ttl_seconds=3600)

    before = datetime.now(UTC)
    await pipeline.ingest_file(
        sample_md_file,
        col_name,
        embedder=embedder,
        # No chunk_ttl_seconds — should fall back to collection default
    )
    after = datetime.now(UTC)

    rows = await _read_chunks(connected_store, col_name)
    assert rows, "Expected at least one chunk to be ingested"

    for row in rows:
        raw = row.get("expires_at")
        assert raw is not None, "expires_at should not be None when collection default TTL=3600"
        expires = _parse_iso(raw)
        assert before + timedelta(seconds=3600) - _TOLERANCE <= expires <= after + timedelta(seconds=3600) + _TOLERANCE, (
            f"expires_at {expires} is not within ±30s of now+3600s"
        )


@pytest.mark.asyncio
async def test_ingest_null_all_ttl_sources_no_expiry(
    connected_store, col_name, sample_md_file
):
    """S3: no request TTL and no collection default → expires_at is None."""
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    # Pre-populate meta without TTL (default_ttl_seconds=None).
    await _set_collection_default_ttl(connected_store, col_name, ttl_seconds=None)

    await pipeline.ingest_file(
        sample_md_file,
        col_name,
        embedder=embedder,
        # No chunk_ttl_seconds
    )

    rows = await _read_chunks(connected_store, col_name)
    assert rows, "Expected at least one chunk to be ingested"

    for row in rows:
        assert row.get("expires_at") is None, (
            f"expires_at should be None when no TTL is set, got {row.get('expires_at')!r}"
        )


# ---------------------------------------------------------------------------
# Unit tests — scopes assignment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_scopes_assigned_to_all_chunks(
    connected_store, col_name, sample_md_file
):
    """chunk_scopes are propagated to every stored chunk."""
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    await pipeline.ingest_file(
        sample_md_file,
        col_name,
        embedder=embedder,
        chunk_scopes=["user:alice"],
    )

    rows = await _read_chunks(connected_store, col_name)
    assert rows, "Expected at least one chunk to be ingested"

    for row in rows:
        stored = row.get("scopes")
        assert stored == ["user:alice"], (
            f"Expected scopes=['user:alice'] but got {stored!r}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_scopes", [[], None])
async def test_ingest_empty_scopes_normalization(
    connected_store, col_name, sample_md_file, chunk_scopes
):
    """Both [] and None as chunk_scopes produce scopes=None on stored chunks."""
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    await pipeline.ingest_file(
        sample_md_file,
        col_name,
        embedder=embedder,
        chunk_scopes=chunk_scopes,
    )

    rows = await _read_chunks(connected_store, col_name)
    assert rows, "Expected at least one chunk to be ingested"

    for row in rows:
        stored = row.get("scopes")
        # Pipeline normalises [] → None before write; LanceDB should store null.
        # We assert the raw value is None (not [] or any other falsy value) to prove
        # the normalisation actually happened, not just that stored is falsy.
        assert stored is None, (
            f"Expected scopes=None (raw) for chunk_scopes={chunk_scopes!r} but got {stored!r}"
        )


# ---------------------------------------------------------------------------
# Integration tests — real pipeline + store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_with_chunk_ttl_seconds_stores_expires_at(
    connected_store, col_name, sample_md_file
):
    """Integration: chunk_ttl_seconds=900 produces expires_at ≈ now+900s in the store."""
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    before = datetime.now(UTC)
    result = await pipeline.ingest_file(
        sample_md_file,
        col_name,
        embedder=embedder,
        chunk_ttl_seconds=900,
    )
    after = datetime.now(UTC)

    assert result.status == "ok", f"Ingest failed: {result.error}"
    assert result.chunks_created > 0

    rows = await _read_chunks(connected_store, col_name)
    for row in rows:
        raw = row.get("expires_at")
        assert raw is not None
        expires = _parse_iso(raw)
        assert before + timedelta(seconds=900) - _TOLERANCE <= expires <= after + timedelta(seconds=900) + _TOLERANCE


@pytest.mark.asyncio
async def test_ingest_with_collection_default_ttl_stores_expires_at(
    connected_store, col_name, sample_md_file
):
    """Integration: collection default_ttl_seconds=1800 is used when no request TTL."""
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    await _set_collection_default_ttl(connected_store, col_name, ttl_seconds=1800)

    before = datetime.now(UTC)
    result = await pipeline.ingest_file(
        sample_md_file,
        col_name,
        embedder=embedder,
    )
    after = datetime.now(UTC)

    assert result.status == "ok", f"Ingest failed: {result.error}"

    rows = await _read_chunks(connected_store, col_name)
    for row in rows:
        raw = row.get("expires_at")
        assert raw is not None
        expires = _parse_iso(raw)
        assert before + timedelta(seconds=1800) - _TOLERANCE <= expires <= after + timedelta(seconds=1800) + _TOLERANCE


@pytest.mark.asyncio
async def test_watcher_ingest_respects_collection_default_ttl(
    connected_store, col_name, sample_md_file
):
    """Watcher-mode ingest (no chunk_ttl_seconds) uses collection default_ttl_seconds=3600."""
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    await _set_collection_default_ttl(connected_store, col_name, ttl_seconds=3600)

    before = datetime.now(UTC)
    # Simulate watcher: no chunk_ttl_seconds, no chunk_scopes
    result = await pipeline.ingest_file(
        sample_md_file,
        col_name,
        embedder=embedder,
        ingested_by="watcher",
    )
    after = datetime.now(UTC)

    assert result.status == "ok", f"Ingest failed: {result.error}"

    rows = await _read_chunks(connected_store, col_name)
    assert rows, "Expected at least one chunk from watcher ingest"

    for row in rows:
        raw = row.get("expires_at")
        assert raw is not None, "Watcher ingest should inherit collection default TTL"
        expires = _parse_iso(raw)
        assert before + timedelta(seconds=3600) - _TOLERANCE <= expires <= after + timedelta(seconds=3600) + _TOLERANCE


@pytest.mark.asyncio
async def test_ingest_directory_forwards_ttl_and_scopes(
    connected_store, col_name, tmp_path
):
    """ingest_directory forwards chunk_ttl_seconds and chunk_scopes to every chunk."""
    # Create two markdown files in a temp directory
    (tmp_path / "a.md").write_text(
        "# File A\n\n" + "Content for file A. " * 30 + "\n"
    )
    (tmp_path / "b.md").write_text(
        "# File B\n\n" + "Content for file B. " * 30 + "\n"
    )

    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder

    before = datetime.now(UTC)
    results = await pipeline.ingest_directory(
        tmp_path,
        col_name,
        embedder=embedder,
        chunk_ttl_seconds=600,
        chunk_scopes=["team:eng"],
    )
    after = datetime.now(UTC)

    assert len(results) == 2, f"Expected 2 IngestResults, got {len(results)}"
    for r in results:
        assert r.status == "ok", f"Ingest failed: {r.error}"

    rows = await _read_chunks(connected_store, col_name)
    assert rows, "Expected chunks from directory ingest"

    for row in rows:
        # Every chunk should carry the TTL
        raw = row.get("expires_at")
        assert raw is not None, "expires_at should be set when chunk_ttl_seconds=600"
        expires = _parse_iso(raw)
        assert (
            before + timedelta(seconds=600) - _TOLERANCE
            <= expires
            <= after + timedelta(seconds=600) + _TOLERANCE
        ), f"expires_at {expires} is not within ±30s of now+600s"

        # Every chunk should carry the scopes
        stored_scopes = row.get("scopes")
        assert stored_scopes == ["team:eng"], (
            f"Expected scopes=['team:eng'] but got {stored_scopes!r}"
        )
