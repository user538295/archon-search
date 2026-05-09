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

These tests are NOT behavioral tests. They fail fast when an implementer
skips prerequisite steps but cannot catch runtime bugs.
"""
from __future__ import annotations

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
