"""CI guard: archon_search/store.py must contain no f-string SQL builder calls (A5b).

Reads store.py as text and asserts none of the three patterns match:
  - .where(f"  or  .where(f'
  - .delete(f"  or  .delete(f'
  - .count_rows(f"  or  .count_rows(f'

Also includes meta-tests that verify the guard itself works correctly.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns under guard
# ---------------------------------------------------------------------------

_PATTERNS = [
    re.compile(r"\.where\(\s*f[\"']"),
    re.compile(r"\.delete\(\s*f[\"']"),
    re.compile(r"\.count_rows\(\s*f[\"']"),
]

_STORE_PATH = Path(__file__).parent.parent / "archon_search" / "store.py"


def _find_violations(source: str) -> list[tuple[int, str, str]]:
    """Return list of (lineno, pattern_str, line) for each match in source."""
    violations = []
    for pat in _PATTERNS:
        for i, line in enumerate(source.splitlines(), start=1):
            if pat.search(line):
                violations.append((i, pat.pattern, line.strip()))
    return violations


# ---------------------------------------------------------------------------
# Main guard test
# ---------------------------------------------------------------------------


def test_no_fstring_sql_in_store_py() -> None:
    """archon_search/store.py contains no f-string-wrapped .where/delete/count_rows calls."""
    source = _STORE_PATH.read_text(encoding="utf-8")
    violations = _find_violations(source)
    if violations:
        details = "\n".join(
            f"  line {lineno}: [{pat}] → {line}"
            for lineno, pat, line in violations
        )
        raise AssertionError(
            f"Found {len(violations)} f-string SQL site(s) in {_STORE_PATH}:\n{details}\n"
            "Use _where_eq() / _where_in() helpers instead."
        )


# ---------------------------------------------------------------------------
# Meta-tests: verify the guard works correctly
# ---------------------------------------------------------------------------


def test_guard_detects_injected_violation() -> None:
    """Meta-test: the guard detects an obvious f-string SQL site."""
    # A string that matches the .where(f"...") pattern
    fake_source = """
async def bad_query(col, val):
    rows = await table.where(f"name = '{val}'").to_list()
"""
    violations = _find_violations(fake_source)
    assert violations, (
        "guard did not detect the injected .where(f'...') violation — "
        "the guard regex may have been weakened"
    )
    # Check the violation points at the right pattern
    assert any("where" in pat for _, pat, _ in violations)


def test_guard_ignores_router_delete_decorator() -> None:
    """Meta-test: @router.delete('/{name}') does NOT match the guard pattern."""
    fake_source = """
@router.delete("/{name}", response_model=DeleteResponse)
async def remove_collection(name: str) -> DeleteResponse:
    pass
"""
    violations = _find_violations(fake_source)
    assert not violations, (
        f"guard incorrectly flagged @router.delete decorator: {violations}"
    )


def test_guard_ignores_helper_internals() -> None:
    """Meta-test: f-string inside a helper body is NOT flagged (not preceded by .where/delete/count_rows)."""
    fake_source = """
def _where_eq(col: str, value: str) -> str:
    return f"{col} = {_sql_quote_str(value)}"

def _where_in(col: str, values) -> str:
    items = ", ".join(_sql_quote_str(v) for v in values)
    return f"{col} IN ({items})" if items else "1=0"
"""
    violations = _find_violations(fake_source)
    assert not violations, (
        f"guard incorrectly flagged helper internals: {violations}"
    )
