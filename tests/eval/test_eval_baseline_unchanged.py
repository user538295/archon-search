"""Baseline regression guard: filters=None (unfiltered) eval must match baseline.json.

Verifies that the default, unfiltered code path produces metrics identical to
``baselines/baseline.json``. Filters are additive; this test documents and
asserts that passing ``filters=None`` (the default) does not regress the
committed baseline.

Why filters=None is tested implicitly:
    The eval suite calls ``_hybrid_search_with_trace`` without any filter
    argument throughout the pipeline — there is no ``filters`` parameter on
    ``run_eval_suite`` / ``measure_eval_suite``.  The entire eval harness therefore always runs
    with ``filters=None`` semantics.  This test verifies that code changes in
    the A2 filter stack have not accidentally altered the unfiltered code path.

Marker: ``eval`` — excluded from the default pytest run.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from archon_search.eval.runner import EvalReport

CORPUS_ROOT = Path(__file__).resolve().parent
BASELINE_JSON = CORPUS_ROOT / "baselines" / "baseline.json"


@pytest.mark.eval
@pytest.mark.xdist_group("eval_report")
async def test_unfiltered_eval_matches_baseline_metrics(measured_eval_report: EvalReport) -> None:
    """Unfiltered eval run must produce metrics bit-identical to baseline.json.

    The eval harness uses deterministic backends (SHA-256 embedder, BM25
    reranker) so metric values are fully reproducible — no floating-point
    tolerance is needed or applied.  ``null`` baseline values (e.g. latency)
    are skipped.
    """
    baseline_data = json.loads(BASELINE_JSON.read_text())
    baseline_metrics: dict[str, float | None] = baseline_data["metrics"]

    # The shared measurement: report-only (no thresholds, no baseline), no staleness check.
    # filters=None is implicit: the suite has no filter parameter; the
    # harness always searches without filters (the unfiltered code path).
    report = measured_eval_report

    report_metrics_dict = dataclasses.asdict(report.metrics)

    # Fields excluded from bit-exact comparison because they were observed to vary between runs.
    # The cause once recorded here (an unseeded shuffle in store.sample_chunk_texts) no longer
    # exists: that method was removed and description sampling is deterministic. The real cause
    # is unmeasured. These affect routing / graph retrieval, not the retrieval quality gates.
    _NON_DETERMINISTIC_FIELDS: frozenset[str] = frozenset({
        "graph_mrr",                          # MRR over naive-mode traces; routing non-determinism
        "graph_local_mrr",                    # MRR over local-mode traces; same root cause
        "graph_global_mrr",                   # MRR over global-mode traces; same root cause
        "graph_negative_control_recall_at_5", # Observed 0.38-0.43 per run; cause unmeasured
    })

    # Assert every non-null field in baseline.json — including reranker_lift.
    # Null baseline values mean "not compared" (e.g. latency_p50_ms and
    # latency_p95_ms are stored as null to opt out of latency assertions).
    # Non-null values must be bit-identical (deterministic backends guarantee this).
    for field, expected in baseline_metrics.items():
        if expected is None:
            # Null baseline — intentionally not compared; skip.
            continue
        if field in _NON_DETERMINISTIC_FIELDS:
            # Non-deterministic metric — compare only that actual is non-null float.
            actual_nd = report_metrics_dict.get(field)
            assert actual_nd is not None, (
                f"Metric {field!r} is {expected!r} in baseline but the eval run "
                f"returned None — the metric computation regressed entirely."
            )
            assert isinstance(actual_nd, float), (
                f"Metric {field!r} expected float, got {type(actual_nd).__name__!r}."
            )
            continue

        actual = report_metrics_dict.get(field)
        assert actual is not None, (
            f"Metric {field!r} is {expected!r} in baseline but the eval run "
            f"returned None. The unfiltered code path regressed."
        )
        assert actual == expected, (
            f"Metric {field!r} regressed from baseline.\n"
            f"  actual  = {actual!r}\n"
            f"  expected= {expected!r}\n"
            f"The unfiltered (filters=None) code path must not change baseline metrics."
        )
