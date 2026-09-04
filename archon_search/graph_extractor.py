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
built from the same chunk's entities (BE-11).

LLM typed relationship extraction (LLCP BE-7) is gated by a SEPARATE
AND-condition: ``config.provider is not None AND config.extraction_model is
not None AND enrichment_client is not None``.  When open, one
``label_relationships`` call is made per plain-text chunk (after prose
extraction) and the returned typed edges are persisted additively alongside
both the engine's own typed edges and the ``related_to`` co-occurrence edges.
When any part of the gate is unset, enrichment is skipped silently (no
warning) — this is a normal, air-gap-safe configuration, not a failure.  A
per-chunk enrichment call that raises is caught, logged as a WARNING, and
that chunk falls back to whatever edges the prose engine and co-occurrence
already produced; it never fails the whole ``extract()`` call.

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
import json
import logging
import operator
import re
import importlib.util
from dataclasses import dataclass
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
from archon_search.paths import get_models_dir
from archon_search.prose_extraction_backend import ProseExtractionBackend

if TYPE_CHECKING:
    from archon_search.config import GraphConfig
    from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Public: retained ONLY for the (already-orphaned-at-runtime, BE-15-scoped-
# for-deletion) install wizard spaCy-provisioning flow — install/extras.py's
# own SPACY_MODEL_NAME copy and tests/test_install_spacy_model.py's
# closes-the-loop check. model_validation.py's startup probe no longer
# consumes this: the actual prose extraction engine since BE-11 is gliner
# (paths.GRAPH_NER_MODEL_NAME), never spaCy (cycle-2 C2-A-01/C2-B-1/C1-B-1).
SPACY_MODEL_NAME: str = "en_core_web_sm"

# Public: retained ONLY for the install wizard's own summary text (same
# already-orphaned-at-runtime spaCy provisioning flow as SPACY_MODEL_NAME
# above; tests/test_install_ui.py pins this alongside its own wording).
# model_validation.py's startup probe no longer discloses this: the shipped
# prose extraction engine (gliner, GRAPH_NER_MODEL_NAME — see paths.py,
# "knowledgator/gliner-relex-multi-v1.0") is multilingual, so there is
# nothing English-only to disclose on GET /status (cycle-2 C2-A-02/C2-B-2).
ENGLISH_ONLY_DISCLOSURE: str = (
    f"graph prose entity extraction is English-only ({SPACY_MODEL_NAME}) "
    "while multilingual = true; non-English documents contribute "
    "code-symbol entities only"
)

# Public: shared with model_validation.py's graph_ner_status so the
# "[graph] extra missing" message reads identically whether it is surfaced
# from ensure_graph_engine_importable's construction-time guard or from
# GET /status (2026-08-19-030 T6, renamed off spaCy in cycle-2 C2-A-01).
GLINER_NOT_INSTALLED_MESSAGE: str = (
    "gliner is not installed. Install the graph extras: pip install 'archon-search[graph]'"
)

# Wire-facing degradation notices for the prose extraction backend (BE-11).
# Sanitized (never carry exception text) per the same rule as the spaCy
# notices above. BE-12 owns tightening these into the pinned DETAIL/CODE
# constant pairs S14/S17/S46 call for; these are BE-11's minimal versions.
_BACKEND_UNAVAILABLE_WARNING: str = (
    "graph prose extraction model is unavailable; prose entity extraction is "
    "disabled for this ingest (code-symbol extraction is unaffected)."
)
_INFERENCE_FAILED_WARNING: str = (
    "graph prose entity/relation extraction failed; prose entity extraction "
    "was skipped for this document (code-symbol extraction is unaffected)."
)

# Comma-separated ">="/"<"-style clause operators, checked longest-prefix-first
# so ">=" is not mistaken for ">" (used by _specifier_satisfied below).
_SPECIFIER_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    "<": operator.lt,
}


_NAME_SPLIT_PATTERN = re.compile(r"\s*(?:/|,| and )\s*")


@dataclass(frozen=True)
class SpacyModelResolution:
    """Outcome of :func:`resolve_spacy_model` (2026-08-19-030 C1-I-3).

    ``target`` is the installed package name or the data-dir path once
    resolution succeeds, else ``None``. ``incompatible_versions`` names
    data-dir candidates that exist but whose ``meta.json`` ``spacy_version``
    range excludes the installed spaCy — "present but not usable", which the
    operator-facing message must distinguish from "absent entirely".
    """

    target: str | None
    incompatible_versions: list[str]


def _version_key(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple of ints.

    Stops at the first non-digit run (adequate for spaCy's plain ``X.Y.Z``
    releases). No dependency on ``packaging`` — it is only a transitive
    dependency here, not a declared one (2026-08-19-030 C1-I-2).
    """
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(itertools.takewhile(str.isdigit, piece))
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _model_version_key(path: Path) -> tuple[int, ...]:
    """Version-aware sort key for a ``en_core_web_sm-<version>`` directory name."""
    _, _, version = path.name.partition(f"{SPACY_MODEL_NAME}-")
    return _version_key(version)


def _specifier_satisfied(specifier: str, version: str) -> bool:
    """Check *version* against a comma-separated ``>=``/``<``-style specifier.

    Covers the clause forms spaCy's ``meta.json`` publishes for its
    ``spacy_version`` field (e.g. ``">=3.8.0,<3.9.0"``). Raises ``ValueError``
    on an unrecognized clause so the caller can fail open rather than
    mis-evaluate.
    """
    parsed = _version_key(version)
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        for op_symbol in _SPECIFIER_OPS:  # declared longest-prefix-first
            if clause.startswith(op_symbol):
                operand = clause[len(op_symbol):].strip()
                # A wildcard/pre-release operand (`3.8.*`, `3.9.0a1`) would
                # truncate to a shorter tuple and compare wrongly — treat it as
                # unrecognized so the caller fails OPEN rather than declaring a
                # usable model incompatible (2026-08-19-030 C2-B-18).
                if not re.fullmatch(r"\d+(?:\.\d+)*", operand):
                    raise ValueError(f"unrecognized version operand: {operand!r}")
                target = _version_key(operand)
                if not _SPECIFIER_OPS[op_symbol](parsed, target):
                    return False
                break
        else:
            raise ValueError(f"unrecognized version clause: {clause!r}")
    return True


def _model_dir_compatible(model_dir: Path, installed_spacy_version: str) -> bool:
    """Return ``False`` only when ``meta.json`` unambiguously rules out
    *installed_spacy_version* (2026-08-19-030 C1-I-3).

    Missing/unreadable ``meta.json``, an absent or unparseable
    ``spacy_version`` field, or an unrecognized clause all read as
    compatible (fail open) — this check exists only to stop a KNOWN-stale
    model directory from reporting healthy on ``GET /status``;
    ``spacy.load()`` at ingest time remains the final authority.
    """
    try:
        meta = json.loads((model_dir / "meta.json").read_text())
    except (OSError, ValueError):
        return True
    spec = meta.get("spacy_version")
    if not isinstance(spec, str) or not spec.strip() or not installed_spacy_version:
        return True
    try:
        return _specifier_satisfied(spec, installed_spacy_version)
    except ValueError:
        return True


def resolve_spacy_model() -> SpacyModelResolution:
    """Resolve a loadable ``en_core_web_sm`` and classify what was found.

    Resolution order (2026-08-19-030): the installed package first — for pip
    installs that already carry it — then the wizard-provisioned directory
    under ``get_models_dir() / "spacy"``, version-sorted (not lexicographic — a
    directory-name compare would rank ``3.9.0`` before ``3.10.0``) and
    filtered to versions compatible with the installed spaCy (C1-I-3).
    Nothing here installs or downloads: a ``uv tool`` venv has no package
    installer, so a runtime download can only ever fail. Both fields read
    empty/``None`` when spaCy itself is not importable.
    """
    try:
        import spacy.util  # noqa: PLC0415
    except ImportError:
        return SpacyModelResolution(target=None, incompatible_versions=[])

    # `import spacy.util` above also binds the `spacy` package name itself.
    installed_spacy_version = getattr(spacy, "__version__", "")

    incompatible: list[str] = []
    if SPACY_MODEL_NAME in spacy.util.get_installed_models():
        # The installed package gets the same compatibility check as a data-dir
        # candidate. Without it, an old pinned `en_core_web_sm` left over from a
        # previous spaCy shadows a correctly provisioned data-dir model
        # permanently — resolution returns here and never reaches the fallback
        # — while reporting healthy on GET /status (2026-08-19-030 C2-I-6).
        try:
            package_path = Path(spacy.util.get_package_path(SPACY_MODEL_NAME))
        except Exception:  # noqa: BLE001 — an unreadable package reads as usable
            return SpacyModelResolution(target=SPACY_MODEL_NAME, incompatible_versions=[])
        model_dirs = [package_path, *sorted(package_path.glob(f"{SPACY_MODEL_NAME}-*"))]
        for model_dir in model_dirs:
            if (model_dir / "meta.json").is_file():
                if _model_dir_compatible(model_dir, installed_spacy_version):
                    return SpacyModelResolution(
                        target=SPACY_MODEL_NAME, incompatible_versions=[]
                    )
                incompatible.append(f"{SPACY_MODEL_NAME} (installed package)")
                break
        else:
            # No meta.json anywhere in the package — fail open, as elsewhere.
            return SpacyModelResolution(target=SPACY_MODEL_NAME, incompatible_versions=[])

    # Strict `X.Y.Z` suffix: a bare glob also matches siblings like
    # `en_core_web_sm-3.8.0.bak`, which `_model_version_key` parses to the same
    # key as the real directory, making `max()` pick between them arbitrarily
    # (2026-08-19-030 C2-I-21). Mirrors the wizard's own guard in extras.py.
    candidates = [
        path
        for path in (get_models_dir() / "spacy").glob(f"{SPACY_MODEL_NAME}-*")
        if re.fullmatch(rf"{re.escape(SPACY_MODEL_NAME)}-\d+\.\d+\.\d+", path.name)
        and (path / "config.cfg").is_file()
    ]
    compatible: list[Path] = []
    for path in candidates:
        if _model_dir_compatible(path, installed_spacy_version):
            compatible.append(path)
        else:
            incompatible.append(path.name)

    if not compatible:
        return SpacyModelResolution(target=None, incompatible_versions=sorted(incompatible))

    newest = max(compatible, key=_model_version_key)
    return SpacyModelResolution(target=str(newest), incompatible_versions=sorted(incompatible))


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


def find_spacy_model() -> str | None:
    """Return a loadable ``en_core_web_sm`` reference, or ``None`` if absent.

    Thin wrapper around :func:`resolve_spacy_model` for callers that only
    need the resolved target, not the incompatible-versions detail.
    """
    return resolve_spacy_model().target


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


def _build_typed_relation_edge(
    src_id: str, tgt_id: str, label: str, doc_id: str
) -> GraphEdge | None:
    """Build a typed relation edge, or ``None`` when it must be skipped.

    Shared by BOTH the prose engine's own directed relations and LLM
    relationship labeling (cycle-2 C2-I-1/C2-I-2/C2-B-5) so the two guards
    below cannot drift apart between the paths:

    - ``label == RelationshipType.related_to.value``: the co-occurrence loop
      already produces the sorted()-normalised, undirected ``related_to``
      edge for every pair (C1-I-2) -- persisting a second, directed one here
      (from either the engine or an LLM echoing the same label) would double it.
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
    never a fatal abort (C2). The class is NOT designed for concurrent access
    from multiple coroutines on the same instance; the pipeline creates one
    shared instance per server process.
    """

    def __init__(
        self,
        config: "GraphConfig",
        enrichment_client: "LLMEnrichmentClientProtocol | None" = None,
    ) -> None:
        self._config = config
        self._enrichment_client = enrichment_client
        self._backend = ProseExtractionBackend(providers=config.providers)
        # Latches the inference-call-failure traceback to one log per process
        # (mirrors the pre-BE-11 spaCy NER-call latch) — the per-document
        # `warnings` entry still fires every time.
        self._inference_failure_logged: bool = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_backend(self) -> str | None:
        """Load the prose extraction backend at most once per process.

        Returns ``None`` when ready for inference, or a sanitized warning
        when the model artifact itself could not be loaded. ``load()`` is
        itself idempotent and latches a failed load (``ProseExtractionBackend``,
        BE-8), so calling it again here after a failure is cheap and never
        re-attempts a load that already failed.
        """
        try:
            await self._backend.load()
        except asyncio.CancelledError:
            # Cancellation says nothing about the model — never latch it as
            # unavailable over a shutdown/job-cancel landing in the load window.
            raise
        except BaseException:
            # `BaseException`, not `Exception`: mirrors the pre-BE-11 spaCy
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
            return _BACKEND_UNAVAILABLE_WARNING
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
        # Typed relationship edges — from the prose engine's own directed
        # relations (BE-11) and/or LLM relationship labeling (LLCP BE-7) —
        # additive alongside the related_to co-occurrence edges built below;
        # merged in after.
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
                warnings.append(_INFERENCE_FAILED_WARNING)
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
                    # returns real entity labels (the "other" decoy and
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

                # ----------------------------------------------------------
                # LLM relationship labeling (LLCP BE-7) — one call per text
                # chunk with 2+ distinct entities. Never fails the whole
                # extract() call: any exception here is caught, logged as a
                # WARNING, and this chunk falls back to co-occurrence edges
                # only (added below, unaffected).
                # ----------------------------------------------------------
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
                        llm_name_to_id = {
                            nodes[eid].entity_name: eid for eid in unique_ids_this_chunk
                        }

                        labeled = await self._enrichment_client.label_relationships(  # type: ignore[union-attr]
                            entity_pairs_by_name, text_chunk.text
                        )

                        for rel in labeled:
                            src_id, tgt_id = _resolve_labeled_pair(
                                rel.source_entity, rel.target_entity, llm_name_to_id
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
                            # Same guards as the engine's own relations above
                            # (cycle-2 C2-I-1/C2-I-2/C2-B-5): an LLM-returned
                            # related_to would double the co-occurrence edge,
                            # a self-loop carries no signal, and an unknown
                            # relationship_type must skip this one relation
                            # rather than aborting the whole chunk.
                            edge = _build_typed_relation_edge(
                                src_id, tgt_id, rel.relationship_type, doc_id
                            )
                            if edge is not None and edge.id not in typed_edges:
                                typed_edges[edge.id] = edge
                    except Exception as exc:  # noqa: BLE001
                        _logger.warning(
                            "GraphExtractor: LLM relationship labeling failed for chunk "
                            "%s: %s; falling back to co-occurrence edges for this chunk",
                            text_chunk.chunk_id,
                            exc,
                        )
                        warnings.append(
                            f"LLM relationship labeling failed for chunk "
                            f"{text_chunk.chunk_id!r}; used co-occurrence "
                            "edges instead."
                        )

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
