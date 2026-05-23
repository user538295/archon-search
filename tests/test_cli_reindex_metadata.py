"""Unit tests for reindex-metadata CLI --normalize-timestamps flag (A2)."""
from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# Regex that defines "fixed-width" timestamp format
# ---------------------------------------------------------------------------

_FIXED_WIDTH_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@pytest.mark.parametrize(
    "ts, expected_match",
    [
        # Fixed-width (normalized): must match
        ("2026-01-01T00:00:00.000000Z", True),
        ("2026-12-31T23:59:59.999999Z", True),
        # Legacy formats: must NOT match
        ("2026-01-01T00:00:00", False),           # no microseconds, no Z
        ("2026-01-01T00:00:00Z", False),           # no microseconds
        ("2026-01-01T00:00:00+00:00", False),      # +00:00 offset, no microseconds
        ("2026-01-01", False),                     # date only
        ("", False),                               # empty string
    ],
)
def test_legacy_format_regex_rejects_known_legacy_shapes(ts: str, expected_match: bool) -> None:
    """The fixed-width timestamp regex must accept normalized and reject legacy forms."""
    assert bool(_FIXED_WIDTH_TS_RE.match(ts)) is expected_match


def test_normalize_timestamps_flag_is_recognized_by_cli() -> None:
    """reindex-metadata command accepts --normalize-timestamps flag without error."""
    from click.testing import CliRunner
    from archon_search.cli.collection import collection

    runner = CliRunner()
    # --help should show the flag
    result = runner.invoke(collection, ["reindex-metadata", "--help"])
    assert result.exit_code == 0
    assert "--normalize-timestamps" in result.output
    assert "--no-normalize-timestamps" in result.output


def test_normalize_timestamps_default_is_on() -> None:
    """reindex-metadata --normalize-timestamps is ON by default."""
    import inspect
    from archon_search.cli.collection import reindex_metadata_cmd

    # The Click command's params include normalize_timestamps with default=True
    from click import Context
    ctx = Context(reindex_metadata_cmd)
    params = {p.name: p for p in reindex_metadata_cmd.params}
    assert "normalize_timestamps" in params
    assert params["normalize_timestamps"].default is True
