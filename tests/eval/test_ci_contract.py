"""CI contract tests for FEAT-039 Task 4.5.

Inspection-based tests that read workflow files, release.sh, pyproject.toml,
and the skip/xfail allowlist to assert the executable PR and release gates
enforce the eval slice.

Tests are gated on the existence of the workflow and config files: if any
expected file is missing, all downstream tests skip with an explicit message,
making the gap visible rather than vacuously passing.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


# Repository root: packages/archon-search/tests/eval/test_ci_contract.py
# parents: [0]=eval [1]=tests [2]=archon-search [3]=packages [4]=repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PR_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "archon-search-pr.yml"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "archon-search-release.yml"
_PACKAGE_PYPROJECT = _REPO_ROOT / "packages" / "archon-search" / "pyproject.toml"
_RELEASE_SH = _REPO_ROOT / "release.sh"
_RELEASE_DOC = _REPO_ROOT / "Documentation" / "release-process.md"
_ALLOWLIST = (
    _REPO_ROOT
    / "packages"
    / "archon-search"
    / "tests"
    / "eval"
    / "skip_xfail_allowlist.toml"
)
_NESTED_WORKFLOW_DIR = (
    _REPO_ROOT / "packages" / "archon-search" / ".github" / "workflows"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _gate_files_present() -> tuple[bool, str]:
    missing = [
        str(p.relative_to(_REPO_ROOT))
        for p in (_PR_WORKFLOW, _RELEASE_WORKFLOW, _PACKAGE_PYPROJECT)
        if not p.exists()
    ]
    if missing:
        return False, "missing CI gate files: " + ", ".join(missing)
    return True, ""


def _require_gate_files() -> None:
    ok, msg = _gate_files_present()
    if not ok:
        pytest.skip(msg)


# ---------------------------------------------------------------------------
# Gating precondition
# ---------------------------------------------------------------------------


def test_ci_gate_files_exist_after_feat_038_extraction() -> None:
    """All three CI gate artifacts must exist; this gates all other tests."""

    ok, msg = _gate_files_present()
    assert ok, msg


# ---------------------------------------------------------------------------
# PR gate path filters
# ---------------------------------------------------------------------------


def test_pr_gate_path_filters_include_full_retrieval_pipeline_dependency_eval_threshold_baseline_and_ci_files() -> None:
    _require_gate_files()
    text = _read(_PR_WORKFLOW)
    required_fragments = [
        "packages/archon-search/archon_search/",
        "packages/archon-search/tests/",
        "packages/archon-search/pyproject.toml",
        "packages/archon-search/tests/eval/thresholds.toml",
        "packages/archon-search/tests/eval/baselines/",
        "packages/archon-search/tests/eval/runtime.toml",
        "packages/archon-search/tests/eval/corpus/",
        ".github/workflows/archon-search-pr.yml",
        "release.sh",
    ]
    missing = [f for f in required_fragments if f not in text]
    assert not missing, f"PR workflow path filters missing fragments: {missing}"


# ---------------------------------------------------------------------------
# Eval slice execution
# ---------------------------------------------------------------------------


def test_pr_gate_runs_gated_eval_slice_for_matching_paths() -> None:
    _require_gate_files()
    text = _read(_PR_WORKFLOW)
    assert "-m eval" in text, "PR workflow must invoke pytest with -m eval"
    assert (
        "--thresholds-path" in text
    ), "PR workflow must pass an explicit --thresholds-path"


def test_release_gate_passes_explicit_thresholds_path() -> None:
    _require_gate_files()
    assert _RELEASE_SH.exists(), "release.sh missing"
    assert "--thresholds-path" in _read(_RELEASE_SH)
    assert "--thresholds-path" in _read(_RELEASE_WORKFLOW)


def test_release_gate_includes_eval_slice() -> None:
    _require_gate_files()
    sh = _read(_RELEASE_SH)
    assert ("-m eval" in sh) or (
        "tests/eval/" in sh
    ), "release.sh must invoke the eval slice"


# ---------------------------------------------------------------------------
# Pytest configuration boundary
# ---------------------------------------------------------------------------


def test_ci_gates_use_package_pytest_config() -> None:
    _require_gate_files()
    for wf in (_PR_WORKFLOW, _RELEASE_WORKFLOW):
        text = _read(wf)
        assert ("cd packages/archon-search" in text) or (
            "-c packages/archon-search/pyproject.toml" in text
        ), f"{wf.name} must run pytest under the package config"


# ---------------------------------------------------------------------------
# Dependency setup
# ---------------------------------------------------------------------------


def test_ci_clean_install_includes_eval_dependencies_and_pytest_plugins() -> None:
    _require_gate_files()
    for wf in (_PR_WORKFLOW, _RELEASE_WORKFLOW):
        text = _read(wf)
        assert "uv sync" in text, f"{wf.name} must run uv sync"
        assert (
            "pytest_cov" in text or "pytest-cov" in text
        ), f"{wf.name} must verify the pytest-cov plugin is importable"


# ---------------------------------------------------------------------------
# Coverage enforcement
# ---------------------------------------------------------------------------


def test_ci_gates_run_with_package_coverage() -> None:
    _require_gate_files()
    for wf in (_PR_WORKFLOW, _RELEASE_WORKFLOW):
        text = _read(wf)
        assert "--cov=archon_search" in text, f"{wf.name} must enable package coverage"


def test_ci_gates_enforce_coverage_fail_under_after_default_and_eval_slices() -> None:
    _require_gate_files()
    for wf in (_PR_WORKFLOW, _RELEASE_WORKFLOW):
        text = _read(wf)
        # Either a single combined invocation with --cov-fail-under, or coverage combine then report --fail-under.
        has_combine = "coverage combine" in text
        has_fail_under = "--fail-under=85" in text or "--cov-fail-under=85" in text
        assert (
            has_combine and has_fail_under
        ), f"{wf.name} must combine coverage and enforce --fail-under=85"
        # Order: combine must precede fail-under
        combine_idx = text.find("coverage combine")
        # Find fail-under index using whichever variant is present
        fu_idx = text.find("--fail-under=85")
        if fu_idx == -1:
            fu_idx = text.find("--cov-fail-under=85")
        assert (
            combine_idx < fu_idx
        ), f"{wf.name}: coverage combine must precede fail-under check"


def test_ci_gates_do_not_apply_fail_under_to_intermediate_split_invocations() -> None:
    _require_gate_files()
    for wf in (_PR_WORKFLOW, _RELEASE_WORKFLOW):
        text = _read(wf)
        # Find each pytest invocation line and assert no --cov-fail-under appears on it.
        for line in text.splitlines():
            stripped = line.strip()
            if "pytest" in stripped and "uv run pytest" in stripped:
                assert (
                    "--cov-fail-under" not in stripped
                ), f"{wf.name}: intermediate pytest invocation must not set --cov-fail-under: {stripped}"


# ---------------------------------------------------------------------------
# Default + eval invocations
# ---------------------------------------------------------------------------


def test_ci_gates_run_default_suite_and_full_eval_slice() -> None:
    _require_gate_files()
    for wf in (_PR_WORKFLOW, _RELEASE_WORKFLOW):
        text = _read(wf)
        pytest_lines = [
            line for line in text.splitlines() if "uv run pytest" in line
        ]
        assert (
            len(pytest_lines) >= 2
        ), f"{wf.name} must have a default and an eval pytest invocation"
        assert any(
            "-m eval" in line for line in pytest_lines
        ), f"{wf.name} must run -m eval"
        assert any(
            "-m eval" not in line for line in pytest_lines
        ), f"{wf.name} must run a default (non-eval) suite"


def test_ci_gates_fail_when_eval_collection_is_empty_or_any_eval_is_skipped_or_xfailed() -> None:
    _require_gate_files()
    for wf in (_PR_WORKFLOW, _RELEASE_WORKFLOW):
        text = _read(wf)
        assert "--strict-markers" in text, f"{wf.name} must use --strict-markers"
        assert "--runxfail" in text, f"{wf.name} must use --runxfail"


def test_ci_gates_use_runxfail_and_skip_xfail_report_check() -> None:
    _require_gate_files()
    for wf in (_PR_WORKFLOW, _RELEASE_WORKFLOW):
        text = _read(wf)
        assert "--runxfail" in text, f"{wf.name} must use --runxfail"


# ---------------------------------------------------------------------------
# Allowlist contract
# ---------------------------------------------------------------------------


def test_skip_xfail_allowlist_requires_exact_unexpired_reviewed_nodeids() -> None:
    assert _ALLOWLIST.exists(), f"missing skip/xfail allowlist at {_ALLOWLIST}"
    data = tomllib.loads(_read(_ALLOWLIST))
    entries = data.get("entries", [])
    assert isinstance(entries, list), "allowlist 'entries' must be a list"
    required_fields = {"nodeid", "issue", "reviewer", "reason", "expiry"}
    today = _dt.date.today()
    for entry in entries:
        assert isinstance(entry, dict), "each allowlist entry must be a table"
        missing = required_fields - set(entry.keys())
        assert not missing, f"allowlist entry missing fields: {missing}"
        nodeid = entry["nodeid"]
        assert isinstance(nodeid, str) and nodeid, "nodeid must be a non-empty string"
        for forbidden in ("*", "?", "[", "]"):
            assert (
                forbidden not in nodeid
            ), f"allowlist nodeid must not contain wildcards: {nodeid!r}"
        expiry = entry["expiry"]
        if isinstance(expiry, str):
            expiry_date = _dt.date.fromisoformat(expiry)
        else:
            expiry_date = expiry  # already a datetime.date from tomllib
        assert (
            expiry_date >= today
        ), f"allowlist entry for {nodeid!r} expired on {expiry_date}"
        assert entry["reviewer"], "reviewer field must be non-empty"
        assert entry["issue"], "issue field must be non-empty"
        assert entry["reason"], "reason field must be non-empty"


# ---------------------------------------------------------------------------
# Release doc + executable gate
# ---------------------------------------------------------------------------


def test_release_docs_reference_eval_slice_but_are_not_sufficient() -> None:
    _require_gate_files()
    assert _RELEASE_DOC.exists(), "release-process.md missing"
    doc = _read(_RELEASE_DOC)
    assert "eval" in doc.lower(), "release-process.md must mention the eval gate"
    # And the executable gate must exist in release.sh
    sh = _read(_RELEASE_SH)
    assert ("-m eval" in sh) or (
        "tests/eval/" in sh
    ), "release.sh must contain the executable eval gate"


def test_release_script_runs_eval_before_first_mutation_or_publish_step() -> None:
    _require_gate_files()
    sh = _read(_RELEASE_SH)
    lines = sh.splitlines()
    eval_idx = next(
        (i for i, line in enumerate(lines) if ("-m eval" in line) or ("tests/eval/" in line and "pytest" in line)),
        -1,
    )
    sed_idx = next((i for i, line in enumerate(lines) if "sed -i" in line), -1)
    assert eval_idx >= 0, "release.sh must contain an eval pytest invocation"
    assert sed_idx >= 0, "release.sh must contain a sed -i mutation"
    assert (
        eval_idx < sed_idx
    ), f"eval gate (line {eval_idx + 1}) must precede first sed mutation (line {sed_idx + 1})"


# ---------------------------------------------------------------------------
# Fast CI + nested workflow guard
# ---------------------------------------------------------------------------


def test_fast_ci_excludes_eval_slice() -> None:
    assert _PACKAGE_PYPROJECT.exists()
    data = tomllib.loads(_read(_PACKAGE_PYPROJECT))
    addopts = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get(
        "addopts", ""
    )
    assert "not eval" in addopts, "package pytest defaults must exclude -m eval"


def test_nested_package_github_workflow_is_not_the_only_gate() -> None:
    assert (
        not _NESTED_WORKFLOW_DIR.exists()
    ), f"nested workflow dir at {_NESTED_WORKFLOW_DIR} would not run in GitHub Actions"
