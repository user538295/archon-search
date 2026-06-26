"""BE-7: Pipeline ingest_file() collects ACL warnings from resolve_acl().

Integration tests using real pipeline + LanceDB store with stub embedder.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_pipeline_acl.py)
# ---------------------------------------------------------------------------


def _make_pipeline(tmp_path: Path):
    """Return a SearchPipeline wired to a real LanceDB store with stub embedder/chunker/parser."""
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord
    from archon_search.pipeline import SearchPipeline
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")

    embedder = MagicMock()
    embedder.embedding_dim = 4
    embedder.model_name = "stub"

    async def _embed(texts):
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    async def _embed_one(text):
        return [0.0, 0.0, 0.0, 0.0]

    embedder.embed = _embed
    embedder.embed_one = _embed_one

    reranker = MagicMock()

    class _StubChunker:
        def chunk(
            self,
            text: str,
            doc_id: str,
            source_path: str,
            *,
            file_type: str = "",
            updated_at: str = "",
            ingested_by: str = "cli",
            language: str = "",
        ) -> list[ChunkRecord]:
            now = datetime.now(timezone.utc).isoformat()
            parts = [text[i : i + 200] for i in range(0, len(text), 200)] if text else []
            return [
                ChunkRecord(
                    doc_id=doc_id,
                    chunk_id="",
                    text=part,
                    vector=[],
                    source_path=source_path,
                    indexed_at=now,
                    file_type=file_type,
                    updated_at=updated_at,
                    ingested_by=ingested_by,  # type: ignore[arg-type]
                    language=language,
                )
                for part in parts
            ]

    class _StubParser:
        async def parse(self, path: Path) -> str:
            return path.read_text(encoding="utf-8", errors="replace")

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=_StubChunker(),
        parser=_StubParser(),
        top_k_retrieve=5,
        top_k_return=3,
    )
    return pipeline, store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_oversized_acl_sidecar_populates_warnings(tmp_path: Path) -> None:
    """Ingesting a file whose .acl sidecar exceeds 64 KB populates IngestResult.warnings."""
    pipeline, store = _make_pipeline(tmp_path)
    await store.connect()
    try:
        col = "test-col"
        await store.ensure_collection(col, embedding_dim=4)

        doc = tmp_path / "doc.md"
        doc.write_text("Hello world content for testing ACL warnings.\n")

        # Write an oversized sidecar (> 65536 bytes)
        sidecar = tmp_path / "doc.md.acl"
        sidecar.write_bytes(b"tenantA\n" + b"x" * 65537)

        result = await pipeline.ingest_file(doc, col, embedder=pipeline._global_embedder)

        assert result.warnings, "Expected non-empty warnings for oversized ACL sidecar"
        assert any("64 KB" in w or "exceeds" in w.lower() for w in result.warnings)
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_ingest_normal_acl_sidecar_no_warnings(tmp_path: Path) -> None:
    """Ingesting a file with a valid (≤ 64 KB) .acl sidecar produces no warnings."""
    pipeline, store = _make_pipeline(tmp_path)
    await store.connect()
    try:
        col = "test-col"
        await store.ensure_collection(col, embedding_dim=4)

        doc = tmp_path / "doc.md"
        doc.write_text("Hello world content.\n")

        sidecar = tmp_path / "doc.md.acl"
        sidecar.write_text("tenantA\n")

        result = await pipeline.ingest_file(doc, col, embedder=pipeline._global_embedder)

        assert result.warnings == [], f"Expected no warnings for valid sidecar, got: {result.warnings}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_ingest_oversized_acl_sidecar_populates_warnings_on_store_busy_error(
    tmp_path: Path,
) -> None:
    """Warnings are included in IngestResult even when StoreBusyError aborts the persist step."""
    from archon_search.store import StoreBusyError

    pipeline, store = _make_pipeline(tmp_path)
    await store.connect()
    try:
        col = "test-col"
        await store.ensure_collection(col, embedding_dim=4)

        doc = tmp_path / "doc.md"
        doc.write_text("Content for store-busy warning test.\n")

        sidecar = tmp_path / "doc.md.acl"
        sidecar.write_bytes(b"tenantA\n" + b"x" * 65537)

        with patch.object(store, "delete_document", new=AsyncMock(side_effect=StoreBusyError("busy"))):
            result = await pipeline.ingest_file(doc, col, embedder=pipeline._global_embedder)

        assert result.status == "error", f"Expected error status, got: {result.status}"
        assert result.warnings, "Expected non-empty warnings even when StoreBusyError occurs"
        assert any("64 KB" in w or "exceeds" in w.lower() for w in result.warnings)
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_ingest_no_sidecar_no_warnings(tmp_path: Path) -> None:
    """Ingesting a file with no .acl sidecar produces no warnings."""
    pipeline, store = _make_pipeline(tmp_path)
    await store.connect()
    try:
        col = "test-col"
        await store.ensure_collection(col, embedding_dim=4)

        doc = tmp_path / "doc.md"
        doc.write_text("Hello world content.\n")

        result = await pipeline.ingest_file(doc, col, embedder=pipeline._global_embedder)

        assert result.warnings == [], f"Expected no warnings with no sidecar, got: {result.warnings}"
    finally:
        await store.disconnect()
