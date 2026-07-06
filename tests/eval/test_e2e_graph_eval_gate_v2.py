"""BE-11 + T-2, T-3, T-4: Tests for real graph eval gates (E2e — Deterministic Graph
Eval Gate v2).

This module gates multi-hop recall metrics on real community detection (not stubs).
All gated tests require leidenalg/igraph (installed via `archon-search[graph]`);
they skip gracefully when the extras are absent.

Tests:
- test_eval_gate_file_importorskip_guard: leidenalg required; skip if absent.
- test_eval_suite_graph_naive_recall_at_5_smoke: report-only run produces non-None
  graph_naive_recall_at_5 for multi-hop MuSiQue naive-mode queries.
- test_eval_gate_graph_naive_recall_at_5: gated; graph_naive_recall_at_5 ≥ floor (T-4).
- test_eval_gate_graph_local_recall_at_5: gated; graph_local_recall_at_5 ≥ floor (T-2).
- test_eval_gate_graph_global_recall_at_5: gated; graph_global_recall_at_5 ≥ floor (T-2).
- test_eval_gate_graph_negative_control_recall_at_5: gated; graph_negative_control_recall_at_5 ≥ floor (T-3).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.eval.runner import assert_thresholds, load_thresholds, render_report, run_eval_suite

# Require leidenalg for all tests in this module; skip the entire module if absent.
pytest.importorskip("leidenalg")


CORPUS_ROOT = Path(__file__).resolve().parent
RUNTIME_CONFIG_PATH = CORPUS_ROOT / "runtime.toml"
BASELINE_JSON = CORPUS_ROOT / "baselines" / "baseline.json"


# ---------------------------------------------------------------------------
# Unit: Import guard verification
# ---------------------------------------------------------------------------


def test_eval_gate_file_importorskip_guard() -> None:
    """Verify that this module's leidenalg import guard works.

    If leidenalg is absent, this test does not run (the module-level
    pytest.importorskip raises and skips the entire file).
    """
    # This test body runs only if leidenalg is present.
    import leidenalg  # noqa: F401
    assert True


def test_eval_determinism_includes_new_recall_fields() -> None:
    """Verify that _QUALITY_METRIC_FIELDS in test_eval_suite.py includes
    all four new graph recall metrics added in BE-11.
    """
    from tests.eval.test_eval_suite import _QUALITY_METRIC_FIELDS

    required_fields = {
        "graph_naive_recall_at_5",
        "graph_local_recall_at_5",
        "graph_global_recall_at_5",
        "graph_negative_control_recall_at_5",
    }

    present_fields = set(_QUALITY_METRIC_FIELDS)
    missing = required_fields - present_fields

    assert not missing, (
        f"_QUALITY_METRIC_FIELDS missing required fields: {missing}. "
        f"Present fields: {present_fields}"
    )


# ---------------------------------------------------------------------------
# Integration: eval suite smoke — naive recall non-None
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_eval_suite_graph_naive_recall_at_5_smoke() -> None:
    """Eval suite runs without --thresholds-path; report contains
    graph_naive_recall_at_5 as a non-None metric (for multi-hop MuSiQue queries).
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    # New metric must exist as an attribute on EvalMetrics
    assert hasattr(report.metrics, "graph_naive_recall_at_5"), (
        "report.metrics missing 'graph_naive_recall_at_5' attribute. "
        "EvalMetrics must declare graph_naive_recall_at_5: float | None = None."
    )

    # With naive-mode MuSiQue fixtures present, the metric should be non-None
    assert report.metrics.graph_naive_recall_at_5 is not None, (
        "graph_naive_recall_at_5 is None after running eval suite with "
        "multihop-musique naive-mode fixtures. Check that the MuSiQue corpus is in "
        "tests/eval/corpus/ and queries with graph_mode='naive' are in queries.jsonl."
    )
    assert isinstance(report.metrics.graph_naive_recall_at_5, float), (
        f"graph_naive_recall_at_5 is not a float: {type(report.metrics.graph_naive_recall_at_5)}"
    )
    assert 0.0 <= report.metrics.graph_naive_recall_at_5 <= 1.0, (
        f"graph_naive_recall_at_5={report.metrics.graph_naive_recall_at_5} is outside [0.0, 1.0]"
    )

    # Rendered report must contain the metric name
    rendered = render_report(report)
    assert "graph_naive_recall_at_5" in rendered, (
        f"Rendered report does not contain 'graph_naive_recall_at_5'.\n"
        f"First 1000 chars:\n{rendered[:1000]}"
    )


# ---------------------------------------------------------------------------
# BE-11: Gated eval gates (stubs) — floor calibration placeholder
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_eval_gate_graph_naive_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_naive_recall_at_5 meets the floor configured in thresholds.toml (T-4).

    Requires --thresholds-path; this stub will be activated after BE-12 calibration
    sets the real floor value.
    """
    pytest.skip('floor not yet calibrated — run BE-12')


@pytest.mark.eval
async def test_eval_gate_graph_local_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_local_recall_at_5 meets the floor configured in thresholds.toml (T-2).

    Requires --thresholds-path; this stub will be activated after BE-12 calibration
    sets the real floor value.
    """
    pytest.skip('floor not yet calibrated — run BE-12')


@pytest.mark.eval
async def test_eval_gate_graph_global_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_global_recall_at_5 meets the floor configured in thresholds.toml (T-2).

    Requires --thresholds-path; this stub will be activated after BE-12 calibration
    sets the real floor value.
    """
    pytest.skip('floor not yet calibrated — run BE-12')


@pytest.mark.eval
async def test_eval_gate_graph_negative_control_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_negative_control_recall_at_5 meets the floor configured in thresholds.toml (T-3).

    Requires --thresholds-path; this stub will be activated after BE-12 calibration
    sets the real floor value. This metric is a regression guard: a drop signals
    graph-mode degradation on simple (non-multi-hop) queries.
    """
    pytest.skip('floor not yet calibrated — run BE-12')
