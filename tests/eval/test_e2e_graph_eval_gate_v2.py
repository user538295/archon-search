"""BE-11 + T-2, T-3, T-4: Gated eval gates for real graph community recall (E2e).

All tests in this module require ``leidenalg``/``igraph`` (installed via
``archon-search[graph]``).  The module-level ``pytest.importorskip`` skips the
entire file gracefully when those extras are absent (S7).

Gated tests enforce real graph recall quality on frozen multi-hop datasets:
- T-4: graph_naive_recall_at_5 on MuSiQue (naive-mode multi-hop queries)
- T-2: graph_local_recall_at_5 on 2WikiMultiHopQA (real Leiden communities, local mode)
- T-2: graph_global_recall_at_5 on 2WikiMultiHopQA (real Leiden communities, global mode)
- T-3: graph_negative_control_recall_at_5 on HotpotQA (regression guard on simple queries)

Non-leidenalg tests (naive-recall smoke, tuple-membership check) live in
``test_eval_suite.py`` which has no importorskip guard and runs on every CI leg.
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
# Gated eval gates (stubs) — floor calibration placeholder
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_eval_gate_graph_naive_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_naive_recall_at_5 meets the floor configured in thresholds.toml (T-4).

    Measures naive-mode multi-hop recall on MuSiQue-Ans queries.  Verifies that
    graph entity expansion (without community-detection) improves or maintains recall
    on multi-hop questions.
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection.
    assert_thresholds(report)

    thresholds = load_thresholds(thresholds_path)
    floor = thresholds.quality_floors.graph_naive_recall_at_5
    actual = report.metrics.graph_naive_recall_at_5

    assert floor is not None, (
        "graph_naive_recall_at_5 floor is not set in thresholds.toml — "
        "add [quality_floors] graph_naive_recall_at_5 = <value>"
    )
    assert actual is not None, (
        "graph_naive_recall_at_5 metric is None — check that MuSiQue naive-mode "
        "queries are in queries.jsonl and their labels are in labels.jsonl"
    )
    assert actual >= floor, (
        f"graph_naive_recall_at_5={actual:.4f} < floor={floor:.4f} "
        f"(T-4 eval gate failed)"
    )


@pytest.mark.eval
async def test_eval_gate_graph_local_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_local_recall_at_5 meets the floor configured in thresholds.toml (T-2).

    Measures local-mode community retrieval on 2WikiMultiHopQA bridge+comparison
    queries. Uses real Leiden-detected communities (seed=42) built from the eval
    corpus. Verifies that community-local representatives improve recall on
    multi-hop bridge/comparison questions.
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection.
    assert_thresholds(report)

    thresholds = load_thresholds(thresholds_path)
    floor = thresholds.quality_floors.graph_local_recall_at_5
    actual = report.metrics.graph_local_recall_at_5

    assert floor is not None, (
        "graph_local_recall_at_5 floor is not set in thresholds.toml — "
        "add [quality_floors] graph_local_recall_at_5 = <value>"
    )
    assert actual is not None, (
        "graph_local_recall_at_5 metric is None — check that 2WikiMultiHopQA local-mode "
        "queries are in queries.jsonl and their labels are in labels.jsonl"
    )
    assert actual >= floor, (
        f"graph_local_recall_at_5={actual:.4f} < floor={floor:.4f} "
        f"(T-2 eval gate failed)"
    )


@pytest.mark.eval
async def test_eval_gate_graph_global_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_global_recall_at_5 meets the floor configured in thresholds.toml (T-2).

    Measures global-mode community aggregation on 2WikiMultiHopQA bridge+comparison
    queries. Uses real Leiden-detected communities (seed=42) built from the eval
    corpus. Verifies that global aggregation of top-N communities improves recall
    on multi-hop bridge/comparison questions.
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection.
    assert_thresholds(report)

    thresholds = load_thresholds(thresholds_path)
    floor = thresholds.quality_floors.graph_global_recall_at_5
    actual = report.metrics.graph_global_recall_at_5

    assert floor is not None, (
        "graph_global_recall_at_5 floor is not set in thresholds.toml — "
        "add [quality_floors] graph_global_recall_at_5 = <value>"
    )
    assert actual is not None, (
        "graph_global_recall_at_5 metric is None — check that 2WikiMultiHopQA global-mode "
        "queries are in queries.jsonl and their labels are in labels.jsonl"
    )
    assert actual >= floor, (
        f"graph_global_recall_at_5={actual:.4f} < floor={floor:.4f} "
        f"(T-2 eval gate failed)"
    )


@pytest.mark.eval
async def test_eval_gate_graph_negative_control_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_negative_control_recall_at_5 meets the floor configured in thresholds.toml (T-3).

    Measures naive-mode on HotpotQA distractor questions (negative control).  This is a
    regression guard: if naive-mode graph expansion regresses on simple single-hop
    distractors, recall drops and this gate fails. Unlike multi-hop positive gates, this
    gate protects against harm on non-adversarial queries.

    Note: this metric has ~0.40-0.43 variance; the floor is set conservatively.
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection.
    assert_thresholds(report)

    thresholds = load_thresholds(thresholds_path)
    floor = thresholds.quality_floors.graph_negative_control_recall_at_5
    actual = report.metrics.graph_negative_control_recall_at_5

    assert floor is not None, (
        "graph_negative_control_recall_at_5 floor is not set in thresholds.toml — "
        "add [quality_floors] graph_negative_control_recall_at_5 = <value>"
    )
    assert actual is not None, (
        "graph_negative_control_recall_at_5 metric is None — check that HotpotQA naive-mode "
        "distractor queries are in queries.jsonl and their labels are in labels.jsonl"
    )
    assert actual >= floor, (
        f"graph_negative_control_recall_at_5={actual:.4f} < floor={floor:.4f} "
        f"(T-3 eval gate failed — regression on simple queries)"
    )
