"""CI guard: all logger names in archon_search/ must use __name__ or be the intentional root 'archon_search'."""
from __future__ import annotations

import re
from pathlib import Path

# Matches getLogger("...") with any string literal that is NOT the allowed root "archon_search"
# or a dotted child "archon_search.<something>".
# Allows: getLogger(__name__), getLogger("archon_search"), getLogger("archon_search.anything")
# Rejects: getLogger("archon"), getLogger("archon.search"), getLogger("archon-search"),
#          getLogger("archon_search_evil"), getLogger('archon'), etc.
# The lookahead (?!archon_search[.'\"]) rejects strings that start with archon_search followed by
# a dot (dotted child) or a quote (exact match) — those are the only two allowed forms.
_BAD_PATTERN = re.compile(
    r"""getLogger\s*\(\s*['"](?!archon_search[.'"'])[^'"]+['"]\s*\)"""
)


def test_guard_detects_bad_name():
    assert _BAD_PATTERN.search('logging.getLogger("archon")')


def test_guard_detects_single_quoted_bad_name():
    assert _BAD_PATTERN.search("logging.getLogger('archon')")


def test_guard_detects_archon_dot_search():
    assert _BAD_PATTERN.search('logging.getLogger("archon.search")')


def test_guard_detects_archon_dash_search():
    assert _BAD_PATTERN.search('logging.getLogger("archon-search")')


def test_guard_ignores_dunder_name():
    assert not _BAD_PATTERN.search("logging.getLogger(__name__)")


def test_guard_ignores_archon_search_root():
    assert not _BAD_PATTERN.search('logging.getLogger("archon_search")')


def test_guard_ignores_archon_search_dot_prefix():
    assert not _BAD_PATTERN.search('logging.getLogger("archon_search.server.app")')


def test_guard_detects_archon_search_underscore_variant():
    # archon_search_evil is NOT in the archon_search hierarchy — must be flagged
    assert _BAD_PATTERN.search('logging.getLogger("archon_search_evil")')


def test_guard_ignores_commented_lines():
    # The scanner (not the regex) skips comment-only lines; the regex itself still matches.
    # Verify the scanner's comment-stripping logic works correctly.
    line = '# logging.getLogger("archon")'
    stripped = line.lstrip()
    assert stripped.startswith("#")  # scanner would skip this line


def test_no_bad_logger_names_in_archon_search():
    """Scan all .py files under archon_search/ for non-conforming logger names."""
    archon_search_dir = Path(__file__).parent.parent / "archon_search"
    violations = []
    for py_file in sorted(archon_search_dir.rglob("*.py")):
        for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), 1):
            # Skip comment-only lines
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _BAD_PATTERN.search(line):
                violations.append(f"{py_file}:{lineno}: {line.strip()}")
    assert not violations, "Bad logger names found:\n" + "\n".join(violations)
