"""BE-11 + T-2, T-3, T-4: Gated eval gates for real graph community recall (E2e).

All tests in this module require ``leidenalg``/``igraph`` (installed via
``archon-search[graph]``).  The module-level ``pytest.importorskip`` skips the
entire file gracefully when those extras are absent (S7).

The stubs use ``pytest.skip('floor not yet calibrated — run BE-12')`` so the
suite stays green between BE-11 and BE-12 merges.  BE-12 replaces the skip
body with real assertions against calibrated floors.

Non-leidenalg tests (naive-recall smoke, tuple-membership check) live in
``test_eval_suite.py`` which has no importorskip guard and runs on every CI leg.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Require leidenalg for all tests in this module; skip the entire module if absent.
pytest.importorskip("leidenalg")


# ---------------------------------------------------------------------------
# Gated eval gates (stubs) — floor calibration placeholder
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_eval_gate_graph_naive_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_naive_recall_at_5 meets the floor configured in thresholds.toml (T-4).

    Stub until BE-12 calibration sets the real floor value.
    """
    pytest.skip('floor not yet calibrated — run BE-12')


@pytest.mark.eval
async def test_eval_gate_graph_local_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_local_recall_at_5 meets the floor configured in thresholds.toml (T-2).

    Stub until BE-12 calibration sets the real floor value.
    """
    pytest.skip('floor not yet calibrated — run BE-12')


@pytest.mark.eval
async def test_eval_gate_graph_global_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_global_recall_at_5 meets the floor configured in thresholds.toml (T-2).

    Stub until BE-12 calibration sets the real floor value.
    """
    pytest.skip('floor not yet calibrated — run BE-12')


@pytest.mark.eval
async def test_eval_gate_graph_negative_control_recall_at_5(thresholds_path: Path) -> None:
    """Gated: graph_negative_control_recall_at_5 meets the floor configured in thresholds.toml (T-3).

    Stub until BE-12 calibration sets the real floor value.  This metric is a
    regression guard: a drop signals graph-mode degradation on HotpotQA distractor
    queries.
    """
    pytest.skip('floor not yet calibrated — run BE-12')
