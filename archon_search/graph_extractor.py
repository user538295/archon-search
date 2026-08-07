"""GraphExtractor — Interface Adapters layer, E1a GraphRAG.

Wraps spaCy NER for entity extraction from text chunks and builds a graph of
co-occurrence edges.  For C3-enriched code chunks (``symbol_type != None``),
uses the code-symbol extraction path instead of spaCy NER — this avoids
double-processing and misclassification of code identifiers.

LLM typed relationship extraction (LLCP BE-7) is gated by an AND-condition:
``config.provider is not None AND config.extraction_model is not None AND
enrichment_client is not None``.  When open, one ``label_relationships`` call
is made per plain-text chunk (after spaCy NER) and the returned typed edges
are persisted additively alongside the ``related_to`` co-occurrence edges.
When any part of the gate is unset, enrichment is skipped silently (no
warning) — this is a normal, air-gap-safe configuration, not a failure.  A
per-chunk enrichment call that raises is caught, logged as a WARNING, and
that chunk falls back to spaCy-only co-occurrence edges; it never fails the
whole ``extract()`` call.

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
import re
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
    make_code_symbol_qualified_name,
    make_stable_edge_id,
    make_stable_entity_id,
)

if TYPE_CHECKING:
    from archon_search.config import GraphConfig
    from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol

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


_NAME_SPLIT_PATTERN = re.compile(r"\s*(?:/|,| and )\s*")


def _resolve_labeled_pair(
    source_raw: str, target_raw: str, name_to_id: dict[str, str]
) -> tuple[str | None, str | None]:
    """Resolve a labeled relationship's entity names to node IDs.

    Small local models occasionally merge both entity names of a pair into a
    single field (e.g. ``source_entity="Bob / Google"``,
    ``target_entity="Google"``) instead of keeping them separate. When one
    side resolves directly and the other splits into exactly two known
    names — one of which is the side that already resolved — recover the
    missing side as the other split part. Returns ``(None, None)`` when
    recovery isn't possible.
    """
    src_id = name_to_id.get(source_raw)
    tgt_id = name_to_id.get(target_raw)
    if src_id is not None and tgt_id is not None:
        return src_id, tgt_id

    if src_id is None and tgt_id is not None:
        parts = [p for p in _NAME_SPLIT_PATTERN.split(source_raw) if p]
        others = {name_to_id[p] for p in parts if p in name_to_id} - {tgt_id}
        if len(parts) == 2 and len(others) == 1:
            return next(iter(others)), tgt_id

    if tgt_id is None and src_id is not None:
        parts = [p for p in _NAME_SPLIT_PATTERN.split(target_raw) if p]
        others = {name_to_id[p] for p in parts if p in name_to_id} - {src_id}
        if len(parts) == 2 and len(others) == 1:
            return src_id, next(iter(others))

    return None, None


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

    def __init__(
        self,
        config: "GraphConfig",
        enrichment_client: "LLMEnrichmentClientProtocol | None" = None,
    ) -> None:
        self._config = config
        self._enrichment_client = enrichment_client
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
            try:
                spacy.cli.download(_SPACY_MODEL)
            except SystemExit as exc:
                # spaCy calls sys.exit(1) when no package installer (pip/uv) is found.
                # SystemExit is BaseException, not Exception, so it escapes the caller's
                # `except Exception` and crashes the server. Convert to RuntimeError here.
                raise RuntimeError(
                    f"spaCy model download failed (no package installer found; exit code {exc.code}). "
                    f"Download the model manually: python -m spacy download {_SPACY_MODEL}"
                ) from exc

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
        # LLM relationship-labeling AND-gate (LLCP BE-7). When closed, this is
        # a normal, air-gap-safe configuration -- no warning, no fallback flag.
        # ------------------------------------------------------------------
        enrichment_gate_open = (
            self._config.provider is not None
            and self._config.extraction_model is not None
            and self._enrichment_client is not None
        )

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
        # LLM-typed relationship edges (LLCP BE-7) — additive alongside the
        # related_to co-occurrence edges built below; merged in after.
        llm_edges: dict[str, GraphEdge] = {}

        # ------------------------------------------------------------------
        # C3 code-symbol path — spaCy NER is NOT run on code chunks.
        # ------------------------------------------------------------------
        for chunk in code_chunks:
            name = self._code_symbol_name(chunk)
            # File-qualify the hash input only (E2g BE-2, Critical #2): two
            # unrelated same-named symbols in different files must hash to
            # distinct node IDs. ``entity_name`` below stays the bare `name`
            # — never file-qualified (Critical #3). When `source_path` is
            # absent the qualifier degrades to just `name`, preserving the
            # pre-BE-2 ID for chunks with no path information.
            qualified_name = make_code_symbol_qualified_name(name, chunk.source_path)
            entity_id = make_stable_entity_id(EntityType.code_symbol.value, qualified_name)
            if entity_id not in nodes:
                nodes[entity_id] = GraphNode(
                    id=entity_id,
                    entity_name=name,
                    entity_type=EntityType.code_symbol,
                    source_doc_id=doc_id,
                    collection_name=collection,
                    entity_subtype=chunk.symbol_subtype,
                    source_path=chunk.source_path,
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

                # --------------------------------------------------------
                # LLM relationship labeling (LLCP BE-7) — one call per text
                # chunk with 2+ distinct entities, after spaCy NER. Never
                # fails the whole extract() call: any exception here is
                # caught, logged as a WARNING, and this chunk falls back to
                # spaCy-only co-occurrence edges (added below, unaffected).
                # --------------------------------------------------------
                seen_this_chunk: set[str] = set()
                unique_ids_this_chunk: list[str] = []
                for eid in ids_this_chunk:
                    if eid not in seen_this_chunk:
                        unique_ids_this_chunk.append(eid)
                        seen_this_chunk.add(eid)

                if enrichment_gate_open and len(unique_ids_this_chunk) >= 2:
                    try:
                        pairs_this_chunk = list(
                            itertools.combinations(sorted(unique_ids_this_chunk), 2)
                        )
                        entity_pairs_by_name = [
                            (nodes[a].entity_name, nodes[b].entity_name)
                            for a, b in pairs_this_chunk
                        ]
                        name_to_id = {
                            nodes[eid].entity_name: eid for eid in unique_ids_this_chunk
                        }

                        labeled = await self._enrichment_client.label_relationships(  # type: ignore[union-attr]
                            entity_pairs_by_name, text_chunk.text
                        )

                        for rel in labeled:
                            src_id, tgt_id = _resolve_labeled_pair(
                                rel.source_entity, rel.target_entity, name_to_id
                            )
                            if src_id is None or tgt_id is None:
                                _logger.warning(
                                    "GraphExtractor: LLM returned an unknown entity name "
                                    "in relationship (%r -> %r) for chunk %s; skipping",
                                    rel.source_entity,
                                    rel.target_entity,
                                    text_chunk.chunk_id,
                                )
                                continue
                            edge_id = make_stable_edge_id(
                                src_id, tgt_id, rel.relationship_type
                            )
                            if edge_id not in llm_edges:
                                llm_edges[edge_id] = GraphEdge(
                                    id=edge_id,
                                    source_node_id=src_id,
                                    target_node_id=tgt_id,
                                    relationship_type=RelationshipType(rel.relationship_type),
                                    source_doc_id=doc_id,
                                )
                    except Exception as exc:  # noqa: BLE001
                        _logger.warning(
                            "GraphExtractor: LLM relationship labeling failed for chunk "
                            "%s: %s; falling back to spaCy-only co-occurrence edges for "
                            "this chunk",
                            text_chunk.chunk_id,
                            exc,
                        )
                        warnings.append(
                            f"LLM relationship labeling failed for chunk "
                            f"{text_chunk.chunk_id!r}: {exc}; used spaCy-only "
                            "co-occurrence edges instead."
                        )
                        llm_fallback_used = True

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

        # LLM-typed edges are additive: merge in alongside (never over) the
        # related_to co-occurrence edges above — distinct relationship_type
        # values produce distinct stable edge IDs, so no key collision.
        edges.update(llm_edges)

        return GraphExtractionResult(
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            mentions=mentions,
            llm_fallback_used=llm_fallback_used,
            warnings=warnings,
        )
