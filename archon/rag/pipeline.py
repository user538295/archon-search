"""RagPipeline — orchestrates ingest, search, and context retrieval (FEAT-019 Task 4.1)."""
from __future__ import annotations

import hashlib
import inspect
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from archon.rag._types import ChunkRecord, CollectionInfo, DocumentInfo, IngestResult, SearchResult
from archon.rag.collection_meta import CollectionMeta
from archon.rag.description_generator import _should_regenerate, generate_description
from archon.rag.chunker import DocumentChunker
from archon.rag.embedder import Embedder, EmbedderBackend, ModelEmbedder
from archon.rag.parser import DocumentParser, ParseError
from archon.rag.reranker import ModelReranker, Reranker, RerankerBackend
from archon.rag.store import RagStore

if TYPE_CHECKING:
    from archon.config.loader import RagConfig

logger = logging.getLogger("archon")

_BINARY_EXTENSIONS = frozenset(
    {
        ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".o", ".a", ".lib",
        ".whl", ".egg", ".class",
        # Raster images (.png/.jpg/etc.) removed — OCR-indexed via _IMAGE_EXTENSIONS in parser.py
        # .gif (animated frames), .ico (favicons, tiny), .svg (XML text — handled by plain-text fallback)
        ".gif", ".ico", ".svg",
        ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        ".db", ".sqlite", ".pkl", ".npy", ".npz", ".h5", ".hdf5",
        ".parquet", ".feather", ".wasm", ".dat", ".lance",
    }
)


def _compute_centroid(vectors: list[list[float]]) -> list[float]:
    """Return element-wise mean of a list of equal-length vectors."""
    n = len(vectors)
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / n for i in range(dim)]


class RagPipeline:
    """Orchestrates document ingest, vector search, and context retrieval."""

    def __init__(
        self,
        store: RagStore,
        embedder: Embedder,
        reranker: Reranker,
        chunker: DocumentChunker,
        parser: DocumentParser,
        top_k_retrieve: int,
        top_k_return: int,
    ) -> None:
        self.store = store
        self._embedder = embedder
        self._reranker = reranker
        self._chunker = chunker
        self._parser = parser
        self._top_k_retrieve = top_k_retrieve
        self._top_k_return = top_k_return

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    async def ingest_file(
        self,
        path: Path,
        collection: str,
        rebuild_fts: bool = True,
        _vector_collector: list[list[float]] | None = None,
        _chunk_collector: list[str] | None = None,
    ) -> IngestResult:
        doc_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()

        # Parse
        try:
            markdown = await self._parser.parse(path)
        except ParseError as e:
            return IngestResult(doc_id=doc_id, chunks_created=0, status="error", error=str(e))

        # Chunk
        records = self._chunker.chunk(markdown, doc_id, str(path))
        if not records:
            return IngestResult(doc_id=doc_id, chunks_created=0, status="ok")

        # Assign sequential chunk IDs
        for idx, record in enumerate(records):
            record.chunk_id = f"{doc_id}-{idx:06d}"

        # Collect chunk texts if requested
        if _chunk_collector is not None:
            _chunk_collector.extend(r.text for r in records)

        # Embed
        vectors = await self._embedder.embed([r.text for r in records])
        for record, vector in zip(records, vectors):
            record.vector = vector
        if _vector_collector is not None:
            _vector_collector.extend(vectors)

        # Persist
        await self.store.ensure_collection(collection, self._embedder.embedding_dim)
        await self.store.delete_document(collection, doc_id)
        await self.store.ingest_chunks(collection, records)

        if rebuild_fts:
            await self.store.rebuild_fts_index(collection)

        return IngestResult(doc_id=doc_id, chunks_created=len(records), status="ok")

    async def ingest_directory(
        self,
        path: Path,
        collection: str,
        glob_pattern: str = "**/*",
        progress_cb: Callable[[int, int], None | Awaitable[None]] | None = None,
        force_regenerate_description: bool = False,
        exclude_paths: frozenset[str] | None = None,
        on_file_complete: Callable[[Path], None] | None = None,
    ) -> list[IngestResult]:
        # Collect and filter files
        files: list[Path] = []
        for file_path in path.glob(glob_pattern):
            if file_path.is_symlink():
                continue
            if not file_path.is_file():
                continue
            # Skip hidden paths
            rel_parts = file_path.relative_to(path).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            # Skip binary extensions
            if file_path.suffix.lower() in _BINARY_EXTENSIONS:
                continue
            files.append(file_path)

        files.sort()

        if exclude_paths is not None:
            files = [f for f in files if str(f) not in exclude_paths]

        if not files:
            return []

        results: list[IngestResult] = []
        total = len(files)
        all_vectors: list[list[float]] = []
        all_chunks: list[str] = []

        for done_count, file_path in enumerate(files, start=1):
            result = await self.ingest_file(
                file_path,
                collection,
                rebuild_fts=False,
                _vector_collector=all_vectors,
                _chunk_collector=all_chunks,
            )
            results.append(result)
            if on_file_complete is not None and result.status == "ok":
                on_file_complete(file_path)
            if progress_cb is not None:
                ret = progress_cb(done_count, total)
                if inspect.isawaitable(ret):
                    await ret

        # Rebuild FTS once if at least one successful ingest
        if any(r.status == "ok" for r in results):
            await self.store.rebuild_fts_index(collection)

        # Compute centroid and (conditionally) regenerate description
        if all_vectors:
            centroid = _compute_centroid(all_vectors)
            ok_results = [r for r in results if r.status == "ok"]
            batch_doc_count = len(ok_results)
            batch_chunk_count = sum(r.chunks_created for r in ok_results)

            # Read existing meta to preserve description state across ingests
            existing_meta = await self.store.get_collection_meta(collection)
            description = existing_meta.description if existing_meta else None
            described_at = existing_meta.described_at_doc_count if existing_meta else None
            last_described = existing_meta.last_described if existing_meta else None

            if force_regenerate_description or _should_regenerate(batch_doc_count, batch_chunk_count, described_at):
                new_desc = await generate_description(all_chunks, collection)
                if new_desc is not None:
                    description = new_desc
                    described_at = batch_doc_count
                    last_described = datetime.now(UTC)

            meta = CollectionMeta(
                name=collection,
                centroid=centroid,
                description=description,
                doc_count=batch_doc_count,
                chunk_count=batch_chunk_count,
                embedding_model=self._embedder.model_name,
                last_indexed=datetime.now(UTC),
                last_described=last_described,
                described_at_doc_count=described_at,
            )
            await self.store.update_collection_meta(meta)

        return results

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(self, query: str, collection: str) -> list[SearchResult]:
        vector = await self._embedder.embed_one(query)
        candidates = await self.store.hybrid_search(collection, vector, query, top_k=self._top_k_retrieve)
        return await self._reranker.rerank(query, candidates, top_k=self._top_k_return)

    async def search_with_context(
        self, query: str, collection: str, context_window: int = 1
    ) -> list[dict[str, Any]]:
        results = await self.search(query, collection)
        output: list[dict[str, Any]] = []

        for result in results:
            try:
                center_idx = int(result.chunk_id.split("-")[-1])
            except ValueError:
                logger.warning("Malformed chunk_id %r — skipping adjacent fetch", result.chunk_id)
                output.append({"result": result, "context_before": [], "context_after": []})
                continue

            neighbors = await self.store.fetch_adjacent_chunks(
                collection, result.doc_id, center_idx, context_window
            )

            context_before: list[ChunkRecord] = []
            context_after: list[ChunkRecord] = []
            for chunk in neighbors:
                try:
                    neighbor_idx = int(chunk.chunk_id.split("-")[-1])
                except ValueError:
                    logger.warning("Malformed neighbor chunk_id %r — skipping", chunk.chunk_id)
                    continue
                if neighbor_idx < center_idx:
                    context_before.append(chunk)
                else:
                    context_after.append(chunk)

            output.append({"result": result, "context_before": context_before, "context_after": context_after})

        return output

    # ------------------------------------------------------------------
    # Document management
    # ------------------------------------------------------------------

    async def delete_document(self, doc_id: str, collection: str) -> int:
        return await self.store.delete_document(collection, doc_id)

    async def list_collections(self) -> list[CollectionInfo]:
        return await self.store.list_collections()

    async def get_all_collections_meta(self) -> list[CollectionMeta]:
        return await self.store.get_all_collections_meta()

    async def get_collection_meta(self, name: str) -> CollectionMeta | None:
        return await self.store.get_collection_meta(name)

    async def list_documents(self, collection: str, limit: int = 100) -> list[DocumentInfo]:
        return await self.store.list_documents(collection, limit)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_pipeline(
    cfg: RagConfig,
    embedder_backend: EmbedderBackend | None = None,
    reranker_backend: RerankerBackend | None = None,
) -> RagPipeline:
    """Build a RagPipeline from a RagConfig.

    Does NOT call store.connect() — caller is responsible for connecting.
    """
    store = RagStore(cfg.db_path)
    _embedder_backend: EmbedderBackend = embedder_backend or ModelEmbedder(
        cfg.embedding_model,
        providers=cfg.providers,
    )
    _reranker_backend: RerankerBackend = reranker_backend or ModelReranker(
        cfg.reranker_model,
        providers=cfg.providers,
    )
    embedder = Embedder(_embedder_backend)
    reranker = Reranker(_reranker_backend)
    chunker = DocumentChunker(cfg.chunk_size)
    parser = DocumentParser()

    return RagPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=chunker,
        parser=parser,
        top_k_retrieve=cfg.top_k_retrieve,
        top_k_return=cfg.top_k_return,
    )
