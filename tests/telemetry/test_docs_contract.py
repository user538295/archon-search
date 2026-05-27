"""Documentation contract tests for the telemetry ADR, the security/privacy
architecture doc, the documentation index, and the README privacy section."""

from __future__ import annotations

from pathlib import Path

# Repo root is two parents up from tests/telemetry/.
_REPO_ROOT = Path(__file__).parents[2]

ADR_PATH = (
    _REPO_ROOT
    / "Documentation"
    / "ADRs"
    / "05_opt_in_local_telemetry_no_raw_query.md"
)

README_PATH = _REPO_ROOT / "README.md"

REQUIRED_HEADINGS = [
    "## Context",
    "## Decision",
    "## Consequences",
    "## Alternatives Considered",
]


def test_adr_telemetry_exists_and_documents_required_sections() -> None:
    assert ADR_PATH.exists(), f"Telemetry ADR not found at {ADR_PATH}"

    text = ADR_PATH.read_text(encoding="utf-8")

    for heading in REQUIRED_HEADINGS:
        assert heading in text, f"Required heading missing: {heading!r}"

    # The ADR's substantive privacy invariants.
    assert "opt-in" in text, "ADR must state telemetry is opt-in"
    assert "local-only" in text, "ADR must state telemetry is local-only"
    assert "raw query" in text, "ADR must document the no-raw-query guarantee"
    assert "export_enabled" in text, (
        "ADR must document that export_enabled is not honored in v1"
    )
    assert "doc_id" in text, "ADR must document the doc_id path-leak risk"


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


ARCH_DOC_PATH = (
    _REPO_ROOT
    / "Documentation"
    / "Architecture"
    / "150_security_and_privacy_architecture.md"
)

DOC_INDEX_PATH = (
    _REPO_ROOT
    / "Documentation"
    / "Architecture"
    / "990_documentation_index_and_contribution_guide.md"
)


def test_security_arch_doc_documents_telemetry_privacy() -> None:
    assert ARCH_DOC_PATH.exists(), f"Security/privacy doc not found at {ARCH_DOC_PATH}"
    arch_doc = ARCH_DOC_PATH.read_text(encoding="utf-8")
    assert "## Privacy" in arch_doc, "doc must have a Privacy section"
    assert "raw query" in arch_doc, "doc must document the no-raw-query guarantee"
    assert "TelemetryEntry" in arch_doc, "doc must reference TelemetryEntry"
    assert "export_enabled" in arch_doc, (
        "doc must document that export_enabled is coerced to false"
    )


def test_doc_index_includes_telemetry_adr_and_guide() -> None:
    assert DOC_INDEX_PATH.exists(), f"Doc index not found at {DOC_INDEX_PATH}"
    index = DOC_INDEX_PATH.read_text(encoding="utf-8")
    assert "ADRs/05_opt_in_local_telemetry_no_raw_query.md" in index, (
        "doc index must reference the telemetry ADR"
    )
    assert "UserManual/06_telemetry.md" in index, (
        "doc index must reference the telemetry user guide"
    )
