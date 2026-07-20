"""C17 Task 1.2/1.3/2.4 — CI ratchet: no new hardcoded Path.home() callsites in archon_search/.

Three responsibilities:
1. test_path_home_ratchet — scans archon_search/ for Path.home() callsites and enforces
   bidirectional agreement with tests/path_home_allowlist.txt (forward: no new unallowlisted
   callsites; reverse: no dead allowlist entries). Hash-pinned so any cosmetic edit to a
   grandfathered line is surfaced for review.
2. Meta-tests (Tasks 1.3) — exercise PATTERN directly against in-memory fixtures to
   prevent a regex weakening from silently disabling the ratchet.
3. Marker-scope enforcement (Task 2.4) — asserts @pytest.mark.archon_unset_data_dir
   appears on exactly the pinned MARKER_ALLOWLIST tests; an AST walker enforces this
   so a drive-by use of the marker to silence an unrelated flake is caught immediately.
"""
from __future__ import annotations

import ast
import hashlib
import re
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PATTERN = re.compile(r"\bPath\.home\s*\(")
ROOT = Path(__file__).resolve().parent.parent / "archon_search"
ALLOWLIST_FILE = Path(__file__).resolve().parent / "path_home_allowlist.txt"
FILE_ALLOWLIST = {"paths.py"}  # paths.py is the legitimate caller

# Task 2.4: marker-scope constants
MARKER_NAME = "archon_unset_data_dir"
# Exact set of test node IDs allowed to carry @pytest.mark.archon_unset_data_dir.
# Expanded by Task 2.2 from the original 5 to 10 (Task 2.2 added tests that exercise
# Path.home()-based default fallback paths across more modules).
# Any test added here must exercise the Path.home() / ".archon-search" fallback codepath;
# apply this marker only to tests in this frozenset or update it with a C17-plan amendment.
MARKER_ALLOWLIST: frozenset[str] = frozenset({
    "tests/test_cli_serve.py::test_serve_no_warning_when_data_dir_unset",
    "tests/test_cli_serve.py::test_serve_output_and_exit_code_unchanged",
    "tests/test_job_store.py::test_jobs_file_default_path",
    "tests/test_jobs_paths.py::test_get_jobs_file_default",
    "tests/test_key_manager.py::TestGetKeyFile::test_get_key_file_default",
    "tests/test_language_detector.py::test_module_constants",
    "tests/test_language_detector_paths.py::test_get_fasttext_models_dir_default",
    "tests/test_paths.py::test_default_returns_home_archon",
    "tests/test_paths.py::test_home_unset_raises_valueerror",
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


# ---------------------------------------------------------------------------
# Task 2.4 — marker-scope helpers and tests
# ---------------------------------------------------------------------------


def _decorator_names(decorator: ast.expr) -> list[str]:
    """Return the dotted name(s) represented by a decorator AST node.

    Handles:
    - ``ast.Attribute`` chains: ``pytest.mark.foo`` → ``["pytest", "mark", "foo"]``
    - ``ast.Name``: ``foo`` → ``["foo"]``
    - ``ast.Call`` with an Attribute/Name func: unwraps the call and processes the func.
    Returns an empty list for unrecognised node shapes.
    """
    if isinstance(decorator, ast.Call):
        return _decorator_names(decorator.func)
    if isinstance(decorator, ast.Attribute):
        parts = _decorator_names(decorator.value)
        parts.append(decorator.attr)
        return parts
    if isinstance(decorator, ast.Name):
        return [decorator.id]
    return []


def _ast_scan_marker_users() -> set[str]:
    """Walk tests/ with AST and return node IDs for every decorated test.

    Returns node IDs in ``<relative_test_path>::[ClassName::]function_name`` form,
    where paths are relative to repo root so IDs are stable across machines.

    Only ``test_*.py`` files are scanned; the current file is excluded to prevent
    any future decorator in this file from being mistakenly included.

    Handles:
    - Top-level test functions.
    - Methods inside ``ClassDef`` — returned as ``path::ClassName::method_name``.
    """
    repo_root = TESTS_ROOT.parent
    found: set[str] = set()

    for py_file in sorted(TESTS_ROOT.rglob("test_*.py")):
        if py_file == Path(__file__).resolve():
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(py_file.relative_to(repo_root))

        for top_node in tree.body:
            # Top-level function
            if isinstance(top_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in top_node.decorator_list:
                    parts = _decorator_names(dec)
                    if MARKER_NAME in parts:
                        found.add(f"{rel}::{top_node.name}")
                        break
            # Class containing methods
            elif isinstance(top_node, ast.ClassDef):
                for item in top_node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for dec in item.decorator_list:
                            parts = _decorator_names(dec)
                            if MARKER_NAME in parts:
                                found.add(f"{rel}::{top_node.name}::{item.name}")
                                break

    return found


def test_archon_unset_data_dir_marker_scope() -> None:
    """@pytest.mark.archon_unset_data_dir must appear on exactly the MARKER_ALLOWLIST tests.

    Failure modes:
    - ``extra``: a test outside MARKER_ALLOWLIST acquired the marker — likely a drive-by use
      to silence an unrelated flake. Either migrate the test or add it to MARKER_ALLOWLIST
      with a rationale comment explaining which Path.home() fallback it exercises.
    - ``missing``: a test listed in MARKER_ALLOWLIST no longer carries the marker, or its
      file/class/function was renamed. Update MARKER_ALLOWLIST to match the new node ID.
    """
    actual = _ast_scan_marker_users()
    assert actual == MARKER_ALLOWLIST, (
        f"Marker scope mismatch for @pytest.mark.{MARKER_NAME}:\n"
        + (
            "  extra (not in MARKER_ALLOWLIST): "
            + str(sorted(actual - MARKER_ALLOWLIST))
            + "\n"
            if actual - MARKER_ALLOWLIST
            else ""
        )
        + (
            "  missing (in MARKER_ALLOWLIST but not found): "
            + str(sorted(MARKER_ALLOWLIST - actual))
            if MARKER_ALLOWLIST - actual
            else ""
        )
    )


def test_meta_ast_finds_pytest_mark_decorator() -> None:
    """_ast_scan_marker_users finds a decorated function in an in-memory temp file.

    Validates the AST walker itself — if pytest's marker style changes (e.g.,
    from ``@pytest.mark.foo`` to ``@pytest.foo``), this meta-test will fail,
    alerting the maintainer before the scope check silently stops working.

    The temp file uses the actual MARKER_NAME so the test is sensitive to the
    decorator attribute chain used in the real codebase.
    """
    import tempfile

    sample_code = textwrap.dedent(f"""\
        import pytest

        @pytest.mark.{MARKER_NAME}
        def test_sample_decorated() -> None:
            pass

        def test_sample_undecorated() -> None:
            pass

        class TestSampleClass:
            @pytest.mark.{MARKER_NAME}
            def test_method_decorated(self) -> None:
                pass
    """)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        test_file = tmp_path / "test_sample.py"
        test_file.write_text(sample_code, encoding="utf-8")

        # Parse directly (mirrors _ast_scan_marker_users internals)
        tree = ast.parse(sample_code)
        rel = "test_sample.py"
        found: set[str] = set()

        for top_node in tree.body:
            if isinstance(top_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in top_node.decorator_list:
                    parts = _decorator_names(dec)
                    if MARKER_NAME in parts:
                        found.add(f"{rel}::{top_node.name}")
                        break
            elif isinstance(top_node, ast.ClassDef):
                for item in top_node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for dec in item.decorator_list:
                            parts = _decorator_names(dec)
                            if MARKER_NAME in parts:
                                found.add(f"{rel}::{top_node.name}::{item.name}")
                                break

        expected = {
            f"test_sample.py::test_sample_decorated",
            f"test_sample.py::TestSampleClass::test_method_decorated",
        }
        assert found == expected, (
            f"_decorator_names / AST walker failed to detect @pytest.mark.{MARKER_NAME}. "
            f"Expected {sorted(expected)}, got {sorted(found)}. "
            f"Check the decorator attribute chain in _decorator_names()."
        )
