"""Unit tests for .github/workflows/archon-search-release.yml structure.

These tests parse the YAML and assert structural invariants of the CI
configuration — no mocking, no network calls.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = (
    Path(__file__).parent.parent
    / ".github"
    / "workflows"
    / "archon-search-release.yml"
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def test_yaml_is_valid(workflow: dict) -> None:
    """YAML must parse without errors."""
    assert isinstance(workflow, dict)


def test_github_release_job_present(workflow: dict) -> None:
    """jobs['github-release'] key must exist."""
    assert "github-release" in workflow["jobs"]


def test_github_release_needs_publish(workflow: dict) -> None:
    """github-release job must declare 'publish' in its needs."""
    needs = workflow["jobs"]["github-release"]["needs"]
    # needs can be a string or a list
    if isinstance(needs, str):
        assert needs == "publish"
    else:
        assert "publish" in needs


def test_github_release_permissions_contents_write(workflow: dict) -> None:
    """Job-level permissions must include contents: write and must NOT include id-token."""
    perms = workflow["jobs"]["github-release"]["permissions"]
    assert perms.get("contents") == "write", "contents must be 'write'"
    assert "id-token" not in perms, "id-token must not be set at github-release job level"


def test_github_release_if_condition(workflow: dict) -> None:
    """github-release job must have the correct if condition."""
    if_cond = workflow["jobs"]["github-release"]["if"]
    assert if_cond == "startsWith(github.ref, 'refs/tags/')"


def test_no_workflow_level_contents_write(workflow: dict) -> None:
    """Top-level permissions must NOT include contents: write."""
    top_perms = workflow.get("permissions")
    if top_perms is None:
        # No top-level permissions key at all — that satisfies the constraint.
        return
    if isinstance(top_perms, str):
        # e.g. permissions: read-all — contents: write is not set
        # write-all grants contents:write implicitly, so also reject it
        assert top_perms not in ("write", "write-all"), (
            f"Workflow top-level permissions must not grant contents: write, got: {top_perms!r}"
        )
        return
    assert top_perms.get("contents") != "write", (
        "Workflow top-level permissions must not grant contents: write"
    )
