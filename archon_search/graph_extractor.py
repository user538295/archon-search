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
import json
import logging
import operator
import re
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

if TYPE_CHECKING:
    from archon_search.config import GraphConfig
    from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Public: the model name for every consumer that may import the graph layer —
# model_validation.py's startup probe does. install/extras.py deliberately does
# NOT: the install package must not depend on the graph layer, which is the same
# reason get_models_dir() lives in paths.py rather than here. Its copy at
# install/extras.py:SPACY_MODEL_NAME is intentional duplication, not an
# unfulfilled TODO; tests/test_install_spacy_model.py closes the loop by
# asserting the wizard's output is what the runtime resolver finds
# (2026-08-19-030 C2-B-4/C2-A-11).
SPACY_MODEL_NAME: str = "en_core_web_sm"

# Public: the English-only limitation of `en_core_web_sm` as worded for every
# consumer that may import the graph layer — model_validation.py's startup
# disclosure reuses this string verbatim (2026-08-19-030 / 2026-08-19-035).
# install/render.py's wizard summary carries its own wording for the same
# reason it carries its own SPACY_MODEL_NAME: the install package must not
# depend on the graph layer. That is deliberate, not a pending follow-up —
# tests/test_install_ui.py pins the shared phrase so the two cannot drift
# apart silently (C2-B-19/C2-T-16).
ENGLISH_ONLY_DISCLOSURE: str = (
    f"graph prose entity extraction is English-only ({SPACY_MODEL_NAME}) "
    "while multilingual = true; non-English documents contribute "
    "code-symbol entities only"
)

# Public: shared with model_validation.py so the "[graph] extra missing"
# message reads identically whether it is surfaced from a failed ingest or
# from GET /status (2026-08-19-030 T6).
SPACY_NOT_INSTALLED_MESSAGE: str = (
    "spaCy is not installed. Install the graph extras: pip install 'archon-search[graph]'"
)

# Wire-facing degradation notices. Deliberately free of exception text: they
# land in `GraphExtractionResult.warnings` → `IngestResult.warnings` (CLAUDE.md:
# never put `str(exc)` in a wire-facing field). The underlying exception is
# logged with a traceback instead. Two variants distinguish "never provisioned"
# from "provisioned but incompatible with the installed spaCy" (C1-I-3) — the
# operator fix differs (provision vs. re-provision).
_MODEL_ABSENT_WARNING: str = (
    f"spaCy model {SPACY_MODEL_NAME!r} is unavailable; prose entity extraction is "
    "disabled for this ingest (code-symbol extraction is unaffected). "
    "Run `archon-search wizard` to provision the model."
)
_MODEL_INCOMPATIBLE_WARNING: str = (
    f"spaCy model {SPACY_MODEL_NAME!r} is present under the data directory but "
    "incompatible with the installed spaCy version; prose entity extraction is "
    "disabled for this ingest (code-symbol extraction is unaffected). "
    "Re-run `archon-search wizard` to provision a compatible model."
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


def ensure_spacy_importable(config: "GraphConfig") -> None:
    """Raise ``ConfigError`` when graph is enabled but spaCy is not importable.

    The single implementation behind both construction-time guards
    (``server/app.py``'s ``_check_graph_deps`` and ``pipeline.create_pipeline``),
    so the two cannot drift. A missing ``[graph]`` extra is an operator
    misconfiguration: failing at construction beats failing every ingest
    pre-persist (2026-08-19-030). No-ops when graph is disabled.
    """
    if not config.enabled:
        return
    from archon_search.config import ConfigError  # noqa: PLC0415

    try:
        # `sys.modules["spacy"] = None` (how the tests stub absence) makes the
        # import statement itself raise ModuleNotFoundError — an ImportError
        # subclass — so the name is never bound and no `is None` check can run.
        import spacy  # type: ignore[import-untyped]  # noqa: PLC0415, F401
    except ImportError as exc:
        raise ConfigError(
            "graph.enabled=true but spacy is not installed; "
            "run: pip install archon-search[graph]"
        ) from exc


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


class _SpacyModelUnavailable(RuntimeError):
    """Raised by :meth:`GraphExtractor._load_nlp_sync` when no loadable model
    was resolved. Carries the absent/incompatible classification directly so
    the caller does not need a second :func:`resolve_spacy_model` filesystem
    probe just to pick the right wire-facing message (2026-08-19-030 T1) —
    ``resolve_spacy_model()`` already ran once, inside ``_load_nlp_sync``.
    """

    def __init__(self, message: str, *, incompatible: bool) -> None:
        super().__init__(message)
        self.incompatible = incompatible


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
        # Latches: each probe/failure runs at most once per process instead of
        # once per file (2026-08-19-030: 734 retries in 27 minutes in
        # production; C1-B-4: the "[graph] extra missing" probe had the same
        # unlatched-reprobe shape and is fixed the same way).
        self._spacy_not_importable: bool = False
        self._nlp_unavailable: bool = False
        self._nlp_unavailable_warning: str | None = None
        self._load_lock: asyncio.Lock = asyncio.Lock()
        # Latches the spaCy-NER-call-failure traceback to one log per process
        # (C1-I-4) — the per-document `warnings` entry still fires every time.
        self._ner_failure_logged: bool = False

    # ------------------------------------------------------------------
    # Internal helpers (synchronous — called inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _load_nlp_sync(self) -> object:
        """Load the spaCy NLP model synchronously.

        Resolves the model via :func:`resolve_spacy_model` and raises when it
        is neither installed nor provisioned (or provisioned but incompatible
        with the installed spaCy — C1-I-3) — runtime never downloads (the
        wizard does that; see 2026-08-19-030).  Must be called inside
        ``asyncio.to_thread()`` — do not call directly from async code.
        """
        import spacy  # noqa: PLC0415

        resolution = resolve_spacy_model()
        if resolution.target is None:
            if resolution.incompatible_versions:
                raise _SpacyModelUnavailable(
                    f"spaCy model {SPACY_MODEL_NAME!r} found under "
                    f"{get_models_dir() / 'spacy'} "
                    f"({', '.join(resolution.incompatible_versions)}) "
                    "but incompatible with the installed spaCy version",
                    incompatible=True,
                )
            raise _SpacyModelUnavailable(
                f"spaCy model {SPACY_MODEL_NAME!r} is neither installed nor present in "
                f"{get_models_dir() / 'spacy'}",
                incompatible=False,
            )
        return spacy.load(resolution.target)

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

    async def _ensure_nlp(self) -> tuple[bool, str | None]:
        """Load the spaCy model at most once per process.

        Returns ``(is_fatal, message)`` — self-describing, so the caller never
        has to separately re-read instance state to learn the outcome
        (2026-08-19-030 C1-I-5). ``is_fatal=True`` ONLY when spaCy itself is
        not importable (the ``[graph]`` extra is missing) — a misconfiguration
        the operator must fix, with ``message`` as the ``fatal_error`` to
        return. ``is_fatal=False`` covers both "ready to use"
        (``message is None``) and "degraded" (``message`` names the auxiliary
        failure the caller should append to ``warnings`` and skip prose NER
        for). Both outcomes latch (``_spacy_not_importable`` /
        ``_nlp_unavailable``) so the probe/load runs at most once per process,
        not once per file — including the ImportError probe (C1-B-4).
        """
        async with self._load_lock:
            if self._spacy_not_importable:
                return True, SPACY_NOT_INSTALLED_MESSAGE
            if self._nlp is not None or self._nlp_unavailable:
                return False, self._nlp_unavailable_warning

            try:
                import spacy as _spacy_probe  # noqa: F401, PLC0415
            except ImportError:
                self._spacy_not_importable = True
                return True, SPACY_NOT_INSTALLED_MESSAGE

            # Load model (CPU-bound) in the default thread-pool executor.
            # `BaseException`, not `Exception`: the model-load path historically
            # raised `SystemExit` (spaCy's downloader called `sys.exit()` on
            # failure) — a narrower catch here would let that escape uncaught
            # and crash the process, defeating the whole point of degrading
            # instead of aborting (2026-08-19-030; mirrors the same
            # `except BaseException` pattern in model_validation.py).
            try:
                self._nlp = await asyncio.to_thread(self._load_nlp_sync)
            except asyncio.CancelledError:
                # Cancellation says nothing about the model. Swallowing it would
                # both break structured concurrency and latch `_nlp_unavailable`
                # for the process lifetime over a shutdown or job-cancel that
                # happened to land in the load window — disabling prose NER for a
                # model that is present and healthy (2026-08-19-030 C2-I-1).
                raise
            except BaseException as exc:
                # `_load_nlp_sync` already ran `resolve_spacy_model()` once and
                # encodes the classification on `_SpacyModelUnavailable` — no
                # second filesystem probe here (2026-08-19-030 T1: the probe
                # must run at most once per process, not twice per latch).
                incompatible = (
                    isinstance(exc, _SpacyModelUnavailable) and exc.incompatible
                )
                message = (
                    _MODEL_INCOMPATIBLE_WARNING if incompatible else _MODEL_ABSENT_WARNING
                )
                self._nlp_unavailable = True
                self._nlp_unavailable_warning = message
                _logger.warning("GraphExtractor: %s", message, exc_info=True)
        return False, self._nlp_unavailable_warning

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

        Returns a ``GraphExtractionResult``.  ``fatal_error`` is non-None ONLY
        when spaCy itself is not importable (the ``[graph]`` extra is missing);
        the pipeline sets ``IngestResult.status = "error"`` for that case alone.
        Every other spaCy failure — an unavailable model above all — degrades:
        ``fatal_error`` stays None, prose NER is skipped, code-symbol output is
        unaffected, and a warning is appended to ``warnings`` (2026-08-19-030).
        """
        warnings: list[str] = []
        llm_fallback_used = False
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
            is_fatal, load_message = await self._ensure_nlp()
            if is_fatal:
                return GraphExtractionResult(
                    nodes=list(nodes.values()),
                    edges=[],
                    mentions=[],
                    fatal_error=load_message,
                    warnings=[load_message] if load_message else [],
                )
            if load_message is not None:
                # Degraded: no prose NER this run.  Code-symbol nodes, mentions
                # and edges collected above still flow through to the caller,
                # and the file still embeds and persists.
                warnings.append(load_message)
                text_chunks = []
                degraded = True

        if text_chunks:
            # Run NER (CPU-bound) in a thread pool.
            texts = [c.text for c in text_chunks]
            try:
                ner_per_chunk = await asyncio.to_thread(
                    self._run_ner_sync, self._nlp, texts
                )
            except Exception:
                # Auxiliary failure: warn and drop prose NER for this document
                # rather than failing the ingest (2026-08-19-030). The
                # traceback is logged once per process, not once per
                # document, matching the model-load latch (C1-I-4) — the
                # per-document `warnings` entry below still fires every time.
                if not self._ner_failure_logged:
                    self._ner_failure_logged = True
                    _logger.warning("GraphExtractor: spaCy NER failed", exc_info=True)
                else:
                    _logger.debug(
                        "GraphExtractor: spaCy NER failed again", exc_info=True
                    )
                warnings.append(
                    "spaCy NER failed; prose entity extraction was skipped for "
                    "this document (code-symbol extraction is unaffected)."
                )
                # Set both explicitly rather than relying on zip()'s silent
                # truncate-to-shortest: a future `zip(..., strict=True)` in
                # the loop below must not raise inside the very handler whose
                # job is keeping the ingest alive.
                text_chunks = []
                ner_per_chunk = []
                degraded = True

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
            degraded=degraded,
        )
