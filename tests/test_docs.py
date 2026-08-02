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


def test_50_ingestion_quotes_full_server_not_running_message() -> None:
    """S13: 50_ingestion must quote the exact CLI string, not a truncated form.

    Wire truth: the message the CLI emits on a refused connection is the
    `_SERVER_NOT_RUNNING_MSG` constant in `archon_search/cli/_helpers.py`.
    """
    from archon_search.cli._helpers import _SERVER_NOT_RUNNING_MSG

    doc = (
        REPO_ROOT / "Documentation" / "UserManual" / "50_ingestion_and_collections.md"
    ).read_text(encoding="utf-8")
    assert _SERVER_NOT_RUNNING_MSG in doc, (
        "50_ingestion_and_collections.md must quote the full server-not-running "
        "message the CLI emits, matching 100_jobs_and_async_operations.md"
    )
    assert 'Start it first."' not in doc, (
        "the truncated server-not-running quote (ending 'Start it first.') must not reappear"
    )


def test_50_ingestion_reindex_metadata_dry_run_example_includes_wait() -> None:
    """S218: the dry-run example must include --wait.

    Wire truth: `reindex_metadata_cmd` in `archon_search/cli/collection.py` only
    echoes processed/updated/skipped counts inside the `if wait_flag:` branch; a
    bare `--dry-run` invocation prints only a job id.
    """
    doc = (
        REPO_ROOT / "Documentation" / "UserManual" / "50_ingestion_and_collections.md"
    ).read_text(encoding="utf-8")
    assert "reindex-metadata docs --dry-run --wait" in doc, (
        "dry-run example must include --wait so the promised counts actually appear"
    )
    assert "reindex-metadata docs --dry-run\n" not in doc, (
        "bare --dry-run example (no --wait) prints only a job id, not counts"
    )
    # Same defect must not survive in the sibling enrichment page.
    doc55 = (
        REPO_ROOT / "Documentation" / "UserManual" / "55_chunk_metadata_and_enrichment.md"
    ).read_text(encoding="utf-8")
    assert "reindex-metadata docs --dry-run\n" not in doc55, (
        "55_chunk_metadata_and_enrichment.md must not show a bare --dry-run example"
    )
