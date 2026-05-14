"""Documentation contract tests for ADR 10 (Task 4.1)."""

from __future__ import annotations

from pathlib import Path

ADR_PATH = (
    Path(__file__).parents[4]
    / "Documentation"
    / "ADRs"
    / "10_search_query_telemetry.md"
)

REQUIRED_HEADINGS = [
    "## Status",
    "## Context",
    "## Decision",
    "## Consequences",
    "## Privacy",
    "## Why `export_enabled` is not a security boundary",
    "## Open questions / FEAT-039c hooks",
]

REQUIRED_SUBSTRING = "absence of export code"


def test_adr_10_exists_and_documents_required_sections() -> None:
    assert ADR_PATH.exists(), f"ADR 10 not found at {ADR_PATH}"

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
