"""Documentation contract tests."""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_jobs_doc_documents_list_response_envelope() -> None:
    """S117: the jobs guide must name the `GET /jobs` response envelope.

    Wire-truth (`server/schemas.py::JobListResponse`): the endpoint returns a
    container object with ``items`` (the job array), ``next_cursor`` (the
    continuation token that supplies the documented ``cursor`` request param),
    and ``total``. `100_jobs_and_async_operations.md` documented only the
    request query parameters, so a reader could not page through the endpoint
    from the doc alone — the field that supplies ``cursor`` was never named.
    """
    schemas_src = (
        REPO_ROOT / "archon_search" / "server" / "schemas.py"
    ).read_text(encoding="utf-8")
    # Guard the wire-truth: JobListResponse still carries these three fields.
    envelope_start = schemas_src.index("class JobListResponse")
    envelope = schemas_src[envelope_start : envelope_start + 300]
    for field in ("items", "next_cursor", "total"):
        assert field in envelope, (
            f"wire-truth drift: JobListResponse no longer has `{field}`"
        )

    content = (
        REPO_ROOT / "Documentation" / "UserManual" / "100_jobs_and_async_operations.md"
    ).read_text(encoding="utf-8")
    for field in ("`items`", "`next_cursor`", "`total`"):
        assert field in content, (
            f"100_jobs_and_async_operations.md must name the GET /jobs "
            f"response-envelope field {field}"
        )


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


def test_wizard_md_documents_graph_enabled_for_code_flag() -> None:
    """S552: wizard.md must document that --code writes [graph].enabled = true.

    Gate re-implementation: wizard.md was updated so the old sentinel
    'The wizard does not configure the [graph] section' is gone. The new
    documented behavior is that --code writes [graph].enabled = true.
    This test asserts the new statement; if it vanishes again, it will catch
    a doc regression that diverges from the wizard's actual behavior.

    Wire-truth: test_wizard_declinesCode_doesNotWriteGraphEnabled confirms
    that --no-code leaves [graph] absent, and test_wizard_code_installs_graph_bundle
    confirms --code writes graph.enabled = true.
    """
    content = (
        REPO_ROOT / "Documentation" / "UserManual" / "20_wizard.md"
    ).read_text(encoding="utf-8")

    # New documented behavior: --code causes [graph].enabled = true
    assert "[graph].enabled = true" in content, (
        "20_wizard.md must state that --code writes [graph].enabled = true "
        "(S552 re-implementation: old sentinel 'does not configure [graph]' was removed)"
    )
    assert "--code" in content, (
        "20_wizard.md must document the --code flag"
    )


def test_security_guide_home_mention_is_not_data_dir_relocation() -> None:
    """S570: SecurityGuide/02_authentication_and_keys.md mentions HOME, but only
    in the context of ARCHON_SEARCH_KEY_FILE tilde expansion — NOT data dir relocation.

    Gate re-implementation: the expected doc-set mentioning HOME changed when
    SecurityGuide/02 was added. This test asserts:
    1. HOME is mentioned in the three expected docs (not more, not fewer).
    2. SecurityGuide/02's HOME mention is scoped to ARCHON_SEARCH_KEY_FILE
       (tilde expansion), not to the data directory.
    """
    docs_root = REPO_ROOT / "Documentation"

    # Scan only published user-facing doc directories, not Backlog/Completed/node_modules.
    scan_dirs = ["SecurityGuide", "UserManual", "OperatorGuide", "DeveloperGuide"]
    home_docs = set()
    for subdir in scan_dirs:
        dir_path = docs_root / subdir
        if not dir_path.exists():
            continue
        for md_file in dir_path.rglob("*.md"):
            if "HOME" in md_file.read_text(encoding="utf-8"):
                rel = md_file.relative_to(REPO_ROOT)
                home_docs.add(str(rel).replace("\\", "/"))

    expected = {
        "Documentation/SecurityGuide/02_authentication_and_keys.md",
        "Documentation/UserManual/140_running_with_docker.md",
        "Documentation/UserManual/30_configuration.md",
    }
    assert home_docs == expected, (
        f"S570 gate: the set of docs mentioning HOME changed.\n"
        f"  Expected: {sorted(expected)}\n"
        f"  Got:      {sorted(home_docs)}\n"
        "Re-check whether HOME is now documented as relocating the data directory "
        "and update this test accordingly."
    )

    # SecurityGuide/02 must scope its HOME mention to ARCHON_SEARCH_KEY_FILE only,
    # not to data directory relocation.
    security_doc = (
        docs_root / "SecurityGuide" / "02_authentication_and_keys.md"
    ).read_text(encoding="utf-8")
    assert "ARCHON_SEARCH_KEY_FILE" in security_doc, (
        "SecurityGuide/02 must mention ARCHON_SEARCH_KEY_FILE near its HOME reference"
    )
    assert "data dir" not in security_doc.lower() or "ARCHON_SEARCH_DATA_DIR" in security_doc, (
        "SecurityGuide/02's HOME mention must not document data-dir relocation via HOME; "
        "use ARCHON_SEARCH_DATA_DIR for that"
    )
