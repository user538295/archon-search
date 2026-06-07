"""Tests for Task 1.1 — [code] optional dep group and import isolation.

These tests verify that:
1. `code_enricher` module is importable without tree-sitter installed
2. `pyproject.toml` has the `code` optional-dependency group with required packages
"""

import sys
import tomllib
from pathlib import Path


def test_code_enricher_importable_without_tree_sitter(monkeypatch):
    """code_enricher must be importable even when tree-sitter packages are absent.

    Design constraint: no module-level tree-sitter import. All grammar loading
    is deferred to _get_grammar() which is called lazily at parse time.
    """
    # Remove any cached import of the module to force a fresh import
    modules_to_remove = [k for k in sys.modules if "code_enricher" in k]
    for m in modules_to_remove:
        monkeypatch.delitem(sys.modules, m)

    # Block tree-sitter packages so we can verify the module loads without them
    tree_sitter_packages = [
        "tree_sitter",
        "tree_sitter_python",
        "tree_sitter_typescript",
        "tree_sitter_javascript",
        "tree_sitter_go",
        "tree_sitter_rust",
        "tree_sitter_java",
        "tree_sitter_bash",
    ]
    for pkg in tree_sitter_packages:
        monkeypatch.setitem(sys.modules, pkg, None)  # type: ignore[arg-type]

    # Must not raise ImportError
    from archon_search.code_enricher import CODE_EXTENSIONS, CodeEnricher  # noqa: F401

    assert CodeEnricher is not None
    assert CODE_EXTENSIONS is not None


def test_code_optional_group_in_pyproject():
    """pyproject.toml must have a 'code' key in [project.optional-dependencies]
    with at least tree-sitter, tree-sitter-python, and tree-sitter-typescript.
    """
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    optional_deps = data.get("project", {}).get("optional-dependencies", {})
    assert "code" in optional_deps, "Missing 'code' optional-dependency group in pyproject.toml"

    code_deps = optional_deps["code"]
    package_names = [dep.split(">=")[0].split("~=")[0].split("==")[0].strip() for dep in code_deps]

    assert "tree-sitter" in package_names, "tree-sitter core missing from [code] deps"
    assert "tree-sitter-python" in package_names, "tree-sitter-python missing from [code] deps"
    assert "tree-sitter-typescript" in package_names, "tree-sitter-typescript missing from [code] deps"
