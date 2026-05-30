"""Live eval report module.

Provides load_live_thresholds() and (in later tasks) MetricVerdict,
LiveEvalReport, build_live_report(), write_live_report_json(), and
write_live_report_markdown().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from archon_search.eval.runner import EvalReport, EvalThresholds, load_thresholds

logger = logging.getLogger("archon")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MetricVerdict:
    """Verdict for a single metric against its threshold and baseline."""

    name: str
    actual: float | None
    threshold: float | None
    kind: Literal["floor", "ceiling"]
    status: Literal["pass", "fail", "skipped"]
    delta_from_threshold: float | None
    baseline_value: float | None
    delta_from_baseline: float | None


@dataclass
class LiveEvalReport:
    """Aggregated live eval report with per-metric verdicts."""

    verdicts: list[MetricVerdict]
    overall_status: Literal["pass", "fail", "report_only"]
    generated_at: datetime
    eval_report: EvalReport


# ---------------------------------------------------------------------------
# Metric lists
# ---------------------------------------------------------------------------

_QUALITY_FLOOR_METRICS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "ndcg_at_10",
)

_OPTIONAL_QUALITY_FLOOR_METRICS = (
    "routing_accuracy",
    "routing_mrr_centroid",
    "routing_mrr_hybrid",
    "routing_precision_at_1_centroid",
    "routing_precision_at_1_hybrid",
)

_LATENCY_CEILING_METRICS = (
    "latency_p50_ms",
    "latency_p95_ms",
)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_live_report(report: EvalReport) -> LiveEvalReport:
    """Build a :class:`LiveEvalReport` from an :class:`EvalReport`.

    Never raises — records failures as ``status="fail"``.
    """
    generated_at = datetime.now(UTC)

    if report.thresholds is None:
        # Report-only mode: emit a skipped verdict for every metric
        verdicts = _build_skipped_verdicts(report)
        return LiveEvalReport(
            verdicts=verdicts,
            overall_status="report_only",
            generated_at=generated_at,
            eval_report=report,
        )

    verdicts: list[MetricVerdict] = []

    # Quality floors (always included)
    for name in _QUALITY_FLOOR_METRICS:
        actual = getattr(report.metrics, name, None)
        threshold = getattr(report.thresholds.quality_floors, name, None)
        verdict = _floor_verdict(name, actual, threshold, report)
        verdicts.append(verdict)

    # Optional quality floors (only when threshold is non-None)
    for name in _OPTIONAL_QUALITY_FLOOR_METRICS:
        actual = getattr(report.metrics, name, None)
        threshold = getattr(report.thresholds.quality_floors, name, None)
        verdict = _floor_verdict(name, actual, threshold, report)
        verdicts.append(verdict)

    # Latency ceilings
    for name in _LATENCY_CEILING_METRICS:
        actual = getattr(report.metrics, name, None)
        threshold = getattr(report.thresholds.latency_ceilings, name, None)
        verdict = _ceiling_verdict(name, actual, threshold, report)
        verdicts.append(verdict)

    overall: Literal["pass", "fail"] = "pass"
    if any(v.status == "fail" for v in verdicts):
        overall = "fail"

    return LiveEvalReport(
        verdicts=verdicts,
        overall_status=overall,
        generated_at=generated_at,
        eval_report=report,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ALL_METRIC_NAMES = (
    list(_QUALITY_FLOOR_METRICS)
    + list(_OPTIONAL_QUALITY_FLOOR_METRICS)
    + list(_LATENCY_CEILING_METRICS)
)


def _build_skipped_verdicts(report: EvalReport) -> list[MetricVerdict]:
    verdicts = []
    for name in _ALL_METRIC_NAMES:
        actual = getattr(report.metrics, name, None)
        baseline_value = _get_baseline(report, name)
        delta_from_baseline = (
            actual - baseline_value
            if actual is not None and baseline_value is not None
            else None
        )
        # Determine kind for skipped verdicts (floor vs ceiling)
        kind: Literal["floor", "ceiling"] = (
            "ceiling" if name in _LATENCY_CEILING_METRICS else "floor"
        )
        verdicts.append(MetricVerdict(
            name=name,
            actual=actual,
            threshold=None,
            kind=kind,
            status="skipped",
            delta_from_threshold=None,
            baseline_value=baseline_value,
            delta_from_baseline=delta_from_baseline,
        ))
    return verdicts


def _get_baseline(report: EvalReport, name: str) -> float | None:
    if report.baseline is None:
        return None
    return report.baseline.metrics.get(name)


def _floor_verdict(
    name: str,
    actual: float | None,
    threshold: float | None,
    report: EvalReport,
) -> MetricVerdict:
    baseline_value = _get_baseline(report, name)
    delta_from_baseline = (
        actual - baseline_value
        if actual is not None and baseline_value is not None
        else None
    )

    if actual is None or threshold is None:
        return MetricVerdict(
            name=name,
            actual=actual,
            threshold=threshold,
            kind="floor",
            status="skipped",
            delta_from_threshold=None,
            baseline_value=baseline_value,
            delta_from_baseline=delta_from_baseline,
        )

    delta = actual - threshold
    status: Literal["pass", "fail"] = "pass" if delta > 0 else "fail"
    return MetricVerdict(
        name=name,
        actual=actual,
        threshold=threshold,
        kind="floor",
        status=status,
        delta_from_threshold=delta,
        baseline_value=baseline_value,
        delta_from_baseline=delta_from_baseline,
    )


def _ceiling_verdict(
    name: str,
    actual: float | None,
    threshold: float | None,
    report: EvalReport,
) -> MetricVerdict:
    baseline_value = _get_baseline(report, name)
    delta_from_baseline = (
        actual - baseline_value
        if actual is not None and baseline_value is not None
        else None
    )

    if actual is None or threshold is None:
        return MetricVerdict(
            name=name,
            actual=actual,
            threshold=threshold,
            kind="ceiling",
            status="skipped",
            delta_from_threshold=None,
            baseline_value=baseline_value,
            delta_from_baseline=delta_from_baseline,
        )

    delta = threshold - actual  # positive = headroom (pass), negative = over ceiling (fail)
    status_c: Literal["pass", "fail"] = "pass" if delta > 0 else "fail"
    return MetricVerdict(
        name=name,
        actual=actual,
        threshold=threshold,
        kind="ceiling",
        status=status_c,
        delta_from_threshold=delta,
        baseline_value=baseline_value,
        delta_from_baseline=delta_from_baseline,
    )


def load_live_thresholds(path: Path) -> EvalThresholds | None:
    """Load live eval thresholds from *path*, returning None in report-only mode.

    Returns None (instead of raising) when:
    - The file does not exist
    - The TOML is malformed or missing required sections
    """
    if not path.exists():
        logger.warning("live_thresholds.toml not found at %s — report-only mode", path)
        return None
    try:
        return load_thresholds(path)
    except ValueError as exc:
        logger.warning(
            "live_thresholds.toml missing required sections (%s) — report-only mode", exc
        )
        return None
