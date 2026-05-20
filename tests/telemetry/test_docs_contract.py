"""Documentation contract tests for ADR 10 (Task 4.1) and README privacy section (Task 4.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

# Standalone repo: repo root is two parents up from tests/telemetry/.
# Monorepo (pre-extraction) used parents[4]; that path no longer exists here.
ADR_PATH = (
    Path(__file__).parents[2]
    / "Documentation"
    / "ADRs"
    / "10_search_query_telemetry.md"
)

README_PATH = Path(__file__).parents[2] / "README.md"

REQUIRED_HEADINGS = [
    "## Status",
    "## Context",
    "## Decision",
    "## Consequences",
    "## Privacy",
    "## Why `export_enabled` is not a security boundary",
    "## Open questions / FEAT-039d hooks",
]

REQUIRED_SUBSTRING = "absence of export code"


def test_adr_10_exists_and_documents_required_sections() -> None:
    if not ADR_PATH.exists():
        pytest.skip(f"ADR not present in standalone repo: {ADR_PATH}")

    text = ADR_PATH.read_text(encoding="utf-8")

    for heading in REQUIRED_HEADINGS:
        assert heading in text, f"Required heading missing: {heading!r}"

    assert REQUIRED_SUBSTRING in text, (
        f"Required substring missing: {REQUIRED_SUBSTRING!r}"
    )

    assert "### Path-derived" in text, "Privacy subsection '### Path-derived' missing"
    assert "omit** the raw query string" in text, (
        "Privacy claim about omitting raw query string missing"
    )


def test_readme_contains_path_derived_doc_id_warning() -> None:
    assert README_PATH.exists(), f"README not found at {README_PATH}"
    text = README_PATH.read_text(encoding="utf-8")
    assert "doc_ids may reveal filesystem paths" in text, (
        "README must warn that doc_ids may reveal filesystem paths"
    )


def test_readme_documents_opt_in_default() -> None:
    assert README_PATH.exists(), f"README not found at {README_PATH}"
    text = README_PATH.read_text(encoding="utf-8")
    assert "enabled = false" in text, (
        "README must document that telemetry is disabled by default (enabled = false)"
    )


def test_readme_links_to_adr_10() -> None:
    assert README_PATH.exists(), f"README not found at {README_PATH}"
    text = README_PATH.read_text(encoding="utf-8")
    assert "ADRs/10_search_query_telemetry.md" in text, (
        "README must link to ADR 10 (ADRs/10_search_query_telemetry.md)"
    )


ARCH_DOC_PATH = (
    Path(__file__).parents[2]
    / "Documentation"
    / "Architecture"
    / "180_search_architecture.md"
)

DOC_INDEX_PATH = (
    Path(__file__).parents[2]
    / "Documentation"
    / "990_documentation_index_and_contribution_guide.md"
)


def test_arch_doc_mentions_telemetry_section() -> None:
    if not ARCH_DOC_PATH.exists():
        pytest.skip(f"Architecture doc not present in standalone repo: {ARCH_DOC_PATH}")
    arch_doc = ARCH_DOC_PATH.read_text(encoding="utf-8")
    assert "## Telemetry (FEAT-039b)" in arch_doc
    assert "ADRs/10_search_query_telemetry.md" in arch_doc
    assert "TelemetryWriter" in arch_doc


def test_doc_index_includes_telemetry_plan_and_adr() -> None:
    if not DOC_INDEX_PATH.exists():
        pytest.skip(f"Doc index not present in standalone repo: {DOC_INDEX_PATH}")
    index = DOC_INDEX_PATH.read_text(encoding="utf-8")
    assert "| `Documentation/Backlog/FEAT-039b-search-telemetry-and-privacy-plan.md`" in index
    assert "| `Documentation/ADRs/10_search_query_telemetry.md`" in index
