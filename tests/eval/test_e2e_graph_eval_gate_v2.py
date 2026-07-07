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

import os
import subprocess
from pathlib import Path

import pytest

from archon_search.eval.runner import assert_thresholds, run_eval_suite

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
    # synonym_bridge_recall_at_5 requires lancedb_root (synonym edges visible to expander);
    # skip it here — enforced exclusively by test_eval_gate_synonym_bridge_recall_at_5.
    assert_thresholds(report, skip_fields=frozenset({"synonym_bridge_recall_at_5"}))

    assert report.thresholds is not None
    floor = report.thresholds.quality_floors.graph_naive_recall_at_5
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
async def test_eval_gate_graph_local_recall_at_5(
    thresholds_path: Path,
    build_communities_for_eval: tuple,
    eval_tmp_lancedb_root: Path,
) -> None:
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
        lancedb_root=eval_tmp_lancedb_root,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection.
    assert_thresholds(report)

    assert report.thresholds is not None
    floor = report.thresholds.quality_floors.graph_local_recall_at_5
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
async def test_eval_gate_graph_global_recall_at_5(
    thresholds_path: Path,
    build_communities_for_eval: tuple,
    eval_tmp_lancedb_root: Path,
) -> None:
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
        lancedb_root=eval_tmp_lancedb_root,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection.
    assert_thresholds(report)

    assert report.thresholds is not None
    floor = report.thresholds.quality_floors.graph_global_recall_at_5
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
    # synonym_bridge_recall_at_5 requires lancedb_root (synonym edges visible to expander);
    # skip it here — enforced exclusively by test_eval_gate_synonym_bridge_recall_at_5.
    assert_thresholds(report, skip_fields=frozenset({"synonym_bridge_recall_at_5"}))

    assert report.thresholds is not None
    floor = report.thresholds.quality_floors.graph_negative_control_recall_at_5
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


@pytest.mark.eval
async def test_eval_gate_hotpotqa_negative_control_unchanged(
    thresholds_path: Path,
    build_communities_for_eval: tuple,
    eval_tmp_lancedb_root: Path,
) -> None:
    """Gated: graph_negative_control_recall_at_5 does not regress when synonym edges are active (BE-8 / S9).

    Runs the eval suite with ``lancedb_root`` wired (synonym edges and real graph
    expander active) and asserts that the HotpotQA distractor recall floor still
    holds.  This confirms that adding synonym expansion to the pipeline does not
    hurt recall on simple single-hop queries (negative control).

    The HotpotQA distractor corpus contains no synonym pairs, so graph expansion
    should neither help nor hurt.  Any drop below the floor signals a regression in
    the baseline retrieval path caused by synonym-expansion interference.
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
        lancedb_root=eval_tmp_lancedb_root,
    )
    # Enforce the full production gate contract first.
    assert_thresholds(report)

    assert report.thresholds is not None
    floor = report.thresholds.quality_floors.graph_negative_control_recall_at_5
    actual = report.metrics.graph_negative_control_recall_at_5

    assert floor is not None, (
        "graph_negative_control_recall_at_5 floor is not set in thresholds.toml — "
        "add [quality_floors] graph_negative_control_recall_at_5 = <value>"
    )
    assert actual is not None, (
        "graph_negative_control_recall_at_5 metric is None — check that HotpotQA "
        "naive-mode distractor queries are in queries.jsonl and labels.jsonl"
    )
    assert actual >= floor, (
        f"graph_negative_control_recall_at_5={actual:.4f} < floor={floor:.4f} "
        f"(BE-8 / S9 eval gate failed — synonym-active run regressed on HotpotQA)"
    )


@pytest.mark.eval
async def test_eval_gate_synonym_bridge_recall_at_5(
    thresholds_path: Path,
    build_communities_for_eval: tuple,
    eval_tmp_lancedb_root: Path,
) -> None:
    """Gated: synonym_bridge_recall_at_5 meets the floor configured in thresholds.toml (BE-8 / S8).

    Measures naive-mode recall on the synonym-bridge collection.  The synonym-bridge
    corpus contains synonym-pair documents (e.g. "Kubernetes"/"K8s", "machine learning"/"ML").
    Queries using one term should also retrieve documents that use the synonymous term
    when synonym edges (relationship_type="synonym_of") are present in the graph.

    Uses ``RealGraphExpander`` wired to the shared eval LanceDB root so that any synonym
    edges written by the ``build_communities_for_eval`` fixture are visible.  The
    ``lancedb_root`` parameter ensures the pipeline shares the same store as the fixture.

    Non-vacuity: the gate is discriminating because of three combined factors:
    1. Measured no-expansion baseline of 0.5, recorded in baseline.json from a run
       with lancedb_root=None (StubGraphExpander path, no synonym knowledge).
       Cross-term retrieval (K8s query → Kubernetes doc) does NOT occur without
       synonym expansion because the SHA-256 embedder treats "k8s" and "kubernetes"
       as unrelated tokens; only the synonym edge bridge enables cross-term retrieval.
    2. The deterministic SHA-256 backend ensures the no-expansion baseline is stable
       at 0.5 across runs — it does not drift due to model weight changes.
    3. floor=0.75 (set in thresholds.toml) exceeds the 0.5 no-expansion baseline,
       so ``actual >= floor`` can only pass when synonym expansion actually fires.
    With synonym_of edges + RealGraphExpander (this test): the expander bridges
    K8s↔Kubernetes and ML↔machine learning, boosting recall@5 to 1.0.
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
        lancedb_root=eval_tmp_lancedb_root,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection.
    assert_thresholds(report)

    assert report.thresholds is not None
    floor = report.thresholds.quality_floors.synonym_bridge_recall_at_5
    actual = report.metrics.synonym_bridge_recall_at_5

    assert floor is not None, (
        "synonym_bridge_recall_at_5 floor is not set in thresholds.toml — "
        "add [quality_floors] synonym_bridge_recall_at_5 = <value>"
    )
    assert actual is not None, (
        "synonym_bridge_recall_at_5 metric is None — check that synonym-bridge naive-mode "
        "queries are in queries.jsonl and their labels are in labels.jsonl"
    )
    # Config-lint guard: prevents floor erosion below the no-expansion baseline.
    # This assertion compares two constants (floor from thresholds.toml vs. the
    # measured 0.5 baseline from baseline.json); it is NOT the primary non-vacuity
    # proof.  The true non-vacuity comes from ``actual >= floor`` (line below)
    # combined with the measured 0.5 baseline in baseline.json.
    # If the corpus is recalibrated, update _NO_EXPANSION_BASELINE to match
    # baseline.json (synonym_bridge_recall_at_5 from the stub-expander run).
    _NO_EXPANSION_BASELINE = 0.5
    assert floor > _NO_EXPANSION_BASELINE, (
        f"synonym_bridge_recall_at_5 floor={floor:.4f} is not above the no-expansion "
        f"baseline={_NO_EXPANSION_BASELINE:.4f} — the gate is vacuous; raise the floor "
        "above the stub-expander recall so the gate only passes when expansion fires"
    )
    assert actual >= floor, (
        f"synonym_bridge_recall_at_5={actual:.4f} < floor={floor:.4f} "
        f"(BE-8 / S8 eval gate failed — synonym expansion may have been disabled or "
        f"synonym_of edges may be absent; without expansion recall@5 ≈ {_NO_EXPANSION_BASELINE:.2f})"
    )


# ---------------------------------------------------------------------------
# T-4: subprocess e2e — run all eval gates in this file and assert they pass
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_THIS_MODULE = "tests/eval/test_e2e_graph_eval_gate_v2.py"


@pytest.mark.integration
@pytest.mark.xdist_group("benchmark")
def test_e2e_eval_gate_synonym_bridge_and_negative_control() -> None:
    """Subprocess e2e: run all eval gates in this file and assert both new tests pass (T-4).

    Runs:
        uv run pytest tests/eval/test_e2e_graph_eval_gate_v2.py \\
            -k '<K_EXPR>' -p no:xdist -o addopts= --no-cov \\
            --thresholds-path tests/eval/thresholds.toml

    as a blocking subprocess (timeout=300s) from the project root.  Asserts
    that exactly 2 tests passed (not skipped, not failed): specifically
    ``test_eval_gate_synonym_bridge_recall_at_5`` (S8) and
    ``test_eval_gate_hotpotqa_negative_control_unchanged`` (S9).

    If the subprocess fails or times out, the captured output is included in
    the assertion error so failures are immediately diagnosable without
    re-running manually.

    Serialised via ``xdist_group("benchmark")`` to avoid running concurrently
    with other subprocess-heavy tests (memory / CPU contention).
    """
    # Recursion guard: if this test is already running inside a subprocess e2e
    # run, skip to prevent infinite subprocess nesting.
    if os.environ.get("_ARCHON_E2E_SUBPROCESS"):
        pytest.skip("Running inside a subprocess e2e run — skipping to prevent recursion")

    # Use -k to select only the two target gates.  This avoids a recursive loop:
    # if the subprocess ran the full file it would include this test, which would
    # spawn another subprocess, and so on indefinitely.
    _K_EXPR = (
        "test_eval_gate_synonym_bridge_recall_at_5"
        " or test_eval_gate_hotpotqa_negative_control_unchanged"
    )
    child_env = {**os.environ, "_ARCHON_E2E_SUBPROCESS": "1", "PYTEST_ADDOPTS": ""}
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                _THIS_MODULE,
                "-m",
                "not live_benchmark",
                "-k",
                _K_EXPR,
                "-p",
                "no:xdist",
                "-o",
                "addopts=",
                "--no-cov",
                "--thresholds-path",
                "tests/eval/thresholds.toml",
            ],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"Subprocess pytest run timed out after 300s.\n"
            f"Command: uv run pytest {_THIS_MODULE} -k '{_K_EXPR}'"
        )
    combined_output = result.stdout + result.stderr
    # Guard: verify the child did NOT spawn xdist workers (which would stack on the parent's
    # worker pool and risk OOM — see CLAUDE.md and learnings.md [2026-07-05]).
    assert "[gw" not in combined_output, (
        "Child subprocess appears to have spawned xdist workers despite '-p no:xdist'. "
        f"This is a critical OOM risk — check addopts override.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"pytest {_THIS_MODULE} -k '{_K_EXPR}' failed with exit code {result.returncode}.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    # Confirm the two target gates ran and passed (not silently skipped).
    assert "2 passed" in combined_output, (
        f"Expected 2 tests to pass but the summary does not show '2 passed'.\n"
        f"This may mean the tests were skipped (leidenalg not installed?) or failed.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

