"""Tests for FEAT-039 Task 4.1 — pytest marker wiring & eval conftest.

These tests inspect ``packages/archon-search/pyproject.toml`` and the
``tests/eval/`` directory to assert that the eval pytest slice is correctly
wired (strict markers, default exclusion, deterministic backends, etc.).

They intentionally use file-reading rather than running pytest sub-processes so
they execute fast and remain hermetic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# tomllib is stdlib >= 3.11
import tomllib


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PACKAGE_ROOT / "pyproject.toml"
EVAL_DIR = PACKAGE_ROOT / "tests" / "eval"


def _load_pytest_ini_options() -> dict:
    data = tomllib.loads(PYPROJECT.read_text())
    return data["tool"]["pytest"]["ini_options"]


def _addopts_str() -> str:
    val = _load_pytest_ini_options()["addopts"]
    if isinstance(val, list):
        return " ".join(val)
    return val


# ---------------------------------------------------------------------------
# pyproject.toml — markers & addopts
# ---------------------------------------------------------------------------


def test_eval_pytest_marker_excluded_from_default_run() -> None:
    addopts = _addopts_str()
    assert "not eval" in addopts, addopts
    assert "not live" in addopts, addopts


def test_package_pytest_config_uses_strict_markers_and_config() -> None:
    addopts = _addopts_str()
    assert "--strict-markers" in addopts
    assert "--strict-config" in addopts


def test_package_pytest_config_targets_archon_search_coverage() -> None:
    addopts = _addopts_str()
    assert "--cov=archon_search" in addopts
    assert "--cov=archon " not in addopts and not addopts.endswith("--cov=archon")


def test_package_pytest_config_sets_or_documents_coverage_fail_under() -> None:
    addopts = _addopts_str()
    raw = PYPROJECT.read_text()
    assert ("--cov-fail-under=85" in addopts) or ("[tool.eval]" in raw)


def test_split_coverage_gate_applies_fail_under_only_after_combine() -> None:
    raw = PYPROJECT.read_text().lower()
    assert "combine" in raw or "combined" in raw, (
        "pyproject.toml must document (comment or [tool.eval] table) that "
        "split-runs combine coverage before applying --cov-fail-under"
    )


def test_local_metric_only_eval_command_is_distinct_from_ci_coverage_gate() -> None:
    addopts = _addopts_str()
    assert "--no-cov" not in addopts, (
        "Package addopts must not pre-include --no-cov; it is a local-only override"
    )
    raw = PYPROJECT.read_text().lower()
    assert "--no-cov" in raw, (
        "pyproject.toml should document --no-cov as a local-only override (in a comment)"
    )


def test_eval_and_live_markers_registered() -> None:
    markers = _load_pytest_ini_options()["markers"]
    joined = "\n".join(markers)
    assert "eval:" in joined
    assert "live:" in joined


# ---------------------------------------------------------------------------
# tests/eval/conftest.py — fixtures
# ---------------------------------------------------------------------------


def test_eval_conftest_exists() -> None:
    assert (EVAL_DIR / "conftest.py").is_file()


def test_eval_conftest_uses_deterministic_eval_backends() -> None:
    text = (EVAL_DIR / "conftest.py").read_text()
    # Autouse backend activation fixture references the deterministic backends.
    assert "archon_search.eval.backends" in text
    assert "autouse" in text
    assert "EvalEmbedderBackend" in text or "EvalRerankerBackend" in text


def test_eval_conftest_registers_thresholds_path_option() -> None:
    text = (EVAL_DIR / "conftest.py").read_text()
    assert "--thresholds-path" in text
    # Must distinguish CI vs local fallback
    assert 'pytest.fail' in text
    assert 'pytest.skip' in text
    assert '"CI"' in text or "'CI'" in text


def test_eval_conftest_provides_corpus_and_lancedb_fixtures() -> None:
    text = (EVAL_DIR / "conftest.py").read_text()
    assert "eval_corpus" in text
    assert "eval_tmp_lancedb_root" in text
    # load_eval_corpus is the real loader symbol
    assert "load_eval_corpus" in text


# ---------------------------------------------------------------------------
# Marker discipline — Task 4.1 says no full-corpus markers added here
# ---------------------------------------------------------------------------


def test_eval_marker_only_marks_full_corpus_tests() -> None:
    """No metric/fixture unit tests should be marked with @pytest.mark.eval."""
    for name in ("test_metrics.py", "test_fixtures.py", "test_backends.py", "test_types.py"):
        path = EVAL_DIR / name
        if not path.is_file():
            continue
        text = path.read_text()
        assert "pytest.mark.eval" not in text, (
            f"{name} contains @pytest.mark.eval; only full-corpus tests should use it"
        )


def test_package_default_suite_covers_unmarked_eval_units() -> None:
    for name in ("test_metrics.py", "test_fixtures.py"):
        path = EVAL_DIR / name
        if not path.is_file():
            continue
        text = path.read_text()
        assert "pytest.mark.eval" not in text
