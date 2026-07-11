"""BE-10: Subprocess eval gate for PPR bridge recall and negative control.

Two subprocess tests (S12, S13) that run the PPR eval gate tests from
``tests/eval/test_ppr_eval_gate.py`` in a fresh child process and assert
that exactly 1 test passes (not skipped, not failed) per invocation.

These tests are serialised via ``xdist_group("benchmark")`` to avoid
running concurrently with other subprocess-heavy tests (memory / CPU
contention).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("benchmark")]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PPR_EVAL_MODULE = "tests/eval/test_ppr_eval_gate.py"


def _run_ppr_gate(k_expr: str, *, timeout: int = 300) -> None:
    """Run a single PPR eval gate test in a child process and assert it passes.

    Asserts:
    - No xdist workers were spawned in the child (OOM guard).
    - The child process exited with code 0.
    - Exactly 1 test passed and no tests were skipped.
    """
    # Recursion guard: if this test is already running inside a subprocess e2e
    # run, skip to prevent infinite subprocess nesting.
    if os.environ.get("_ARCHON_E2E_SUBPROCESS"):
        pytest.skip("Running inside a subprocess e2e run — skipping to prevent recursion")

    child_env = {**os.environ, "_ARCHON_E2E_SUBPROCESS": "1", "PYTEST_ADDOPTS": ""}
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                _PPR_EVAL_MODULE,
                "-m",
                "not live_benchmark",
                "-k",
                k_expr,
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
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"Subprocess pytest run timed out after {timeout}s.\n"
            f"Command: uv run pytest {_PPR_EVAL_MODULE} -k '{k_expr}'"
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
        f"pytest {_PPR_EVAL_MODULE} -k '{k_expr}' failed with exit code {result.returncode}.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    # Confirm the target gate ran and passed (not silently skipped).
    # Use word-boundary match to avoid " 11 passed" or "21 passed" false positives.
    assert re.search(r"\b1 passed\b", combined_output), (
        f"Expected 1 test to pass but the summary does not show '1 passed'.\n"
        f"This may mean the test was skipped (networkx not installed?) or failed.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert " skipped" not in combined_output, (
        f"Expected no skipped tests but the summary contains ' skipped'.\n"
        f"This may mean the test was skipped (networkx not installed?).\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_e2h_pprBridgeRecall_subprocessGate() -> None:
    """Subprocess e2e: run the PPR bridge recall gate in a fresh process (S12).

    Runs:
        uv run pytest tests/eval/test_ppr_eval_gate.py \\
            -k 'test_pprEvalGate_nonVacuous_pprOutperformsNoGraph'
            -m 'not live_benchmark' -p no:xdist -o addopts= --no-cov \\
            --thresholds-path tests/eval/thresholds.toml

    as a blocking subprocess (timeout=300s) from the project root. Asserts
    that exactly 1 test passed (not skipped, not failed): specifically
    ``test_pprEvalGate_nonVacuous_pprOutperformsNoGraph``.

    Serialised via ``xdist_group("benchmark")`` to avoid running concurrently
    with other subprocess-heavy tests (memory / CPU contention).
    """
    _run_ppr_gate("test_pprEvalGate_nonVacuous_pprOutperformsNoGraph")


def test_e2h_pprNegativeControl_subprocessGate() -> None:
    """Subprocess e2e: run the PPR negative control gate in a fresh process (S13).

    Runs:
        uv run pytest tests/eval/test_ppr_eval_gate.py \\
            -k 'test_pprNegativeControlGate_nonVacuous_independentFromNaiveBucket'
            -m 'not live_benchmark' -p no:xdist -o addopts= --no-cov \\
            --thresholds-path tests/eval/thresholds.toml

    as a blocking subprocess (timeout=300s) from the project root. Asserts
    that exactly 1 test passed (not skipped, not failed): specifically
    ``test_pprNegativeControlGate_nonVacuous_independentFromNaiveBucket``.

    This test is INDEPENDENT of the existing naive-mode
    ``graph_negative_control_recall_at_5`` bucket — it targets only the PPR
    structural-independence assertion (``ppr_entities_matched`` set vs None),
    NOT the naive recall metric from ``test_e2e_graph_eval_gate_v2.py``.

    Serialised via ``xdist_group("benchmark")`` to avoid running concurrently
    with other subprocess-heavy tests (memory / CPU contention).
    """
    _run_ppr_gate("test_pprNegativeControlGate_nonVacuous_independentFromNaiveBucket")
