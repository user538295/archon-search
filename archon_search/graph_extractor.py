"""GraphExtractor — Interface Adapters layer, E1a/E1c GraphRAG.

Delegates prose entity/relation extraction to a shared ``ProseExtractionBackend``
instance (BE-8/BE-9, ``archon_search/prose_extraction_backend.py``) and builds
a graph of co-occurrence edges from the entities it returns. For C3-enriched
code chunks (``symbol_type != None``), uses the code-symbol extraction path
instead — this avoids double-processing and misclassification of code
identifiers; code chunks never reach the prose engine.

Entity types are the engine's own ``EntityType`` labels, used verbatim — no
intermediate vocabulary. Directed relations the engine returns are persisted
as typed edges, additive alongside the ``related_to`` co-occurrence edges
built from the same chunk's entities (BE-11) — the sole source of typed prose
edges since BE-17 removed the LLM relationship-labelling path.

Edge creation (co-occurrence):
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
import importlib.util
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
from archon_search.prose_extraction_backend import (
    ProseExtractionBackend,
    ProseExtractionLoadTimeoutError,
)

if TYPE_CHECKING:
    from archon_search.config import GraphConfig

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Public: shared with model_validation.py's graph_ner_status so the
# "[graph] extra missing" message reads identically whether it is surfaced
# from ensure_graph_engine_importable's construction-time guard or from
# GET /status (2026-08-19-030 T6, renamed off the prior NER engine in cycle-2 C2-A-01).
GLINER_NOT_INSTALLED_MESSAGE: str = (
    "gliner is not installed. Install the graph extras: pip install 'archon-search[graph]'"
)

# Wire-facing degradation notices for the prose extraction backend (BE-11,
# tightened by BE-12 for S14/S17/S46). Sanitized (never carry exception text
# or str(exc)) per the same rule as the gliner notice above, and per
# parser.py's ``_PARSE_WORKER_LOST_DETAIL`` convention — a DETAIL-only
# constant, since ``GraphExtractionResult.warnings`` is plain ``list[str]``
# with no separate wire-facing "code" field (graph_types.py) to pair it with.
_BACKEND_UNAVAILABLE_DETAIL: str = (
    "graph prose extraction model is unavailable; prose entity extraction is "
    "disabled for this ingest (code-symbol extraction is unaffected)."
)
_INFERENCE_FAILED_DETAIL: str = (
    "graph prose entity/relation extraction failed; prose entity extraction "
    "was skipped for this document (code-symbol extraction is unaffected)."
)
# S46: a bounded WAITER timeout on another caller's in-flight backend load
# (prose_extraction_backend.ProseExtractionLoadTimeoutError, a module-level
# class, not nested under ProseExtractionBackend) — never a 503, always a
# per-document degrade like the two notices above.
_LOAD_WAIT_TIMEOUT_DETAIL: str = (
    "graph prose extraction model is still loading for another document; "
    "prose entity extraction is disabled for this ingest (code-symbol "
    "extraction is unaffected)."
)

def gliner_absent() -> bool:
    """Return ``True`` when ``gliner`` is not importable.

    Shared by :func:`ensure_graph_engine_importable`'s construction-time guard
    and ``model_validation.graph_ner_status``'s ``GET /status`` probe so the
    two presence checks cannot drift apart (C3-B-1).

    `find_spec` avoids paying gliner's own multi-second cold import (it pulls
    torch/transformers transitively) just to probe presence — but a
    present-but-`__spec__`-less entry in `sys.modules` (the shape the test
    suite's `sys.modules["gliner"] = None` absence stub takes) makes the bare
    form raise `ValueError` instead of returning a clean `None`. A
    `ValueError` only happens when *something* is already reachable under
    that name, so it reads as "found", never as "absent".
    """
    try:
        return importlib.util.find_spec("gliner") is None
    except ValueError:
        return False


def ensure_graph_engine_importable(config: "GraphConfig") -> None:
    """Raise ``ConfigError`` when graph is enabled but the prose extraction
    engine (``gliner``) is not importable.

    The single implementation behind both construction-time guards
    (``server/app.py``'s ``_check_graph_deps`` and ``pipeline.create_pipeline``),
    so the two cannot drift. A missing ``[graph]`` extra is an operator
    misconfiguration: failing at construction beats failing every ingest
    pre-persist (2026-08-19-030). No-ops when graph is disabled.
    """
    if not config.enabled:
        return
    from archon_search.config import ConfigError  # noqa: PLC0415

    if gliner_absent():
        raise ConfigError(f"graph.enabled=true but {GLINER_NOT_INSTALLED_MESSAGE}")


def _build_typed_relation_edge(
    src_id: str, tgt_id: str, label: str, doc_id: str
) -> GraphEdge | None:
    """Build a typed relation edge, or ``None`` when it must be skipped.

    Fed by the prose engine's own directed relations (cycle-2
    C2-I-1/C2-I-2/C2-B-5). The guards below hold for every relation the
    engine emits:

    - ``label == RelationshipType.related_to.value``: the co-occurrence loop
      already produces the sorted()-normalised, undirected ``related_to``
      edge for every pair (C1-I-2) -- persisting a second, directed one here
      would double it.
    - ``src_id == tgt_id``: a self-loop carries no graph signal and must
      never be persisted, regardless of which path produced it.

    An unrecognized *label* degrades to a per-relation skip (debug-logged),
    never a raised ``ValueError`` — one bad label must not discard every
    other relation for the chunk.
    """
    if label == RelationshipType.related_to.value:
        return None
    if src_id == tgt_id:
        return None
    try:
        relationship_type = RelationshipType(label)
    except ValueError:
        _logger.debug(
            "GraphExtractor: unknown relation label %r; skipping", label
        )
        return None
    edge_id = make_stable_edge_id(src_id, tgt_id, label)
    return GraphEdge(
        id=edge_id,
        source_node_id=src_id,
        target_node_id=tgt_id,
        relationship_type=relationship_type,
        source_doc_id=doc_id,
    )


# ---------------------------------------------------------------------------
# GraphExtractor
# ---------------------------------------------------------------------------


class GraphExtractor:
    """Extracts graph entities and co-occurrence edges from document chunks.

    Prose entity/relation extraction (BE-11) delegates to a shared
    ``ProseExtractionBackend`` instance — ``gliner`` importability is already
    guaranteed by the time ``extract()`` runs (``ensure_graph_engine_importable``
    gates construction, in ``server/app.py``/``pipeline.py``), so only the
    model *artifact* itself can still fail to load here; that is a degrade,
    never a fatal abort (C2). ``extract()`` calls to the SAME instance from
    concurrent coroutines are safe with respect to the shared
    ``ProseExtractionBackend``'s load/waiter mechanism — one caller loads,
    the rest wait, and a wait-timeout degrades just that document (Task
    BE-12, S46) without disturbing the in-flight load. The remaining mutable
    instance state (``self._inference_failure_logged``) is a best-effort
    once-per-process log latch, not a correctness-critical value — a benign
    race there can at most log one extra WARNING, never corrupt extraction
    results. The pipeline creates one shared instance per server process.
    """

    def __init__(
        self,
        config: "GraphConfig",
    ) -> None:
        self._config = config
        self._backend = ProseExtractionBackend(providers=config.providers)
        # Latches the inference-call-failure traceback to one log per process
        # (mirrors the pre-BE-11 prose NER-call latch) — the per-document
        # `warnings` entry still fires every time.
        self._inference_failure_logged: bool = False

    @property
    def load_count(self) -> int:
        """Passthrough to the backend's `load_count` (C1 `EngineCapability.loadCount`,
        S26/S48) — spares callers a private `_backend` reach for the one figure the
        real-artifact lane's non-vacuity assert needs."""
        return self._backend.load_count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_backend(self) -> str | None:
        """Load the prose extraction backend, latching a genuine success or
        failure at most once per process — with one deliberate exception.

        Returns ``None`` when ready for inference, or a sanitized warning
        when the model artifact itself could not be loaded. ``load()`` is
        itself idempotent and latches a genuine load failure
        (``ProseExtractionBackend``, BE-8), so calling it again here after
        such a failure is cheap and never re-attempts a load that already
        failed. A ``ProseExtractionLoadTimeoutError`` (S46) is the exception
        to that "at most once" claim: a timed-out WAITER's failure is
        deliberately NOT latched — it carries no information about whether
        the model itself is loadable, only that this caller didn't wait long
        enough for someone else's load — so the next call here always gets a
        genuine fresh wait/load attempt, never a cached timeout.
        """
        try:
            await self._backend.load()
        except asyncio.CancelledError:
            # Cancellation says nothing about the model — never latch it as
            # unavailable over a shutdown/job-cancel landing in the load window.
            raise
        except ProseExtractionLoadTimeoutError:
            # S46: we were a WAITER on another caller's in-flight load, and it
            # didn't finish within the bounded wait. The loader itself is
            # unaffected — degrade only this document, never raise/503.
            _logger.warning(
                "GraphExtractor: timed out waiting for the prose extraction "
                "backend's in-flight load"
            )
            return _LOAD_WAIT_TIMEOUT_DETAIL
        except BaseException:
            # `BaseException`, not `Exception`: mirrors the pre-BE-11 prose
            # model-load path, which historically raised `SystemExit` — a
            # narrower catch here would let that escape uncaught and crash
            # the process, defeating the whole point of degrading instead of
            # aborting. `exc_info=True` preserves the traceback so a load
            # failure (import error, torch/gliner issue) is diagnosable
            # instead of silently degrading forever with no trace (C1-A-04).
            _logger.warning(
                "GraphExtractor: prose extraction backend failed to load",
                exc_info=True,
            )
            return _BACKEND_UNAVAILABLE_DETAIL
        return None

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
        path; plain text chunks go through the prose extraction backend
        (``ProseExtractionBackend``, BE-8/BE-9) — entities carry the engine's
        own ``EntityType`` label verbatim (no intermediate vocabulary), and
        any directed relations the engine returns are persisted as typed
        edges, additive alongside the ``related_to`` co-occurrence edges.

        Returns a ``GraphExtractionResult``.  ``fatal_error`` is reserved for
        "the extraction package is not importable" — unreachable from here
        since ``ensure_graph_engine_importable`` already gates ``GraphExtractor``
        construction (``server/app.py``/``pipeline.py``). A missing/unusable
        model *artifact*, or an inference call that raises, degrades instead:
        ``fatal_error`` stays None, prose extraction is skipped, code-symbol
        output is unaffected, and a warning is appended to ``warnings``.
        """
        warnings: list[str] = []
        degraded = False

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
        # Typed relationship edges — from the prose engine's own directed
        # relations (BE-11) — additive alongside the related_to co-occurrence
        # edges built below; merged in after.
        typed_edges: dict[str, GraphEdge] = {}

        # ------------------------------------------------------------------
        # C3 code-symbol path — the prose extraction backend is NOT run on
        # code chunks.
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
        # Prose extraction backend path for plain-text chunks (BE-11).
        # ------------------------------------------------------------------
        if text_chunks:
            # Gate: load the backend. Only a missing/unusable model artifact
            # can fail here — gliner importability is already guaranteed by
            # construction-time's ensure_graph_engine_importable.
            load_warning = await self._ensure_backend()
            if load_warning is not None:
                # Degraded: no prose extraction this run. Code-symbol nodes,
                # mentions and edges collected above still flow through to
                # the caller, and the file still embeds and persists.
                warnings.append(load_warning)
                text_chunks = []
                degraded = True

        if text_chunks:
            texts = [c.text for c in text_chunks]
            try:
                batch = await self._backend.inference(
                    texts, self._config.ner_confidence, self._config.relation_confidence
                )
            except Exception:
                # Auxiliary failure: warn and drop prose extraction for this
                # document rather than failing the ingest. The traceback is
                # logged once per process, not once per document.
                if not self._inference_failure_logged:
                    self._inference_failure_logged = True
                    _logger.warning(
                        "GraphExtractor: prose extraction inference failed", exc_info=True
                    )
                else:
                    _logger.debug(
                        "GraphExtractor: prose extraction inference failed again",
                        exc_info=True,
                    )
                warnings.append(_INFERENCE_FAILED_DETAIL)
                # Set both explicitly rather than relying on zip()'s silent
                # truncate-to-shortest: a future `zip(..., strict=True)` in
                # the loop below must not raise inside the very handler whose
                # job is keeping the ingest alive.
                text_chunks = []
                batch = None
                degraded = True

            for text_chunk, chunk_extraction in zip(
                text_chunks, batch.chunks if batch is not None else []
            ):
                ids_this_chunk: list[str] = []
                name_to_id: dict[str, str] = {}
                for entity in chunk_extraction.entities:
                    # Use the engine's own label verbatim — no intermediate
                    # vocabulary (S2). ProseExtractionBackend only ever
                    # returns real entity labels (non-real labels and
                    # numeric/temporal noise are discarded before this point).
                    # An off-vocabulary label must still degrade gracefully
                    # (skip, not raise) rather than fail the whole ingest.
                    try:
                        entity_type = EntityType(entity.label)
                    except ValueError:
                        _logger.debug(
                            "GraphExtractor: unknown entity label %r from prose "
                            "engine; skipping entity %r",
                            entity.label,
                            entity.text,
                        )
                        continue
                    entity_id = make_stable_entity_id(entity_type.value, entity.text)
                    if entity_id not in nodes:
                        nodes[entity_id] = GraphNode(
                            id=entity_id,
                            entity_name=entity.text,
                            entity_type=entity_type,
                            source_doc_id=doc_id,
                            collection_name=collection,
                        )
                    ids_this_chunk.append(entity_id)
                    name_to_id[entity.text] = entity_id
                    # Add mention for the entity in this chunk (E2b)
                    mentions.append(GraphMention(
                        entity_id=entity_id,
                        chunk_id=text_chunk.chunk_id,
                        doc_id=doc_id,
                    ))
                chunk_entity_ids.append(ids_this_chunk)

                # ----------------------------------------------------------
                # Directed relations from the prose engine (BE-11, S3/S4/S5):
                # persisted head→tail with no lexicographic normalisation —
                # additive alongside the related_to co-occurrence edges built
                # below, never overriding them.
                # ----------------------------------------------------------
                for relation in chunk_extraction.relations:
                    src_id = name_to_id.get(relation.head)
                    tgt_id = name_to_id.get(relation.tail)
                    if src_id is None or tgt_id is None:
                        continue
                    edge = _build_typed_relation_edge(
                        src_id, tgt_id, relation.label, doc_id
                    )
                    if edge is not None and edge.id not in typed_edges:
                        typed_edges[edge.id] = edge

        # ------------------------------------------------------------------
        # Co-occurrence edge creation.
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

        # Typed edges are additive: merge in alongside (never over) the
        # related_to co-occurrence edges above — distinct relationship_type
        # and/or direction produce distinct stable edge IDs, so no key
        # collision (S4).
        edges.update(typed_edges)

        return GraphExtractionResult(
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            mentions=mentions,
            warnings=warnings,
            degraded=degraded,
        )
