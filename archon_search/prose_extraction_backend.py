"""Prose entity/relation extraction layer — wraps gliner.GLiNER with async support.

Lazy-loading, single-shared-instance backend for the graph NER/RelEx engine
(Task BE-8). Mirrors ``embedder.py``'s lazy-load-behind-a-lock pattern: the
model is constructed once per process, off the event loop, on first ``load()``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from archon_search.graph_types import EntityType, RelationshipType
from archon_search.paths import (
    GRAPH_NER_MODEL_NAME,
    GRAPH_NER_MODEL_REVISION,
    get_graph_models_dir,
)

if TYPE_CHECKING:
    # Type-checking only — the runtime import stays deferred inside _load_sync().
    from gliner import GLiNER

logger = logging.getLogger(__name__)

# ponytail: fixed 1/1 intra/inter-op thread pin (ceiling), not derived from cpu_count()
# — this process already runs N pytest-xdist/uvicorn workers plus the embedder/
# reranker threads, and oversubscribing torch threads per worker regressed
# throughput in the fastembed backends.
_TORCH_INTRA_OP_THREADS = 1
_TORCH_INTER_OP_THREADS = 1

# [graph].providers values are ONNX-style execution provider strings (see
# archon-search.toml.example's [database] section, which the reranker/embedder
# share) — never the literal "cuda"/"mps" torch device names.
_CUDA_PROVIDER_MARKER = "cuda"
_MPS_PROVIDER_MARKER = "coreml"

# Mirrors embedder.py / reranker.py's _WARMUP_TIMEOUT_SECONDS: bounds the blocking
# GLiNER.from_pretrained() download so a stalled network cannot hang the caller
# (or the install wizard's pre-warm step) forever.
_LOAD_TIMEOUT_SECONDS = 300.0

# Task BE-12 (S46). Bounds how long a WAITER — a concurrent caller whose load()
# lands while another caller is already loading — blocks on that in-flight
# load, mirroring EmbedderCache._LOAD_WAIT_TIMEOUT_SECONDS (embedder_cache.py).
# Deliberately much shorter than _LOAD_TIMEOUT_SECONDS: a waiter here is a
# per-document ingest that must degrade (skip prose extraction, keep going)
# rather than hang the whole ingest for up to 300s behind someone else's load.
_LOAD_WAIT_TIMEOUT_SECONDS = 30.0

# Pinned module constant (Task BE-9, S53) — NOT a [graph] config key, mirroring
# S43's treatment of adjacency_threshold. Chunks are grouped into sub-batches of
# at most this many texts per call to gliner's own `inference()`, so a document
# of N chunks issues ceil(N / GRAPH_NER_SUB_BATCH_SIZE) calls at the gliner
# boundary, never one call per chunk and never one unbounded forward pass.
GRAPH_NER_SUB_BATCH_SIZE: int = 8

# Pinned word-count window (Task BE-9, S20) — gliner's own enforced `max_len`,
# not the encoder's declared-but-unenforced 512 subword-token limit. A chunk
# longer than this is truncated before being sent to the model.
GRAPH_NER_TOKEN_WINDOW_WORDS: int = 2048

# The "other" decoy entity label (Q3): its spans are always discarded before
# reaching the graph — it exists only to give the model a place to put spans
# that don't fit any real entity label, improving precision on the real labels.
_OTHER_LABEL = "other"

# Separator gliner's descriptive-label prompting convention uses between a
# label and its one-line description.
_LABEL_DESCRIPTION_SEP = " <> "

# The graph's own entity labels, paired with a one-line description each (C1).
# `code_symbol` is excluded — code-symbol chunks never reach this seam.
_ENTITY_LABEL_DESCRIPTIONS: dict[str, str] = {
    EntityType.person.value: "a named individual person",
    EntityType.concept.value: "an abstract idea, topic, or named concept",
    EntityType.system.value: "a named software system, product, service, or platform",
    EntityType.event.value: "a named occurrence or incident",
}

# The graph's own prose relation labels (C1). `synonym_of` is excluded — it is
# produced by a separate embedding-similarity mechanism (E2f), not RelEx.
_RELATION_LABEL_DESCRIPTIONS: dict[str, str] = {
    RelationshipType.uses.value: "the head uses or depends on the tail at runtime",
    RelationshipType.implements.value: "the head implements the tail",
    RelationshipType.depends_on.value: "the head requires the tail to function",
    RelationshipType.related_to.value: "the head is generically related to the tail",
    RelationshipType.calls.value: "the head calls or invokes the tail",
    RelationshipType.imports.value: "the head imports the tail",
    RelationshipType.defines.value: "the head defines the tail",
    RelationshipType.inherits.value: "the head inherits from the tail",
}

# Sanitized, pinned log message (S30) — must NEVER interpolate chunk text.
_TRUNCATION_LOG_MESSAGE = (
    "graph NER: a chunk exceeded GRAPH_NER_TOKEN_WINDOW_WORDS words and was "
    "truncated before extraction"
)


def _labels_with_descriptions(descriptions: dict[str, str]) -> list[str]:
    """Build gliner's flat label prompt list from a label -> description dict."""
    return [
        f"{label}{_LABEL_DESCRIPTION_SEP}{description}"
        for label, description in descriptions.items()
    ]


# The exact prompt lists sent to gliner — built once at import, not per call.
_ENTITY_LABELS: list[str] = [
    *_labels_with_descriptions(_ENTITY_LABEL_DESCRIPTIONS),
    _OTHER_LABEL,
]
_RELATION_LABELS: list[str] = _labels_with_descriptions(_RELATION_LABEL_DESCRIPTIONS)

# The real entity labels — anything else gliner decodes as a span type (the
# "other" decoy above all) disqualifies a relation endpoint (C1).
_REAL_ENTITY_LABELS: frozenset[str] = frozenset(_ENTITY_LABEL_DESCRIPTIONS)


def _strip_label(label: str) -> str:
    """Recover the bare label name from a `"label <> description"` prompt string."""
    return label.split(_LABEL_DESCRIPTION_SEP, 1)[0]


@dataclass(frozen=True)
class ExtractedEntity:
    """A single threshold-filtered entity span (real substring, char offsets)."""

    text: str
    label: str
    start: int
    end: int
    score: float


@dataclass(frozen=True)
class ExtractedRelation:
    """A single threshold-filtered, directed relation triple."""

    head: str
    tail: str
    label: str
    score: float


@dataclass(frozen=True)
class ChunkExtraction:
    """Entities and relations extracted for one prose chunk."""

    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)


@dataclass(frozen=True)
class BatchExtraction:
    """Result of one ``ProseExtractionBackend.inference()`` call.

    ``chunks`` is aligned index-for-index with the input ``texts``.
    """

    chunks: list[ChunkExtraction]
    truncated: bool


class ProseExtractionLoadTimeoutError(RuntimeError):
    """Raised when a WAITER times out on another caller's in-flight ``load()``.

    Says nothing about whether the load itself will succeed or fail — the
    loader keeps running, unaffected. Unlike ``EmbedderCache``'s
    ``EmbedderNotReadyError`` (which callers map to HTTP 503), this must never
    reach the wire as a 503: ``GraphExtractor`` catches it and degrades just
    this document's prose extraction (S46), leaving the loader free to finish
    for the next one.
    """


class ProseExtractionBackend:
    """Lazy-loading gliner.GLiNER backend for prose entity/relation extraction.

    One shared instance per process. ``load()`` is idempotent, single-flight
    (concurrent cold callers share one in-flight load: the first becomes the
    LOADER, the rest become WAITERS blocking on an ``asyncio.Event`` bounded
    by ``_LOAD_WAIT_TIMEOUT_SECONDS`` — Task BE-12, S46 — rather than each
    triggering their own load), and off the event loop via
    ``asyncio.to_thread``. A failed load is latched — the
    second call raises the same exception without retrying — except for
    ``asyncio.CancelledError``, which is never latched so a cancelled caller's
    successor can retry.
    """

    def __init__(
        self,
        model_name: str = GRAPH_NER_MODEL_NAME,
        revision: str = GRAPH_NER_MODEL_REVISION,
        providers: list[str] | None = None,
    ) -> None:
        self._model_name = model_name
        self._revision = revision
        # [graph].providers only — never the embedder's or reranker's own provider
        # list. Unset (None/[]) means CPU.
        self._providers = providers or None
        self._model: GLiNER | None = None
        self._sync_lock = threading.Lock()
        # Guards only the brief check-and-register step below — never held for
        # the duration of an actual load (mirrors EmbedderCache._lock).
        self._load_lock = asyncio.Lock()
        # Non-None while a load is in flight; set when that load finishes
        # (success, failure, or cancellation) so waiters wake and re-check.
        self._load_event: asyncio.Event | None = None
        self._load_exception: BaseException | None = None
        # Truncation-log latch (S30): once per backend INSTANCE — and the backend
        # itself is a per-process singleton by construction (one shared instance,
        # BE-8), which is what makes that once-per-process in practice. Guarded by
        # its own lock since inference() may run concurrently across threads.
        self._truncation_log_lock = threading.Lock()
        self._truncation_logged = False

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _resolve_device(self) -> str:
        """Map [graph].providers onto a torch device: cpu/cuda/mps.

        Recognises this repo's real ONNX provider-string vocabulary
        (``CUDAExecutionProvider``, ``CoreMLExecutionProvider``, ...), not
        literal "cuda"/"mps". Steps down to CPU — logging a WARNING rather than
        raising — when the resolved device is unavailable at runtime, so a
        stale/misconfigured providers list cannot latch a permanent failure.
        """
        if not self._providers:
            return "cpu"
        first = self._providers[0].lower()
        if _CUDA_PROVIDER_MARKER in first:
            import torch  # noqa: PLC0415 — lazy; not installed at import time

            if torch.cuda.is_available():
                return "cuda"
            logger.warning(
                "[graph].providers=%r requests CUDA but torch.cuda.is_available() "
                "is False — falling back to CPU.",
                self._providers,
            )
            return "cpu"
        if _MPS_PROVIDER_MARKER in first:
            import torch  # noqa: PLC0415 — lazy; not installed at import time

            if torch.backends.mps.is_available():
                return "mps"
            logger.warning(
                "[graph].providers=%r requests CoreML/MPS but "
                "torch.backends.mps.is_available() is False — falling back to CPU.",
                self._providers,
            )
            return "cpu"
        return "cpu"

    def _load_sync(self) -> None:
        """Blocking model construction — runs off the event loop via asyncio.to_thread."""
        if self._model is not None:
            return
        with self._sync_lock:
            if self._model is not None:
                return
            # Deferred: gliner/torch/transformers must never load at module import
            # time, so create_app() stays cheap (BE-8's contract) — these load only
            # on the first real load(), off the event loop.
            import torch  # noqa: PLC0415 — lazy; not installed at import time
            from gliner import GLiNER  # noqa: PLC0415 — lazy; not installed at import time

            # Thread pinning is best-effort, not fatal to the load: torch raises
            # RuntimeError if either setter is called more than once per process
            # (e.g. a second ProseExtractionBackend instance, or a retried load
            # after CancelledError) — that must never poison the load-exception
            # latch below.
            try:
                torch.set_num_threads(_TORCH_INTRA_OP_THREADS)
                torch.set_num_interop_threads(_TORCH_INTER_OP_THREADS)
            except RuntimeError as exc:
                logger.debug("torch thread pinning skipped (already set): %s", exc)

            model = GLiNER.from_pretrained(
                self._model_name,
                revision=self._revision,
                cache_dir=str(get_graph_models_dir()),
            )
            self._model = model.to(self._resolve_device())

    async def load(self) -> None:
        """Load the model, off the event loop, on first call.

        Idempotent and single-flight: concurrent callers share one in-flight
        load rather than each triggering their own. A failed load latches —
        every subsequent call re-raises the same exception without retrying
        — except ``asyncio.CancelledError``, which never latches, so a
        cancelled caller's successor gets a genuine retry.

        A concurrent caller that lands while another is already loading is a
        WAITER, not the loader: it blocks on that in-flight load for at most
        ``_LOAD_WAIT_TIMEOUT_SECONDS`` (Task BE-12, S46) and raises
        ``ProseExtractionLoadTimeoutError`` on timeout — mirroring
        ``EmbedderCache.get_or_load``'s waiter (``embedder_cache.py``). A
        waiter's timeout NEVER touches ``_model``/``_load_exception``/
        ``_load_event``: it has no authority over the loader's lifecycle,
        which cleans up its own registration on every exit path.
        """
        while True:
            if self._model is not None:
                return
            if self._load_exception is not None:
                raise self._load_exception

            async with self._load_lock:
                if self._model is not None:
                    return
                if self._load_exception is not None:
                    raise self._load_exception
                if self._load_event is not None:
                    event = self._load_event
                else:
                    event = asyncio.Event()
                    self._load_event = event
                    break  # We are the loader — proceed below, lock released.

            # We are a waiter — block outside the lock so the loader can run.
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=_LOAD_WAIT_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                raise ProseExtractionLoadTimeoutError(
                    "ProseExtractionBackend: timed out after "
                    f"{_LOAD_WAIT_TIMEOUT_SECONDS}s waiting for another caller's "
                    "in-flight model load"
                ) from None
            # Event fired: loop back and re-check model/exception. If the
            # loader was cancelled without resolving either, self._load_event
            # is already cleared (see finally below) and we become the next
            # loader ourselves.

        # --- We are the loader (lock released at the break above) ---
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._load_sync), timeout=_LOAD_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # asyncio.CancelledError is a BaseException, not an Exception, so
            # it already bypasses this clause and is never latched here —
            # cancellation says nothing about the model.
            logger.warning(
                "Failed to load graph NER model %r: %s", self._model_name, exc
            )
            self._load_exception = exc
            raise
        finally:
            # Runs on every exit — success, exception, or cancellation — so a
            # waiter is never left blocked past our own resolution. No lock
            # here: the two writes are plain synchronous attribute ops with no
            # await point between them, so they are already atomic under
            # single-threaded asyncio. This is a defensive simplification —
            # mirroring EmbedderCache.get_or_load's own lock-free wakeup in
            # embedder_cache.py — not a fix for a reachable wedge: the
            # loader-registration critical section this lock guards (see
            # load()'s `async with self._load_lock` block above) contains no
            # await point, so that lock can never actually be contended across
            # a suspension point in the first place.
            self._load_event = None
            event.set()

    def _truncate_to_window(self, text: str) -> str:
        """Truncate `text` to GRAPH_NER_TOKEN_WINDOW_WORDS words, logging the
        first truncation for this backend instance at WARNING with the pinned
        sanitized message — never the chunk text itself (S20, S30).

        The result is always a byte-for-byte PREFIX of `text`: the offsets
        gliner returns for the truncated string must still index the original
        chunk text, so whitespace runs (newlines, tabs, double spaces) must
        survive — a `" ".join(text.split())` rebuild would silently shift every
        entity `start`/`end` past the first irregular gap.
        """
        words = list(re.finditer(r"\S+", text))
        if len(words) <= GRAPH_NER_TOKEN_WINDOW_WORDS:
            return text
        with self._truncation_log_lock:
            if not self._truncation_logged:
                self._truncation_logged = True
                logger.warning(_TRUNCATION_LOG_MESSAGE)
        return text[: words[GRAPH_NER_TOKEN_WINDOW_WORDS - 1].end()]

    @staticmethod
    def _dedupe_relations(
        relations: list[ExtractedRelation],
    ) -> list[ExtractedRelation]:
        """Deduplicate relation triples keyed on (head, tail, label) — NOT
        (head, tail) — so distinct relation types over the same node pair
        both survive. gliner's PyTorch checkpoint is known to emit exact
        duplicate (head, tail, relation) triples 2x-4x."""
        seen: set[tuple[str, str, str]] = set()
        deduped: list[ExtractedRelation] = []
        for relation in relations:
            key = (relation.head, relation.tail, relation.label)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(relation)
        return deduped

    def _infer_sync(
        self, texts: list[str], ner_confidence: float, relation_confidence: float
    ) -> BatchExtraction:
        """Blocking sub-batched inference — runs off the event loop via
        asyncio.to_thread. Requires ``self._model`` to already be loaded."""
        assert self._model is not None  # noqa: S101 — inference() enforces this

        truncated_any = False
        prepared_texts: list[str] = []
        for text in texts:
            truncated_text = self._truncate_to_window(text)
            truncated_any = truncated_any or truncated_text != text
            prepared_texts.append(truncated_text)

        chunks: list[ChunkExtraction] = []
        for start in range(0, len(prepared_texts), GRAPH_NER_SUB_BATCH_SIZE):
            sub_batch = prepared_texts[start : start + GRAPH_NER_SUB_BATCH_SIZE]
            # `adjacency_threshold` is deliberately NOT passed: K2g proved it inert
            # on this checkpoint three independent ways (it only reaches candidate-
            # pair selection when the model has a `relations_rep_layer`, which this
            # one does not), so Q29's pinned ~0.6 was dropped rather than pinned —
            # see the tasks file's spike-gate Notes ("REMOVED, not pinned").
            batch_entities, batch_relations = self._model.inference(
                sub_batch,
                _ENTITY_LABELS,
                relations=_RELATION_LABELS,
                threshold=ner_confidence,
                relation_threshold=relation_confidence,
                batch_size=GRAPH_NER_SUB_BATCH_SIZE,
                return_relations=True,
            )
            for raw_entities, raw_relations in zip(
                batch_entities, batch_relations, strict=True
            ):
                entities = [
                    ExtractedEntity(
                        text=raw_entity["text"],
                        label=label,
                        start=raw_entity["start"],
                        end=raw_entity["end"],
                        score=raw_entity["score"],
                    )
                    for raw_entity in raw_entities
                    if (label := _strip_label(raw_entity["label"]))
                    in _REAL_ENTITY_LABELS
                ]
                relations = self._dedupe_relations(
                    [
                        ExtractedRelation(
                            head=raw_relation["head"]["text"],
                            tail=raw_relation["tail"]["text"],
                            label=_strip_label(raw_relation["relation"]),
                            score=raw_relation["score"],
                        )
                        for raw_relation in raw_relations
                        # A relation whose head or tail span was itself decoded as
                        # the "other" decoy (or any non-real label) must not reach
                        # the graph — filtering entities alone leaves the decoy in
                        # as a relation endpoint.
                        if _strip_label(raw_relation["head"]["type"])
                        in _REAL_ENTITY_LABELS
                        and _strip_label(raw_relation["tail"]["type"])
                        in _REAL_ENTITY_LABELS
                    ]
                )
                chunks.append(ChunkExtraction(entities=entities, relations=relations))

        if len(chunks) != len(texts):
            # BatchExtraction.chunks is contracted to align index-for-index with
            # the input texts; a short per-text list from gliner would silently
            # misattribute every entity after the gap.
            raise RuntimeError(
                "graph NER returned "
                f"{len(chunks)} per-chunk results for {len(texts)} input texts"
            )
        return BatchExtraction(chunks=chunks, truncated=truncated_any)

    async def inference(
        self, texts: list[str], ner_confidence: float, relation_confidence: float
    ) -> BatchExtraction:
        """Extract entities and relations for a batch of prose chunks (Task BE-9).

        Prompts gliner with the graph's own entity/relation labels plus the
        ``"other"`` decoy entity label (Q3); "other"-labelled spans are
        discarded before reaching the graph, as are relations with an
        "other"-typed head or tail. Both confidence thresholds are
        applied by the model itself. Texts are grouped into sub-batches of at
        most ``GRAPH_NER_SUB_BATCH_SIZE`` per call to ``gliner``'s own
        ``inference()`` (S7, S53); any chunk exceeding
        ``GRAPH_NER_TOKEN_WINDOW_WORDS`` words is truncated to a byte-for-byte
        prefix with a sanitized log emitted once per backend instance — which
        is once per process, the backend being a shared singleton (S20, S30).
        Duplicate relation triples
        — keyed on (head, tail, label) — are removed per chunk.

        Requires ``load()`` to have completed; raises ``RuntimeError``
        otherwise.
        """
        if self._model is None:
            raise RuntimeError(
                "ProseExtractionBackend.inference() called before load()"
            )
        return await asyncio.to_thread(
            self._infer_sync, texts, ner_confidence, relation_confidence
        )
