"""Tests for — pytest marker wiring & eval conftest.

These tests inspect ``packages/archon-search/pyproject.toml`` and the
``tests/eval/`` directory to assert that the eval pytest slice is correctly
wired (strict markers, default exclusion, deterministic backends, etc.).

They intentionally use file-reading rather than running pytest sub-processes so
they execute fast and remain hermetic.
"""
from __future__ import annotations

import ast
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


def test_eval_pytest_marker_included_in_default_run() -> None:
    addopts = _addopts_str()
    assert "not eval" not in addopts, "eval marker must not be excluded from the default run"
    # live_benchmark IS excluded from addopts by design (module-level sys.modules mutation
    # requires process isolation). Check that only live_benchmark is excluded, not live.
    # We match on word-boundary equivalents: "not live " or "not live\"" excludes the live
    # marker, but "not live_benchmark" does not exclude the live marker.
    import re
    assert not re.search(r'not live(?!_)', addopts), (
        "live marker must not be excluded from the default run "
        "(only live_benchmark is excluded by design)"
    )


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
# Marker discipline
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


# ---------------------------------------------------------------------------
# CI guard: no integration-only test may consume thresholds_path
# ---------------------------------------------------------------------------

_FIXTURE_NAME = "thresholds_path"


def _get_marker_names(decorators: list[ast.expr]) -> set[str]:
    names: set[str] = set()
    for dec in decorators:
        if (
            isinstance(dec, ast.Attribute)
            and isinstance(dec.value, ast.Attribute)
            and dec.value.attr == "mark"
            and isinstance(dec.value.value, ast.Name)
            and dec.value.value.id == "pytest"
        ):
            names.add(dec.attr)
        elif (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and isinstance(dec.func.value, ast.Attribute)
            and dec.func.value.attr == "mark"
            and isinstance(dec.func.value.value, ast.Name)
            and dec.func.value.value.id == "pytest"
        ):
            names.add(dec.func.attr)
    return names


def test_no_integration_only_thresholds_path_test() -> None:
    """Any test using `thresholds_path` must not be integration-only (needs eval too).

    The CI integration step runs without --thresholds-path; the thresholds_path
    fixture calls pytest.fail() in CI when that flag is absent. Tests gated on
    thresholds_path belong in the eval step, which supplies the flag.
    """
    violations: list[str] = []

    for path in sorted(EVAL_DIR.rglob("*.py")):
        if path.name.startswith("conftest"):
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            param_names = {arg.arg for arg in node.args.args}
            if _FIXTURE_NAME not in param_names:
                continue
            markers = _get_marker_names(node.decorator_list)
            if "integration" in markers and "eval" not in markers:
                rel = path.relative_to(PACKAGE_ROOT)
                violations.append(
                    f"  {rel}::{node.name} -- @pytest.mark.integration without"
                    " @pytest.mark.eval while consuming `thresholds_path`"
                )

    assert not violations, (
        "The following tests use `thresholds_path` and carry @pytest.mark.integration"
        " without @pytest.mark.eval.\n"
        "The CI integration step runs without --thresholds-path → pytest.fail() in CI.\n"
        "Fix: add @pytest.mark.eval or remove @pytest.mark.integration.\n\n"
        + "\n".join(violations)
    )
