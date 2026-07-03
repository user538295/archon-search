"""GraphExtractor — Interface Adapters layer, E1a GraphRAG.

Wraps spaCy NER for entity extraction from text chunks and builds a graph of
co-occurrence edges.  For C3-enriched code chunks (``symbol_type != None``),
uses the code-symbol extraction path instead of spaCy NER — this avoids
double-processing and misclassification of code identifiers.

LLM typed relationship extraction is config-guarded in E1a: when
``extraction_model`` is configured, a WARNING is logged and the extractor
falls back to spaCy-only.  Full LLM extraction is deferred until an eval
baseline exists.

All CPU-bound spaCy operations inside ``extract()`` are wrapped in
``asyncio.to_thread()`` because spaCy NER is CPU-bound and the ingest
pipeline is async.

Edge creation (spaCy-only mode):
  For each pair of distinct entities co-occurring within the SAME CHUNK, ONE
  directed edge is created per ordered pair where ``source_id < target_id``
  (lexicographic comparison), making the graph de-facto undirected without
  doubling edges.  For N entities in a chunk this produces N*(N-1)/2 edges.
  Entity pairs already sharing an edge (by stable edge ID) are upserted —
  no duplicates; GraphStore handles the upsert via ``merge_insert``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from archon_search.graph_types import (
    ChunkInput,
    EntityType,
    GraphEdge,
    GraphExtractionResult,
    GraphMention,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)

if TYPE_CHECKING:
    from archon_search.config import GraphConfig

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPACY_MODEL: str = "en_core_web_sm"

# Mapping from spaCy NER labels to EntityType.
# Numeric / temporal categories (DATE, TIME, MONEY, PERCENT, QUANTITY,
# ORDINAL, CARDINAL) are intentionally absent — they are noise for the graph.
_LABEL_TO_ENTITY_TYPE: dict[str, EntityType] = {
    "PERSON": EntityType.person,
    "ORG": EntityType.system,
    "GPE": EntityType.system,
    "LOC": EntityType.system,
    "FAC": EntityType.system,
    "PRODUCT": EntityType.system,
    "EVENT": EntityType.event,
    "WORK_OF_ART": EntityType.concept,
    "LAW": EntityType.concept,
    "LANGUAGE": EntityType.concept,
    "NORP": EntityType.concept,
}


# ---------------------------------------------------------------------------
# GraphExtractor
# ---------------------------------------------------------------------------


class GraphExtractor:
    """Extracts graph entities and co-occurrence edges from document chunks.

    Thread-safety: ``_nlp`` is set lazily on first call to ``extract()``.
    The class is NOT designed for concurrent access from multiple coroutines
    on the same instance.  The pipeline creates one shared instance per server
    process; ``asyncio.to_thread()`` serialises CPU-bound spaCy calls into the
    default thread-pool executor.
    """

    def __init__(self, config: "GraphConfig") -> None:
        self._config = config
        self._nlp: object = None  # spaCy NLP model; loaded lazily on first call
        self._load_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers (synchronous — called inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _load_nlp_sync(self) -> object:
        """Load the spaCy NLP model synchronously.

        If ``en_core_web_sm`` is not in the list of installed models, it is
        auto-downloaded and an INFO log is emitted (same transparency pattern
        as fastembed auto-download).  Must be called inside
        ``asyncio.to_thread()`` — do not call directly from async code.
        """
        import spacy  # noqa: PLC0415
        import spacy.cli  # noqa: PLC0415
        import spacy.util  # noqa: PLC0415

        if _SPACY_MODEL not in spacy.util.get_installed_models():
            _logger.info(
                "spaCy model %r not found; auto-downloading (first call only).",
                _SPACY_MODEL,
            )
            spacy.cli.download(_SPACY_MODEL)

        return spacy.load(_SPACY_MODEL)

    def _run_ner_sync(
        self,
        nlp: object,
        texts: list[str],
    ) -> list[list[tuple[str, str]]]:
        """Run spaCy NER on a list of texts synchronously.

        Returns one list of ``(entity_text, spaCy_label)`` tuples per input text.
        Must be called inside ``asyncio.to_thread()`` — do not call directly from
        async code.
        """
        results: list[list[tuple[str, str]]] = []
        for text in texts:
            doc = nlp(text)  # type: ignore[operator]
            results.append([(ent.text, ent.label_) for ent in doc.ents])
        return results

    def _code_symbol_name(self, chunk: ChunkInput) -> str:
        """Derive the entity name for a C3 code chunk.

        Priority order:
        1. ``containing_function`` — for function-level chunks.
        2. ``containing_class`` — for class-level chunks.
        3. ``source_path`` basename (stem) — module-level fallback.
        4. ``f'unknown:{chunk.chunk_id}'`` — last resort when all fields are absent/empty (preserves chunk uniqueness).
        """
        if chunk.containing_function:
            return chunk.containing_function
        if chunk.containing_class:
            return chunk.containing_class
        if chunk.source_path:
            return Path(chunk.source_path).stem
        return f"unknown:{chunk.chunk_id}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(
        self,
        chunks: list[ChunkInput],
        doc_id: str,
        collection: str,
    ) -> GraphExtractionResult:
        """Extract entities and co-occurrence edges from a list of chunks.

        C3-enriched code chunks (``symbol_type != None``) use the code-symbol
        path; plain text chunks go through spaCy NER.

        All CPU-bound spaCy calls are wrapped in ``asyncio.to_thread()``.

        Returns a ``GraphExtractionResult``.  When ``fatal_error`` is non-None
        on the result, extraction failed completely; the pipeline should set
        ``IngestResult.status = "error"``.
        """
        warnings: list[str] = []
        llm_fallback_used = False

        # ------------------------------------------------------------------
        # LLM extraction stub (E1a) — log warning and continue spaCy-only.
        # ------------------------------------------------------------------
        if self._config.extraction_model:
            _logger.warning(
                "extraction_model %r is configured but LLM extraction is not "
                "implemented in E1a; falling back to spaCy-only extraction.",
                self._config.extraction_model,
            )
            warnings.append(
                f"LLM extraction model {self._config.extraction_model!r} is not "
                "available in E1a; fell back to spaCy-only extraction."
            )
            llm_fallback_used = True

        # ------------------------------------------------------------------
        # Partition chunks into code (C3) vs plain-text.
        # ------------------------------------------------------------------
        code_chunks = [c for c in chunks if c.symbol_type]
        text_chunks = [c for c in chunks if not c.symbol_type]

        # Per-chunk entity ID lists — used later for co-occurrence edge creation.
        chunk_entity_ids: list[list[str]] = []
        # Deduplicated node map across all chunks (id → GraphNode).
        nodes: dict[str, GraphNode] = {}
        # Mentions: entity incidence records for salience derivation (E2b).
        mentions: list[GraphMention] = []

        # ------------------------------------------------------------------
        # C3 code-symbol path — spaCy NER is NOT run on code chunks.
        # ------------------------------------------------------------------
        for chunk in code_chunks:
            name = self._code_symbol_name(chunk)
            entity_id = make_stable_entity_id(EntityType.code_symbol.value, name)
            if entity_id not in nodes:
                nodes[entity_id] = GraphNode(
                    id=entity_id,
                    entity_name=name,
                    entity_type=EntityType.code_symbol,
                    source_doc_id=doc_id,
                    collection_name=collection,
                    entity_subtype=chunk.symbol_subtype,
                )
            chunk_entity_ids.append([entity_id])
            # Add mention for the entity in this chunk (E2b)
            mentions.append(GraphMention(
                entity_id=entity_id,
                chunk_id=chunk.chunk_id,
                doc_id=doc_id,
            ))

        # ------------------------------------------------------------------
        # spaCy NER path for plain-text chunks.
        # ------------------------------------------------------------------
        if text_chunks:
            # Gate: check spaCy is importable.  Fires when the [graph] extras
            # are not installed (spaCy absent on the import path).
            async with self._load_lock:
                if self._nlp is None:
                    try:
                        import spacy as _spacy_probe  # noqa: F401, PLC0415
                    except ImportError:
                        error_msg = (
                            "spaCy is not installed. "
                            "Install the graph extras: pip install 'archon-search[graph]'"
                        )
                        return GraphExtractionResult(
                            nodes=list(nodes.values()),
                            edges=[],
                            mentions=[],
                            fatal_error=error_msg,
                            warnings=[error_msg],
                        )

                    # Load model (CPU-bound) in the default thread-pool executor.
                    try:
                        self._nlp = await asyncio.to_thread(self._load_nlp_sync)
                    except Exception as exc:
                        error_msg = (
                            f"Failed to load spaCy model {_SPACY_MODEL!r}: {exc}. "
                            f"On air-gapped installs, download the model manually: "
                            f"python -m spacy download {_SPACY_MODEL}"
                        )
                        return GraphExtractionResult(
                            nodes=list(nodes.values()),
                            edges=[],
                            mentions=[],
                            fatal_error=error_msg,
                            warnings=[error_msg],
                        )

            # Run NER (CPU-bound) in a thread pool.
            texts = [c.text for c in text_chunks]
            try:
                ner_per_chunk = await asyncio.to_thread(
                    self._run_ner_sync, self._nlp, texts
                )
            except Exception as exc:
                error_msg = (
                    f"spaCy NER failed: {exc}. "
                    f"On air-gapped installs, download the model manually: "
                    f"python -m spacy download {_SPACY_MODEL}"
                )
                return GraphExtractionResult(
                    nodes=list(nodes.values()),
                    edges=[],
                    mentions=[],
                    fatal_error=error_msg,
                    warnings=[error_msg],
                )

            for text_chunk, raw_entities in zip(text_chunks, ner_per_chunk):
                ids_this_chunk: list[str] = []
                for ent_text, ent_label in raw_entities:
                    entity_type = _LABEL_TO_ENTITY_TYPE.get(ent_label)
                    if entity_type is None:
                        continue  # skip noise labels (CARDINAL, DATE, etc.)
                    entity_id = make_stable_entity_id(entity_type.value, ent_text)
                    if entity_id not in nodes:
                        nodes[entity_id] = GraphNode(
                            id=entity_id,
                            entity_name=ent_text,
                            entity_type=entity_type,
                            source_doc_id=doc_id,
                            collection_name=collection,
                        )
                    ids_this_chunk.append(entity_id)
                    # Add mention for the entity in this chunk (E2b)
                    mentions.append(GraphMention(
                        entity_id=entity_id,
                        chunk_id=text_chunk.chunk_id,
                        doc_id=doc_id,
                    ))
                chunk_entity_ids.append(ids_this_chunk)

        # ------------------------------------------------------------------
        # Co-occurrence edge creation (spaCy-only mode).
        # For each chunk: ONE directed edge per ordered pair where
        # source_id < target_id (lexicographic).  N entities → N*(N-1)/2 edges.
        # ------------------------------------------------------------------
        edges: dict[str, GraphEdge] = {}
        for ids in chunk_entity_ids:
            # Deduplicate within this chunk while preserving first-occurrence order.
            seen: set[str] = set()
            unique_ids: list[str] = []
            for eid in ids:
                if eid not in seen:
                    unique_ids.append(eid)
                    seen.add(eid)

            # itertools.combinations on a sorted list guarantees src < tgt.
            for src_id, tgt_id in itertools.combinations(sorted(unique_ids), 2):
                edge_id = make_stable_edge_id(
                    src_id, tgt_id, RelationshipType.related_to.value
                )
                if edge_id not in edges:
                    edges[edge_id] = GraphEdge(
                        id=edge_id,
                        source_node_id=src_id,
                        target_node_id=tgt_id,
                        relationship_type=RelationshipType.related_to,
                        source_doc_id=doc_id,
                    )

        return GraphExtractionResult(
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            mentions=mentions,
            llm_fallback_used=llm_fallback_used,
            warnings=warnings,
        )
