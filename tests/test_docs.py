"""Documentation contract tests."""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_adr_07_exists_and_references_adr_04() -> None:
    adr_path = REPO_ROOT / "Documentation" / "ADRs" / "07_description_embedding_hybrid_routing.md"
    assert adr_path.exists(), f"ADR-07 not found at {adr_path}"
    content = adr_path.read_text(encoding="utf-8")
    assert content.strip(), "ADR-07 is empty"
    assert "ADR-04" in content, "ADR-07 must reference ADR-04"


def test_claude_md_mentions_git_cliff() -> None:
    content = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "git-cliff" in content, "CLAUDE.md must mention git-cliff as a release prerequisite"


def test_contributing_md_mentions_git_cliff() -> None:
    content = (REPO_ROOT / "contributing.md").read_text(encoding="utf-8")
    assert "git-cliff" in content, "contributing.md must mention git-cliff"
    assert ">= 2.4" in content, "contributing.md must specify git-cliff >= 2.4"


def test_contributing_md_changelog_ownership_rule() -> None:
    content = (REPO_ROOT / "contributing.md").read_text(encoding="utf-8")
    lower = content.lower()
    assert "changelog.md" in lower, "contributing.md must mention CHANGELOG.md"
    assert "do not edit" in lower or "do not manually" in lower, (
        "contributing.md must state that CHANGELOG.md must not be edited manually"
    )
