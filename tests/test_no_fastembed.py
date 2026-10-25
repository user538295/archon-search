"""Guard: archon-search runs on PyTorch and no longer depends on fastembed (ADR 18)."""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = _ROOT / "archon_search"
_PACKAGE_NAME = "fastembed"


def _imported_top_level_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_no_module_under_archon_search_imports_fastembed() -> None:
    sources = sorted(_PACKAGE.rglob("*.py"))
    assert sources, f"no modules found under {_PACKAGE}"
    offenders = [
        str(path.relative_to(_ROOT))
        for path in sources
        if _PACKAGE_NAME in _imported_top_level_names(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


def test_pyproject_does_not_depend_on_fastembed() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        *project["project"]["dependencies"],
        *(dep for deps in project["project"].get("optional-dependencies", {}).values() for dep in deps),
        *(dep for group in project.get("dependency-groups", {}).values() for dep in group if isinstance(dep, str)),
    ]
    names = [re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower() for requirement in requirements]
    assert _PACKAGE_NAME not in names
