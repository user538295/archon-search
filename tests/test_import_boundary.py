"""Import boundary lint: no file in archon_search/ may import from `archon.`
(the main Archon daemon package). Only `archon_search.` imports are allowed.
"""

import ast
from pathlib import Path

ARCHON_SEARCH_PKG = Path(__file__).parent.parent / "archon_search"


def _collect_violations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[str] = []
    rel = path.relative_to(ARCHON_SEARCH_PKG)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "archon" or alias.name.startswith("archon."):
                    violations.append(f"{rel}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "archon" or module.startswith("archon."):
                names = ", ".join(a.name for a in node.names)
                violations.append(f"{rel}: from {module} import {names}")

    return violations


def test_no_archon_imports_in_archon_search() -> None:
    """archon_search/ must not import from the archon. namespace (daemon package)."""
    py_files = sorted(ARCHON_SEARCH_PKG.rglob("*.py"))
    assert py_files, "No .py files found — check ARCHON_SEARCH_PKG path"

    all_violations: list[str] = []
    for py_file in py_files:
        all_violations.extend(_collect_violations(py_file))

    assert not all_violations, (
        "archon_search/ must not import from archon. (daemon package) — "
        f"found {len(all_violations)} violation(s):\n" + "\n".join(all_violations)
    )
