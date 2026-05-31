"""Tests for the awk extraction command used in CI to extract the latest changelog section."""

import subprocess

AWK_CMD = "awk '/^## /{if(found) exit; found=1; next} found'"

MULTI_SECTION_INPUT = """\
# Changelog

## [1.0.0] - 2026-01-01

### Features
- add search endpoint

## [0.9.0] - 2025-12-01

### Bug Fixes
- fix something old
"""


def _run_awk(content: str) -> str:
    result = subprocess.run(
        AWK_CMD,
        shell=True,
        input=content,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip()


def test_awk_extraction_on_stub() -> None:
    stub_content = """\
# Changelog

All notable changes to archon-search are recorded here.
Prior release history is available via `git log`.
"""
    output = _run_awk(stub_content)
    assert output == ""


def test_awk_extraction_single_section() -> None:
    output = _run_awk(MULTI_SECTION_INPUT)
    assert "### Features" in output
    assert "add search endpoint" in output


def test_awk_extraction_heading_not_included() -> None:
    output = _run_awk(MULTI_SECTION_INPUT)
    assert "## [1.0.0] - 2026-01-01" not in output


def test_awk_extraction_multiple_sections() -> None:
    output = _run_awk(MULTI_SECTION_INPUT)
    assert "fix something old" not in output
