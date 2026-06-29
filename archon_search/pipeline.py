"""SearchPipeline — orchestrates ingest, search, and context retrieval."""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import inspect
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from archon_search._diagnostics import ScoredSearchCandidate
from archon_search._privacy import _query_fingerprint
from archon_search._types import ChunkRecord, CollectionInfo, DocumentInfo, ExcludedCollection, FanoutTimings, IngestedBy, IngestError, IngestResult, SearchResult, _file_exceeds_limit
from archon_search.acl import apply_acl_filter, resolve_acl
from archon_search.observability import record_stage
from archon_search.filters import SearchFilters
from archon_search.constants import DEFAULT_NAMESPACE, _INGEST_CHUNK_BATCH_SIZE
from archon_search.collection_meta import CollectionMeta
from archon_search.description_generator import MAX_SAMPLE_CHUNKS, _should_regenerate, generate_description
from archon_search.chunker import DocumentChunker
from archon_search.embedder import Embedder, EmbedderBackend, ModelEmbedder
from archon_search.code_enricher import CODE_EXTENSIONS, CodeEnricher
from archon_search.enricher import MarkdownEnricher, is_docling_source, source_subtype_for
from archon_search.parser import DocumentParser, ParseError
from archon_search.reranker import ModelReranker, Reranker, RerankerBackend
from archon_search.store import SearchStore, StoreBusyError, elementwise_sum
from archon_search.store_filters import GLOB_OVERFETCH_FACTOR
from archon_search.graph_types import ChunkInput

if TYPE_CHECKING:
    from archon_search.config import GraphConfig, RAGFusionConfig, SearchConfig
    from archon_search.graph_expander import ExpandedQuery, GraphExpander
    from archon_search.graph_extractor import GraphExtractor
    from archon_search.graph_store import GraphStore
    from archon_search.language_detector import LanguageDetector
    from archon_search.rag_fusion import RAGFusionGenerator

logger = logging.getLogger(__name__)


@dataclass
class SearchPipelineResult:
    results: list[SearchResult]
    acl_filtered: bool
    excluded_collections: list[ExcludedCollection] = field(default_factory=list)
    fanout_timings: FanoutTimings | None = None
    rag_fusion_applied: bool = False
    rag_fusion_queries_used: int = 0
    rag_fusion_attempted: bool = False
    rag_fusion_warning: str | None = None
    graph_expansion_applied: bool = False


@dataclass
class SearchWithContextResult:
    """Return type for search_with_context() — carries context results and the pipeline result."""

    results: list[dict[str, Any]]
    pipeline_result: SearchPipelineResult


class CollectionNotFoundError(Exception):
    """One or more requested collections were absent from the namespace metadata."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__(f"collections not found: {names!r}")


class FanoutTimeoutError(Exception):
    """The multi-collection fan-out exceeded its wall-clock budget."""


class MetadataLookupError(Exception):
    """Loading collection metadata for the fan-out failed."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"metadata lookup failed: {cause}")


@dataclass
class RagFusionSubQueryInfo:
    """Pipeline-internal type for per-sub-query RAG Fusion result info.

    Route handlers map this to the Pydantic ``RagFusionSubQueryResult`` schema by field name.
    variant_index=0 is the original query; 1..N are LLM-generated variants.
    """

    variant_index: int
    result_count: int
    top_doc_ids: list[str]


@dataclass
class ExplainPipelineResult:
    top_results: list[ScoredSearchCandidate]
    near_misses: list[ScoredSearchCandidate]
    acl_filtered: bool
    excluded_collections: list[ExcludedCollection] = field(default_factory=list)
    rag_fusion_applied: bool = False
    rag_fusion_queries_used: int = 0
    rag_fusion_attempted: bool = False
    rag_fusion_failure_reason: str | None = None
    rag_fusion_sub_query_results: list[RagFusionSubQueryInfo] | None = None


class ExplainMultiCollectionNoRerankError(Exception):
    """rerank=False is not permitted for multi-collection explain in v1."""

    def __init__(self) -> None:
        super().__init__("reranking cannot be disabled for multi-collection search in v1")


class ExplainStageError(Exception):
    """A pipeline stage (store / reranker) failed during explain.

    Carries the stage name so the route layer can surface a stage-specific
    500 detail without re-implementing the pipeline or inspecting tracebacks.
    """

    def __init__(self, stage: str, original: Exception) -> None:
        self.stage = stage
        self.original = original
        super().__init__(f"{stage} error: {original}")


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


# File extensions for which YAML front matter detection is safe
# (text-based formats where `---\n...\n---` at the top is genuinely front matter).
# Binary formats (PDF, DOCX, etc.) are excluded — their extracted text may begin
# with `---` by coincidence and must not be parsed for front matter.
_FRONT_MATTER_EXTENSIONS = frozenset({".md", ".txt", ".rst", ".html"})


def _fuse_rag_fusion_results(
    variant_results: list[list[ScoredSearchCandidate]],
    k: int = 60,
) -> list[ScoredSearchCandidate]:
    """Second-pass Reciprocal Rank Fusion across RAG Fusion variant result lists.

    For each variant result list, each candidate at index *i* (0-indexed) receives
    score = 1.0 / (k + i + 1).  When the same chunk_id appears in multiple variant
    lists, scores are accumulated.  The candidate instance kept is the one from the
    variant where it ranked highest (lowest index).

    Returns candidates sorted descending by accumulated fused RRF score.
    Returns [] for empty or all-empty input.

    k=60 matches the first-pass per-variant RRF constant used in store.py (_RRF_K).
    Do NOT import _rrf_score from store.py — implement inline to avoid coupling to
    store internals.
    """
    # accumulated score per chunk_id
    scores: dict[str, float] = {}
    # best candidate instance per chunk_id (from variant with lowest rank)
    best_candidate: dict[str, ScoredSearchCandidate] = {}
    best_rank: dict[str, int] = {}

    for variant_list in variant_results:
        for index, candidate in enumerate(variant_list):
            cid = candidate.chunk_id
            score = 1.0 / (k + index + 1)
            scores[cid] = scores.get(cid, 0.0) + score
            if cid not in best_rank or index < best_rank[cid]:
                best_rank[cid] = index
                best_candidate[cid] = candidate

    return sorted(best_candidate.values(), key=lambda c: scores[c.chunk_id], reverse=True)


def _extract_front_matter(text: str) -> tuple[dict, str]:
    """Detect and strip YAML front matter from the top of a text document.

    Returns (front_matter_dict, body_text).  If no front matter is found, returns
    ({}, original_text).  Only the opening `---\\n...\\n---` block is parsed.
    Import of yaml is deferred to avoid adding a hard dependency.
    """
    if not text.startswith("---"):
        return {}, text

    # Find closing `---` delimiter (must be on its own line)
    rest = text[3:]
    # Accept both `---\n` and `---\r\n` line endings
    if rest and rest[0] in ("\n", "\r"):
        rest = rest.lstrip("\r\n")  # normalise
    else:
        # `---` not followed by newline → not a valid front matter block
        return {}, text

    # Re-scan for the closing delimiter from the start of the block
    try:
        end_idx = text.index("\n---", 3)
    except ValueError:
        return {}, text

    raw_yaml = text[4:end_idx]  # content between the two `---` markers

    try:
        import yaml  # noqa: PLC0415

        parsed = yaml.safe_load(raw_yaml)
    except Exception:
        return {}, text

    if not isinstance(parsed, dict):
        return {}, text

    # Body is everything after the closing `---\n` (or `---` at EOF)
    body_start = end_idx + 4  # skip `\n---`
    body = text[body_start:].lstrip("\r\n")
    return parsed, body



class SearchPipeline:
    """Orchestrates document ingest, vector search, and context retrieval."""

    def __init__(
        self,
        store: SearchStore,
        embedder: Embedder,
        reranker: Reranker | None,
        chunker: DocumentChunker,
        parser: DocumentParser,
        top_k_retrieve: int,
        top_k_return: int,
        max_fanout: int = 8,
        fanout_leg_trim: int = 40,
        fanout_timeout_seconds: float = 30.0,
        language_detector: LanguageDetector | None = None,
        language_detection_confidence_threshold: float = 0.7,
        max_file_mb: int = 0,
        graph_extractor: GraphExtractor | None = None,
        graph_store: GraphStore | None = None,
        graph_config: GraphConfig | None = None,
        graph_expander: "GraphExpander | None" = None,
    ) -> None:
        self.store = store
        self._global_embedder = embedder
        self._reranker = reranker
        self._chunker = chunker
        self._parser = parser
        self._top_k_retrieve = top_k_retrieve
        self._top_k_return = top_k_return
        self._max_fanout = max_fanout
        self._fanout_leg_trim = fanout_leg_trim
        self._fanout_timeout_seconds = fanout_timeout_seconds
        self._language_detector = language_detector
        self._language_detection_confidence_threshold = language_detection_confidence_threshold
        self._max_file_mb = max_file_mb
        self._graph_extractor = graph_extractor
        self._graph_store = graph_store
        self._graph_config = graph_config
        self._graph_expander = graph_expander

    # ------------------------------------------------------------------
    # Warm-status accessors (used by health/readiness route handlers)
    # ------------------------------------------------------------------

    @property
    def reranker_is_warm(self) -> bool:
        return self._reranker.is_warm if self._reranker is not None else False

    @property
    def embedder_is_warm(self) -> bool:
        return self._global_embedder.is_warm


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
        *,
        embedder: Embedder,
        namespace: str = DEFAULT_NAMESPACE,
        ingested_by: IngestedBy = "cli",
        collection_root: Path | None = None,
    ) -> IngestResult:
        doc_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()

        # Skip .acl sidecar files — they are metadata, not indexable content
        if path.suffix == ".acl" or path.name.endswith(".acl"):
            return IngestResult(doc_id=doc_id, chunks_created=0, status="ok")

        # Size guard — checked before parse; max_file_mb=0 disables the check.
        # Follows symlinks (os.path.getsize dereferences symlinks by design).
        # Boundary: strictly greater-than (size > limit), so a file exactly at
        # the limit is accepted.
        if self._max_file_mb > 0:
            try:
                exceeds = _file_exceeds_limit(path, self._max_file_mb)
                if exceeds:
                    _size_bytes = os.path.getsize(path)  # for human-readable message
            except OSError:
                logger.warning("Cannot stat file for size guard: %s", path, exc_info=True)
                return IngestResult(
                    doc_id=doc_id, chunks_created=0, status="error",
                    error=f"Cannot determine file size for {path.name}",
                )
            if exceeds:
                err = IngestError(
                    file_size_mb=math.ceil(_size_bytes / (1024 * 1024)),
                    limit_mb=self._max_file_mb,
                )
                return IngestResult(doc_id=doc_id, chunks_created=0, status="error", error=err.message, code="file_too_large")

        # Parse
        try:
            with record_stage("parse"):
                markdown = await self._parser.parse(path)
        except ParseError as e:
            return IngestResult(doc_id=doc_id, chunks_created=0, status="error", error=str(e))

        # Extract front matter (text files only; binary files skipped to avoid false positives)
        is_text_type = path.suffix.lower() in _FRONT_MATTER_EXTENSIONS
        if is_text_type:
            front_matter, markdown = _extract_front_matter(markdown)
            _acl = front_matter.pop("_acl", None)
        else:
            _acl = None

        # Resolve effective ACL for this document
        resolved_acl, acl_warnings = resolve_acl(path, _acl)

        # Derive metadata fields at the call site (Task 3.3).
        file_type = path.suffix.lower().lstrip(".")
        try:
            mtime = path.stat().st_mtime
            updated_at = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
        except OSError:
            logger.debug("stat() failed for %s; updated_at will be empty", path)
            updated_at = ""

        # C3a / C3b / C3c: enrichment routing — choose the correct enricher based on
        # source type. Code files go through CodeEnricher (C3c). Docling-parsed sources
        # (pdf, image) go through MarkdownEnricher.preprocess() (C3b). Text-format
        # sources go through MarkdownEnricher.prepare() (C3a).
        suffix = path.suffix.lower()
        if suffix in CODE_EXTENSIONS:
            # C3c path: code file → AST-based symbol enrichment.
            enricher: MarkdownEnricher | CodeEnricher = CodeEnricher()
            scope_table = enricher.prepare(markdown, suffix, path, collection_root)
            heading_table = None
            page_table = None
        else:
            enricher = MarkdownEnricher()
            subtype = source_subtype_for(path.suffix)
            if is_docling_source(subtype):
                # C3b path: strip markers, build page table; heading enrichment skipped for v1.
                markdown, page_table = enricher.preprocess(markdown)
                heading_table = None
            else:
                # C3a path: text-format sources get heading enrichment.
                heading_table = enricher.prepare(markdown) if is_text_type else []
                page_table = None
            scope_table = None

        # C2: language detection — runs after parse, before chunk
        if self._language_detector is not None:
            try:
                with record_stage("language_detect"):
                    lang = await self._language_detector.detect(
                        markdown,
                        confidence_threshold=self._language_detection_confidence_threshold,
                    )
            except Exception as e:
                logger.warning(
                    "language detection failed for %s — tagging chunks as untagged: %s",
                    path,
                    e,
                )
                lang = ""
        else:
            lang = ""

        records = self._chunker.chunk(
            markdown,
            doc_id,
            str(path),
            file_type=file_type,
            updated_at=updated_at,
            ingested_by=ingested_by,
            language=lang,
        )
        if not records:
            return IngestResult(doc_id=doc_id, chunks_created=0, status="ok", warnings=acl_warnings)

        # C3a / C3b / C3c: enrich every chunk with symbol or heading/page metadata.
        for record in records:
            if isinstance(enricher, CodeEnricher):
                enrichment = enricher.enrich_chunk(record, scope_table)
            else:
                enrichment = enricher.enrich_chunk(
                    record, heading_table=heading_table, page_table=page_table
                )
            record.metadata.update(enrichment)

        # Assign sequential chunk IDs and propagate ACL
        for idx, record in enumerate(records):
            record.chunk_id = f"{doc_id}-{idx:06d}"
            record.acl = resolved_acl

        # Collect chunk texts if requested (before batching)
        if _chunk_collector is not None:
            _chunk_collector.extend(r.text for r in records)

        # E1a / BE-5: graph extraction — runs after chunk IDs are assigned, before embed/persist.
        _graph_enabled = (
            self._graph_extractor is not None
            and self._graph_store is not None
            and self._graph_config is not None
            and self._graph_config.enabled
        )
        _extraction_result = None
        if _graph_enabled:
            chunk_inputs = [
                ChunkInput(
                    chunk_id=r.chunk_id,
                    text=r.text,
                    symbol_type=r.metadata.get("_symbol_type") or None,
                    symbol_subtype=r.metadata.get("_symbol_subtype") or None,
                    containing_function=r.metadata.get("_containing_function") or None,
                    containing_class=r.metadata.get("_containing_class") or None,
                    source_path=r.source_path,
                )
                for r in records
            ]
            try:
                _extraction_result = await self._graph_extractor.extract(chunk_inputs, doc_id, collection)
                if _extraction_result.fatal_error:
                    return IngestResult(
                        doc_id=doc_id,
                        chunks_created=0,
                        status="error",
                        error=_extraction_result.fatal_error,
                        warnings=acl_warnings,
                    )
            except Exception:
                logger.warning(
                    "Graph extraction raised unexpectedly; skipping graph for %r in %r",
                    doc_id, collection,
                    exc_info=True,
                )
                _extraction_result = None

        # Embed first batch to initialise embedding_dim before ensure_collection
        first_batch = records[:_INGEST_CHUNK_BATCH_SIZE]
        first_vectors = await embedder.embed([r.text for r in first_batch])
        for record, vector in zip(first_batch, first_vectors):
            record.vector = vector

        # Persist
        with record_stage("persist"):
            await self.store.ensure_collection(collection, embedder.embedding_dim)
            try:
                await self.store.delete_document(collection, doc_id, namespace=namespace, skip_fts_optimize=True)
            except StoreBusyError:
                return IngestResult(doc_id=doc_id, chunks_created=0, status="error", warnings=acl_warnings)

            chunks_created = 0
            needs_recompute = False
            try:
                for i in range(0, len(records), _INGEST_CHUNK_BATCH_SIZE):
                    batch = records[i : i + _INGEST_CHUNK_BATCH_SIZE]
                    if i == 0:
                        # First batch already embedded above; vectors are set on records
                        vectors = first_vectors
                    else:
                        vectors = await embedder.embed([r.text for r in batch])
                        for record, vector in zip(batch, vectors):
                            record.vector = vector
                    if _vector_collector is not None:
                        _vector_collector.extend(vectors)
                    ingest_result = await self.store.ingest_chunks(
                        collection,
                        batch,
                        embedding_model=embedder.model_name,
                        namespace=namespace,
                        _is_continuation=(i > 0),
                    )
                    chunks_created += ingest_result.chunks_ingested
                    needs_recompute = needs_recompute or ingest_result.needs_recompute
            except StoreBusyError:
                return IngestResult(doc_id=doc_id, chunks_created=0, status="error", warnings=acl_warnings)

            if rebuild_fts and needs_recompute:
                await self.recompute_collection_meta(collection, self._global_embedder, namespace=namespace)

            if rebuild_fts:
                if self.store.supports_incremental_fts_delete:
                    try:
                        await self.store.optimize_fts(collection)
                    except Exception:
                        dominant_lang = await self.store.get_dominant_language(collection)
                        logger.warning(
                            "optimize_fts failed for collection %r; falling back to rebuild_fts_index",
                            collection,
                            exc_info=True,
                        )
                        await self.store.rebuild_fts_index(collection, language=dominant_lang)
                else:
                    dominant_lang = await self.store.get_dominant_language(collection)
                    await self.store.rebuild_fts_index(collection, language=dominant_lang)

        # E1a / BE-5: write graph extraction results after persist completes.
        if _graph_enabled and _extraction_result is not None:
            if _extraction_result.warnings:
                acl_warnings.extend(_extraction_result.warnings)
            try:
                await self._graph_store.ensure_graph_tables(collection)
                if _extraction_result.nodes or _extraction_result.edges:
                    await self._graph_store.write_graph(
                        collection, _extraction_result.nodes, _extraction_result.edges
                    )
                edge_count = await self._graph_store.edge_count(collection)
                if edge_count >= self._graph_config.backend_threshold_edges:
                    hint = (
                        f"Graph edge count ({edge_count:,}) has reached "
                        f"backend_threshold_edges ({self._graph_config.backend_threshold_edges:,}). "
                        "NetworkX in-memory traversal may become latency-noticeable. "
                        "Consider migrating to the Kuzu backend (available in E1b)."
                    )
                    logger.warning(hint)
                    acl_warnings.append(hint)
            except Exception:
                logger.warning(
                    "Graph write failed for %r in %r; graph data may be incomplete",
                    doc_id, collection,
                    exc_info=True,
                )
                acl_warnings.append(
                    f"Graph write failed for {doc_id!r}: graph data may be incomplete"
                )

        return IngestResult(
            doc_id=doc_id,
            chunks_created=chunks_created,
            status="ok",
            needs_recompute=needs_recompute,
            warnings=acl_warnings,
        )

    async def ingest_directory(
        self,
        path: Path,
        collection: str,
        glob_pattern: str = "**/*",
        progress_cb: Callable[[int, int], None | Awaitable[None]] | None = None,
        force_regenerate_description: bool = False,
        exclude_paths: frozenset[str] | None = None,
        on_file_complete: Callable[[Path], None] | None = None,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        embedder: Embedder,
        ingested_by: IngestedBy = "cli",
        collection_root: Path | None = None,
        rebuild_fts: bool = True,
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
            # Skip .acl sidecar files — they are metadata, not indexable content
            if file_path.suffix == ".acl" or file_path.name.endswith(".acl"):
                continue
            files.append(file_path)

        files.sort()

        if exclude_paths is not None:
            files = [f for f in files if str(f) not in exclude_paths]

        if not files:
            return []

        results: list[IngestResult] = []
        total = len(files)

        for done_count, file_path in enumerate(files, start=1):
            result = await self.ingest_file(
                file_path,
                collection,
                rebuild_fts=False,
                embedder=embedder,
                namespace=namespace,
                collection_root=collection_root,
                ingested_by=ingested_by,
            )
            results.append(result)
            if on_file_complete is not None and result.status == "ok":
                on_file_complete(file_path)
            if progress_cb is not None:
                ret = progress_cb(done_count, total)
                if inspect.isawaitable(ret):
                    await ret

        # Optimize (or rebuild) FTS once if at least one successful ingest
        if rebuild_fts and any(r.status == "ok" for r in results):
            if self.store.supports_incremental_fts_delete:
                try:
                    await self.store.optimize_fts(collection)
                except Exception:
                    dominant_lang = await self.store.get_dominant_language(collection)
                    logger.warning(
                        "optimize_fts failed for collection %r; falling back to rebuild_fts_index",
                        collection,
                        exc_info=True,
                    )
                    await self.store.rebuild_fts_index(collection, language=dominant_lang)
            else:
                dominant_lang = await self.store.get_dominant_language(collection)
                await self.store.rebuild_fts_index(collection, language=dominant_lang)

        # Only update metadata when at least one file was successfully ingested
        if any(r.status == "ok" for r in results):
            # Regenerate description using fresh store counts from B5 incremental path
            existing_meta = await self.store.get_collection_meta(collection, namespace=namespace)
            description = existing_meta.description if existing_meta else None
            described_at = existing_meta.described_at_doc_count if existing_meta else None
            last_described = existing_meta.last_described if existing_meta else None
            batch_doc_count = existing_meta.doc_count if existing_meta else 0
            batch_chunk_count = existing_meta.chunk_count if existing_meta else 0

            if force_regenerate_description or _should_regenerate(batch_doc_count, batch_chunk_count, described_at):
                sample_texts = await self.store.sample_chunk_texts(collection, namespace, n=MAX_SAMPLE_CHUNKS)
                new_desc = await generate_description(sample_texts, collection)
                if new_desc is not None:
                    description = new_desc
                    described_at = batch_doc_count
                    last_described = datetime.now(UTC)

            await self.store.update_description(
                collection,
                description,
                last_described,
                described_at_doc_count=described_at,
                last_indexed=datetime.now(UTC),
                namespace=namespace,
            )

            # Aggregate needs_recompute signal: if any file triggered it, fire recompute
            if any(r.needs_recompute for r in results):
                await self.recompute_collection_meta(collection, self._global_embedder, namespace=namespace)

        return results

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        collection: str,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        embedder: Embedder,
        filters: SearchFilters | None = None,
        query_vector: list[float] | None = None,
        rag_fusion: bool = False,
        rag_fusion_generator: "RAGFusionGenerator | None" = None,
        rag_fusion_config: "RAGFusionConfig | None" = None,
        graph_mode: str | None = None,
    ) -> SearchPipelineResult:
        # --- Graph expansion (naive mode) — applied to original query before all other paths ---
        # Expansion is applied to the original query only.  RAG Fusion variants are generated
        # from the original (unexpanded) query.  HyDE uses the original query; when expansion
        # is active the expanded text is used for both FTS and vector embedding.
        effective_query = query
        graph_expansion_applied = False
        if graph_mode == "naive" and self._graph_expander is not None:
            expanded = await self._graph_expander.expand(query, collection)
            if expanded.expansion_applied:
                effective_query = expanded.expanded_text
                graph_expansion_applied = True

        # --- RAG Fusion path ---
        if (
            rag_fusion
            and rag_fusion_generator is not None
            and rag_fusion_config is not None
            and rag_fusion_config.enabled
        ):
            from archon_search.rag_fusion import RAGFusionDependencyError  # noqa: PLC0415

            # 0. Vector conflict guard: ignore caller-supplied query_vector when RAG Fusion active.
            if query_vector is not None:
                logger.warning(
                    "rag_fusion=True received with pre-computed query_vector=%s; ignoring query_vector",
                    _query_fingerprint(query),
                )
                query_vector = None

            # 1. FTS-only guard.
            if not await self.store.has_vector_index(collection):
                result = await self._search_standard(
                    effective_query, collection, namespace, embedder=embedder,
                    filters=filters, query_vector=None,
                )
                result.graph_expansion_applied = graph_expansion_applied
                return result

            # 2. Generate variants from the ORIGINAL (unexpanded) query.
            try:
                variants = await rag_fusion_generator.generate_variants(query)
            except RAGFusionDependencyError:
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "rag_fusion generate_variants timed out for query=%s; falling back",
                    _query_fingerprint(query),
                )
                fallback = await self._search_standard(
                    effective_query, collection, namespace, embedder=embedder,
                    filters=filters, query_vector=None,
                    rag_fusion_attempted=True,
                )
                fallback.rag_fusion_warning = "RAG Fusion timed out"
                fallback.graph_expansion_applied = graph_expansion_applied
                return fallback
            except Exception:
                logger.warning(
                    "rag_fusion generate_variants failed unexpectedly for query=%s; falling back",
                    _query_fingerprint(query),
                )
                fallback = await self._search_standard(
                    effective_query, collection, namespace, embedder=embedder,
                    filters=filters, query_vector=None,
                    rag_fusion_attempted=True,
                )
                fallback.rag_fusion_warning = "RAG Fusion expansion failed"
                fallback.graph_expansion_applied = graph_expansion_applied
                return fallback

            # 3. All queries: slot 0 = effective_query (expanded if applicable); variants from original.
            all_queries = [effective_query] + variants

            # 4. Embed all queries in parallel.
            try:
                vectors = await asyncio.gather(*[embedder.embed_one(q) for q in all_queries])
            except Exception:
                logger.warning(
                    "rag_fusion embedding stage failed for query=%s; falling back to single-query search",
                    _query_fingerprint(query),
                )
                fallback = await self._search_standard(
                    effective_query, collection, namespace, embedder=embedder,
                    filters=filters, query_vector=None,
                    rag_fusion_attempted=True,
                )
                fallback.rag_fusion_warning = "RAG Fusion expansion failed"
                fallback.graph_expansion_applied = graph_expansion_applied
                return fallback

            # 5. Parallel variant searches using hybrid_search_with_trace.
            _rag_candidate_depth = max(self._top_k_retrieve * 3, 20)
            search_calls = [
                self.store.hybrid_search_with_trace(
                    collection, v, query, candidate_depth=_rag_candidate_depth, filters=filters
                )
                for v in vectors
            ]
            raw_results = await asyncio.gather(*search_calls, return_exceptions=True)

            # 6. Partition successes and failures.
            successful_results: list[list[ScoredSearchCandidate]] = []
            for idx, r in enumerate(raw_results):
                if isinstance(r, BaseException):
                    logger.warning(
                        "rag_fusion variant search %d failed for query=%s: %s",
                        idx, _query_fingerprint(query), type(r).__name__,
                    )
                else:
                    successful_results.append(r)  # type: ignore[arg-type]

            if not successful_results:
                # All searches failed — fall back to standard single-query search.
                result = await self._search_standard(
                    effective_query, collection, namespace, embedder=embedder,
                    filters=filters, query_vector=None,
                    rag_fusion_attempted=True,
                )
                result.graph_expansion_applied = graph_expansion_applied
                return result

            # rag_fusion_queries_used = successful variant searches (not counting original).
            # The original query is index 0; variants start at 1.
            num_successful_variants = sum(
                1 for idx, r in enumerate(raw_results)
                if idx > 0 and not isinstance(r, BaseException)
            )

            # 7. Fuse results.
            fused = _fuse_rag_fusion_results(successful_results)

            # 7b. Apply source_path_glob post-filter (store.hybrid_search_with_trace does not filter).
            if filters and filters.source_path_glob:
                _glob = filters.source_path_glob
                fused = [c for c in fused if fnmatch.fnmatchcase(c.source_path, _glob)]

            # 8. ACL filter on fused set.
            fused, acl_filtered = apply_acl_filter(fused, lambda c: c.acl, namespace)

            # 9. Rerank on fused set using the original query.
            if self._reranker is not None:
                fused = await self._reranker.rerank_candidates(
                    query, fused, top_k=self._top_k_return
                )
            else:
                fused = fused[:self._top_k_return]

            results = [self._candidate_to_search_result(c) for c in fused]
            # rag_fusion_applied=True only when at least one variant was generated and searched.
            # When variants=[], num_successful_variants=0 and rag_fusion_applied=False.
            rag_fusion_applied = num_successful_variants > 0
            return SearchPipelineResult(
                results=results,
                acl_filtered=acl_filtered,
                rag_fusion_applied=rag_fusion_applied,
                rag_fusion_queries_used=num_successful_variants,
                rag_fusion_attempted=True,
                graph_expansion_applied=graph_expansion_applied,
            )

        # --- Standard path ---
        # When graph expansion is active, re-embed the expanded text (pass query_vector=None).
        # When no expansion, honour the caller-supplied query_vector (e.g. HyDE pre-computed vector).
        effective_query_vector = None if graph_expansion_applied else query_vector
        result = await self._search_standard(
            effective_query, collection, namespace, embedder=embedder,
            filters=filters, query_vector=effective_query_vector,
        )
        result.graph_expansion_applied = graph_expansion_applied
        return result

    async def _search_standard(
        self,
        query: str,
        collection: str,
        namespace: str,
        *,
        embedder: Embedder,
        filters: SearchFilters | None = None,
        query_vector: list[float] | None = None,
        rag_fusion_attempted: bool = False,
    ) -> SearchPipelineResult:
        """Standard single-query search path (no RAG Fusion)."""
        vector = list(query_vector) if query_vector is not None else await embedder.embed_one(query)
        candidates = await self.store.hybrid_search_with_trace(
            collection, vector, query, candidate_depth=max(self._top_k_retrieve * 3, 20), filters=filters
        )
        if filters and filters.source_path_glob:
            glob_pattern = filters.source_path_glob
            candidates = [c for c in candidates if fnmatch.fnmatchcase(c.source_path, glob_pattern)]
        candidates_before_acl = len(candidates)
        candidates, acl_filtered = apply_acl_filter(candidates, lambda r: r.acl, namespace)
        acl_denied = candidates_before_acl - len(candidates)
        if filters is not None and len(candidates) < self._top_k_return:
            filter_flags = {
                k: v
                for k, v in filters.model_dump().items()
                if k != "include_metadata" and v is not None and v is not False
            }
            if filter_flags:
                logger.warning(
                    "filter+ACL combined attrition: only %d/%d candidates reached reranker"
                    " (filter_flags=%r, acl_denied=%d)",
                    len(candidates),
                    self._top_k_return,
                    filter_flags,
                    acl_denied,
                )
        if self._reranker is not None:
            reranked = await self._reranker.rerank_candidates(query, candidates, top_k=self._top_k_return)
            results = [self._candidate_to_search_result(c) for c in reranked]
        else:
            candidates = candidates[:self._top_k_return]
            results = [self._candidate_to_search_result(c) for c in candidates]
        return SearchPipelineResult(
            results=results,
            acl_filtered=acl_filtered,
            rag_fusion_attempted=rag_fusion_attempted,
        )

    async def explain(
        self,
        query: str,
        collection: str | None = None,
        *,
        collections: list[str] | None = None,
        top_k: int = 5,
        rerank: bool = True,
        namespace: str = DEFAULT_NAMESPACE,
        query_vector: list[float] | None = None,
        embedder: Embedder | None = None,
        rag_fusion: bool = False,
        rag_fusion_generator: "RAGFusionGenerator | None" = None,
        rag_fusion_config: "RAGFusionConfig | None" = None,
    ) -> ExplainPipelineResult:
        """Fetch an amplified pool (``max(top_k_retrieve*3, 20)`` candidates) and, when
        ``rerank=True``, rerank the entire ACL-filtered pool so near-misses carry real
        reranker scores.  Top-k equality with ``search`` holds only when corpus ≤
        ``top_k_retrieve`` (identical pools) and all reranker scores are distinct.

        Pass ``collections`` (instead of ``collection``) to explain a multi-collection
        fan-out: legs are merged, ACL-filtered, and reranked as a single pool, with
        per-collection provenance preserved on each candidate.  The route layer resolves
        routing before calling this method, so exactly one of ``collection`` /
        ``collections`` must be supplied.

        When ``rag_fusion=True`` and a generator is supplied, the single-collection path
        decomposes the query into variants, searches in parallel, and fuses results via
        second-pass RRF. Multi-collection path (``collections``) ignores RAG Fusion in
        this version (falls back to standard explain).
        """
        if collection is not None and collections is not None:
            raise ValueError("supply either collection or collections, not both")
        if collection is None and collections is None:
            raise ValueError("supply either collection or collections")

        def _final_score(c: ScoredSearchCandidate) -> float:
            rs = c.score_breakdown.reranker_score
            return rs if rs is not None else c.score_breakdown.rrf_score

        if collections is not None:
            if rerank is False and len(collections) > 1 and self._reranker is not None:
                raise ExplainMultiCollectionNoRerankError()

            vector = query_vector if query_vector is not None else await self._global_embedder.embed_one(query)

            # Metadata lookup, validation, namespace + model partitioning
            # (mirrors search_many step 2).
            try:
                all_meta = await self.get_all_collections_meta(namespace)
            except Exception as exc:
                raise MetadataLookupError(exc) from exc

            meta_by_name = {m.name: m for m in all_meta}
            missing = [name for name in collections if name not in meta_by_name]
            if missing:
                raise CollectionNotFoundError(missing)

            excluded: list[ExcludedCollection] = []
            collections_in_scope: list[str] = []
            for name in collections:
                if meta_by_name[name].active_embedding_model != self._global_embedder.model_name:
                    excluded.append(ExcludedCollection(name=name, reason="embedding_model_mismatch"))
                else:
                    collections_in_scope.append(name)

            if not collections_in_scope:
                return ExplainPipelineResult(
                    top_results=[],
                    near_misses=[],
                    acl_filtered=False,
                    excluded_collections=excluded,
                )

            candidate_depth = max(self._top_k_retrieve * 3, 20)
            merged, acl_filtered, _leg_times = await self._fanout_merge_acl(
                query, vector, collections_in_scope, namespace, candidate_depth
            )

            if rerank and self._reranker is not None:
                candidates = await self._reranker.rerank_candidates(
                    query, merged, top_k=len(merged)
                )
            else:
                candidates = merged

            candidates.sort(key=lambda c: (-_final_score(c), c.doc_id, c.chunk_id))

            return ExplainPipelineResult(
                top_results=candidates[:top_k],
                near_misses=candidates[top_k : top_k + 20],
                acl_filtered=acl_filtered,
                excluded_collections=excluded,
            )

        # --- Single-collection RAG Fusion path ---
        if (
            rag_fusion
            and rag_fusion_generator is not None
            and rag_fusion_config is not None
            and rag_fusion_config.enabled
        ):
            from archon_search.rag_fusion import RAGFusionDependencyError  # noqa: PLC0415

            _single_embedder = embedder if embedder is not None else self._global_embedder
            candidate_depth = max(self._top_k_retrieve * 3, 20)

            # FTS-only guard.
            if not await self.store.has_vector_index(collection):
                return await self._explain_standard(
                    query, collection, top_k=top_k, rerank=rerank, namespace=namespace,
                    query_vector=query_vector, embedder=_single_embedder,
                )

            # Generate variants.
            try:
                variants = await rag_fusion_generator.generate_variants(query)
            except RAGFusionDependencyError:
                raise
            except Exception as exc:
                logger.warning(
                    "rag_fusion explain generate_variants failed for query=%s; falling back",
                    _query_fingerprint(query),
                )
                return await self._explain_standard(
                    query, collection, top_k=top_k, rerank=rerank, namespace=namespace,
                    query_vector=None, embedder=_single_embedder,
                    rag_fusion_attempted=True,
                    rag_fusion_failure_reason=type(exc).__name__,
                )

            # All queries = original + variants.
            all_queries = [query] + variants

            # Embed all queries in parallel.
            try:
                vectors = await asyncio.gather(*[_single_embedder.embed_one(q) for q in all_queries])
            except Exception as exc:
                logger.warning(
                    "rag_fusion explain embedding stage failed for query=%s; falling back",
                    _query_fingerprint(query),
                )
                return await self._explain_standard(
                    query, collection, top_k=top_k, rerank=rerank, namespace=namespace,
                    query_vector=None, embedder=_single_embedder,
                    rag_fusion_attempted=True,
                    rag_fusion_failure_reason=type(exc).__name__,
                )

            # Parallel variant searches using hybrid_search_with_trace.
            search_calls = [
                self.store.hybrid_search_with_trace(
                    collection, v, query, candidate_depth=candidate_depth
                )
                for v in vectors
            ]
            raw_results = await asyncio.gather(*search_calls, return_exceptions=True)

            # Partition successes and failures; track which variant_index each maps to.
            successful_results: list[list[ScoredSearchCandidate]] = []
            successful_indices: list[int] = []  # 0=original, 1..N=variants
            for idx, r in enumerate(raw_results):
                if isinstance(r, BaseException):
                    logger.warning(
                        "rag_fusion explain variant search %d failed for query=%s: %s",
                        idx, _query_fingerprint(query), type(r).__name__,
                    )
                else:
                    successful_results.append(r)  # type: ignore[arg-type]
                    successful_indices.append(idx)

            if not successful_results:
                # All searches failed — fall back to standard explain.
                return await self._explain_standard(
                    query, collection, top_k=top_k, rerank=rerank, namespace=namespace,
                    query_vector=None, embedder=_single_embedder,
                    rag_fusion_attempted=True,
                )

            # rag_fusion_queries_used = successful variant searches (not counting original).
            num_successful_variants = sum(1 for idx in successful_indices if idx > 0)

            # Fuse results via second-pass RRF.
            fused = _fuse_rag_fusion_results(successful_results)

            # ACL filter on fused set.
            fused, acl_filtered = apply_acl_filter(fused, lambda c: c.acl, namespace)

            # Rerank on fused set using the original query.
            if rerank and self._reranker is not None:
                try:
                    fused = await self._reranker.rerank_candidates(
                        query, fused, top_k=len(fused)
                    )
                except Exception as exc:
                    raise ExplainStageError("reranker", exc) from exc

            fused.sort(key=lambda c: (-_final_score(c), c.doc_id, c.chunk_id))

            top_results = fused[:top_k]
            near_misses = fused[top_k : top_k + 20]

            # Build sub_query_results — only for successful searches (failed variants omitted).
            sub_query_results = [
                RagFusionSubQueryInfo(
                    variant_index=variant_idx,
                    result_count=len(result_list),
                    top_doc_ids=[c.doc_id for c in result_list[:5]],
                )
                for variant_idx, result_list in zip(successful_indices, successful_results)
            ]

            return ExplainPipelineResult(
                top_results=top_results,
                near_misses=near_misses,
                acl_filtered=acl_filtered,
                rag_fusion_applied=num_successful_variants > 0,
                rag_fusion_queries_used=num_successful_variants,
                rag_fusion_attempted=True,
                rag_fusion_sub_query_results=sub_query_results,
            )

        # --- Standard single-collection path ---
        _single_embedder = embedder if embedder is not None else self._global_embedder
        return await self._explain_standard(
            query, collection, top_k=top_k, rerank=rerank, namespace=namespace,
            query_vector=query_vector, embedder=_single_embedder,
        )

    async def _explain_standard(
        self,
        query: str,
        collection: str,
        *,
        top_k: int = 5,
        rerank: bool = True,
        namespace: str = DEFAULT_NAMESPACE,
        query_vector: list[float] | None = None,
        embedder: Embedder,
        rag_fusion_attempted: bool = False,
        rag_fusion_failure_reason: str | None = None,
    ) -> ExplainPipelineResult:
        """Standard single-collection explain path (no RAG Fusion)."""

        def _final_score(c: ScoredSearchCandidate) -> float:
            rs = c.score_breakdown.reranker_score
            return rs if rs is not None else c.score_breakdown.rrf_score

        vector = query_vector if query_vector is not None else await embedder.embed_one(query)
        candidate_depth = max(self._top_k_retrieve * 3, 20)
        try:
            candidates = await self.store.hybrid_search_with_trace(
                collection, vector, query, candidate_depth=candidate_depth
            )
        except Exception as exc:
            raise ExplainStageError("store", exc) from exc

        candidates, acl_filtered = apply_acl_filter(candidates, lambda c: c.acl, namespace)

        if rerank and self._reranker is not None:
            try:
                candidates = await self._reranker.rerank_candidates(
                    query, candidates, top_k=len(candidates)
                )
            except Exception as exc:
                raise ExplainStageError("reranker", exc) from exc

        candidates.sort(key=lambda c: (-_final_score(c), c.doc_id, c.chunk_id))

        top_results = candidates[:top_k]
        near_misses = candidates[top_k : top_k + 20]

        return ExplainPipelineResult(
            top_results=top_results,
            near_misses=near_misses,
            acl_filtered=acl_filtered,
            rag_fusion_attempted=rag_fusion_attempted,
            rag_fusion_failure_reason=rag_fusion_failure_reason,
        )

    async def search_many(
        self,
        query: str,
        collections: list[str],
        namespace: str = DEFAULT_NAMESPACE,
        query_vector: list[float] | None = None,
        rag_fusion: bool = False,
        rag_fusion_generator: "RAGFusionGenerator | None" = None,
        rag_fusion_config: "RAGFusionConfig | None" = None,
        filters: SearchFilters | None = None,
        graph_mode: str | None = None,
    ) -> SearchPipelineResult:
        """Embed the query once, fan out hybrid retrieval across ``collections`` in
        parallel, merge with provenance, run a single global rerank pass, and return a
        unified result."""
        # Step 1: metadata lookup, validation, namespace + model partitioning.
        try:
            all_meta = await self.get_all_collections_meta(namespace)
        except Exception as exc:
            raise MetadataLookupError(exc) from exc

        meta_by_name = {m.name: m for m in all_meta}
        missing = [name for name in collections if name not in meta_by_name]
        if missing:
            raise CollectionNotFoundError(missing)

        excluded_collections: list[ExcludedCollection] = []
        collections_in_scope: list[str] = []
        for name in collections:
            if meta_by_name[name].active_embedding_model != self._global_embedder.model_name:
                excluded_collections.append(
                    ExcludedCollection(name=name, reason="embedding_model_mismatch")
                )
            else:
                collections_in_scope.append(name)

        if not collections_in_scope:
            return SearchPipelineResult(
                results=[],
                acl_filtered=False,
                excluded_collections=excluded_collections,
            )

        # --- RAG Fusion path ---
        if (
            rag_fusion
            and rag_fusion_generator is not None
            and rag_fusion_config is not None
            and rag_fusion_config.enabled
        ):
            from archon_search.rag_fusion import RAGFusionDependencyError  # noqa: PLC0415

            candidate_depth = max(self._top_k_retrieve * 3, 20)
            if filters and filters.source_path_glob:
                candidate_depth = max(candidate_depth * GLOB_OVERFETCH_FACTOR, 60)

            # Step A: Generate variants once (single LLM call, not per-collection).
            _rag_fusion_warning: str | None = None
            try:
                variants = await rag_fusion_generator.generate_variants(query)
            except RAGFusionDependencyError:
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "rag_fusion search_many generate_variants timed out for query=%s; falling back",
                    _query_fingerprint(query),
                )
                _rag_fusion_warning = "RAG Fusion timed out"
                variants = []
            except Exception:
                logger.warning(
                    "rag_fusion search_many generate_variants failed for query=%s; falling back",
                    _query_fingerprint(query),
                )
                _rag_fusion_warning = "RAG Fusion expansion failed"
                variants = []

            all_queries_rf = [query] + variants
            rag_fusion_attempted = True

            # Step B: Embed all queries in parallel.
            try:
                all_vectors: list[list[float]] = list(await asyncio.gather(
                    *[self._global_embedder.embed_one(q) for q in all_queries_rf]
                ))
            except Exception:
                logger.warning(
                    "rag_fusion search_many embedding failed for query=%s; falling back to single-query",
                    _query_fingerprint(query),
                )
                _rag_fusion_warning = _rag_fusion_warning or "RAG Fusion expansion failed"
                # Explicit fallback: run standard fan-out with rag_fusion_attempted preserved.
                std_vector = await self._global_embedder.embed_one(query)
                std_merged, std_acl_filtered, std_leg_times = await self._fanout_merge_acl(
                    query, std_vector, collections_in_scope, namespace, candidate_depth, filters=filters
                )
                if self._reranker is not None:
                    t0 = monotonic()
                    std_ranked = await self._reranker.rerank_candidates(
                        query, std_merged, top_k=self._top_k_return
                    )
                    std_rerank_ms = (monotonic() - t0) * 1000.0
                else:
                    std_merged.sort(key=lambda c: -c.score_breakdown.rrf_score)
                    std_ranked = std_merged[:self._top_k_return]
                    std_rerank_ms = 0.0
                return SearchPipelineResult(
                    results=[self._candidate_to_search_result(c) for c in std_ranked],
                    acl_filtered=std_acl_filtered,
                    excluded_collections=excluded_collections,
                    fanout_timings=FanoutTimings(leg_times=std_leg_times, rerank_time_ms=std_rerank_ms),
                    rag_fusion_attempted=True,
                    rag_fusion_warning=_rag_fusion_warning,
                )

            # Step C: Per-collection fan-out with fusion.
            trim = max(self._fanout_leg_trim, 1)
            trimmed_per_coll: dict[str, list[ScoredSearchCandidate]] = {}
            # Track successful variant searches across all collections (variant idx > 0).
            # A variant is "successful" if its search succeeded for at least one collection.
            successful_variant_indices: set[int] = set()

            for coll in sorted(collections_in_scope):
                has_vi = await self.store.has_vector_index(coll)
                if has_vi:
                    # N+1 searches (original + variants) in parallel.
                    coll_raw = await asyncio.gather(
                        *[
                            self.store.hybrid_search_with_trace(
                                coll, v, query, candidate_depth=candidate_depth, filters=filters
                            )
                            for v in all_vectors
                        ],
                        return_exceptions=True,
                    )
                    successful_coll: list[list[ScoredSearchCandidate]] = []
                    for idx, r in enumerate(coll_raw):
                        if not isinstance(r, BaseException):
                            successful_coll.append(r)  # type: ignore[arg-type]
                            if idx > 0:  # idx 0 = original query, idx 1..N = variants
                                successful_variant_indices.add(idx)
                    if successful_coll:
                        fused_coll = _fuse_rag_fusion_results(successful_coll)
                    else:
                        fused_coll = []
                else:
                    # FTS-only collection: single search with original query only.
                    fts_result = await self.store.hybrid_search_with_trace(
                        coll, all_vectors[0], query, candidate_depth=candidate_depth, filters=filters
                    )
                    fused_coll = list(fts_result)

                # Apply source_path_glob post-filter per-collection before trim.
                if filters and filters.source_path_glob:
                    _glob = filters.source_path_glob
                    fused_coll = [c for c in fused_coll if fnmatch.fnmatchcase(c.source_path, _glob)]

                fused_sorted = sorted(
                    fused_coll, key=lambda c: (-c.score_breakdown.rrf_score, c.chunk_id)
                )
                trimmed_per_coll[coll] = fused_sorted[:trim]

            # Step D: Cross-collection merge.
            merged: list[ScoredSearchCandidate] = []
            for coll in sorted(trimmed_per_coll):
                merged.extend(trimmed_per_coll[coll])

            # Step E: ACL filter on merged set.
            merged, acl_filtered = apply_acl_filter(merged, lambda c: c.acl, namespace)

            # Step F: Rerank on merged set using original query.
            if self._reranker is not None:
                t0 = monotonic()
                ranked = await self._reranker.rerank_candidates(
                    query, merged, top_k=self._top_k_return
                )
                rerank_time_ms = (monotonic() - t0) * 1000.0
            else:
                merged.sort(key=lambda c: -c.score_breakdown.rrf_score)
                ranked = merged[:self._top_k_return]
                rerank_time_ms = 0.0

            results = [self._candidate_to_search_result(c) for c in ranked]

            # rag_fusion_queries_used = successful LLM-generated variant searches.
            # Count unique variant indices that succeeded in at least one collection.
            num_successful_variants = len(successful_variant_indices)

            return SearchPipelineResult(
                results=results,
                acl_filtered=acl_filtered,
                excluded_collections=excluded_collections,
                rag_fusion_applied=num_successful_variants > 0,
                rag_fusion_queries_used=num_successful_variants,
                rag_fusion_attempted=rag_fusion_attempted,
                rag_fusion_warning=_rag_fusion_warning,
            )

        # --- Standard path with graph expansion (per-leg) ---
        if graph_mode == "naive" and self._graph_expander is not None:
            # Step 1: Expand query per collection in parallel.
            expansions: list["ExpandedQuery"] = list(await asyncio.gather(*[
                self._graph_expander.expand(query, coll)
                for coll in collections_in_scope
            ]))
            graph_expansion_applied = any(e.expansion_applied for e in expansions)

            # Step 2: Embed each unique effective query text (deduplicated).
            unique_texts: list[str] = list(dict.fromkeys(e.expanded_text for e in expansions))
            embedded_vecs: list[list[float]] = list(await asyncio.gather(*[
                self._global_embedder.embed_one(t) for t in unique_texts
            ]))
            text_to_vec: dict[str, list[float]] = dict(zip(unique_texts, embedded_vecs))

            # Step 3: Per-leg search in parallel with timeout protection.
            candidate_depth = max(self._top_k_retrieve * 3, 20)
            if filters and filters.source_path_glob:
                candidate_depth = max(candidate_depth * GLOB_OVERFETCH_FACTOR, 60)
            trim = max(self._fanout_leg_trim, 1)

            async def _graph_leg(
                coll: str, exp: "ExpandedQuery",
            ) -> tuple[str, list[ScoredSearchCandidate], float]:
                effective_text = exp.expanded_text
                effective_vec = text_to_vec[effective_text]
                t0 = monotonic()
                cands = await self.store.hybrid_search_with_trace(
                    coll, effective_vec, effective_text,
                    candidate_depth=candidate_depth, filters=filters,
                )
                return coll, cands, (monotonic() - t0) * 1000.0

            try:
                async with asyncio.timeout(self._fanout_timeout_seconds):
                    leg_raw_with_exc = await asyncio.gather(*[
                        _graph_leg(coll, exp)
                        for coll, exp in zip(collections_in_scope, expansions)
                    ], return_exceptions=True)
            except TimeoutError:
                raise FanoutTimeoutError()

            # Step 4: Trim, merge, ACL-filter, rerank.
            leg_times: dict[str, float] = {}
            all_cands: list[ScoredSearchCandidate] = []
            for coll, leg_result in zip(collections_in_scope, leg_raw_with_exc):
                if isinstance(leg_result, BaseException):
                    logger.warning(
                        "search_many graph expansion: leg %r failed: %s",
                        coll, type(leg_result).__name__,
                    )
                    continue
                coll_name, cands, leg_ms = leg_result
                if filters and filters.source_path_glob:
                    _glob = filters.source_path_glob
                    cands = [c for c in cands if fnmatch.fnmatchcase(c.source_path, _glob)]
                cands_sorted = sorted(
                    cands, key=lambda c: (-c.score_breakdown.rrf_score, c.chunk_id)
                )
                all_cands.extend(cands_sorted[:trim])
                leg_times[coll_name] = leg_ms

            all_cands, acl_filtered = apply_acl_filter(all_cands, lambda c: c.acl, namespace)

            if self._reranker is not None:
                t0 = monotonic()
                ranked = await self._reranker.rerank_candidates(query, all_cands, top_k=self._top_k_return)
                rerank_time_ms = (monotonic() - t0) * 1000.0
            else:
                all_cands.sort(key=lambda c: -c.score_breakdown.rrf_score)
                ranked = all_cands[:self._top_k_return]
                rerank_time_ms = 0.0

            results = [self._candidate_to_search_result(c) for c in ranked]
            return SearchPipelineResult(
                results=results,
                acl_filtered=acl_filtered,
                excluded_collections=excluded_collections,
                fanout_timings=FanoutTimings(leg_times=leg_times, rerank_time_ms=rerank_time_ms),
                graph_expansion_applied=graph_expansion_applied,
            )

        # --- Standard path ---
        # Step 1: embed exactly once (or use caller-provided vector for HyDE).
        vector = list(query_vector) if query_vector is not None else await self._global_embedder.embed_one(query)

        # Step 3: fan-out + per-leg trim + merge + ACL.
        candidate_depth = max(self._top_k_retrieve * 3, 20)
        if filters and filters.source_path_glob:
            candidate_depth = max(candidate_depth * GLOB_OVERFETCH_FACTOR, 60)
        merged, acl_filtered, leg_times = await self._fanout_merge_acl(
            query, vector, collections_in_scope, namespace, candidate_depth, filters=filters
        )

        # Step 7: single global rerank pass.
        if self._reranker is not None:
            t0 = monotonic()
            ranked = await self._reranker.rerank_candidates(query, merged, top_k=self._top_k_return)
            rerank_time_ms = (monotonic() - t0) * 1000.0
        else:
            merged.sort(key=lambda c: -c.score_breakdown.rrf_score)
            ranked = merged[:self._top_k_return]
            rerank_time_ms = 0.0

        # Step 8: convert to public results.
        results = [self._candidate_to_search_result(c) for c in ranked]

        fanout_timings = FanoutTimings(leg_times=leg_times, rerank_time_ms=rerank_time_ms)
        return SearchPipelineResult(
            results=results,
            acl_filtered=acl_filtered,
            excluded_collections=excluded_collections,
            fanout_timings=fanout_timings,
        )

    async def _fanout_merge_acl(
        self,
        query: str,
        vector,  # type: ignore[no-untyped-def]
        collections_in_scope: list[str],
        namespace: str,
        candidate_depth: int,
        filters: SearchFilters | None = None,
    ) -> tuple[list[ScoredSearchCandidate], bool, dict[str, float]]:
        async def _leg(coll: str):  # type: ignore[no-untyped-def]
            t0 = monotonic()
            cands = await self.store.hybrid_search_with_trace(
                coll, vector, query, candidate_depth=candidate_depth, filters=filters
            )
            return coll, cands, (monotonic() - t0) * 1000.0

        try:
            async with asyncio.timeout(self._fanout_timeout_seconds):
                try:
                    async with asyncio.TaskGroup() as tg:
                        tasks = [tg.create_task(_leg(c)) for c in collections_in_scope]
                except* Exception as eg:
                    logger.error(
                        "search_many fan-out: %d legs failed: %s", len(eg.exceptions), eg
                    )
                    # Re-raise the first leg failure as a plain exception (not an
                    # ExceptionGroup) so the route layer's 500 mapping fires; chain
                    # to the group to preserve sibling context.
                    raise eg.exceptions[0] from eg
        except TimeoutError:
            raise FanoutTimeoutError()

        leg_results = [t.result() for t in tasks]

        trim = max(self._fanout_leg_trim, 1)
        trimmed: dict[str, list[ScoredSearchCandidate]] = {}
        leg_times: dict[str, float] = {}
        for coll, cands, leg_ms in leg_results:
            # Apply source_path_glob post-filter per-leg before trim.
            if filters and filters.source_path_glob:
                _glob = filters.source_path_glob
                cands = [c for c in cands if fnmatch.fnmatchcase(c.source_path, _glob)]
            cands_sorted = sorted(
                cands, key=lambda c: (-c.score_breakdown.rrf_score, c.chunk_id)
            )
            trimmed[coll] = cands_sorted[:trim]
            leg_times[coll] = leg_ms

        merged: list[ScoredSearchCandidate] = []
        for coll in sorted(trimmed):
            merged.extend(trimmed[coll])

        merged, acl_filtered = apply_acl_filter(merged, lambda c: c.acl, namespace)
        return merged, acl_filtered, leg_times

    def _candidate_to_search_result(self, c: ScoredSearchCandidate) -> SearchResult:
        score = c.score_breakdown.reranker_score
        if score is None:
            score = c.score_breakdown.rrf_score
        return SearchResult(
            doc_id=c.doc_id,
            chunk_id=c.chunk_id,
            text=c.text,
            score=score,
            source_path=c.source_path,
            file_type=c.file_type,
            language=c.language,
            indexed_at=c.indexed_at,
            updated_at=c.updated_at,
            ingested_by=c.ingested_by,
            metadata=c.metadata,
            acl=c.acl,
            collection=c.collection,
        )

    async def search_with_context(
        self,
        query: str,
        collection: str,
        context_window: int = 1,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        embedder: Embedder,
        filters: SearchFilters | None = None,
        query_vector: list[float] | None = None,
        rag_fusion: bool = False,
        rag_fusion_generator: "RAGFusionGenerator | None" = None,
        rag_fusion_config: "RAGFusionConfig | None" = None,
    ) -> SearchWithContextResult:
        """Search with surrounding context chunks.

        Returns a :class:`SearchWithContextResult` containing the context-enriched
        result list and the underlying :class:`SearchPipelineResult` (which carries
        ``rag_fusion_applied``, ``rag_fusion_queries_used``, etc.).

        The MCP ``search_with_context`` handler (Task 5.1) unpacks the
        ``pipeline_result`` field to include RAG Fusion metadata in its return dict.
        """
        result_obj = await self.search(
            query, collection, namespace=namespace, embedder=embedder,
            filters=filters, query_vector=query_vector,
            rag_fusion=rag_fusion, rag_fusion_generator=rag_fusion_generator,
            rag_fusion_config=rag_fusion_config,
        )
        output: list[dict[str, Any]] = []

        with record_stage("context"):
            for result in result_obj.results:
                try:
                    center_idx = int(result.chunk_id.split("-")[-1])
                except ValueError:
                    logger.warning("Malformed chunk_id %r — skipping adjacent fetch", result.chunk_id)
                    output.append({"result": result, "context_before": [], "context_after": []})
                    continue

                neighbors = await self.store.fetch_adjacent_chunks(
                    collection, result.doc_id, center_idx, context_window
                )
                neighbors, _ = apply_acl_filter(neighbors, lambda c: c.acl, namespace)

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

        return SearchWithContextResult(results=output, pipeline_result=result_obj)

    # ------------------------------------------------------------------
    # Document management
    # ------------------------------------------------------------------

    async def delete_document(self, doc_id: str, collection: str, namespace: str = DEFAULT_NAMESPACE) -> int:
        meta = await self.store.get_collection_meta(collection, namespace=namespace)
        if meta is None:
            raise ValueError(f"collection {collection!r} not found in namespace {namespace!r}")
        return await self.store.delete_document(collection, doc_id, namespace=namespace)

    async def list_collections(self) -> list[CollectionInfo]:
        return await self.store.list_collections()

    async def get_all_collections_meta(self, namespace: str = DEFAULT_NAMESPACE) -> list[CollectionMeta]:
        all_meta = await self.store.get_all_collections_meta()
        return [m for m in all_meta if m.namespace == namespace]

    async def get_collection_meta(self, name: str, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None:
        return await self.store.get_collection_meta(name, namespace=namespace)

    async def list_documents(
        self,
        collection: str,
        limit: int = 100,
        cursor: str | None = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> tuple[list[DocumentInfo], str | None, int]:
        """List documents in a collection with cursor-based pagination.

        Returns ``(items, next_cursor, total)`` — delegates directly to
        :meth:`SearchStore.list_documents` after validating namespace access.
        Returns ``([], None, 0)`` if the collection is not found in the given
        namespace.
        """
        meta = await self.store.get_collection_meta(collection, namespace=namespace)
        if meta is None:
            return [], None, 0
        return await self.store.list_documents(collection, limit, cursor=cursor)

    async def recompute_collection_meta(
        self,
        collection: str,
        global_embedder: Embedder,
        namespace: str = DEFAULT_NAMESPACE,
        force: bool = False,
    ) -> None:
        """Recompute and persist CollectionMeta (centroid, centroid_sum, doc/chunk counts).

        Reads all vectors from the store, recomputes the centroid and centroid_sum,
        resets mutations_since_recompute to 0 and needs_recompute to False, and
        updates the collection metadata. Preserves existing description fields.

        Short-circuit: when force=False, skips the full scan if the meta row already
        has needs_recompute=False and mutations_since_recompute=0.

        force=True bypasses the short-circuit entirely (crash-recovery / reindex path).
        """
        existing_meta = await self.store.get_collection_meta(collection, namespace=namespace)

        if not force:
            if (
                existing_meta is not None
                and existing_meta.needs_recompute is False
                and existing_meta.mutations_since_recompute == 0
            ):
                return

        vectors = await self.store.get_all_vectors(collection)

        description = existing_meta.description if existing_meta else None
        last_described = existing_meta.last_described if existing_meta else None
        described_at = existing_meta.described_at_doc_count if existing_meta else None

        # Determine C1 fields: preserve from existing meta, or use defaults for new collections.
        if existing_meta is not None:
            active_embedding_model = existing_meta.active_embedding_model
            pending_embedding_model = existing_meta.pending_embedding_model
            needs_reindex = existing_meta.needs_reindex
            reindex_job_id = existing_meta.reindex_job_id
        else:
            active_embedding_model = global_embedder.model_name
            pending_embedding_model = None
            needs_reindex = False
            reindex_job_id = None

        if not vectors:
            if force or existing_meta is not None:
                doc_count = await self.store.count_documents(collection)
                meta = CollectionMeta(
                    name=collection,
                    centroid=None,
                    centroid_sum=None,
                    description=description,
                    doc_count=doc_count,
                    chunk_count=0,
                    active_embedding_model=active_embedding_model,
                    pending_embedding_model=pending_embedding_model,
                    needs_reindex=needs_reindex,
                    reindex_job_id=reindex_job_id,
                    last_indexed=datetime.now(UTC),
                    last_described=last_described,
                    described_at_doc_count=described_at,
                    namespace=namespace,
                    description_embedding=None,
                    mutations_since_recompute=0,
                    needs_recompute=False,
                    schema_version=existing_meta.schema_version if existing_meta else 0,
                )
                await self.store.update_collection_meta(meta)
            return

        centroid_sum = elementwise_sum(vectors)
        chunk_count = len(vectors)
        centroid = [x / chunk_count for x in centroid_sum]
        doc_count = await self.store.count_documents(collection)

        if description is not None:
            description_embedding = await global_embedder.embed_one(description)
        else:
            description_embedding = None

        meta = CollectionMeta(
            name=collection,
            centroid=centroid,
            centroid_sum=centroid_sum,
            description=description,
            doc_count=doc_count,
            chunk_count=chunk_count,
            active_embedding_model=active_embedding_model,
            pending_embedding_model=pending_embedding_model,
            needs_reindex=needs_reindex,
            reindex_job_id=reindex_job_id,
            last_indexed=datetime.now(UTC),
            last_described=last_described,
            described_at_doc_count=described_at,
            namespace=namespace,
            description_embedding=description_embedding,
            mutations_since_recompute=0,
            needs_recompute=False,
            schema_version=existing_meta.schema_version if existing_meta else 0,
        )
        await self.store.update_collection_meta(meta)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_pipeline(
    cfg: SearchConfig,
    embedder_backend: EmbedderBackend | None = None,
    reranker_backend: RerankerBackend | None = None,
) -> SearchPipeline:
    """Build a SearchPipeline from a SearchConfig.

    Does NOT call store.connect() — caller is responsible for connecting.
    """
    from archon_search.language_detector import (  # noqa: PLC0415
        LanguageDetector,
        FASTTEXT_MODEL_FILENAME,
        get_fasttext_models_dir,
    )

    store = SearchStore(cfg.db_path)
    _embedder_backend: EmbedderBackend = embedder_backend or ModelEmbedder(
        cfg.embedding_model,
        providers=cfg.providers,
    )
    if cfg.reranker_model:
        _reranker_backend: RerankerBackend = reranker_backend or ModelReranker(
            cfg.reranker_model,
            providers=cfg.providers,
        )
        reranker: Reranker | None = Reranker(_reranker_backend)
    else:
        reranker = None
    embedder = Embedder(_embedder_backend)
    chunker = DocumentChunker(cfg.chunk_size)
    parser = DocumentParser()

    language_detector: LanguageDetector | None = None
    if cfg.multilingual:
        model_path = get_fasttext_models_dir() / FASTTEXT_MODEL_FILENAME
        language_detector = LanguageDetector(model_path)

    graph_extractor = None
    graph_store = None
    graph_expander = None
    if cfg.graph.enabled:
        from archon_search.graph_extractor import GraphExtractor  # noqa: PLC0415
        from archon_search.graph_store import GraphStore as _GraphStore  # noqa: PLC0415
        from archon_search.graph_expander import GraphExpander  # noqa: PLC0415
        graph_store = _GraphStore(cfg.db_path)
        graph_extractor = GraphExtractor(cfg.graph)
        graph_expander = GraphExpander(graph_store)

    return SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=chunker,
        parser=parser,
        top_k_retrieve=cfg.top_k_retrieve,
        top_k_return=cfg.top_k_return,
        max_fanout=cfg.max_fanout,
        fanout_leg_trim=cfg.fanout_leg_trim,
        fanout_timeout_seconds=cfg.fanout_timeout_seconds,
        language_detector=language_detector,
        language_detection_confidence_threshold=cfg.language_detection_confidence_threshold,
        max_file_mb=cfg.ingest.max_file_mb,
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=cfg.graph,
        graph_expander=graph_expander,
    )
