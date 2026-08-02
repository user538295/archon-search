"""Documentation contract tests."""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_graph_search_doc_states_naive_expansion_precondition() -> None:
    """S59: the naive worked example must state the lexical-overlap precondition.

    Wire-truth (`graph_expander.py`): ``GraphExpander.expand`` sets
    ``expansion_applied=True`` only when a query N-gram matches an extracted
    entity NAME (``find_nodes_by_name`` on N-grams); a non-matching query
    returns ``expansion_applied=False`` with plain hybrid results and no error.
    Graph-existing is necessary but NOT sufficient, so the doc's sole stated
    precondition ("Nothing needs to be pre-built beyond the graph itself") is
    incomplete. The doc must say expansion is triggered by lexical overlap
    between the query and entity names, not by semantic relevance.
    """
    expander_src = (
        REPO_ROOT / "archon_search" / "graph_expander.py"
    ).read_text(encoding="utf-8")
    assert "find_nodes_by_name" in expander_src, (
        "wire-truth drift: naive expansion no longer keys off entity-name lookup"
    )

    content = (
        REPO_ROOT / "Documentation" / "UserManual" / "65_graph_search.md"
    ).read_text(encoding="utf-8")
    # Scope assertions to the `naive` subsection so precondition prose belongs
    # next to the worked example, not borrowed from the ppr/synonym sections.
    start = content.index("### `naive`")
    end = content.index("### `local`", start)
    naive = content[start:end].lower()
    assert "entity name" in naive, (
        "naive section must state expansion requires the query to overlap an "
        "extracted entity name"
    )
    assert "lexical" in naive or "verbatim" in naive, (
        "naive section must state the match is lexical (not semantic)"
    )
    assert "not by semantic" in naive or "not semantic" in naive, (
        "naive section must state expansion is NOT triggered by semantic relevance"
    )
    assert "graph_expansion_applied: false" in naive, (
        "naive section must state a non-matching query yields "
        "graph_expansion_applied: false with plain hybrid results"
    )


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
