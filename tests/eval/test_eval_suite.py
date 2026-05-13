"""End-to-end report-only eval smoke tests — FEAT-039 Task 4.2.

These tests exercise the full eval pipeline against the committed corpus and
runtime config in calibration (report-only) mode. They never call
``assert_thresholds`` — that is the job of Task 4.3 gated tests.

The ``eval`` marker excludes these from the default pytest run; invoke them
with ``-m eval`` (or ``pytest tests/eval/``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_search.eval.runner import assert_thresholds, render_report, run_eval_suite


CORPUS_ROOT = Path(__file__).resolve().parent
RUNTIME_CONFIG_PATH = CORPUS_ROOT / "runtime.toml"
BASELINE_JSON = CORPUS_ROOT / "baselines" / "baseline.json"


_QUALITY_METRIC_FIELDS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "ndcg_at_10",
    "routing_accuracy",
)


@pytest.mark.eval
async def test_eval_suite_report_only_smoke() -> None:
    """Full eval suite runs end-to-end against the committed corpus and renders
    a non-empty report — without calling ``assert_thresholds``."""
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    rendered = render_report(report)
    assert rendered and rendered.strip(), (
        f"render_report returned empty output.\nReport:\n{rendered!r}"
    )


@pytest.mark.eval
async def test_eval_suite_report_only_does_not_assert_thresholds() -> None:
    """Calibration mode (thresholds=None) never raises from missing floors:
    the suite executes, the report renders, and ``report.thresholds`` is None."""
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    assert report.thresholds is None, (
        f"Expected report.thresholds to be None in calibration mode, "
        f"got {report.thresholds!r}"
    )

    rendered = render_report(report)
    assert rendered and rendered.strip(), (
        f"render_report returned empty output.\nReport:\n{rendered!r}"
    )


@pytest.mark.eval
async def test_eval_suite_is_deterministic_except_latency() -> None:
    """Two fresh runs of the eval suite produce identical quality metrics and
    identical ranked result orderings. Latency percentiles are excluded from
    the comparison because they are wall-clock measurements."""
    report1 = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )
    report2 = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    rendered1 = render_report(report1)
    rendered2 = render_report(report2)

    for field in _QUALITY_METRIC_FIELDS:
        v1 = getattr(report1.metrics, field)
        v2 = getattr(report2.metrics, field)
        assert v1 == v2, (
            f"Quality metric {field!r} differs between runs: {v1!r} vs {v2!r}\n"
            f"--- run 1 report ---\n{rendered1}\n"
            f"--- run 2 report ---\n{rendered2}"
        )

    def ordering(report) -> dict[str, list[str]]:
        return {t.query_id: [r.doc_id for r in t.results] for t in report.traces}

    o1, o2 = ordering(report1), ordering(report2)
    assert o1 == o2, (
        f"Result orderings differ between runs.\n"
        f"--- run 1 report ---\n{rendered1}\n"
        f"--- run 2 report ---\n{rendered2}"
    )


# ---------------------------------------------------------------------------
# Task 4.4 — gated eval smoke tests
# ---------------------------------------------------------------------------


def _write_baseline(path: Path, base: dict, **overrides) -> Path:
    """Write a copy of *base* with *overrides* applied to *path*."""
    data = dict(base)
    data.update(overrides)
    path.write_text(json.dumps(data))
    return path


@pytest.mark.eval
async def test_eval_suite_gated_smoke(thresholds_path: Path) -> None:
    """Gated suite runs end-to-end against committed thresholds + baseline and
    ``assert_thresholds`` does not raise."""
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    assert_thresholds(report)  # must not raise


@pytest.mark.eval
async def test_eval_suite_gated_smoke_reports_baseline_deltas(
    thresholds_path: Path,
) -> None:
    """Rendered report contains baseline delta lines."""
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    rendered = render_report(report).lower()
    assert "baseline" in rendered
    assert "delta" in rendered


@pytest.mark.eval
async def test_eval_suite_gated_smoke_rejects_stale_benchmark_or_threshold_hashes(
    thresholds_path: Path,
    tmp_path: Path,
) -> None:
    """A baseline with a stale ``thresholds_hash`` (mismatching the current
    thresholds.toml) fails gating with an explicit refresh message."""
    base = json.loads(BASELINE_JSON.read_text())
    stale = _write_baseline(
        tmp_path / "baseline.json",
        base,
        thresholds_hash="0" * 64,  # obviously wrong
    )
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=stale,
    )
    with pytest.raises(AssertionError, match="(?i)stale|refresh|hash"):
        assert_thresholds(report)


@pytest.mark.eval
async def test_eval_suite_gated_smoke_rejects_calibration_only_baseline(
    thresholds_path: Path,
    tmp_path: Path,
) -> None:
    """Baseline with ``thresholds_hash: null`` fails gating with refresh message."""
    base = json.loads(BASELINE_JSON.read_text())
    calibration_only = _write_baseline(
        tmp_path / "baseline.json",
        base,
        thresholds_hash=None,
    )
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=calibration_only,
    )
    with pytest.raises(AssertionError, match="(?i)calibration|refresh"):
        assert_thresholds(report)


@pytest.mark.eval
async def test_eval_suite_report_only_accepts_calibration_baseline_without_thresholds(
    tmp_path: Path,
) -> None:
    """In report-only mode (no thresholds), a calibration-only baseline is
    accepted: ``run_eval_suite`` succeeds and renders deltas; we do NOT call
    ``assert_thresholds``."""
    base = json.loads(BASELINE_JSON.read_text())
    calibration_only = _write_baseline(
        tmp_path / "baseline.json",
        base,
        thresholds_hash=None,
    )
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=calibration_only,
    )
    assert report.thresholds is None
    assert report.baseline is not None
    assert report.baseline.thresholds_hash is None
    rendered = render_report(report).lower()
    assert "baseline" in rendered
    assert "delta" in rendered


@pytest.mark.eval
async def test_eval_suite_gated_smoke_rejects_stale_eval_hash(
    thresholds_path: Path,
    tmp_path: Path,
) -> None:
    """A baseline with an obviously-wrong ``eval_hash`` fails gating."""
    base = json.loads(BASELINE_JSON.read_text())
    stale = _write_baseline(
        tmp_path / "baseline.json",
        base,
        eval_hash="0" * 64,
    )
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=stale,
    )
    with pytest.raises(AssertionError, match="(?i)stale|refresh|hash"):
        assert_thresholds(report)
