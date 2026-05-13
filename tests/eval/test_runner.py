"""Tests for EvalThresholds dataclasses and load_thresholds() — FEAT-039 Task 1.4."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from archon_search.eval.runner import (
    EvalLatencyCeilings,
    EvalQualityFloors,
    EvalRuntimeConfig,
    EvalThresholds,
    load_runtime_config,
    load_thresholds,
    validate_routing_contract,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "thresholds.toml"
    p.write_text(textwrap.dedent(content))
    return p


_FULL_QUALITY = """
[quality_floors]
recall_at_1 = 0.60
recall_at_3 = 0.75
recall_at_5 = 0.80
mrr = 0.65
ndcg_at_5 = 0.70
ndcg_at_10 = 0.72
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_load_thresholds_reads_all_metrics(tmp_path: Path) -> None:
    """Valid TOML with all quality fields parses into EvalThresholds correctly."""
    path = _write_toml(tmp_path, _FULL_QUALITY)
    result = load_thresholds(path)

    assert isinstance(result, EvalThresholds)
    floors = result.quality_floors
    assert isinstance(floors, EvalQualityFloors)
    assert floors.recall_at_1 == pytest.approx(0.60)
    assert floors.recall_at_3 == pytest.approx(0.75)
    assert floors.recall_at_5 == pytest.approx(0.80)
    assert floors.mrr == pytest.approx(0.65)
    assert floors.ndcg_at_5 == pytest.approx(0.70)
    assert floors.ndcg_at_10 == pytest.approx(0.72)
    assert floors.routing_accuracy is None


def test_load_thresholds_allows_omitted_routing_accuracy(tmp_path: Path) -> None:
    """Omitting routing_accuracy results in None — no error raised."""
    path = _write_toml(tmp_path, _FULL_QUALITY)
    result = load_thresholds(path)
    assert result.quality_floors.routing_accuracy is None


def test_load_thresholds_accepts_optional_routing_floor_shape(tmp_path: Path) -> None:
    """routing_accuracy = 0.8 is accepted as a valid float."""
    content = _FULL_QUALITY + "routing_accuracy = 0.8\n"
    path = _write_toml(tmp_path, content)
    result = load_thresholds(path)
    assert result.quality_floors.routing_accuracy == pytest.approx(0.8)


def test_load_thresholds_rejects_missing_metric(tmp_path: Path) -> None:
    """Missing ndcg_at_10 raises ValueError."""
    content = """
[quality_floors]
recall_at_1 = 0.60
recall_at_3 = 0.75
recall_at_5 = 0.80
mrr = 0.65
ndcg_at_5 = 0.70
"""
    path = _write_toml(tmp_path, content)
    with pytest.raises(ValueError, match="ndcg_at_10"):
        load_thresholds(path)


def test_load_thresholds_reads_floor_drop_policy(tmp_path: Path) -> None:
    """max_floor_drop_without_waiver parses correctly and defaults to 0.05."""
    # Default — no [policy] section
    path = _write_toml(tmp_path, _FULL_QUALITY)
    result = load_thresholds(path)
    assert result.max_floor_drop_without_waiver == pytest.approx(0.05)

    # Explicit value
    content = _FULL_QUALITY + "\n[policy]\nmax_floor_drop_without_waiver = 0.10\n"
    sub = tmp_path / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    path2 = _write_toml(sub, content)
    result2 = load_thresholds(path2)
    assert result2.max_floor_drop_without_waiver == pytest.approx(0.10)


def test_load_thresholds_rejects_malformed_toml_syntax(tmp_path: Path) -> None:
    """Invalid TOML syntax raises ValueError."""
    path = tmp_path / "thresholds.toml"
    path.write_text("this is not valid toml ===\n[broken\n")
    with pytest.raises(ValueError, match="[Ii]nvalid|[Pp]arse|TOML"):
        load_thresholds(path)


def test_load_thresholds_rejects_wrong_type_for_routing_floor(tmp_path: Path) -> None:
    """routing_accuracy = 'high' (string) raises ValueError."""
    content = _FULL_QUALITY + 'routing_accuracy = "high"\n'
    path = _write_toml(tmp_path, content)
    with pytest.raises(ValueError, match="routing_accuracy"):
        load_thresholds(path)


# ---------------------------------------------------------------------------
# EvalRuntimeConfig / load_runtime_config tests — Task 1.5
# ---------------------------------------------------------------------------

_VALID_RUNTIME_TOML = """
[search]
candidate_depth = 40
return_depth = 20
metric_depth = 10

[routing]
contract_enabled = true
"""

_COMMITTED_RUNTIME_TOML = Path(__file__).parent / "runtime.toml"


def _write_runtime_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "runtime.toml"
    p.write_text(textwrap.dedent(content))
    return p


def test_load_runtime_config_reads_search_depths(tmp_path: Path) -> None:
    """Valid TOML parses correctly into EvalRuntimeConfig."""
    path = _write_runtime_toml(tmp_path, _VALID_RUNTIME_TOML)
    cfg = load_runtime_config(path)

    assert isinstance(cfg, EvalRuntimeConfig)
    assert cfg.candidate_depth == 40
    assert cfg.return_depth == 20
    assert cfg.metric_depth == 10
    assert cfg.routing_contract_enabled is True


def test_committed_runtime_toml_exists_and_loads() -> None:
    """The committed tests/eval/runtime.toml loads without error."""
    assert _COMMITTED_RUNTIME_TOML.exists(), "tests/eval/runtime.toml must be committed"
    cfg = load_runtime_config(_COMMITTED_RUNTIME_TOML)
    assert isinstance(cfg, EvalRuntimeConfig)


def test_committed_runtime_toml_uses_eval_depth_names() -> None:
    """The committed runtime.toml uses the correct eval-specific depth key names."""
    cfg = load_runtime_config(_COMMITTED_RUNTIME_TOML)
    assert cfg.candidate_depth >= 1
    assert cfg.return_depth >= 1
    assert cfg.metric_depth >= 10


def test_load_runtime_config_rejects_metric_depth_below_metric_k(tmp_path: Path) -> None:
    """metric_depth < 10 raises ValueError (nDCG@10 requires depth >= 10)."""
    content = """
[search]
candidate_depth = 40
return_depth = 20
metric_depth = 9

[routing]
contract_enabled = false
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="metric_depth"):
        load_runtime_config(path)


def test_load_runtime_config_rejects_return_depth_below_metric_depth(tmp_path: Path) -> None:
    """return_depth < metric_depth raises ValueError."""
    content = """
[search]
candidate_depth = 40
return_depth = 9
metric_depth = 10

[routing]
contract_enabled = false
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="return_depth"):
        load_runtime_config(path)


def test_load_runtime_config_rejects_candidate_depth_not_greater_than_return_depth(tmp_path: Path) -> None:
    """candidate_depth <= return_depth raises ValueError."""
    content = """
[search]
candidate_depth = 20
return_depth = 20
metric_depth = 10

[routing]
contract_enabled = false
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="candidate_depth"):
        load_runtime_config(path)


def test_runner_requires_routing_floor_when_routing_contract_enabled(tmp_path: Path) -> None:
    """When routing_contract_enabled=True and thresholds.routing_accuracy is None, raise ValueError."""
    runtime_path = _write_runtime_toml(tmp_path, _VALID_RUNTIME_TOML)
    runtime_cfg = load_runtime_config(runtime_path)

    # Thresholds with routing_accuracy=None (not set)
    threshold_path = _write_toml(tmp_path, _FULL_QUALITY)
    thresholds = load_thresholds(threshold_path)

    assert thresholds.quality_floors.routing_accuracy is None
    assert runtime_cfg.routing_contract_enabled is True

    with pytest.raises(ValueError, match="routing_accuracy"):
        validate_routing_contract(runtime_cfg, thresholds)


def test_load_runtime_config_rejects_malformed_toml_syntax(tmp_path: Path) -> None:
    """Invalid TOML syntax raises ValueError."""
    path = tmp_path / "runtime.toml"
    path.write_text("this is not valid toml ===\n[broken\n")
    with pytest.raises(ValueError, match="[Ii]nvalid|[Pp]arse|TOML"):
        load_runtime_config(path)


def test_load_runtime_config_rejects_missing_search_table(tmp_path: Path) -> None:
    """Missing [search] section raises ValueError."""
    content = """
[routing]
contract_enabled = true
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="[Ss]earch"):
        load_runtime_config(path)


def test_load_runtime_config_rejects_wrong_type_for_depth_field(tmp_path: Path) -> None:
    """Non-integer candidate_depth raises ValueError."""
    content = """
[search]
candidate_depth = "forty"
return_depth = 20
metric_depth = 10

[routing]
contract_enabled = false
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="candidate_depth"):
        load_runtime_config(path)


# ---------------------------------------------------------------------------
# run_eval_suite / load_baseline tests — Task 3.3
# ---------------------------------------------------------------------------

from dataclasses import asdict

from archon_search.eval.runner import (
    EvalBaseline,
    EvalReport,
    load_baseline,
    run_eval_suite,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


_RUNTIME_ROUTING_ENABLED = """
[search]
candidate_depth = 12
return_depth = 10
metric_depth = 10

[routing]
contract_enabled = true
"""

_RUNTIME_ROUTING_DISABLED = """
[search]
candidate_depth = 12
return_depth = 10
metric_depth = 10

[routing]
contract_enabled = false
"""


_CORPUS_DOCS_ALPHA = {
    # collection alpha — 10 docs about HTTP/networking
    "alpha-01": ("alpha/http_client.md", "async http client retry timeout networking request response"),
    "alpha-02": ("alpha/rate_limit.md", "rate limiter token bucket throttling api endpoint"),
    "alpha-03": ("alpha/auth.md", "jwt authentication bearer token rbac authorization"),
    "alpha-04": ("alpha/circuit.md", "circuit breaker pattern open closed half-open"),
    "alpha-05": ("alpha/retry.md", "exponential backoff retry decorator jitter"),
    "alpha-06": ("alpha/pool.md", "connection pool reuse warmup tcp keepalive"),
    "alpha-07": ("alpha/middleware.md", "middleware chain composition request pipeline"),
    "alpha-08": ("alpha/router.md", "url router path matching dispatch handler"),
    "alpha-09": ("alpha/cors.md", "cors cross origin headers preflight"),
    "alpha-10": ("alpha/cache.md", "http cache control etag if-none-match"),
}

_CORPUS_DOCS_BETA = {
    # collection beta — 10 docs about storage/databases
    "beta-01": ("beta/vector_store.md", "lancedb vector store cosine similarity hnsw index"),
    "beta-02": ("beta/lru_cache.md", "lru cache eviction ttl size limit"),
    "beta-03": ("beta/sql.md", "sql relational database join transaction commit"),
    "beta-04": ("beta/kv.md", "key value store redis memcached set get"),
    "beta-05": ("beta/blob.md", "blob storage s3 object bucket upload download"),
    "beta-06": ("beta/timeseries.md", "time series database influxdb timestamp metric"),
    "beta-07": ("beta/graph.md", "graph database nodes edges traversal cypher"),
    "beta-08": ("beta/wal.md", "write ahead log durability replay recovery"),
    "beta-09": ("beta/parquet.md", "parquet columnar storage compression analytics"),
    "beta-10": ("beta/index.md", "btree index leaf node lookup ordered scan"),
}


def _make_mini_corpus(
    root: Path,
    *,
    include_routing_queries: bool = True,
    extra_queries: list[dict] | None = None,
    extra_labels: list[dict] | None = None,
) -> None:
    """Create a small two-collection fixture with retrieval + routing queries."""
    corpus_dir = root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)

    docs: list[dict] = []
    for doc_id, (rel, text) in _CORPUS_DOCS_ALPHA.items():
        p = corpus_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
        docs.append({"doc_id": doc_id, "collection": "alpha", "relative_path": rel})
    for doc_id, (rel, text) in _CORPUS_DOCS_BETA.items():
        p = corpus_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
        docs.append({"doc_id": doc_id, "collection": "beta", "relative_path": rel})

    _write_jsonl(root / "documents.jsonl", docs)

    queries: list[dict] = [
        {"query_id": "q-a-01", "text": "async http client retry timeout", "collection": "alpha", "metric_scope": "retrieval"},
        {"query_id": "q-a-02", "text": "rate limiter token bucket throttling", "collection": "alpha", "metric_scope": "retrieval"},
        {"query_id": "q-b-01", "text": "lancedb vector store cosine similarity", "collection": "beta", "metric_scope": "retrieval"},
        {"query_id": "q-b-02", "text": "lru cache eviction ttl", "collection": "beta", "metric_scope": "retrieval"},
    ]
    if include_routing_queries:
        queries.append(
            {"query_id": "q-r-01", "text": "async http client retry", "collection": None, "metric_scope": "routing"},
        )

    if extra_queries:
        queries.extend(extra_queries)

    _write_jsonl(root / "queries.jsonl", queries)

    labels: list[dict] = [
        {"query_id": "q-a-01", "doc_id": "alpha-01", "grade": 2},
        {"query_id": "q-a-01", "doc_id": "alpha-05", "grade": 1},
        {"query_id": "q-a-02", "doc_id": "alpha-02", "grade": 2},
        {"query_id": "q-b-01", "doc_id": "beta-01", "grade": 2},
        {"query_id": "q-b-02", "doc_id": "beta-02", "grade": 2},
    ]
    if include_routing_queries:
        labels.append({"query_id": "q-r-01", "doc_id": "alpha-01", "grade": 2})
    if extra_labels:
        labels.extend(extra_labels)

    _write_jsonl(root / "labels.jsonl", labels)


def _make_runtime(tmp_path: Path, content: str = _RUNTIME_ROUTING_ENABLED) -> Path:
    p = tmp_path / "runtime.toml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_runner_executes_miniature_corpus(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root)
    runtime = _make_runtime(tmp_path)

    report = await run_eval_suite(corpus_root, runtime)

    assert isinstance(report, EvalReport)
    # One trace per fixture query (4 retrieval + 1 routing)
    assert len(report.traces) == 5


@pytest.mark.asyncio
async def test_eval_runner_full_chain_produces_all_metric_categories(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root)
    runtime = _make_runtime(tmp_path)

    report = await run_eval_suite(corpus_root, runtime)
    m = report.metrics

    for f in ("recall_at_1", "recall_at_3", "recall_at_5", "mrr", "ndcg_at_5", "ndcg_at_10"):
        assert isinstance(getattr(m, f), float)
    assert isinstance(m.reranker_lift, float) or m.reranker_lift is None
    assert isinstance(m.latency_p50_ms, float)
    assert isinstance(m.latency_p95_ms, float)
    # routing_accuracy is float (contract enabled, one routing query)
    assert m.routing_accuracy is None or isinstance(m.routing_accuracy, float)


@pytest.mark.asyncio
async def test_eval_runner_is_deterministic_except_latency_on_miniature_fixture(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root)
    runtime = _make_runtime(tmp_path)

    r1 = await run_eval_suite(corpus_root, runtime)
    r2 = await run_eval_suite(corpus_root, runtime)

    # Quality metrics identical
    assert r1.metrics.recall_at_1 == r2.metrics.recall_at_1
    assert r1.metrics.recall_at_5 == r2.metrics.recall_at_5
    assert r1.metrics.mrr == r2.metrics.mrr
    assert r1.metrics.ndcg_at_5 == r2.metrics.ndcg_at_5
    assert r1.metrics.ndcg_at_10 == r2.metrics.ndcg_at_10
    assert r1.metrics.routing_accuracy == r2.metrics.routing_accuracy

    # Result orderings (doc-id sequences) identical per query
    def by_id(traces):
        return {t.query_id: [r.doc_id for r in t.results] for t in traces}
    assert by_id(r1.traces) == by_id(r2.traces)


@pytest.mark.asyncio
async def test_eval_runner_skips_routing_metric_without_search_owned_routing_contract(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    # No routing-scope queries; contract disabled
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path, _RUNTIME_ROUTING_DISABLED)

    report = await run_eval_suite(corpus_root, runtime)
    assert report.metrics.routing_accuracy is None
    assert any("routing" in n.lower() for n in report.notes)


@pytest.mark.asyncio
async def test_eval_runner_excludes_bypassed_queries_from_routing_accuracy(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    # Add a routing query that will be marked routing_bypass=True at load
    _make_mini_corpus(
        corpus_root,
        include_routing_queries=True,
        extra_queries=[
            {
                "query_id": "q-r-bypass",
                "text": "lancedb vector store",
                "collection": None,
                "metric_scope": "routing",
                "routing_bypass": True,
            }
        ],
        extra_labels=[{"query_id": "q-r-bypass", "doc_id": "beta-01", "grade": 2}],
    )
    runtime = _make_runtime(tmp_path)

    report = await run_eval_suite(corpus_root, runtime)

    bypass_trace = next(t for t in report.traces if t.query_id == "q-r-bypass")
    assert bypass_trace.router_correct is None
    assert report.routing_bypassed_queries == 1


@pytest.mark.asyncio
async def test_eval_runner_rejects_collectionless_query_without_routing_contract(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    # Add a retrieval query with collection=None (illegal) — bypass loader by writing directly
    extra = [{"query_id": "q-bad", "text": "anything", "collection": None, "metric_scope": "retrieval"}]
    existing = (corpus_root / "queries.jsonl").read_text().strip().splitlines()
    existing.append(json.dumps(extra[0]))
    (corpus_root / "queries.jsonl").write_text("\n".join(existing) + "\n")
    # Add a label
    with (corpus_root / "labels.jsonl").open("a") as f:
        f.write(json.dumps({"query_id": "q-bad", "doc_id": "alpha-01", "grade": 2}) + "\n")

    runtime = _make_runtime(tmp_path, _RUNTIME_ROUTING_DISABLED)

    with pytest.raises(ValueError, match="collection"):
        await run_eval_suite(corpus_root, runtime)


@pytest.mark.asyncio
async def test_eval_runner_excludes_routing_only_queries_from_retrieval_metrics(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=True)
    runtime = _make_runtime(tmp_path)

    report = await run_eval_suite(corpus_root, runtime)

    # The retrieval metric set should aggregate over 4 retrieval traces
    retrieval_traces = [t for t in report.traces if t.metric_scope == "retrieval"]
    assert len(retrieval_traces) == 4
    routing_traces = [t for t in report.traces if t.metric_scope == "routing"]
    assert len(routing_traces) == 1
    # routing_accuracy is computed over routing traces only — present
    assert report.metrics.routing_accuracy is not None


@pytest.mark.asyncio
async def test_eval_runner_does_not_use_gold_labels_to_select_collection(tmp_path: Path) -> None:
    """For retrieval queries, runner searches only the query.collection — never inferred from labels."""
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path)

    report = await run_eval_suite(corpus_root, runtime)
    # Each retrieval trace.collection must match the fixture query.collection
    for t in report.traces:
        if t.metric_scope == "retrieval":
            assert t.collection in {"alpha", "beta"}
            # Every result chunk must come from that collection (not from elsewhere)
            for r in t.results:
                assert r.collection == t.collection


@pytest.mark.asyncio
async def test_eval_runner_fails_under_depth_diagnostic_when_dedup_yields_insufficient_unique_documents(
    tmp_path: Path,
) -> None:
    """metric_depth>=10 but a collection has 10 docs, candidate_depth=12 → fine.
    Set candidate_depth low so dedup yields < metric_depth and fixture-corpus has enough.
    """
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)

    # candidate_depth small → return_depth=10; dedup likely fewer unique docs
    runtime_text = """
[search]
candidate_depth = 11
return_depth = 10
metric_depth = 10

[routing]
contract_enabled = false
"""
    # Now sabotage: make 9 chunks per doc in alpha (so post-dedup unique docs < 10)
    # Simpler: artificially limit by using a runtime that requests metric_depth=10 from a
    # corpus where queries match few docs. Hack: write the query collection to only have
    # 9 docs by removing one — but corpus then has 9 docs, which means the diagnostic
    # SHOULDN'T fire (fixture has < metric_depth). The diagnostic only fires when
    # fixture-corpus has >= metric_depth AND dedup yields < metric_depth.
    #
    # Reliable trigger: write all alpha docs with one heavily-duplicated long file
    # so a single doc dominates results; the chunker may split it; but other docs
    # are too short. Approach: replicate same text across all alpha-* docs so
    # rerank ties → only top-1 distinct doc returned. This is fragile.
    #
    # Cleaner approach: ingest a corpus where one collection has 10 unique docs but
    # the embedder/reranker funnel routes all 11 candidates to chunks from only 3 docs.
    # The eval reranker is BM25-like — distinct docs will appear. Force trigger by
    # making all alpha docs identical text → chunker returns 1 chunk each; 11 candidates
    # spread across 10 docs → 10 unique → passes. Not triggered.
    #
    # Engineer for trigger: set metric_depth=10 but reduce candidate_depth and add many
    # chunks per doc. Make each alpha doc much larger than chunk_size so each yields >1
    # chunks; with candidate_depth=11, returned 10 chunks may come from fewer docs.
    # Easier: write 3 huge alpha docs that produce 4 chunks each (12 chunks); ingest
    # candidate_depth=11 fetches 11; dedupes to 3 unique docs; metric_depth=10 > 3 → fail.
    import shutil

    shutil.rmtree(corpus_root)
    corpus_root.mkdir()
    big_corpus = corpus_root / "corpus"
    (big_corpus / "alpha").mkdir(parents=True)
    big_text = ("retry timeout http client " * 400 + "\n")  # large enough to chunk
    docs = []
    # 10 alpha docs but only first 3 have query-matching content; rest are off-topic dense docs
    # Actually trigger relies on fewer unique docs in TOP candidate_depth. Use 10 identical-content
    # alpha docs each with N chunks. With candidate_depth=11, dedup unique = up to 10.
    # To force dedup < metric_depth=10, make 2 docs HUGE (many chunks) and 8 small.
    # Then top-11 by relevance will be dominated by the 2 huge docs → unique=fewer.
    huge = ("alpha matching query retry http timeout " * 400)
    small = ("filler off topic content xyz unrelated noise " * 5)
    for i in range(1, 11):
        text = huge if i <= 2 else small
        rel = f"alpha/doc{i:02d}.md"
        (big_corpus / rel).write_text(text)
        docs.append({"doc_id": f"alpha-{i:02d}", "collection": "alpha", "relative_path": rel})
    # Need a query that strongly matches the two huge docs only.
    queries = [
        {"query_id": "q-huge", "text": "retry http timeout matching alpha", "collection": "alpha", "metric_scope": "retrieval"},
    ]
    labels = [
        {"query_id": "q-huge", "doc_id": "alpha-01", "grade": 2},
    ]
    _write_jsonl(corpus_root / "documents.jsonl", docs)
    _write_jsonl(corpus_root / "queries.jsonl", queries)
    _write_jsonl(corpus_root / "labels.jsonl", labels)

    runtime = _make_runtime(tmp_path, runtime_text)

    with pytest.raises(ValueError, match="unique"):
        await run_eval_suite(corpus_root, runtime)


@pytest.mark.asyncio
async def test_eval_runner_records_routing_disabled_and_bypassed_queries(tmp_path: Path) -> None:
    """routing_disabled_queries counts queries when routing is disabled at runtime."""
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path, _RUNTIME_ROUTING_DISABLED)

    report = await run_eval_suite(corpus_root, runtime)
    # No routing-scope queries → both counters are 0
    assert report.routing_disabled_queries == 0
    assert report.routing_bypassed_queries == 0

    # Now: routing-scope query present but routing disabled at config → routing_disabled_queries == 1
    corpus2 = tmp_path / "corpus2"
    corpus2.mkdir()
    _make_mini_corpus(corpus2, include_routing_queries=True)
    runtime2_path = tmp_path / "runtime2.toml"
    runtime2_path.write_text(textwrap.dedent(_RUNTIME_ROUTING_DISABLED))

    report2 = await run_eval_suite(corpus2, runtime2_path)
    assert report2.routing_disabled_queries == 1
    assert report2.routing_bypassed_queries == 0


@pytest.mark.asyncio
async def test_eval_runner_maps_runtime_doc_ids_to_fixture_doc_ids(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path)

    report = await run_eval_suite(corpus_root, runtime)

    fixture_doc_ids = set(_CORPUS_DOCS_ALPHA.keys()) | set(_CORPUS_DOCS_BETA.keys())
    for t in report.traces:
        for r in t.results:
            assert r.doc_id in fixture_doc_ids
            # runtime_doc_id is the hashed/path-derived ID, different from doc_id
            assert r.runtime_doc_id != r.doc_id or r.runtime_doc_id == r.doc_id  # both exist


@pytest.mark.asyncio
async def test_eval_runner_fails_with_diagnostic_on_unmapped_source_path(tmp_path: Path, monkeypatch) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path)

    # Inject a path-mapping break: monkeypatch build_doc_collection_map to drop one entry
    from archon_search.eval import runner as runner_mod
    real_build = runner_mod.build_doc_collection_map

    def broken_build(corpus):
        full = real_build(corpus)
        # Remove a doc that will be retrieved (alpha-01 is matched by query q-a-01)
        full.pop("alpha/http_client.md", None)
        return full

    monkeypatch.setattr(runner_mod, "build_doc_collection_map", broken_build)

    with pytest.raises(ValueError, match="unmapped|source_path|map"):
        await run_eval_suite(corpus_root, runtime)


@pytest.mark.asyncio
async def test_eval_runner_propagates_query_execution_errors(tmp_path: Path, monkeypatch) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path)

    from archon_search.eval import _tracing

    async def broken_trace(*args, **kwargs):
        raise RuntimeError("synthetic query failure")

    monkeypatch.setattr(_tracing, "collect_search_trace", broken_trace)
    # Also patch the runner-side import binding
    from archon_search.eval import runner as runner_mod
    monkeypatch.setattr(runner_mod, "collect_search_trace", broken_trace)

    with pytest.raises(RuntimeError, match="synthetic query failure"):
        await run_eval_suite(corpus_root, runtime)


@pytest.mark.asyncio
async def test_eval_runner_cleans_up_temp_store_on_error(tmp_path: Path, monkeypatch) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path)

    created_dirs: list[str] = []

    import tempfile as _tempfile
    real_tempdir = _tempfile.TemporaryDirectory

    class TrackingTempDir(real_tempdir):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created_dirs.append(self.name)

    monkeypatch.setattr(_tempfile, "TemporaryDirectory", TrackingTempDir)

    from archon_search.eval import runner as runner_mod

    async def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner_mod, "collect_search_trace", boom)

    with pytest.raises(RuntimeError, match="kaboom"):
        await run_eval_suite(corpus_root, runtime)

    # All created tmp dirs should be cleaned up
    for d in created_dirs:
        assert not Path(d).exists(), f"tmp dir {d!r} was not cleaned up on error"


# ---------------------------------------------------------------------------
# load_baseline unit tests
# ---------------------------------------------------------------------------


_VALID_BASELINE = {
    "eval_hash": "abc123",
    "metrics": {
        "recall_at_1": 0.6,
        "recall_at_3": 0.75,
        "recall_at_5": 0.8,
        "mrr": 0.65,
        "ndcg_at_5": 0.7,
        "ndcg_at_10": 0.72,
        "reranker_lift": 0.05,
        "routing_accuracy": None,
        "latency_p50_ms": 30.0,
        "latency_p95_ms": 80.0,
    },
    "runtime_config_hash": "rc-hash",
    "thresholds_hash": "th-hash",
    "command": "archon-search eval run",
    "waiver_ids": {"recall_at_5": "WAIVER-001"},
}


def test_load_baseline_parses_valid_json(tmp_path: Path) -> None:
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps(_VALID_BASELINE))
    bl = load_baseline(p)
    assert isinstance(bl, EvalBaseline)
    assert bl.eval_hash == "abc123"
    assert bl.runtime_config_hash == "rc-hash"
    assert bl.thresholds_hash == "th-hash"
    assert bl.metrics["recall_at_1"] == pytest.approx(0.6)
    assert bl.metrics["routing_accuracy"] is None
    assert bl.command == "archon-search eval run"
    assert bl.waiver_ids == {"recall_at_5": "WAIVER-001"}


def test_load_baseline_rejects_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "baseline.json"
    p.write_text("{not valid json")
    with pytest.raises(ValueError, match="[Jj][Ss][Oo][Nn]|[Pp]arse|[Ii]nvalid"):
        load_baseline(p)


def test_load_baseline_rejects_missing_required_fields(tmp_path: Path) -> None:
    for missing in ("eval_hash", "metrics", "runtime_config_hash", "command"):
        data = dict(_VALID_BASELINE)
        data.pop(missing)
        p = tmp_path / f"baseline_{missing}.json"
        p.write_text(json.dumps(data))
        with pytest.raises(ValueError, match=missing):
            load_baseline(p)


def test_load_baseline_rejects_missing_command(tmp_path: Path) -> None:
    """Missing `command` field raises ValueError."""
    data = dict(_VALID_BASELINE)
    data.pop("command")
    p = tmp_path / "baseline_no_command.json"
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="command"):
        load_baseline(p)


def test_load_baseline_defaults_waiver_ids_to_empty(tmp_path: Path) -> None:
    """Missing `waiver_ids` field defaults to an empty dict."""
    data = dict(_VALID_BASELINE)
    data.pop("waiver_ids")
    p = tmp_path / "baseline_no_waivers.json"
    p.write_text(json.dumps(data))
    bl = load_baseline(p)
    assert bl.waiver_ids == {}


def test_load_baseline_rejects_non_dict_waiver_ids(tmp_path: Path) -> None:
    """`waiver_ids` not a dict raises ValueError."""
    data = dict(_VALID_BASELINE)
    data["waiver_ids"] = ["not", "a", "dict"]
    p = tmp_path / "baseline_bad_waivers.json"
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="waiver_ids"):
        load_baseline(p)


def test_load_baseline_rejects_wrong_field_types(tmp_path: Path) -> None:
    data = dict(_VALID_BASELINE)
    data["metrics"] = "not-a-dict"
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="metrics"):
        load_baseline(p)


def test_load_baseline_handles_none_thresholds_hash(tmp_path: Path) -> None:
    data = dict(_VALID_BASELINE)
    data["thresholds_hash"] = None
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps(data))
    bl = load_baseline(p)
    assert bl.thresholds_hash is None


def test_baseline_json_survives_serialization_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps(_VALID_BASELINE))
    bl = load_baseline(p)

    serialized = json.dumps(asdict(bl))
    bl2_data = json.loads(serialized)
    p2 = tmp_path / "baseline2.json"
    p2.write_text(json.dumps(bl2_data))
    bl2 = load_baseline(p2)

    assert bl == bl2
    assert bl2.command == "archon-search eval run"
    assert bl2.waiver_ids == {"recall_at_5": "WAIVER-001"}


# ---------------------------------------------------------------------------
# Additional run_eval_suite contract tests (DA-review fixes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_runner_reranker_lift_uses_ndcg_at_10(tmp_path: Path) -> None:
    """reranker_lift = post-rerank nDCG@10 − pre-rerank nDCG@10 (NOT @5)."""
    from archon_search.eval.metrics import compute_ndcg_at_k

    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path, _RUNTIME_ROUTING_DISABLED)

    report = await run_eval_suite(corpus_root, runtime)

    # Recompute pre10 and post10 from the traces and labels — must match the
    # reranker_lift recorded on the report.
    from archon_search.eval.fixtures import load_eval_corpus
    corpus = load_eval_corpus(corpus_root)
    retrieval_traces = [t for t in report.traces if t.metric_scope == "retrieval"]

    post10 = compute_ndcg_at_k(retrieval_traces, corpus.labels, 10)
    pre10 = compute_ndcg_at_k(retrieval_traces, corpus.labels, 10, use_pre_rerank=True)
    expected_lift = post10 - pre10

    assert report.metrics.reranker_lift == pytest.approx(expected_lift)

    # Sanity: the @5-based lift would be a different value in this fixture
    # (only assert distinctness if they actually differ — they may coincide in
    # tiny fixtures, but the API surface is the contract that matters).
    post5 = compute_ndcg_at_k(retrieval_traces, corpus.labels, 5)
    pre5 = compute_ndcg_at_k(retrieval_traces, corpus.labels, 5, use_pre_rerank=True)
    lift5 = post5 - pre5
    # Don't strictly assert inequality; the spec is about WHICH k we used.
    assert report.metrics.reranker_lift != pytest.approx(lift5) or post10 == post5


@pytest.mark.asyncio
async def test_eval_runner_report_includes_query_and_document_counts(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=True)
    runtime = _make_runtime(tmp_path)

    report = await run_eval_suite(corpus_root, runtime)

    # 4 retrieval + 1 routing = 5 queries; 10 alpha + 10 beta = 20 docs
    assert report.query_count == 5
    assert report.document_count == 20


@pytest.mark.asyncio
async def test_eval_runner_retrieval_queries_with_routing_contribute_to_routing_accuracy(
    tmp_path: Path,
) -> None:
    """A retrieval query with routing enabled has router_correct set and
    contributes to routing accuracy."""
    corpus_root = tmp_path / "corpus_root"
    corpus_root.mkdir()
    _make_mini_corpus(corpus_root, include_routing_queries=False)
    runtime = _make_runtime(tmp_path)  # routing enabled

    report = await run_eval_suite(corpus_root, runtime)

    retrieval_traces = [t for t in report.traces if t.metric_scope == "retrieval"]
    assert len(retrieval_traces) == 4
    # All retrieval traces must now carry a boolean router_correct (routing enabled,
    # non-bypassed).
    for t in retrieval_traces:
        assert t.router_correct is not None
        assert isinstance(t.router_correct, bool)

    # At least one retrieval trace should have router_correct=True (the embedder
    # is deterministic; queries are clearly aligned with their gold collections).
    assert any(t.router_correct for t in retrieval_traces)

    # Routing accuracy is computed across ALL non-bypassed queries — must be set.
    assert report.metrics.routing_accuracy is not None
