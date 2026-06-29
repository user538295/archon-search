"""A5b Task 2.4 — CI guard: no f-string SQL in archon_search/store.py.

One default-tier guard test reads store.py as text and asserts none of the
three dangerous patterns are present. Three meta-tests verify the guard's
own regex behaves correctly (prevents the guard becoming a silent no-op).
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Patterns the guard watches for — factored out so meta-tests use the same
# compiled regexes as the real guard.  re.DOTALL so \s* spans newlines.
# ---------------------------------------------------------------------------

_PATTERNS = [
    re.compile(r"\.where\(\s*f[\"']", re.DOTALL),
    re.compile(r"\.delete\(\s*f[\"']", re.DOTALL),
    re.compile(r"\.count_rows\(\s*f[\"']", re.DOTALL),
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


def test_guard_detects_multiline_violation() -> None:
    """The guard regex must fire when .where( and f" appear on separate lines.

    Validates that the full-text scan (with re.DOTALL) closes the line-by-line
    hole where \\s* would not cross a newline boundary.
    """
    content = textwrap.dedent(
        """\
        await table.where(
                f"x = '{y}'"
            )
        """
    )
    matches = [p.search(content) for p in _PATTERNS]
    assert any(m is not None for m in matches), (
        "Guard regex failed to detect a multiline f-string SQL violation — "
        "re.DOTALL flag may be missing from the pattern compilation"
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
    content = 'return f"{col} = {_sql_quote_str(value)}"\n'
    for p in _PATTERNS:
        assert p.search(content) is None, (
            f"Pattern {p.pattern!r} falsely matched helper internals: {content!r}"
        )


# ---------------------------------------------------------------------------
# Real guard — reads store.py as a single string and asserts zero violations.
# Line numbers are computed from match offsets so multiline spans are caught.
# ---------------------------------------------------------------------------


def test_no_fstring_sql_in_graph_store() -> None:
    """graph_store.py must contain zero f-string-wrapped .where/.delete/.count_rows calls."""
    graph_store_path = Path(__file__).parent.parent / "archon_search" / "graph_store.py"
    source = graph_store_path.read_text(encoding="utf-8")

    violations: list[str] = []
    for pat in _PATTERNS:
        for m in pat.finditer(source):
            lineno = source.count("\n", 0, m.start()) + 1
            snippet = source.splitlines()[lineno - 1].strip()
            violations.append(f"  line {lineno}: {snippet}")

    assert not violations, (
        "F-string SQL violations found in archon_search/graph_store.py:\n"
        + "\n".join(violations)
        + "\nReplace with _where_eq / _where_in helpers."
    )


def test_no_fstring_sql_in_store() -> None:
    """store.py must contain zero f-string-wrapped .where/.delete/.count_rows calls.

    On failure the assertion message names the matching line numbers so a
    contributor sees exactly where the violation is.
    """
    store_path = Path(__file__).parent.parent / "archon_search" / "store.py"
    source = store_path.read_text(encoding="utf-8")

    violations: list[str] = []
    for pat in _PATTERNS:
        for m in pat.finditer(source):
            lineno = source.count("\n", 0, m.start()) + 1
            snippet = source.splitlines()[lineno - 1].strip()
            violations.append(f"  line {lineno}: {snippet}")

    assert not violations, (
        "F-string SQL violations found in archon_search/store.py:\n"
        + "\n".join(violations)
        + "\nReplace with _where_eq / _where_in helpers."
    )
