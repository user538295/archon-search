"""Eval runner types and config loaders — FEAT-039.

Provides threshold and runtime config dataclasses with their loaders, plus
the trace-executing eval suite runner (Task 3.3).
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger("archon")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

_REQUIRED_QUALITY_KEYS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "ndcg_at_10",
)


@dataclass
class EvalQualityFloors:
    """Minimum acceptable quality metric thresholds."""

    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    ndcg_at_5: float
    ndcg_at_10: float
    routing_accuracy: float | None = None


@dataclass
class EvalLatencyCeilings:
    """Maximum acceptable latency thresholds (None = not gated)."""

    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None


@dataclass
class EvalThresholds:
    """Combined eval thresholds loaded from thresholds.toml."""

    quality_floors: EvalQualityFloors
    latency_ceilings: EvalLatencyCeilings = field(default_factory=EvalLatencyCeilings)
    max_floor_drop_without_waiver: float = 0.05


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_thresholds(config_path: Path) -> EvalThresholds:
    """Parse *config_path* (a TOML file) into :class:`EvalThresholds`.

    Raises :class:`ValueError` on:
    - Invalid TOML syntax
    - Missing required quality floor keys
    - Wrong type for any quality floor value
    """
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception as exc:
        raise ValueError(f"Invalid TOML in {config_path}: {exc}") from exc

    # --- quality_floors section -----------------------------------------------
    raw_floors = data.get("quality_floors", {})

    for key in _REQUIRED_QUALITY_KEYS:
        if key not in raw_floors:
            raise ValueError(
                f"Missing required key in [quality_floors]: {key!r}"
            )
        if not isinstance(raw_floors[key], (int, float)):
            raise ValueError(
                f"[quality_floors].{key} must be a float, got {type(raw_floors[key]).__name__!r}"
            )

    routing_accuracy = raw_floors.get("routing_accuracy")
    if routing_accuracy is not None and not isinstance(routing_accuracy, (int, float)):
        raise ValueError(
            f"[quality_floors].routing_accuracy must be a float, "
            f"got {type(routing_accuracy).__name__!r}"
        )

    quality_floors = EvalQualityFloors(
        recall_at_1=float(raw_floors["recall_at_1"]),
        recall_at_3=float(raw_floors["recall_at_3"]),
        recall_at_5=float(raw_floors["recall_at_5"]),
        mrr=float(raw_floors["mrr"]),
        ndcg_at_5=float(raw_floors["ndcg_at_5"]),
        ndcg_at_10=float(raw_floors["ndcg_at_10"]),
        routing_accuracy=float(routing_accuracy) if routing_accuracy is not None else None,
    )

    # --- latency_ceilings section (optional) ----------------------------------
    raw_latency = data.get("latency_ceilings", {})
    latency_ceilings = EvalLatencyCeilings(
        latency_p50_ms=float(raw_latency["latency_p50_ms"]) if "latency_p50_ms" in raw_latency else None,
        latency_p95_ms=float(raw_latency["latency_p95_ms"]) if "latency_p95_ms" in raw_latency else None,
    )

    # --- policy section -------------------------------------------------------
    raw_policy = data.get("policy", {})
    max_floor_drop = float(raw_policy.get("max_floor_drop_without_waiver", 0.05))

    return EvalThresholds(
        quality_floors=quality_floors,
        latency_ceilings=latency_ceilings,
        max_floor_drop_without_waiver=max_floor_drop,
    )


# ---------------------------------------------------------------------------
# EvalRuntimeConfig
# ---------------------------------------------------------------------------

_METRIC_K = 10  # nDCG@10 requires at least this depth


@dataclass
class EvalRuntimeConfig:
    """Eval runtime settings loaded from runtime.toml."""

    candidate_depth: int
    return_depth: int
    metric_depth: int
    routing_contract_enabled: bool


def load_runtime_config(config_path: Path) -> EvalRuntimeConfig:
    """Parse *config_path* (a TOML file) into :class:`EvalRuntimeConfig`.

    Raises :class:`ValueError` on:
    - Invalid TOML syntax
    - Missing [search] section
    - Wrong type for any depth field
    - Constraint violations (metric_depth >= 10, return_depth >= metric_depth,
      candidate_depth > return_depth)
    """
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception as exc:
        raise ValueError(f"Invalid TOML in {config_path}: {exc}") from exc

    if "search" not in data:
        raise ValueError("Missing required [search] section in runtime config")

    raw_search = data["search"]

    for key in ("candidate_depth", "return_depth", "metric_depth"):
        if key not in raw_search:
            raise ValueError(f"Missing required key in [search]: {key!r}")
        if not isinstance(raw_search[key], int):
            raise ValueError(
                f"[search].{key} must be an integer, got {type(raw_search[key]).__name__!r}"
            )

    candidate_depth: int = raw_search["candidate_depth"]
    return_depth: int = raw_search["return_depth"]
    metric_depth: int = raw_search["metric_depth"]

    if metric_depth < _METRIC_K:
        raise ValueError(
            f"metric_depth must be >= {_METRIC_K} (required for nDCG@10), got {metric_depth}"
        )
    if return_depth < metric_depth:
        raise ValueError(
            f"return_depth ({return_depth}) must be >= metric_depth ({metric_depth})"
        )
    if candidate_depth <= return_depth:
        raise ValueError(
            f"candidate_depth ({candidate_depth}) must be > return_depth ({return_depth})"
        )

    raw_routing = data.get("routing", {})
    routing_contract_enabled: bool = bool(raw_routing.get("contract_enabled", False))

    return EvalRuntimeConfig(
        candidate_depth=candidate_depth,
        return_depth=return_depth,
        metric_depth=metric_depth,
        routing_contract_enabled=routing_contract_enabled,
    )


def validate_routing_contract(
    runtime_cfg: EvalRuntimeConfig,
    thresholds: EvalThresholds,
) -> None:
    """Validate that routing_accuracy threshold is set when routing contract is enabled.

    Raises :class:`ValueError` if ``runtime_cfg.routing_contract_enabled`` is True
    but ``thresholds.quality_floors.routing_accuracy`` is None.
    """
    if runtime_cfg.routing_contract_enabled and thresholds.quality_floors.routing_accuracy is None:
        raise ValueError(
            "routing_contract_enabled=True requires a numeric routing_accuracy floor "
            "in thresholds config, but routing_accuracy is None"
        )


# ---------------------------------------------------------------------------
# Task 3.3 — run_eval_suite + EvalReport + EvalBaseline + load_baseline
# ---------------------------------------------------------------------------


from archon_search.eval.fixtures import (
    EvalCorpus,
    EvalQuery,
    build_doc_collection_map,
    load_eval_corpus,
)
from archon_search.eval.types import EvalMetrics, EvalSearchResult, QueryEvalTrace
from archon_search.eval._tracing import collect_search_trace
from archon_search.eval._hashing import (
    compute_eval_hash,
    compute_runtime_config_hash,
    compute_thresholds_hash,
)


@dataclass
class EvalBaseline:
    """Machine-readable baseline metadata loaded from a baseline.json file.

    Attributes:
        eval_hash: Stable hash identifying the eval run that produced this baseline.
        metrics: Mapping of metric name → numeric value (or None for absent metrics).
        runtime_config_hash: Hash of the runtime config used to produce the baseline.
        command: The CLI command (or invocation string) that produced this
            baseline — recorded so reviewers can reproduce it.
        thresholds_hash: Hash of the thresholds at acceptance time, or ``None`` for
            a calibration-only baseline (recorded before thresholds existed).
        waiver_ids: Mapping of metric name → waiver identifier for floors waived
            at the time the baseline was accepted. Empty by default.
    """

    eval_hash: str
    metrics: dict[str, float | None]
    runtime_config_hash: str
    command: str
    thresholds_hash: str | None = None
    waiver_ids: dict[str, str] = field(default_factory=dict)


_BASELINE_REQUIRED_FIELDS = ("eval_hash", "metrics", "runtime_config_hash", "command")


def load_baseline(path: Path) -> EvalBaseline:
    """Parse *path* (a JSON file) into :class:`EvalBaseline`.

    Raises :class:`ValueError` on malformed JSON, missing required fields,
    or wrong field types.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    except OSError as exc:  # pragma: no cover — defensive
        raise ValueError(f"Cannot read baseline file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"Baseline must be a JSON object, got {type(raw).__name__}")

    for key in _BASELINE_REQUIRED_FIELDS:
        if key not in raw:
            raise ValueError(f"Baseline missing required field {key!r}")

    if not isinstance(raw["eval_hash"], str):
        raise ValueError("Baseline field 'eval_hash' must be a string")
    if not isinstance(raw["runtime_config_hash"], str):
        raise ValueError("Baseline field 'runtime_config_hash' must be a string")
    if not isinstance(raw["command"], str):
        raise ValueError("Baseline field 'command' must be a string")
    if not isinstance(raw["metrics"], dict):
        raise ValueError(
            f"Baseline field 'metrics' must be an object, got {type(raw['metrics']).__name__}"
        )

    thresholds_hash = raw.get("thresholds_hash")
    if thresholds_hash is not None and not isinstance(thresholds_hash, str):
        raise ValueError("Baseline field 'thresholds_hash' must be a string or null")

    raw_waivers = raw.get("waiver_ids", {})
    if not isinstance(raw_waivers, dict):
        raise ValueError(
            f"Baseline field 'waiver_ids' must be an object, got {type(raw_waivers).__name__}"
        )
    waiver_ids: dict[str, str] = {}
    for k, v in raw_waivers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(
                "Baseline field 'waiver_ids' must map string keys to string values"
            )
        waiver_ids[k] = v

    metrics: dict[str, float | None] = {}
    for k, v in raw["metrics"].items():
        if v is None:
            metrics[k] = None
        elif isinstance(v, (int, float)):
            metrics[k] = float(v)
        else:
            raise ValueError(
                f"Baseline metric {k!r} must be a number or null, got {type(v).__name__}"
            )

    return EvalBaseline(
        eval_hash=raw["eval_hash"],
        metrics=metrics,
        runtime_config_hash=raw["runtime_config_hash"],
        command=raw["command"],
        thresholds_hash=thresholds_hash,
        waiver_ids=waiver_ids,
    )


@dataclass
class EvalReport:
    """Full in-memory eval report for a single ``run_eval_suite`` invocation."""

    metrics: EvalMetrics
    traces: list[QueryEvalTrace]
    corpus_root: Path
    runtime_config: EvalRuntimeConfig
    thresholds: EvalThresholds | None
    baseline: EvalBaseline | None
    notes: list[str] = field(default_factory=list)
    routing_disabled_queries: int = 0
    routing_bypassed_queries: int = 0
    query_count: int = 0
    document_count: int = 0
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Hashes of the current inputs used to produce this report. Populated by
    # ``run_eval_suite`` and compared against ``baseline`` in
    # ``assert_thresholds`` to detect stale baselines.
    current_eval_hash: str | None = None
    current_runtime_config_hash: str | None = None
    current_thresholds_hash: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers for run_eval_suite
# ---------------------------------------------------------------------------


def _map_result(
    raw: EvalSearchResult,
    path_to_fixture: dict[str, tuple[str, str]],
    corpus_root: Path,
) -> EvalSearchResult:
    """Replace runtime path-derived doc_id with the stable fixture doc_id.

    Raises ValueError if the result's source_path cannot be mapped.
    """
    # source_path is the absolute path used at ingest time.  Convert back to
    # a corpus-relative path (the key used by path_to_fixture) — relative to
    # the ``corpus/`` subdirectory, which is what build_doc_collection_map uses.
    corpus_dir = (corpus_root / "corpus").resolve()
    try:
        rel = str(Path(raw.source_path).resolve().relative_to(corpus_dir))
    except ValueError:
        rel = raw.source_path  # fallback: not under corpus/

    entry = path_to_fixture.get(rel)
    if entry is None:
        available = sorted(path_to_fixture.keys())
        raise ValueError(
            f"unmapped source_path: cannot map {raw.source_path!r} (relative={rel!r}) "
            f"to any fixture doc_id. Available fixture paths: {available}"
        )
    fixture_doc_id, _ = entry
    return EvalSearchResult(
        doc_id=fixture_doc_id,
        runtime_doc_id=raw.runtime_doc_id,
        chunk_id=raw.chunk_id,
        text=raw.text,
        source_path=raw.source_path,
        collection=raw.collection,
        score_breakdown=raw.score_breakdown,
    )


def _unique_doc_count(results: list[EvalSearchResult]) -> int:
    return len({r.doc_id for r in results})


def _per_collection_unique_doc_counts(corpus: EvalCorpus) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in corpus.documents:
        counts[d.collection] = counts.get(d.collection, 0) + 1
    return counts


def _gold_collections_for(query_id: str, corpus: EvalCorpus) -> set[str]:
    doc_col = {d.doc_id: d.collection for d in corpus.documents}
    return {
        doc_col[lbl.doc_id]
        for lbl in corpus.labels
        if lbl.query_id == query_id and lbl.grade > 0 and lbl.doc_id in doc_col
    }


def _validate_queries(corpus: EvalCorpus, runtime_cfg: EvalRuntimeConfig) -> None:
    """Defensive check beyond the loader: reject retrieval queries with collection=None."""
    for q in corpus.queries:
        if q.metric_scope == "retrieval" and q.collection is None:
            raise ValueError(
                f"retrieval query {q.query_id!r} has collection=None — "
                "retrieval queries must specify an explicit collection"
            )
        if q.metric_scope == "routing" and not runtime_cfg.routing_contract_enabled:
            # routing-scope query with routing disabled is allowed; just emits a note.
            pass


async def _build_pipeline_with_eval_backends(
    db_path: Path,
):
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    embedder = Embedder(EvalEmbedderBackend())
    reranker = Reranker(EvalRerankerBackend())
    chunker = DocumentChunker(chunk_size=256)
    parser = DocumentParser()
    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=chunker,
        parser=parser,
        top_k_retrieve=10,
        top_k_return=10,
    )
    return pipeline


async def _ingest_corpus(pipeline, corpus_root: Path, corpus: EvalCorpus) -> None:
    """Ingest all corpus documents grouped by collection."""
    by_collection: dict[str, list[Path]] = {}
    for d in corpus.documents:
        by_collection.setdefault(d.collection, []).append(
            (corpus_root / "corpus" / d.relative_path).resolve()
        )
    for collection, paths in by_collection.items():
        for p in paths:
            result = await pipeline.ingest_file(p, collection, rebuild_fts=False)
            if result.status != "ok":
                raise RuntimeError(
                    f"failed to ingest {p}: {result.error}"
                )
        await pipeline.store.rebuild_fts_index(collection)
        # `ingest_file` does not persist a CollectionMeta (centroid, model). The
        # router needs that meta to rank — recompute it once per collection
        # after all files are ingested.
        await pipeline.recompute_collection_meta(collection)


async def _run_router_for_query(
    pipeline,
    query_text: str,
    collection_metas,
) -> list[str]:
    """Rank collections via centroid similarity and return shortlist names."""
    from archon_search.router import MultiCollectionRouter

    # Eval-time router divergence from production `/route` semantics:
    # we set confidence_threshold=0.0 and shortlist_size=len(collections) so the
    # router never filters or truncates candidates. This isolates the routing
    # signal (centroid ranking only) from production threshold tuning, which is
    # owned by the live search service and not part of the offline contract.
    router = MultiCollectionRouter(
        search_url="http://invalid.example/route",  # unused — we bypass fetch_metadata
        embedder=pipeline._embedder,
        shortlist_size=max(1, len(collection_metas)),
        confidence_threshold=0.0,  # accept any non-zero similarity
        embedding_model=pipeline._embedder.model_name,
    )
    # Inject metadata directly to avoid HTTP fetch
    router._cached_metadata = list(collection_metas)
    shortlist = await router.select(query_text)
    return [m.name for m in shortlist]


async def run_eval_suite(
    corpus_root: Path,
    runtime_config_path: Path,
    thresholds_path: Path | None = None,
    baseline_path: Path | None = None,
) -> EvalReport:
    """Execute the trace-enabled eval suite over the corpus.

    See FEAT-039 Task 3.3 for the full specification.
    """
    from archon_search.eval.metrics import (
        compute_latency_percentiles,
        compute_mrr,
        compute_ndcg_at_k,
        compute_recall_at_k,
        compute_reranker_lift,
        compute_routing_accuracy,
    )

    corpus_root = Path(corpus_root)
    runtime_cfg = load_runtime_config(Path(runtime_config_path))

    thresholds: EvalThresholds | None = None
    if thresholds_path is not None:
        thresholds = load_thresholds(Path(thresholds_path))
        validate_routing_contract(runtime_cfg, thresholds)

    baseline: EvalBaseline | None = None
    if baseline_path is not None:
        baseline = load_baseline(Path(baseline_path))

    corpus = load_eval_corpus(corpus_root)
    _validate_queries(corpus, runtime_cfg)

    path_to_fixture = build_doc_collection_map(corpus)
    per_col_unique = _per_collection_unique_doc_counts(corpus)

    notes: list[str] = []
    routing_disabled_queries = 0
    routing_bypassed_queries = 0

    if not runtime_cfg.routing_contract_enabled:
        notes.append(
            "Routing accuracy not computed: routing.contract_enabled = false in runtime config."
        )

    traces: list[QueryEvalTrace] = []

    with tempfile.TemporaryDirectory(prefix="archon-search-eval-") as tmpdir:
        db_path = Path(tmpdir) / "lancedb"
        pipeline = await _build_pipeline_with_eval_backends(db_path)
        try:
            await _ingest_corpus(pipeline, corpus_root, corpus)

            collection_metas = await pipeline.get_all_collections_meta()

            for q in corpus.queries:
                if q.metric_scope == "retrieval":
                    trace = await _execute_retrieval_query(
                        pipeline=pipeline,
                        query=q,
                        runtime_cfg=runtime_cfg,
                        path_to_fixture=path_to_fixture,
                        corpus_root=corpus_root,
                        per_col_unique=per_col_unique,
                        corpus=corpus,
                        collection_metas=collection_metas,
                    )
                    traces.append(trace)
                else:
                    # routing-scope query
                    if not runtime_cfg.routing_contract_enabled:
                        routing_disabled_queries += 1
                        traces.append(
                            QueryEvalTrace(
                                query_id=q.query_id,
                                query_text=q.text,
                                collection=None,
                                metric_scope="routing",
                                results=[],
                                pre_rerank_results=[],
                                router_correct=None,
                                latency_ms=0.0,
                            )
                        )
                        continue
                    if q.routing_bypass:
                        routing_bypassed_queries += 1
                        traces.append(
                            QueryEvalTrace(
                                query_id=q.query_id,
                                query_text=q.text,
                                collection=None,
                                metric_scope="routing",
                                results=[],
                                pre_rerank_results=[],
                                router_correct=None,
                                latency_ms=0.0,
                            )
                        )
                        continue

                    t0 = time.perf_counter()
                    shortlist_names = await _run_router_for_query(
                        pipeline, q.text, collection_metas
                    )
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    gold = _gold_collections_for(q.query_id, corpus)
                    router_correct = bool(set(shortlist_names) & gold) if gold else False
                    traces.append(
                        QueryEvalTrace(
                            query_id=q.query_id,
                            query_text=q.text,
                            collection=None,
                            metric_scope="routing",
                            results=[],
                            pre_rerank_results=[],
                            router_correct=router_correct,
                            latency_ms=elapsed,
                        )
                    )
        finally:
            try:
                await pipeline.store.disconnect()
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("ignored disconnect error: %s", exc)

    # ----- metrics ----------------------------------------------------------
    retrieval_traces = [t for t in traces if t.metric_scope == "retrieval"]

    # Per-collection corpus size for the under-depth diagnostic
    def _corpus_for_trace(t: QueryEvalTrace) -> int | None:
        return per_col_unique.get(t.collection) if t.collection else None

    # We pre-validated under-depth inside _execute_retrieval_query.  Now compute metrics
    # WITHOUT passing corpus_size (already enforced) to keep this layer simple.
    r1 = compute_recall_at_k(retrieval_traces, corpus.labels, 1)
    r3 = compute_recall_at_k(retrieval_traces, corpus.labels, 3)
    r5 = compute_recall_at_k(retrieval_traces, corpus.labels, 5)
    mrr = compute_mrr(retrieval_traces, corpus.labels)
    ndcg5 = compute_ndcg_at_k(retrieval_traces, corpus.labels, 5)
    ndcg10 = compute_ndcg_at_k(retrieval_traces, corpus.labels, 10)

    try:
        pre10 = compute_ndcg_at_k(retrieval_traces, corpus.labels, 10, use_pre_rerank=True)
        reranker_lift: float | None = compute_reranker_lift(pre10, ndcg10)
    except ValueError:
        reranker_lift = None

    routing_accuracy = compute_routing_accuracy(
        traces, routing_contract_enabled=runtime_cfg.routing_contract_enabled
    )

    latencies = [t.latency_ms for t in retrieval_traces]
    p50, p95 = compute_latency_percentiles(latencies)

    metrics = EvalMetrics(
        recall_at_1=r1,
        recall_at_3=r3,
        recall_at_5=r5,
        mrr=mrr,
        ndcg_at_5=ndcg5,
        ndcg_at_10=ndcg10,
        reranker_lift=reranker_lift,
        routing_accuracy=routing_accuracy,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
    )

    current_eval_hash = compute_eval_hash(corpus_root)
    current_runtime_config_hash = compute_runtime_config_hash(Path(runtime_config_path))
    current_thresholds_hash = (
        compute_thresholds_hash(Path(thresholds_path))
        if thresholds_path is not None
        else None
    )

    return EvalReport(
        metrics=metrics,
        traces=traces,
        corpus_root=corpus_root,
        runtime_config=runtime_cfg,
        thresholds=thresholds,
        baseline=baseline,
        notes=notes,
        routing_disabled_queries=routing_disabled_queries,
        routing_bypassed_queries=routing_bypassed_queries,
        query_count=len(traces),
        document_count=len(corpus.documents),
        current_eval_hash=current_eval_hash,
        current_runtime_config_hash=current_runtime_config_hash,
        current_thresholds_hash=current_thresholds_hash,
    )


# ---------------------------------------------------------------------------
# Task 3.4 — assert_thresholds + render_report
# ---------------------------------------------------------------------------

_QUALITY_FLOOR_FIELDS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "ndcg_at_10",
    "routing_accuracy",
)


def _fmt(value: float | None) -> str:
    return f"{value:.4f}" if isinstance(value, (int, float)) else "n/a"


def assert_thresholds(report: EvalReport) -> None:
    """Gate an :class:`EvalReport` against its configured thresholds.

    Raises :class:`AssertionError` on:
    - Missing thresholds (gating requires explicit thresholds).
    - Any required quality metric below its floor.
    - Any required quality metric ``None`` while a floor is configured.
    - Any gated latency metric above its ceiling.
    - Baseline present with ``thresholds_hash is None`` while gating is configured
      (calibration-only baseline cannot serve as a gating baseline).
    - Any quality floor set more than ``max_floor_drop_without_waiver`` below the
      corresponding baseline metric without a named waiver in
      ``baseline.waiver_ids``.

    Skipped without error:
    - Metric ``None`` AND floor ``None`` (note appended internally).
    - Latency percentile with no ceiling configured.
    """
    if report.thresholds is None:
        raise AssertionError(
            "Cannot gate eval results: no thresholds configured. "
            "Pass --thresholds <path> or set thresholds_path to enable gating. "
            "Report-only (calibration) mode does not assert."
        )

    thresholds = report.thresholds
    baseline = report.baseline

    # Calibration-only baseline cannot gate.
    if baseline is not None and baseline.thresholds_hash is None:
        raise AssertionError(
            "Baseline is calibration-only (thresholds_hash is None) but gating "
            "thresholds are configured. Refresh the baseline against the current "
            "thresholds (accept it as a gating baseline) before enabling gating."
        )

    # Staleness check: when both thresholds and baseline are present, the
    # baseline's recorded hashes must match the current inputs. Any drift means
    # the baseline no longer describes the system under test and must be
    # refreshed before gating can proceed.
    if baseline is not None:
        stale: list[str] = []
        if (
            report.current_eval_hash is not None
            and baseline.eval_hash != report.current_eval_hash
        ):
            stale.append(
                f"eval_hash: baseline={baseline.eval_hash} != "
                f"current={report.current_eval_hash}"
            )
        if (
            report.current_runtime_config_hash is not None
            and baseline.runtime_config_hash != report.current_runtime_config_hash
        ):
            stale.append(
                f"runtime_config_hash: baseline={baseline.runtime_config_hash} != "
                f"current={report.current_runtime_config_hash}"
            )
        if (
            report.current_thresholds_hash is not None
            and baseline.thresholds_hash != report.current_thresholds_hash
        ):
            stale.append(
                f"thresholds_hash: baseline={baseline.thresholds_hash} != "
                f"current={report.current_thresholds_hash}"
            )
        if stale:
            raise AssertionError(
                "Stale baseline — recorded hashes differ from current inputs. "
                "Refresh the baseline before re-enabling gating:\n  - "
                + "\n  - ".join(stale)
            )

    failures: list[str] = []
    floors = thresholds.quality_floors
    metrics = report.metrics

    # Quality floors --------------------------------------------------------
    for field_name in _QUALITY_FLOOR_FIELDS:
        floor: float | None = getattr(floors, field_name)
        actual: float | None = getattr(metrics, field_name)
        baseline_value: float | None = (
            baseline.metrics.get(field_name) if baseline is not None else None
        )

        if floor is None:
            if actual is None:
                report.notes.append(
                    f"{field_name}: metric is None and no floor configured — skipped."
                )
            continue

        if actual is None:
            failures.append(
                f"{field_name}: metric is None but floor={floor:.4f} is configured "
                f"(baseline={_fmt(baseline_value)}). Configure the metric or remove "
                f"the floor."
            )
            continue

        if actual < floor:
            delta_threshold = actual - floor
            delta_baseline = (
                actual - baseline_value if baseline_value is not None else None
            )
            failures.append(
                f"{field_name}: actual={actual:.4f} < floor={floor:.4f} "
                f"(delta_from_threshold={delta_threshold:+.4f}, "
                f"baseline={_fmt(baseline_value)}, "
                f"delta_from_baseline="
                f"{f'{delta_baseline:+.4f}' if delta_baseline is not None else 'n/a'})"
            )

        # Floor-drop policy vs baseline
        if baseline_value is not None:
            drop = baseline_value - floor
            if drop > thresholds.max_floor_drop_without_waiver:
                if field_name not in baseline.waiver_ids:
                    failures.append(
                        f"{field_name}: floor={floor:.4f} is {drop:+.4f} below "
                        f"baseline={baseline_value:.4f} which exceeds "
                        f"max_floor_drop_without_waiver="
                        f"{thresholds.max_floor_drop_without_waiver:.4f}. "
                        f"Add a named waiver to baseline.waiver_ids[{field_name!r}]."
                    )

    # Latency ceilings ------------------------------------------------------
    ceilings = thresholds.latency_ceilings
    for field_name in ("latency_p50_ms", "latency_p95_ms"):
        ceiling: float | None = getattr(ceilings, field_name)
        actual_l: float = getattr(metrics, field_name)
        baseline_l: float | None = (
            baseline.metrics.get(field_name) if baseline is not None else None
        )
        if ceiling is None:
            continue
        if actual_l > ceiling:
            delta_threshold = actual_l - ceiling
            delta_baseline = (
                actual_l - baseline_l if baseline_l is not None else None
            )
            failures.append(
                f"{field_name}: actual={actual_l:.2f}ms > ceiling={ceiling:.2f}ms "
                f"(delta_from_threshold={delta_threshold:+.2f}, "
                f"baseline={_fmt(baseline_l)}, "
                f"delta_from_baseline="
                f"{f'{delta_baseline:+.2f}' if delta_baseline is not None else 'n/a'})"
            )

    if failures:
        raise AssertionError(
            "Eval thresholds violated:\n  - " + "\n  - ".join(failures)
        )


_RENDERED_QUALITY_FIELDS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "ndcg_at_10",
    "reranker_lift",
    "routing_accuracy",
)

_RENDERED_LATENCY_FIELDS = ("latency_p50_ms", "latency_p95_ms")


def render_report(report: EvalReport) -> str:
    """Render *report* to a human-readable text block.

    Includes all metric categories, baseline deltas (when a baseline is present),
    notes (including routing skip), and a footer documenting that latency was
    measured using deterministic eval backends.
    """
    metrics = report.metrics
    baseline = report.baseline
    lines: list[str] = []

    lines.append("=== Archon Search Eval Report ===")
    lines.append(f"generated_at: {report.generated_at.isoformat()}")
    lines.append(f"corpus_root:  {report.corpus_root}")
    lines.append(
        f"queries:      {report.query_count} "
        f"(routing_disabled={report.routing_disabled_queries}, "
        f"routing_bypassed={report.routing_bypassed_queries})"
    )
    lines.append(f"documents:    {report.document_count}")
    lines.append("")
    lines.append("Quality metrics:")
    for field_name in _RENDERED_QUALITY_FIELDS:
        actual = getattr(metrics, field_name)
        line = f"  {field_name:18s} = {_fmt(actual)}"
        if baseline is not None:
            bv = baseline.metrics.get(field_name)
            if bv is not None and isinstance(actual, (int, float)):
                delta = actual - bv
                line += f"  (baseline={_fmt(bv)}, delta={delta:+.4f})"
            elif bv is not None:
                line += f"  (baseline={_fmt(bv)})"
        lines.append(line)

    if metrics.routing_accuracy is None:
        lines.append("  routing_accuracy: skipped (no routing-scope queries or routing disabled)")

    lines.append("")
    lines.append("Latency (ms):")
    for field_name in _RENDERED_LATENCY_FIELDS:
        actual = getattr(metrics, field_name)
        line = f"  {field_name:18s} = {actual:.2f}"
        if baseline is not None:
            bv = baseline.metrics.get(field_name)
            if bv is not None:
                delta = actual - bv
                line += f"  (baseline={bv:.2f}, delta={delta:+.2f})"
        lines.append(line)

    if report.thresholds is not None:
        lines.append("")
        lines.append("Configured floors:")
        floors = report.thresholds.quality_floors
        for field_name in _QUALITY_FLOOR_FIELDS:
            v = getattr(floors, field_name)
            lines.append(f"  {field_name:18s} >= {_fmt(v)}")
        c = report.thresholds.latency_ceilings
        lines.append("Configured latency ceilings:")
        for field_name in _RENDERED_LATENCY_FIELDS:
            v = getattr(c, field_name)
            lines.append(f"  {field_name:18s} <= {_fmt(v)}")

    if report.notes:
        lines.append("")
        lines.append("Notes:")
        for n in report.notes:
            lines.append(f"  - {n}")

    if baseline is not None:
        lines.append("")
        lines.append(
            f"Baseline: eval_hash={baseline.eval_hash}, "
            f"thresholds_hash={baseline.thresholds_hash}, "
            f"command={baseline.command!r}"
        )

    lines.append("")
    lines.append(
        "Note: latency was measured using deterministic eval backends "
        "(EvalEmbedderBackend, EvalRerankerBackend); values are not comparable "
        "to production runtime latency."
    )

    return "\n".join(lines)


async def _execute_retrieval_query(
    *,
    pipeline,
    query: EvalQuery,
    runtime_cfg: EvalRuntimeConfig,
    path_to_fixture: dict[str, tuple[str, str]],
    corpus_root: Path,
    per_col_unique: dict[str, int],
    corpus: EvalCorpus,
    collection_metas,
) -> QueryEvalTrace:
    """Run a single retrieval-scope query and return a fully-populated trace."""
    if query.collection is None:
        # Defensive — should have been caught by _validate_queries.
        raise ValueError(
            f"retrieval query {query.query_id!r} has collection=None"
        )

    t0 = time.perf_counter()
    pre_raw, post_raw = await collect_search_trace(
        pipeline,
        query.text,
        query.collection,
        runtime_cfg.candidate_depth,
        runtime_cfg.return_depth,
        runtime_cfg.metric_depth,
    )
    elapsed = (time.perf_counter() - t0) * 1000.0

    pre_mapped = [_map_result(r, path_to_fixture, corpus_root) for r in pre_raw]
    post_mapped = [_map_result(r, path_to_fixture, corpus_root) for r in post_raw]

    # Deterministic tie-breaking on equal scores (FEAT-039 spec): primary key
    # is the ranking score (descending); ties break by doc_id then chunk_id
    # (ascending). Pre-rerank uses rrf_score; post-rerank uses reranker_score
    # when available, falling back to rrf_score. LanceDB tie ordering can
    # differ between cold and warm runs — this finalization pass guarantees
    # determinism in the eval harness without touching production search.
    def _pre_key(r: EvalSearchResult) -> tuple[float, str, str]:
        return (-r.score_breakdown.rrf_score, r.doc_id, r.chunk_id)

    def _post_key(r: EvalSearchResult) -> tuple[float, str, str]:
        score = r.score_breakdown.reranker_score
        if score is None:
            score = r.score_breakdown.rrf_score
        return (-score, r.doc_id, r.chunk_id)

    pre_mapped.sort(key=_pre_key)
    post_mapped.sort(key=_post_key)

    # Under-depth diagnostic — based on post-rerank unique-doc count vs metric_depth,
    # gated by per-collection fixture-corpus size.
    available = per_col_unique.get(query.collection, 0)
    unique_post = _unique_doc_count(post_mapped)
    if available >= runtime_cfg.metric_depth and unique_post < runtime_cfg.metric_depth:
        raise ValueError(
            f"under-depth diagnostic: query {query.query_id!r} on collection "
            f"{query.collection!r} returned {unique_post} unique documents after "
            f"dedup (need >= {runtime_cfg.metric_depth}); collection has "
            f"{available} unique documents in the fixture corpus. Increase "
            f"candidate_depth or improve retrieval quality — do not loop."
        )

    # Routing accuracy is computed across ALL non-bypassed queries regardless of
    # metric_scope (per FEAT-039 spec). For retrieval queries with routing enabled,
    # run the router and record whether its shortlist includes the gold collection.
    router_correct: bool | None = None
    if runtime_cfg.routing_contract_enabled and not query.routing_bypass:
        shortlist_names = await _run_router_for_query(
            pipeline, query.text, collection_metas
        )
        gold = _gold_collections_for(query.query_id, corpus)
        router_correct = bool(set(shortlist_names) & gold) if gold else False

    return QueryEvalTrace(
        query_id=query.query_id,
        query_text=query.text,
        collection=query.collection,
        metric_scope="retrieval",
        results=post_mapped,
        pre_rerank_results=pre_mapped,
        router_correct=router_correct,
        latency_ms=elapsed,
    )
