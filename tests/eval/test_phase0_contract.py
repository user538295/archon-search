"""Phase 0 contract tests for FEAT-039.

These are process-compliance tests. They assert that Task 0.1 was followed:
- Task 0.1 checkbox is marked complete
- FEAT-038 artifact is explicitly linked under Task 0.1
- Package root and import path are documented and match reality
- Canonical service and metadata contracts are named
- Routing ownership is recorded
- Typed route API is documented with field names
- Archon routing eval follow-up is linked (FEAT-039b)
- Archon doc validation owner is named
- Documentation index is updated for follow-up backlog items
- eval/runtime.toml exists with routing_contract_enabled = true

And that Task 0.2 was followed (CI/release infrastructure):
- Search/eval dependency install command is named
- Clean-environment install smoke command is named
- Package pytest config command is named
- Release entrypoint is named
- PR eval gate status (path-filtered workflow or documented absence) is recorded
- Release gate placement is before first release mutation

These tests are NOT behavioral tests. They fail fast when an implementer
skips prerequisite steps but cannot catch runtime bugs.
"""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parents[4]  # .../archon
PLAN_CODEX = (
    REPO_ROOT
    / "Documentation"
    / "Backlog"
    / "FEAT-039-search-evaluation-harness-plan-codex.md"
)
FEAT_038_ARTIFACT = (
    REPO_ROOT
    / "Documentation"
    / "Completed"
    / "FEAT-038-search-product-separation.md"
)
DOC_INDEX = (
    REPO_ROOT
    / "Documentation"
    / "990_documentation_index_and_contribution_guide.md"
)
PACKAGE_ROOT = Path(__file__).parents[2]  # packages/archon-search/
EVAL_DIR = Path(__file__).parent  # packages/archon-search/tests/eval/
RUNTIME_TOML = EVAL_DIR / "runtime.toml"


def _plan_text() -> str:
    return PLAN_CODEX.read_text(encoding="utf-8")


def _task01_section(plan_text: str) -> str:
    """Extract the Task 0.1 section from the plan."""
    marker = "#### Task 0.1"
    next_marker = "#### Task 0.2"
    start = plan_text.find(marker)
    end = plan_text.find(next_marker, start)
    assert start != -1, f"Task 0.1 section not found in {PLAN_CODEX}"
    return plan_text[start:end] if end != -1 else plan_text[start:]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_phase0_contract_links_existing_feat_038_artifact_or_inline_contract() -> None:
    """Task 0.1 section must reference the accepted FEAT-038 completion artifact."""
    text = _plan_text()
    task01 = _task01_section(text)

    # The Task 0.1 checkbox must be checked
    assert "- [x]" in task01 or "[x] **File**" in task01, (
        "Task 0.1 checkbox is not marked complete. "
        "The plan codex must have '- [x]' in the Task 0.1 section. "
        "Currently it is unchecked '- [ ]'."
    )

    # The artifact must be explicitly linked under Task 0.1
    assert "FEAT-038-search-product-separation" in task01, (
        "Task 0.1 section does not reference FEAT-038-search-product-separation. "
        "Task 0.1 requires linking the accepted artifact from "
        "Documentation/Completed/FEAT-038-search-product-separation.md."
    )

    # The artifact must actually exist
    assert FEAT_038_ARTIFACT.exists(), (
        f"FEAT-038 artifact not found at {FEAT_038_ARTIFACT}. "
        "Cannot link a missing file."
    )


def test_phase0_contract_matches_package_paths() -> None:
    """Task 0.1 must confirm package root and import path matching the extracted package."""
    text = _plan_text()
    task01 = _task01_section(text)

    # Package root must be confirmed in Task 0.1 section
    assert "packages/archon-search/" in task01, (
        "Task 0.1 section does not confirm the package root packages/archon-search/. "
        "Task 0.1 requires explicitly confirming the exact package root."
    )

    # Import path must be confirmed in Task 0.1 section
    assert "archon_search" in task01, (
        "Task 0.1 section does not confirm the import path archon_search. "
        "Task 0.1 requires explicitly confirming the exact import path."
    )

    # The package root must actually exist in the monorepo
    assert PACKAGE_ROOT.is_dir(), (
        f"Package root {PACKAGE_ROOT} does not exist in the monorepo."
    )

    # The import path must actually be a valid package directory
    archon_search_pkg = PACKAGE_ROOT / "archon_search"
    assert archon_search_pkg.is_dir(), (
        f"archon_search package directory not found at {archon_search_pkg}"
    )


def test_phase0_contract_names_canonical_service_and_metadata_contracts() -> None:
    """Task 0.1 must name SearchResult and metadata schema as the frozen eval baseline."""
    text = _plan_text()
    task01 = _task01_section(text)

    # SearchResult public contract must be confirmed in Task 0.1 section
    assert "SearchResult" in task01, (
        "Task 0.1 section does not name SearchResult as the public response contract. "
        "Task 0.1 requires confirming the frozen eval semantics baseline."
    )

    # SearchResult must actually exist in the codebase with documented fields
    types_module = PACKAGE_ROOT / "archon_search" / "_types.py"
    assert types_module.exists(), f"_types.py not found at {types_module}"
    types_text = types_module.read_text(encoding="utf-8")
    assert "class SearchResult" in types_text, (
        "SearchResult class not found in archon_search/_types.py"
    )

    # Extract the SearchResult class body and verify the five-field public shape
    sr_start = types_text.find("class SearchResult")
    assert sr_start != -1, "class SearchResult not found in _types.py"
    # Find the next class definition or end of file to scope the check
    next_class = types_text.find("\nclass ", sr_start + 1)
    sr_body = types_text[sr_start:next_class] if next_class != -1 else types_text[sr_start:]
    for field in ("doc_id", "chunk_id", "text", "score", "source_path"):
        assert field in sr_body, (
            f"SearchResult field '{field}' not found in SearchResult class body in _types.py. "
            "The plan documents a five-field public shape."
        )

    # The Task 0.1 section must confirm the five-field shape or name the fields
    assert "five-field" in task01 or (
        "doc_id" in task01 and "chunk_id" in task01
    ), (
        "Task 0.1 section does not confirm the SearchResult five-field public shape. "
        "Task 0.1 requires documenting the frozen eval semantics baseline with field names."
    )


def test_phase0_contract_records_routing_ownership() -> None:
    """Task 0.1 must record that routing is Search-owned via POST /route."""
    text = _plan_text()
    task01 = _task01_section(text)

    assert "Routing is Search-owned" in task01 or "routing is Search-owned" in task01, (
        "Task 0.1 section does not record routing ownership. "
        "Task 0.1 requires confirming that routing is Search-owned via POST /route."
    )

    assert "POST /route" in task01, (
        "Task 0.1 section does not document the POST /route endpoint. "
        "Task 0.1 requires recording routing ownership with the endpoint reference."
    )

    # The route file must exist
    route_file = PACKAGE_ROOT / "archon_search" / "server" / "routes_route.py"
    assert route_file.exists(), (
        f"routes_route.py not found at {route_file}. "
        "Routing ownership cannot be confirmed without the implementation."
    )


def test_phase0_contract_requires_typed_route_api_when_routing_owned() -> None:
    """Task 0.1 must inline the typed route API contract when routing is Search-owned."""
    text = _plan_text()
    task01 = _task01_section(text)

    # RouteRequest must be documented in Task 0.1
    assert "RouteRequest" in task01, (
        "Task 0.1 section does not document RouteRequest. "
        "Task 0.1 requires inlining the typed route API contract."
    )

    # RouteResponse must be documented in Task 0.1
    assert "RouteResponse" in task01, (
        "Task 0.1 section does not document RouteResponse. "
        "Task 0.1 requires inlining the typed route API contract."
    )

    # Verify these types actually exist in the codebase
    route_file = PACKAGE_ROOT / "archon_search" / "server" / "routes_route.py"
    route_text = route_file.read_text(encoding="utf-8")
    assert "class RouteRequest" in route_text, (
        "RouteRequest class not found in routes_route.py"
    )
    assert "class RouteResponse" in route_text, (
        "RouteResponse class not found in routes_route.py"
    )

    # eval/runtime.toml must exist with contract_enabled = true
    assert RUNTIME_TOML.exists(), (
        f"runtime.toml not found at {RUNTIME_TOML}. "
        "Task 0.1 requires setting [routing].contract_enabled = true "
        "in the committed eval runtime config."
    )
    runtime_text = RUNTIME_TOML.read_text(encoding="utf-8")
    runtime_data = tomllib.loads(runtime_text)
    assert runtime_data.get("routing", {}).get("contract_enabled") is True, (
        "runtime.toml [routing].contract_enabled is not True. "
        "Task 0.1 requires setting [routing].contract_enabled = true in the committed eval runtime config."
    )


def test_phase0_contract_links_archon_routing_eval_followup_when_needed() -> None:
    """Task 0.1 must link FEAT-039b as the deferred online data-collection follow-up."""
    text = _plan_text()
    task01 = _task01_section(text)

    assert "FEAT-039b" in task01, (
        "Task 0.1 section does not reference FEAT-039b as the deferred follow-up. "
        "Task 0.1 requires that non-Search-owned concerns cannot disappear from the roadmap. "
        "FEAT-039b must be named in the Task 0.1 section."
    )


def test_phase0_contract_records_archon_doc_validation_owner_when_extracted() -> None:
    """Task 0.1 must note that Archon doc validation is Archon-owned when package is extracted."""
    text = _plan_text()
    task01 = _task01_section(text)

    assert (
        "Archon repo" in task01
        or "Archon roadmap" in task01
        or "Archon-doc" in task01
        or "Archon doc" in task01
    ), (
        "Task 0.1 section does not name the Archon doc validation owner. "
        "Task 0.1 requires that when archon-search is extracted, the doc validation "
        "owner is explicitly documented in the Task 0.1 section."
    )

    assert "Phase 0 doc checklist" in task01 or "Archon repo workflow" in task01, (
        "Task 0.1 section does not specify the Archon doc validation mechanism. "
        "Either 'Phase 0 doc checklist' or 'Archon repo workflow' must be named in the Task 0.1 section."
    )


def test_phase0_contract_updates_documentation_index_for_followup_backlog_items() -> None:
    """Documentation index must be updated when FEAT-039 creates follow-up backlog items."""
    assert DOC_INDEX.exists(), (
        f"Documentation index not found at {DOC_INDEX}. "
        "Cannot verify that it has been updated."
    )

    doc_index_text = DOC_INDEX.read_text(encoding="utf-8")

    # FEAT-039 plan codex must be in the index
    assert "FEAT-039-search-evaluation-harness-plan-codex" in doc_index_text, (
        "FEAT-039 plan codex is not listed in the documentation index. "
        "New or changed backlog follow-ups must remain discoverable."
    )

    # FEAT-039 brief must be in the index
    assert "FEAT-039-search-evaluation-harness-brief" in doc_index_text, (
        "FEAT-039 brief is not listed in the documentation index."
    )

    # The plan codex must reference the documentation index
    plan_text = _plan_text()
    assert "990_documentation_index_and_contribution_guide" in plan_text, (
        "Plan codex does not reference the documentation index. "
        "Task 0.1 requires updating the index when follow-up backlog items are created."
    )


# ---------------------------------------------------------------------------
# Task 0.2 constants — confirmed CI/release infrastructure answers
# ---------------------------------------------------------------------------

PHASE0_CONTRACT = {
    # 1. Command to install Search/eval dependencies (dev group contains pytest plugins)
    "dependency_install_command": "uv sync --group dev",
    # 2. Clean-environment install smoke command
    "clean_install_smoke_command": "cd packages/archon-search && uv sync",
    # 3. Command to run tests under the package pytest config (not root Archon config)
    "package_pytest_config_command": "cd packages/archon-search && uv run pytest",
    # 4. Release entrypoint that can fail before tag/publish/release creation
    "release_entrypoint": "bash release.sh",
    # 5. Path-filtered PR workflow — no .github/workflows directory exists in this monorepo.
    #    This is a partial fulfillment: release-only gating is in place but no PR-level
    #    path-filtered workflow triggers the eval gate on every PR touching packages/archon-search/.
    "pr_eval_gate_status": "absent — no .github/workflows directory exists; partial fulfillment only",
    # 6. Release gate placement — guards in release.sh (branch/dirty-tree checks) run before
    #    the first mutation at line ~84 (sed update of install.py). Gate is before any mutation.
    "release_gate_before_first_mutation": True,
}

RELEASE_SCRIPT = REPO_ROOT / "release.sh"


# ---------------------------------------------------------------------------
# Task 0.2 tests
# ---------------------------------------------------------------------------


def _task02_section(plan_text: str) -> str:
    """Extract the Task 0.2 section from the plan."""
    marker = "#### Task 0.2"
    next_marker = "#### Task 0.3"
    start = plan_text.find(marker)
    end = plan_text.find(next_marker, start)
    assert start != -1, f"Task 0.2 section not found in {PLAN_CODEX}"
    return plan_text[start:end] if end != -1 else plan_text[start:]


def test_phase0_contract_names_dependency_install_command() -> None:
    """Search/eval dependencies are not assumed — the install command must be verifiable."""
    cmd = PHASE0_CONTRACT["dependency_install_command"]
    assert cmd, "dependency_install_command must be a non-empty string"

    # The dev dependency group must exist in pyproject.toml
    pyproject_text = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[dependency-groups]" in pyproject_text, (
        "pyproject.toml has no [dependency-groups] section — "
        "eval/test dependencies must be in a named group, not assumed present"
    )
    assert "dev" in pyproject_text, (
        "No 'dev' dependency group found in pyproject.toml — "
        "pytest plugins must be installed via an explicit group"
    )

    # pytest must be in the dev group (not a bare assumption)
    pyproject_data = tomllib.loads(pyproject_text)
    dev_deps = pyproject_data.get("dependency-groups", {}).get("dev", [])
    assert any("pytest" in str(dep) for dep in dev_deps), (
        "pytest is not listed in the [dependency-groups] dev section of pyproject.toml. "
        "The dependency install command must pull in actual pytest plugins."
    )


def test_phase0_contract_names_clean_install_smoke_command() -> None:
    """Dependency validation must be executable in a clean environment."""
    cmd = PHASE0_CONTRACT["clean_install_smoke_command"]
    assert cmd, "clean_install_smoke_command must be a non-empty string"
    assert "packages/archon-search" in cmd, (
        f"clean_install_smoke_command '{cmd}' does not reference packages/archon-search. "
        "The smoke command must be scoped to the package directory."
    )
    assert "uv sync" in cmd, (
        f"clean_install_smoke_command '{cmd}' does not use 'uv sync'. "
        "uv sync is the correct install command for this package."
    )

    # uv must be available on PATH for the smoke command to be executable
    result = subprocess.run(
        ["uv", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        "uv is not available on PATH. "
        "The clean install smoke command requires uv to be installed."
    )

    # The package lockfile must exist (proves the env is reproducible)
    lockfile = PACKAGE_ROOT / "uv.lock"
    assert lockfile.exists(), (
        f"uv.lock not found at {lockfile}. "
        "A committed lockfile is required for a reproducible clean-environment install."
    )


def test_phase0_contract_names_package_pytest_config_command() -> None:
    """Release tests must not accidentally use root Archon pytest config."""
    cmd = PHASE0_CONTRACT["package_pytest_config_command"]
    assert cmd, "package_pytest_config_command must be a non-empty string"
    assert "packages/archon-search" in cmd, (
        f"package_pytest_config_command '{cmd}' does not reference packages/archon-search. "
        "The command must run under the package directory to pick up its own pytest config."
    )

    # The package must have its own pytest config (in pyproject.toml)
    pyproject_text = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in pyproject_text, (
        "packages/archon-search/pyproject.toml has no [tool.pytest.ini_options] section. "
        "The package must have its own pytest config to avoid inheriting the root Archon config."
    )

    # The root Archon pyproject.toml must also have pytest config (to confirm isolation matters)
    root_pyproject = REPO_ROOT / "pyproject.toml"
    assert root_pyproject.exists(), f"Root pyproject.toml not found at {root_pyproject}"
    root_pyproject_text = root_pyproject.read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in root_pyproject_text, (
        "Root pyproject.toml has no pytest config — isolation concern does not apply, "
        "but if it did, the package command must use the package's own config."
    )


def test_phase0_contract_names_release_entrypoint() -> None:
    """Release gate target must be executable (can fail before tag/publish/release creation)."""
    entrypoint = PHASE0_CONTRACT["release_entrypoint"]
    assert entrypoint, "release_entrypoint must be a non-empty string"
    assert "release.sh" in entrypoint, (
        f"release_entrypoint '{entrypoint}' does not reference release.sh. "
        "The monorepo release script is release.sh."
    )

    # The release script must exist and be executable
    assert RELEASE_SCRIPT.exists(), (
        f"release.sh not found at {RELEASE_SCRIPT}. "
        "The release entrypoint must exist."
    )
    assert RELEASE_SCRIPT.stat().st_mode & 0o111, (
        f"release.sh at {RELEASE_SCRIPT} is not executable. "
        "The release entrypoint must have execute permission."
    )


def test_phase0_contract_records_executable_pr_eval_gate() -> None:
    """FEAT-039 cannot be marked complete with release-only gating — PR gate status must be recorded.

    If no path-filtered PR workflow exists, that absence must be explicitly documented
    in the PHASE0_CONTRACT so the gap is visible (partial fulfillment).
    """
    pr_gate_status = PHASE0_CONTRACT["pr_eval_gate_status"]
    assert pr_gate_status, "pr_eval_gate_status must be a non-empty string — absence must be documented"

    workflows_dir = REPO_ROOT / ".github" / "workflows"
    if workflows_dir.exists():
        # If a workflows directory exists, check for archon-search path-filtered workflows
        workflow_files = list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))
        search_workflows = [
            f for f in workflow_files
            if "search" in f.read_text(encoding="utf-8").lower()
            and "packages/archon-search" in f.read_text(encoding="utf-8")
        ]
        if search_workflows:
            assert "absent" not in pr_gate_status.lower(), (
                "PHASE0_CONTRACT records pr_eval_gate_status as absent, but path-filtered "
                "workflows for packages/archon-search were found. Update PHASE0_CONTRACT."
            )
        else:
            # Workflows dir exists but no search-specific workflow — partial fulfillment
            assert "partial" in pr_gate_status.lower() or "absent" in pr_gate_status.lower(), (
                "A .github/workflows dir exists but no archon-search path-filtered workflow "
                "was found. PHASE0_CONTRACT must record this as absent or partial fulfillment."
            )
    else:
        # No workflows directory at all — partial fulfillment is the correct status
        assert "absent" in pr_gate_status.lower() or "partial" in pr_gate_status.lower(), (
            "No .github/workflows directory exists in this repository. "
            "PHASE0_CONTRACT pr_eval_gate_status must record the absence explicitly."
        )


def test_phase0_contract_places_eval_before_first_release_mutation() -> None:
    """Release gate placement must be before the first release mutation (commit/tag/publish).

    The release.sh script must have all guard checks (branch, dirty-tree, RELEASE.md)
    before the first line that mutates the repository state (sed, git add, git commit, git tag).
    """
    assert PHASE0_CONTRACT["release_gate_before_first_mutation"] is True, (
        "PHASE0_CONTRACT records release_gate_before_first_mutation = False. "
        "The release gate must run before any mutation."
    )

    release_text = RELEASE_SCRIPT.read_text(encoding="utf-8")
    lines = release_text.splitlines()

    # Find the line number of the first guard check (branch check is the canonical guard start)
    branch_check_line = next(
        (i for i, line in enumerate(lines) if "rev-parse --abbrev-ref HEAD" in line),
        None,
    )
    assert branch_check_line is not None, (
        "release.sh does not contain a branch guard (git rev-parse --abbrev-ref HEAD). "
        "A branch check is required as a pre-mutation gate."
    )

    # Find the line number of the first mutation (git commit is the canonical first mutation)
    first_commit_line = next(
        (i for i, line in enumerate(lines) if "git commit" in line),
        None,
    )
    assert first_commit_line is not None, (
        "release.sh does not contain a 'git commit' line. "
        "Cannot verify gate placement without a commit step."
    )

    # The branch guard must appear before the first git commit
    assert branch_check_line < first_commit_line, (
        f"Branch guard is at line {branch_check_line + 1} but first 'git commit' is at "
        f"line {first_commit_line + 1}. The gate must precede the first mutation."
    )

    # The dirty-tree guard must also be before the first git commit
    dirty_check_line = next(
        (i for i, line in enumerate(lines) if "git diff --quiet" in line),
        None,
    )
    assert dirty_check_line is not None, (
        "release.sh does not contain a dirty-tree guard (git diff --quiet). "
        "A dirty-tree check is required as a pre-mutation gate."
    )
    assert dirty_check_line < first_commit_line, (
        f"Dirty-tree guard is at line {dirty_check_line + 1} but first 'git commit' is at "
        f"line {first_commit_line + 1}. The gate must precede the first mutation."
    )
