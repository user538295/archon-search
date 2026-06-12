"""Pure-logic acceptance tests for the live eval lane (marked `eval`).

These tests do not require model weights and run in the PR eval suite.
"""

import pytest
import tomllib
from pathlib import Path

from archon_search.eval.types import EvalMetrics
from archon_search.eval.runner import (
    EvalReport,
    EvalThresholds,
    EvalBaseline,
    EvalQualityFloors,
    EvalLatencyCeilings,
    EvalRuntimeConfig,
)

PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"
CORPUS_ROOT = Path(__file__).resolve().parent
RUNTIME_CFG_PATH = CORPUS_ROOT / "runtime.toml"


@pytest.mark.eval
def test_live_eval_marker_included_in_default_run() -> None:
    """live_eval must appear in the markers list and must NOT be excluded from addopts."""
    with PYPROJECT_PATH.open("rb") as f:
        config = tomllib.load(f)

    pytest_cfg = config["tool"]["pytest"]["ini_options"]
    markers: list[str] = pytest_cfg["markers"]
    addopts: str = pytest_cfg["addopts"]

    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "live_eval" in marker_names, "live_eval marker must be registered in pyproject.toml [markers]"
    assert "not live_eval" not in addopts, "live_eval must not be excluded from the default run"


@pytest.mark.eval
async def test_deterministic_backend_uses_stubs(tmp_path: Path) -> None:
    """Default backend must use EvalEmbedderBackend and EvalRerankerBackend (brief test 2)."""
    from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
    from archon_search.eval.runner import _build_pipeline_with_eval_backends

    pipeline = await _build_pipeline_with_eval_backends(tmp_path)
    assert isinstance(pipeline._global_embedder._backend, EvalEmbedderBackend)
    assert isinstance(pipeline._reranker._backend, EvalRerankerBackend)


@pytest.mark.eval
async def test_live_backend_guard_fires_when_eval_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """live backend must raise RuntimeError when ARCHON_SEARCH_EVAL_BACKENDS=1 is set."""
    from archon_search.eval.runner import _build_pipeline_with_eval_backends

    monkeypatch.setenv("ARCHON_SEARCH_EVAL_BACKENDS", "1")
    with pytest.raises(RuntimeError, match="ARCHON_SEARCH_EVAL_BACKENDS"):
        await _build_pipeline_with_eval_backends(tmp_path, backend="live")


@pytest.mark.eval
async def test_run_eval_suite_default_backend_is_deterministic() -> None:
    """run_eval_suite() with no backend kwarg uses deterministic backend and returns EvalReport."""
    from archon_search.eval.runner import EvalReport, run_eval_suite

    report = await run_eval_suite(CORPUS_ROOT, RUNTIME_CFG_PATH)
    assert isinstance(report, EvalReport)
    assert report.metrics.recall_at_1 >= 0.0


_LIVE_THRESHOLDS_TOML = CORPUS_ROOT / "live_thresholds.toml"

_VALID_THRESHOLDS_CONTENT = """\
[quality_floors]
recall_at_1 = 0.5
recall_at_3 = 0.7
recall_at_5 = 0.8
mrr = 0.6
ndcg_at_5 = 0.65
ndcg_at_10 = 0.7

[latency_ceilings]
latency_p50_ms = 200.0
latency_p95_ms = 500.0
"""


@pytest.mark.eval
def test_load_live_thresholds_with_valid_file(tmp_path: Path) -> None:
    """Valid TOML with [quality_floors] returns EvalThresholds (brief test 4, with-section case)."""
    from archon_search.eval.live_report import load_live_thresholds
    from archon_search.eval.runner import EvalThresholds

    p = tmp_path / "live_thresholds.toml"
    p.write_text(_VALID_THRESHOLDS_CONTENT)
    result = load_live_thresholds(p)
    assert isinstance(result, EvalThresholds)
    assert result.quality_floors.recall_at_1 == 0.5


@pytest.mark.eval
def test_load_live_thresholds_empty_stub() -> None:
    """The comment-only stub returns None without raising (brief test 4, without-section case)."""
    from archon_search.eval.live_report import load_live_thresholds

    result = load_live_thresholds(_LIVE_THRESHOLDS_TOML)
    assert result is None


@pytest.mark.eval
def test_load_live_thresholds_missing_file(tmp_path: Path) -> None:
    """Non-existent path returns None without raising."""
    from archon_search.eval.live_report import load_live_thresholds

    result = load_live_thresholds(tmp_path / "nonexistent.toml")
    assert result is None


@pytest.mark.eval
def test_load_live_thresholds_malformed_toml(tmp_path: Path) -> None:
    """Invalid TOML syntax returns None without raising."""
    from archon_search.eval.live_report import load_live_thresholds

    p = tmp_path / "bad.toml"
    p.write_text("[[[\n")
    result = load_live_thresholds(p)
    assert result is None


# ---------------------------------------------------------------------------
# Task 2.3 — MetricVerdict + LiveEvalReport + build_live_report helpers
# ---------------------------------------------------------------------------


def _make_metrics(**overrides) -> EvalMetrics:
    defaults = dict(
        recall_at_1=0.70,
        recall_at_3=0.80,
        recall_at_5=0.85,
        mrr=0.75,
        ndcg_at_5=0.78,
        ndcg_at_10=0.80,
        reranker_lift=0.05,
        routing_accuracy=None,
        latency_p50_ms=25.0,
        latency_p95_ms=80.0,
        routing_mrr_centroid=None,
        routing_mrr_hybrid=None,
        routing_precision_at_1_centroid=None,
        routing_precision_at_1_hybrid=None,
    )
    defaults.update(overrides)
    return EvalMetrics(**defaults)


def _make_floors(**overrides) -> EvalQualityFloors:
    defaults: dict = dict(
        recall_at_1=0.60,
        recall_at_3=0.70,
        recall_at_5=0.75,
        mrr=0.65,
        ndcg_at_5=0.68,
        ndcg_at_10=0.70,
        routing_accuracy=None,
    )
    defaults.update(overrides)
    return EvalQualityFloors(**defaults)


def _make_thresholds(**overrides) -> EvalThresholds:
    floors = overrides.pop("quality_floors", _make_floors())
    latency = overrides.pop("latency_ceilings", EvalLatencyCeilings())
    return EvalThresholds(quality_floors=floors, latency_ceilings=latency, **overrides)


def _make_runtime_cfg() -> EvalRuntimeConfig:
    return EvalRuntimeConfig(
        candidate_depth=12,
        return_depth=10,
        metric_depth=10,
        routing_contract_enabled=False,
    )


def _make_report(
    metrics=None,
    thresholds=None,
    baseline=None,
) -> EvalReport:
    return EvalReport(
        metrics=metrics if metrics is not None else _make_metrics(),
        traces=[],
        corpus_root=Path("/tmp/eval"),
        runtime_config=_make_runtime_cfg(),
        thresholds=thresholds,
        baseline=baseline,
        notes=[],
        routing_disabled_queries=0,
        routing_bypassed_queries=0,
        query_count=0,
        document_count=0,
    )


@pytest.mark.eval
def test_threshold_comparison_passes() -> None:
    """All metrics well above floors and below latency ceilings → overall_status == pass."""
    from archon_search.eval.live_report import build_live_report

    thresholds = _make_thresholds(
        latency_ceilings=EvalLatencyCeilings(latency_p50_ms=200.0, latency_p95_ms=500.0),
    )
    report = _make_report(
        metrics=_make_metrics(latency_p50_ms=25.0, latency_p95_ms=80.0),
        thresholds=thresholds,
    )
    live = build_live_report(report)
    assert live.overall_status == "pass"
    assert len(live.verdicts) == 13
    for v in live.verdicts:
        assert v.status in ("pass", "skipped"), f"{v.name} expected pass or skipped, got {v.status}"


@pytest.mark.eval
def test_threshold_comparison_fails() -> None:
    """recall_at_1 below floor → overall_status==fail, only recall_at_1 verdict fails."""
    from archon_search.eval.live_report import build_live_report

    thresholds = _make_thresholds(
        quality_floors=_make_floors(recall_at_1=0.5, routing_accuracy=None),
        latency_ceilings=EvalLatencyCeilings(latency_p50_ms=200.0, latency_p95_ms=500.0),
    )
    report = _make_report(
        metrics=_make_metrics(recall_at_1=0.3),
        thresholds=thresholds,
    )
    live = build_live_report(report)
    assert live.overall_status == "fail"

    by_name = {v.name: v for v in live.verdicts}
    recall_v = by_name["recall_at_1"]
    assert recall_v.status == "fail"
    assert recall_v.delta_from_threshold is not None
    assert recall_v.delta_from_threshold < 0

    for name, v in by_name.items():
        if name != "recall_at_1":
            assert v.status != "fail", f"{name} should not fail"


@pytest.mark.eval
def test_report_only_when_no_thresholds() -> None:
    """thresholds=None → overall_status==report_only; all verdicts skipped."""
    from archon_search.eval.live_report import build_live_report

    report = _make_report(thresholds=None)
    live = build_live_report(report)
    assert live.overall_status == "report_only"
    for v in live.verdicts:
        assert v.status == "skipped", f"{v.name} expected skipped, got {v.status}"


@pytest.mark.eval
def test_report_only_with_baseline_computes_deltas() -> None:
    """thresholds=None + baseline present → skipped verdicts still compute delta_from_baseline."""
    from archon_search.eval.live_report import build_live_report

    baseline = EvalBaseline(
        eval_hash="abc123",
        metrics={"recall_at_1": 0.65, "mrr": 0.70},
        runtime_config_hash="def456",
        command="uv run pytest",
    )
    report = _make_report(thresholds=None, baseline=baseline)
    live = build_live_report(report)
    assert live.overall_status == "report_only"
    by_name = {v.name: v for v in live.verdicts}
    # Metrics present in baseline get delta computed
    r1 = by_name["recall_at_1"]
    assert r1.status == "skipped"
    assert r1.baseline_value == pytest.approx(0.65)
    assert r1.delta_from_baseline == pytest.approx(0.70 - 0.65)
    # Metrics absent from baseline have None delta
    ndcg = by_name["ndcg_at_5"]
    assert ndcg.baseline_value is None
    assert ndcg.delta_from_baseline is None


@pytest.mark.eval
def test_build_live_report_never_raises() -> None:
    """Even with all metrics below all floors, build_live_report returns without raising."""
    from archon_search.eval.live_report import LiveEvalReport, build_live_report

    thresholds = _make_thresholds(
        quality_floors=_make_floors(
            recall_at_1=0.99, recall_at_3=0.99, recall_at_5=0.99,
            mrr=0.99, ndcg_at_5=0.99, ndcg_at_10=0.99,
        ),
        latency_ceilings=EvalLatencyCeilings(latency_p50_ms=1.0, latency_p95_ms=1.0),
    )
    report = _make_report(
        metrics=_make_metrics(latency_p50_ms=9999.0, latency_p95_ms=9999.0),
        thresholds=thresholds,
    )
    result = build_live_report(report)
    assert isinstance(result, LiveEvalReport)


@pytest.mark.eval
def test_build_live_report_with_none_actual_metrics() -> None:
    """routing_accuracy=None in metrics → routing_accuracy verdict is skipped."""
    from archon_search.eval.live_report import build_live_report

    thresholds = _make_thresholds(
        quality_floors=_make_floors(routing_accuracy=0.80),
    )
    report = _make_report(
        metrics=_make_metrics(routing_accuracy=None),
        thresholds=thresholds,
    )
    live = build_live_report(report)
    by_name = {v.name: v for v in live.verdicts}
    assert "routing_accuracy" in by_name
    assert by_name["routing_accuracy"].status == "skipped"


@pytest.mark.eval
def test_optional_routing_metric_with_values_passes() -> None:
    """routing_accuracy with actual > floor → pass verdict (not skipped)."""
    from archon_search.eval.live_report import build_live_report

    thresholds = _make_thresholds(
        quality_floors=_make_floors(routing_accuracy=0.80),
    )
    report = _make_report(
        metrics=_make_metrics(routing_accuracy=0.90),
        thresholds=thresholds,
    )
    live = build_live_report(report)
    by_name = {v.name: v for v in live.verdicts}
    assert by_name["routing_accuracy"].status == "pass"
    assert by_name["routing_accuracy"].delta_from_threshold == pytest.approx(0.90 - 0.80)


@pytest.mark.eval
def test_ceiling_metric_delta_sign_convention() -> None:
    """delta_from_threshold for ceiling = ceiling - actual (positive=pass, negative=fail)."""
    from archon_search.eval.live_report import build_live_report

    # Case 1: actual > ceiling → fail, delta negative
    thresholds = _make_thresholds(
        latency_ceilings=EvalLatencyCeilings(latency_p50_ms=None, latency_p95_ms=500.0),
    )
    report = _make_report(
        metrics=_make_metrics(latency_p95_ms=600.0),
        thresholds=thresholds,
    )
    live = build_live_report(report)
    by_name = {v.name: v for v in live.verdicts}
    v95 = by_name["latency_p95_ms"]
    assert v95.status == "fail"
    assert v95.delta_from_threshold == pytest.approx(500.0 - 600.0)  # -100

    # Case 2: actual < ceiling → pass, delta positive
    report2 = _make_report(
        metrics=_make_metrics(latency_p95_ms=400.0),
        thresholds=thresholds,
    )
    live2 = build_live_report(report2)
    by_name2 = {v.name: v for v in live2.verdicts}
    v95_2 = by_name2["latency_p95_ms"]
    assert v95_2.status == "pass"
    assert v95_2.delta_from_threshold == pytest.approx(500.0 - 400.0)  # +100


@pytest.mark.eval
def test_threshold_comparison_boundary_exact_equality() -> None:
    """Exact equality (actual == floor) → status==fail with delta==0.0 (strict floor)."""
    from archon_search.eval.live_report import build_live_report

    thresholds = _make_thresholds(
        quality_floors=_make_floors(recall_at_1=0.5),
    )
    report = _make_report(
        metrics=_make_metrics(recall_at_1=0.5),
        thresholds=thresholds,
    )
    live = build_live_report(report)
    by_name = {v.name: v for v in live.verdicts}
    assert by_name["recall_at_1"].status == "fail"
    assert by_name["recall_at_1"].delta_from_threshold == pytest.approx(0.0)


@pytest.mark.eval
def test_ceiling_exact_equality_is_fail() -> None:
    """Exact equality (actual == ceiling) → status==fail with delta==0.0 (strict ceiling)."""
    from archon_search.eval.live_report import build_live_report

    thresholds = _make_thresholds(
        latency_ceilings=EvalLatencyCeilings(latency_p50_ms=None, latency_p95_ms=500.0),
    )
    report = _make_report(
        metrics=_make_metrics(latency_p95_ms=500.0),
        thresholds=thresholds,
    )
    live = build_live_report(report)
    by_name = {v.name: v for v in live.verdicts}
    assert by_name["latency_p95_ms"].status == "fail"
    assert by_name["latency_p95_ms"].delta_from_threshold == pytest.approx(0.0)


@pytest.mark.eval
def test_build_live_report_with_partial_thresholds() -> None:
    """latency_p50_ms=None ceiling → latency_p50_ms verdict is skipped."""
    from archon_search.eval.live_report import build_live_report

    thresholds = _make_thresholds(
        latency_ceilings=EvalLatencyCeilings(latency_p50_ms=None, latency_p95_ms=500.0),
    )
    report = _make_report(thresholds=thresholds)
    live = build_live_report(report)
    by_name = {v.name: v for v in live.verdicts}
    assert by_name["latency_p50_ms"].status == "skipped"
    assert by_name["latency_p95_ms"].status == "pass"


@pytest.mark.eval
def test_build_live_report_with_baseline() -> None:
    """With baseline present, verdicts have baseline_value and delta_from_baseline."""
    from archon_search.eval.live_report import build_live_report

    baseline = EvalBaseline(
        eval_hash="abc123",
        metrics={"recall_at_1": 0.65, "mrr": 0.70},
        runtime_config_hash="def456",
        command="uv run pytest",
    )
    thresholds = _make_thresholds()
    report = _make_report(thresholds=thresholds, baseline=baseline)
    live = build_live_report(report)
    by_name = {v.name: v for v in live.verdicts}

    r1 = by_name["recall_at_1"]
    assert r1.baseline_value == pytest.approx(0.65)
    assert r1.delta_from_baseline is not None
    assert r1.delta_from_baseline == pytest.approx(0.70 - 0.65)  # actual(0.70) - baseline(0.65)


# ---------------------------------------------------------------------------
# Task 2.4 — write_live_report_json + write_live_report_markdown
# ---------------------------------------------------------------------------


def _make_live_report() -> "LiveEvalReport":
    """Build a synthetic LiveEvalReport for serialisation tests."""
    from archon_search.eval.live_report import LiveEvalReport, MetricVerdict

    from datetime import datetime, UTC

    eval_report = _make_report(thresholds=_make_thresholds())
    verdicts = [
        MetricVerdict(
            name="recall_at_1",
            actual=0.75,
            threshold=0.60,
            kind="floor",
            status="pass",
            delta_from_threshold=0.15,
            baseline_value=0.65,
            delta_from_baseline=0.10,
        ),
        # Verdict with None actual and None threshold — must serialize as JSON null
        MetricVerdict(
            name="routing_accuracy",
            actual=None,
            threshold=None,
            kind="floor",
            status="skipped",
            delta_from_threshold=None,
            baseline_value=None,
            delta_from_baseline=None,
        ),
    ]
    return LiveEvalReport(
        verdicts=verdicts,
        overall_status="pass",
        generated_at=datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC),
        eval_report=eval_report,
    )


@pytest.mark.eval
def test_report_generation_format(tmp_path: Path) -> None:
    """write_live_report_json and write_live_report_markdown produce well-formed output."""
    import json
    import re

    from archon_search.eval.live_report import write_live_report_json, write_live_report_markdown

    r = _make_live_report()
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"

    write_live_report_json(r, json_path)
    write_live_report_markdown(r, md_path)

    # --- JSON assertions ---
    data = json.loads(json_path.read_text())
    assert set(data.keys()) == {"verdicts", "overall_status", "generated_at", "eval_report"}

    # generated_at is an ISO-8601 string
    assert isinstance(data["generated_at"], str)
    assert re.match(r"\d{4}-\d{2}-\d{2}T", data["generated_at"])

    # verdicts: None values serialize as JSON null (not string "None")
    routing_verdict = next(v for v in data["verdicts"] if v["name"] == "routing_accuracy")
    assert routing_verdict["actual"] is None  # JSON null → Python None
    assert routing_verdict["threshold"] is None
    assert routing_verdict["delta_from_threshold"] is None
    assert "None" not in json_path.read_text()

    # eval_report projected dict has exactly 4 keys
    er = data["eval_report"]
    assert set(er.keys()) == {"metrics", "query_count", "document_count", "generated_at"}
    assert isinstance(er["metrics"], dict)
    assert isinstance(er["query_count"], int)
    assert isinstance(er["document_count"], int)
    assert isinstance(er["generated_at"], str)
    assert re.match(r"\d{4}-\d{2}-\d{2}T", er["generated_at"])

    # --- Markdown assertions ---
    md = md_path.read_text()
    assert "# Live Eval Report" in md

    # Verdict table header present
    assert "| Metric |" in md or "Metric |" in md

    # Fenced block with the rendered report
    assert "```" in md
    assert "=== Archon Search Eval Report ===" in md

    # At least one data row (not just header)
    lines = md.splitlines()
    table_data_rows = [
        ln for ln in lines
        if ln.startswith("|") and "Metric" not in ln and "---" not in ln
    ]
    assert len(table_data_rows) >= 1

    # No Python literal "None" in any cell; None values render as em-dash
    routing_row = next((r for r in table_data_rows if "routing_accuracy" in r), None)
    assert routing_row is not None, "routing_accuracy row not found in table"
    assert "—" in routing_row, "None values should render as '—' in markdown table"
    for row in table_data_rows:
        assert "None" not in row, f"Python None literal found in table row: {row!r}"

    # Status column has at least one valid status value
    statuses_in_rows = [
        cell.strip()
        for row in table_data_rows
        for cell in row.split("|")
        if cell.strip() in ("pass", "fail", "skipped")
    ]
    assert len(statuses_in_rows) >= 1

    # Disclaimer line stripped from fenced block
    assert "deterministic eval backends" not in md


@pytest.mark.eval
def test_write_json_creates_parent_dirs(tmp_path: Path) -> None:
    """write_live_report_json creates nested parent directories automatically."""
    from archon_search.eval.live_report import write_live_report_json

    r = _make_live_report()
    deep_path = tmp_path / "nested" / "dir" / "report.json"
    write_live_report_json(r, deep_path)
    assert deep_path.exists()


@pytest.mark.eval
def test_write_markdown_creates_parent_dirs(tmp_path: Path) -> None:
    """write_live_report_markdown creates nested parent directories automatically."""
    from archon_search.eval.live_report import write_live_report_markdown

    r = _make_live_report()
    deep_path = tmp_path / "nested" / "dir" / "report.md"
    write_live_report_markdown(r, deep_path)
    assert deep_path.exists()
