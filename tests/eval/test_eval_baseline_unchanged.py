"""Baseline regression guard: filters=None (unfiltered) eval must match baseline.json.

Verifies that the default, unfiltered code path produces metrics identical to
``baselines/baseline.json``. Filters are additive; this test documents and
asserts that passing ``filters=None`` (the default) does not regress the
committed baseline.

Why filters=None is tested implicitly:
    ``run_eval_suite`` calls ``_hybrid_search_with_trace`` without any filter
    argument throughout the pipeline — there is no ``filters`` parameter on
    ``run_eval_suite`` itself.  The entire eval harness therefore always runs
    with ``filters=None`` semantics.  This test verifies that code changes in
    the A2 filter stack have not accidentally altered the unfiltered code path.

Marker: ``eval`` — excluded from the default pytest run.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from archon_search.eval.runner import run_eval_suite


CORPUS_ROOT = Path(__file__).resolve().parent
RUNTIME_CONFIG_PATH = CORPUS_ROOT / "runtime.toml"
BASELINE_JSON = CORPUS_ROOT / "baselines" / "baseline.json"


@pytest.mark.eval
async def test_unfiltered_eval_matches_baseline_metrics() -> None:
    """Unfiltered eval run must produce metrics bit-identical to baseline.json.

    The eval harness uses deterministic backends (SHA-256 embedder, BM25
    reranker) so metric values are fully reproducible — no floating-point
    tolerance is needed or applied.  ``null`` baseline values (e.g. latency)
    are skipped.
    """
    baseline_data = json.loads(BASELINE_JSON.read_text())
    baseline_metrics: dict[str, float | None] = baseline_data["metrics"]

    # Run without thresholds or baseline_path — report-only, no staleness check.
    # filters=None is implicit: run_eval_suite has no filter parameter; the
    # harness always searches without filters (the unfiltered code path).
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    report_metrics_dict = dataclasses.asdict(report.metrics)

    # Assert every non-null field in baseline.json — including reranker_lift.
    # Null baseline values mean "not compared" (e.g. latency_p50_ms and
    # latency_p95_ms are stored as null to opt out of latency assertions).
    # Non-null values must be bit-identical (deterministic backends guarantee this).
    for field, expected in baseline_metrics.items():
        if expected is None:
            # Null baseline — intentionally not compared; skip.
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
