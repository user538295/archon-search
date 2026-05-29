"""B5 Task 2.4 — CI guard: no _do_*_unlocked call from non-_do_* methods.

Two tests:
- test_no_public_method_calls_unlocked_helper: AST-scans the real store.py and
  asserts no violation exists.
- test_no_unlocked_call_violation: synthesises a minimal offending module string,
  runs the same guard against it, and asserts the guard detects the violation
  (negative test — verifies the guard is not a silent no-op).
"""
from __future__ import annotations

import ast
import textwrap
from collections import deque
from pathlib import Path

# ---------------------------------------------------------------------------
# Allowed exceptions: methods that acquire _lock_for(collection) themselves
# before delegating to _unlocked helpers.  The guard skips these names.
# ---------------------------------------------------------------------------

_ALLOWED_CALLERS = frozenset(
    {
        "update_collection_meta",  # acquires _lock_for; calls _do_write_meta_unlocked
        "delete_document",         # acquires _lock_for; calls _do_fetch_doc_vectors_unlocked / _do_subtract_meta_on_delete
        "update_description",      # Task 5.1; acquires _lock_for; calls _do_read_meta_unlocked / _do_write_meta_unlocked
    }
)


def _iter_body_calls(func_node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Yield Call nodes from func_node's body without descending into nested functions.

    ast.walk() recurses into nested FunctionDef/AsyncFunctionDef — this would
    cause false positives when a non-_do_ method contains a nested _do_* helper
    that legally calls _unlocked helpers.  We use an explicit BFS queue and stop
    descending when we hit a nested function boundary.
    """
    queue: deque[ast.AST] = deque(func_node.body)
    while queue:
        node = queue.popleft()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # do not descend into nested functions
        if isinstance(node, ast.Call):
            yield node
        queue.extend(ast.iter_child_nodes(node))


# ---------------------------------------------------------------------------
# Guard implementation
# ---------------------------------------------------------------------------


def _find_violations(source: str, filename: str = "<string>") -> list[str]:
    """Return a list of human-readable violation strings.

    A violation is: a method whose name does NOT start with ``_do_`` (and is
    not in ``_ALLOWED_CALLERS``) that contains a direct ``self.<x>_unlocked(…)``
    call in its own body (nested function bodies are excluded to avoid false
    positives from ``_do_*`` closures).
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        enclosing_name: str = node.name
        if enclosing_name.startswith("_do_"):
            continue  # _do_* methods are allowed to call _unlocked helpers
        if enclosing_name in _ALLOWED_CALLERS:
            continue  # explicitly whitelisted lock-acquiring callers

        for call in _iter_body_calls(node):
            func = call.func
            # Match self.<attr>_unlocked(…) — Attribute call whose attr ends with _unlocked
            if (
                isinstance(func, ast.Attribute)
                and func.attr.endswith("_unlocked")
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                violations.append(
                    f"  line {call.lineno}: {enclosing_name!r} calls self.{func.attr}()"
                )

    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_public_method_calls_unlocked_helper() -> None:
    """store.py must not contain any non-_do_* method directly calling an _unlocked helper.

    On failure the assertion message names the offending method and line number
    so a contributor can find the violation immediately.
    """
    store_path = Path(__file__).parent.parent / "archon_search" / "store.py"
    source = store_path.read_text(encoding="utf-8")

    violations = _find_violations(source, filename=str(store_path))

    assert not violations, (
        "Unlocked-helper call violations found in archon_search/store.py:\n"
        + "\n".join(violations)
        + "\nOnly _do_* methods (or the explicitly whitelisted lock-acquiring callers) "
        "may call _unlocked helpers."
    )


def test_no_unlocked_call_violation() -> None:
    """The guard must detect a method that directly calls an _unlocked helper.

    Synthesises a minimal module with a non-_do_ method that calls
    self._do_write_meta_unlocked() and asserts _find_violations returns a
    non-empty list — preventing the guard from silently becoming a no-op.
    """
    bad_source = textwrap.dedent(
        """\
        class Store:
            def bad(self):
                self._do_write_meta_unlocked(db, col, meta)
        """
    )
    violations = _find_violations(bad_source, filename="<bad_source>")
    assert violations, (
        "Guard failed to detect a violation: a non-_do_ method calling "
        "self._do_write_meta_unlocked() was not flagged"
    )


def test_no_false_positive_for_nested_do_function() -> None:
    """Guard must NOT flag a public method that contains a nested _do_* function.

    The nested _do_* function is allowed to call _unlocked helpers; the outer
    public method itself makes no such call.  Prior to the BFS fix, ast.walk()
    descended into nested functions and blamed the outer method.
    """
    source = textwrap.dedent(
        """\
        class Store:
            def outer_public(self):
                async def _do_inner(self):
                    self._do_write_meta_unlocked(db, col, meta)
        """
    )
    violations = _find_violations(source, filename="<nested_source>")
    assert not violations, (
        "False positive: nested _do_* function's unlocked call was incorrectly "
        f"attributed to the outer public method: {violations}"
    )
