"""RagPipeline — orchestrates ingest, search, and context retrieval (FEAT-019 Task 4.1)."""
from __future__ import annotations

import hashlib
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from archon.rag._types import ChunkRecord, CollectionInfo, DocumentInfo, IngestResult, SearchResult
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
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
        ".tiff", ".tif",
        ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        ".db", ".sqlite", ".pkl", ".npy", ".npz", ".h5", ".hdf5",
        ".parquet", ".feather", ".wasm", ".dat", ".lance",
    }
)


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
        self, path: Path, collection: str, rebuild_fts: bool = True
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

        # Embed
        vectors = await self._embedder.embed([r.text for r in records])
        for record, vector in zip(records, vectors):
            record.vector = vector

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

        if not files:
            return []

        results: list[IngestResult] = []
        total = len(files)

        for done_count, file_path in enumerate(files, start=1):
            result = await self.ingest_file(file_path, collection, rebuild_fts=False)
            results.append(result)
            if progress_cb is not None:
                ret = progress_cb(done_count, total)
                if inspect.isawaitable(ret):
                    await ret

        # Rebuild FTS once if at least one successful ingest
        if any(r.status == "ok" for r in results):
            await self.store.rebuild_fts_index(collection)

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
