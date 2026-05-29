"""Pure-logic acceptance tests for the live eval lane (marked `eval`).

These tests do not require model weights and run in the PR eval suite.
"""

import pytest
import tomllib
from pathlib import Path

PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


@pytest.mark.eval
def test_live_eval_marker_excluded_from_default_run() -> None:
    """live_eval must appear in both markers list and addopts exclusion expression."""
    with PYPROJECT_PATH.open("rb") as f:
        config = tomllib.load(f)

    pytest_cfg = config["tool"]["pytest"]["ini_options"]
    markers: list[str] = pytest_cfg["markers"]
    addopts: str = pytest_cfg["addopts"]

    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "live_eval" in marker_names, "live_eval marker must be registered in pyproject.toml [markers]"
    assert "not live_eval" in addopts, "live_eval must be negated in the addopts exclusion expression"
