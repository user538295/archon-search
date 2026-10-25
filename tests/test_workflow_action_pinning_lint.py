"""Static lint: every third-party GitHub Action in CI is pinned to a full commit SHA.

A tag such as ``actions/checkout@v7.0.1`` is a movable pointer: whoever controls the action's repo
(or a compromised maintainer token) can re-point it at new code that then runs with this repo's
``GITHUB_TOKEN`` and secrets (the release workflow publishes to PyPI and GHCR). A 40-hex commit SHA
cannot be re-pointed. A trailing ``# <tag>`` comment keeps the human-readable version.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOWS_DIR.glob("*.yml"))
FULL_SHA = re.compile(r"[0-9a-f]{40}")
USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<ref>\S+)(?P<tail>.*)$")
TAG_COMMENT = re.compile(r"^\s+#\s*v?\d[\w.\-]*\s*$")
LOCAL_PREFIX = "./"


def _uses_lines(workflow: Path) -> list[tuple[int, str, str]]:
    """(line number, action ref, text after the ref) for every ``uses:`` line."""
    found = []
    for number, line in enumerate(workflow.read_text().splitlines(), start=1):
        match = USES_LINE.match(line)
        if match:
            found.append((number, match["ref"], match["tail"]))
    return found


def _is_pinned(ref: str) -> bool:
    if ref.startswith(LOCAL_PREFIX):
        return True  # a reusable workflow in this repo is versioned with the caller
    _, _, revision = ref.partition("@")
    return bool(FULL_SHA.fullmatch(revision))


@pytest.mark.parametrize(
    ("ref", "pinned"),
    [
        ("actions/checkout@v7.0.1", False),
        ("actions/checkout@main", False),
        ("actions/checkout", False),
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b", False),  # 39 hex
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", True),
        ("./.github/workflows/archon-search-unlocked-deps.yml", True),
    ],
)
def test_pin_detector(ref: str, pinned: bool) -> None:
    assert _is_pinned(ref) is pinned


def test_workflows_exist() -> None:
    assert WORKFLOWS, f"no workflows found under {WORKFLOWS_DIR}"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha_with_a_tag_comment(workflow: Path) -> None:
    unpinned = []
    for number, ref, tail in _uses_lines(workflow):
        if not _is_pinned(ref):
            unpinned.append(f"{workflow.name}:{number} {ref}")
        elif not ref.startswith(LOCAL_PREFIX) and not TAG_COMMENT.match(tail):
            unpinned.append(f"{workflow.name}:{number} {ref} (missing trailing `# <tag>` comment)")
    assert not unpinned, "pin to a commit SHA, keeping the tag as a comment: " + "; ".join(unpinned)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_line_scan_sees_every_uses_key(workflow: Path) -> None:
    """The line scan must not miss a ``uses`` the YAML parser finds, or the lint is blind to it."""
    doc = yaml.safe_load(workflow.read_text())
    parsed = [
        step["uses"]
        for job in doc["jobs"].values()
        for step in [*job.get("steps", []), job]
        if "uses" in step
    ]
    scanned = [ref for _, ref, _ in _uses_lines(workflow)]
    assert sorted(parsed) == sorted(scanned)


RUFF_STEP = re.compile(r"^uvx ruff@\d+\.\d+\.\d+ check \.$")


def test_pr_gate_runs_pinned_ruff_over_the_whole_repo() -> None:
    """The PR gate lints with an exactly pinned ruff (the default rule set moves between versions)."""
    doc = yaml.safe_load((WORKFLOWS_DIR / "archon-search-pr.yml").read_text())
    runs = [step.get("run", "") for job in doc["jobs"].values() for step in job.get("steps", [])]
    assert any(RUFF_STEP.match(run.strip()) for run in runs), "archon-search-pr.yml needs `uvx ruff@<x.y.z> check .`"
