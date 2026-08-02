"""Unit tests for the pure ``_source_path_matches`` matcher — bug S68 fix.

Tiered precedence (see ``_source_path_matches`` docstring):
- a directory-qualified query matches only by exact string or path-suffix,
  never basename (so it can't cross into an unrelated directory);
- a bare-basename query matches by exact string or basename;
- comparisons are case-insensitive (macOS-primary case-insensitive FS);
- empty/whitespace query or ``None``/empty stored path never matches.
"""
from __future__ import annotations

import pytest

from archon_search.graph_store import _source_path_matches


@pytest.mark.parametrize(
    ("stored", "query", "expected"),
    [
        # exact match
        ("/proj/sub/auth.py", "/proj/sub/auth.py", True),
        # bare basename query matches stored absolute path
        ("/proj/sub/auth.py", "auth.py", True),
        # path-suffix match
        ("/proj/sub/auth.py", "sub/auth.py", True),
        # FIX 1: directory-qualified query must NOT fall through to basename
        # against a different directory.
        ("/proj/other/dir/auth.py", "sub/auth.py", False),
        # non-boundary suffix rejected (must match at a path separator)
        ("/x/helpers_a.py", "lpers_a.py", False),
        # FIX 1 boundary: the suffix must align on a "/" segment boundary — a
        # dir whose name ENDS WITH the query's leading segment must NOT match
        # (guards against dropping the leading "/" in the endswith check).
        ("/proj/mysub/auth.py", "sub/auth.py", False),
        # mixed separators: query uses backslash, stored uses forward slash
        ("/a/sub/x.py", "sub\\x.py", True),
        ("C:\\a\\sub\\x.py", "sub/x.py", True),
        # scope limit: a full-path query with foreign separators is NOT
        # normalized before the exact-equality check, and the suffix tier
        # requires a leading "/" a full path can't satisfy → no match. Pinned
        # to document that full-path-with-mixed-separators equality is out of
        # scope (the route passes relative-ish file_path in practice).
        ("C:/a/sub/x.py", "C:\\a\\sub\\x.py", False),
        # FIX 3: case-insensitive matching
        ("/x/helpers_a.py", "Helpers_A.py", True),
        # empty / whitespace query never matches
        ("/x/y.py", "", False),
        ("/x/y.py", "   ", False),
        # stored None never matches
        (None, "y.py", False),
        # stored empty string never matches
        ("", "y.py", False),
    ],
)
def test_sourcePathMatches(stored: str | None, query: str, expected: bool) -> None:
    assert _source_path_matches(stored, query) is expected
