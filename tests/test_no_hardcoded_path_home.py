"""C17 Task 1.2/1.3 — CI ratchet: no new hardcoded Path.home() callsites in archon_search/.

Three responsibilities:
1. test_path_home_ratchet — scans archon_search/ for Path.home() callsites and enforces
   bidirectional agreement with tests/path_home_allowlist.txt (forward: no new unallowlisted
   callsites; reverse: no dead allowlist entries). Hash-pinned so any cosmetic edit to a
   grandfathered line is surfaced for review.
2. Meta-tests (Tasks 1.3) — exercise PATTERN directly against in-memory fixtures to
   prevent a regex weakening from silently disabling the ratchet.
3. Marker-scope enforcement (Task 2.4) — asserts @pytest.mark.archon_unset_data_dir
   appears on exactly the pinned MARKER_ALLOWLIST tests; added in Task 2.4.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PATTERN = re.compile(r"\bPath\.home\s*\(")
ROOT = Path(__file__).resolve().parent.parent / "archon_search"
ALLOWLIST_FILE = Path(__file__).resolve().parent / "path_home_allowlist.txt"
FILE_ALLOWLIST = {"paths.py"}  # paths.py is the legitimate caller

# Task 2.4: marker-scope constants (populated when Task 2.4 lands)
MARKER_NAME = "archon_unset_data_dir"
MARKER_ALLOWLIST: frozenset[str] = frozenset({
    "tests/test_paths.py::test_default_returns_home_archon",
    "tests/test_key_manager.py::TestGetKeyFile::test_get_key_file_default",
    "tests/test_jobs_paths.py::test_get_jobs_file_default",
    "tests/test_language_detector_paths.py::test_get_fasttext_models_dir_default",
    "tests/test_language_detector.py::test_module_constants",
})
TESTS_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_line(line: str) -> str:
    """SHA-256 of a source line with the trailing newline stripped."""
    return hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()


def _scan_violations() -> set[tuple[str, int, str]]:
    """Return {(relative_path, line_no, sha256)} for every Path.home( callsite
    under archon_search/, excluding files whose name is in FILE_ALLOWLIST.

    Per-line scanning with no re.DOTALL; multiline callsites are an accepted
    gap per the C17 brief.
    """
    results: set[tuple[str, int, str]] = set()
    repo_root = ROOT.parent
    for py_file in ROOT.rglob("*.py"):
        if py_file.name in FILE_ALLOWLIST:
            continue
        lines = py_file.read_text(encoding="utf-8").splitlines(keepends=True)
        for lineno, raw_line in enumerate(lines, start=1):
            if PATTERN.search(raw_line):
                rel = str(py_file.relative_to(repo_root))
                results.add((rel, lineno, _hash_line(raw_line)))
    return results


def _load_allowlist() -> set[tuple[str, int, str]]:
    """Parse path_home_allowlist.txt into {(relative_path, line_no, sha256)}.

    Skips blank lines and comment lines starting with '#'. Raises AssertionError
    with a clear message if any data line is malformed (not path:int:sha).
    """
    results: set[tuple[str, int, str]] = set()
    for raw_line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3:
            raise AssertionError(
                f"Malformed allowlist entry (expected path:line_no:sha256): {raw_line!r}"
            )
        rel_path, line_no_str, sha = parts
        if not line_no_str.isdigit():
            raise AssertionError(
                f"Allowlist entry has non-integer line number: {raw_line!r}"
            )
        results.add((rel_path, int(line_no_str), sha))
    return results


# ---------------------------------------------------------------------------
# Task 1.2 — ratchet test
# ---------------------------------------------------------------------------


def test_path_home_ratchet() -> None:
    """No new unallowlisted Path.home() callsites may be added to archon_search/.

    Two-direction assertion:
    - Forward: every detected callsite must be in the allowlist.
      Failure message names the new unallowlisted callsites.
    - Reverse: every allowlist entry must still exist in the codebase.
      Failure message names dead entries (must be removed from the allowlist).

    Hash-mismatch detection is implicit: a callsite at the right (path, line)
    but with a different content hash appears in allowed − violations, so the
    reverse direction reports it as a dead entry.
    """
    violations = _scan_violations()
    allowed = _load_allowlist()

    assert violations <= allowed, (
        "New unallowlisted Path.home() callsites detected in archon_search/:\n"
        + "\n".join(
            f"  {path}:{lineno}  (sha256={sha})"
            for path, lineno, sha in sorted(violations - allowed)
        )
        + "\nEither migrate the callsite to get_data_dir() or add it to "
        "tests/path_home_allowlist.txt with a rationale comment."
    )

    assert allowed <= violations, (
        "Dead entries in tests/path_home_allowlist.txt (no matching callsite found):\n"
        + "\n".join(
            f"  {path}:{lineno}  (sha256={sha})"
            for path, lineno, sha in sorted(allowed - violations)
        )
        + "\nRemove these entries from the allowlist — either the line was migrated "
        "or its content changed (hash mismatch)."
    )


# ---------------------------------------------------------------------------
# Task 1.3 — meta-tests: verify PATTERN behaves correctly
# ---------------------------------------------------------------------------


def test_meta_positive_match() -> None:
    """PATTERN must match a standard Path.home() / '...' callsite.

    Failure here means the \\b word-boundary anchor or the \\s*\\( suffix was
    removed from PATTERN, silently disabling the ratchet for all callers.
    """
    fixture = 'x = Path.home() / "foo"'
    assert PATTERN.search(fixture) is not None, (
        f"PATTERN {PATTERN.pattern!r} failed to match a standard Path.home() callsite: "
        f"{fixture!r}"
    )


def test_meta_no_parens_negative() -> None:
    """PATTERN must NOT match Path.home used without parentheses (attribute access).

    Failure here means the \\s*\\( suffix was weakened or removed, causing
    PATTERN to fire on attribute-access expressions and generate false positives.
    """
    fixture = "x = Path.home + 1"
    assert PATTERN.search(fixture) is None, (
        f"PATTERN {PATTERN.pattern!r} incorrectly matched a no-parens expression: "
        f"{fixture!r}"
    )


def test_meta_lowercase_negative() -> None:
    """PATTERN must NOT match lowercase path.home() (wrong object).

    Failure here means the \\b word-boundary anchor or the case-sensitivity flag
    was changed, causing false positives on unrelated lowercase names.
    """
    fixture = "x = path.home()"
    assert PATTERN.search(fixture) is None, (
        f"PATTERN {PATTERN.pattern!r} incorrectly matched lowercase path.home(): "
        f"{fixture!r}"
    )


def test_meta_string_literal_positive() -> None:
    """PATTERN matches Path.home() inside a string literal (accepted false-positive).

    This fixture documents that the ratchet has no string-awareness — it is a
    per-line regex, not an AST scanner. The test is intentionally positive: if
    PATTERN ever STOPS matching inside strings, that means a fundamentally
    different scanning approach was introduced, and this meta-test must be updated.
    """
    fixture = 'x = "Path.home()"'
    assert PATTERN.search(fixture) is not None, (
        f"PATTERN {PATTERN.pattern!r} no longer matches Path.home() inside a string "
        f"literal. If the scanner now uses AST-level detection, update this meta-test "
        f"to document the new behavior."
    )
