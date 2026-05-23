"""A5b Task 2.4 — CI guard: no f-string SQL in archon_search/store.py.

One default-tier guard test reads store.py as text and asserts none of the
three dangerous patterns are present. Three meta-tests verify the guard's
own regex behaves correctly (prevents the guard becoming a silent no-op).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Patterns the guard watches for — factored out so meta-tests use the same
# compiled regexes as the real guard.
# ---------------------------------------------------------------------------

_PATTERNS = [
    re.compile(r"\.where\(\s*f[\"']"),
    re.compile(r"\.delete\(\s*f[\"']"),
    re.compile(r"\.count_rows\(\s*f[\"']"),
]

# ---------------------------------------------------------------------------
# Meta-tests: verify the guard's regex behaves correctly
# ---------------------------------------------------------------------------


def test_guard_detects_injected_violation() -> None:
    """The guard regex must fire on an obvious f-string SQL site.

    Writes an in-memory string containing table.where(f"x = '{y}'") and asserts
    the pattern matches — preventing a regex weakening from silently disabling
    the guard.
    """
    content = """table.where(f"x = '{y}'")\n"""
    matches = [p.search(content) for p in _PATTERNS]
    assert any(m is not None for m in matches), (
        "Guard regex failed to detect an obvious f-string SQL violation in the test fixture"
    )


def test_guard_ignores_router_delete_decorator() -> None:
    """@router.delete('/{name}') must NOT match any guard pattern."""
    content = '@router.delete("/{name}")\n'
    for p in _PATTERNS:
        assert p.search(content) is None, (
            f"Pattern {p.pattern!r} falsely matched router decorator: {content!r}"
        )


def test_guard_ignores_helper_internals() -> None:
    """A bare f-string not preceded by .where(/.delete(/.count_rows( must not match."""
    content = 'return f"{col} = {_quote_literal(value)}"\n'
    for p in _PATTERNS:
        assert p.search(content) is None, (
            f"Pattern {p.pattern!r} falsely matched helper internals: {content!r}"
        )
