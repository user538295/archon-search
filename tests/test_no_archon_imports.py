"""
Unit: test_no_archon_config_imports — archon.config.loader not imported anywhere in archon_search/
"""

from pathlib import Path


ARCHON_SEARCH_PKG = Path(__file__).parent.parent / "archon_search"

FORBIDDEN_PATTERNS = [
    "from archon.config",
    "import archon.config",
]

# Files that are known to still need migration in later tasks
_PENDING_MIGRATION = {"pipeline.py", "mcp.py"}


def _collect_py_files() -> list[Path]:
    return sorted(ARCHON_SEARCH_PKG.rglob("*.py"))


def _file_contains_forbidden_import(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    hits = []
    for line in source.splitlines():
        stripped = line.strip()
        for pattern in FORBIDDEN_PATTERNS:
            if stripped.startswith(pattern):
                hits.append(f"{path.relative_to(ARCHON_SEARCH_PKG)}: {stripped}")
    return hits


def test_no_archon_config_imports() -> None:
    """No file in archon_search/ (outside pending migration list) may import from archon.config."""
    py_files = _collect_py_files()
    assert py_files, "No .py files found — check ARCHON_SEARCH_PKG path"

    violations: list[str] = []
    for py_file in py_files:
        if py_file.name in _PENDING_MIGRATION:
            continue
        violations.extend(_file_contains_forbidden_import(py_file))

    assert not violations, (
        "archon_search/ must not import from archon.config — "
        f"found {len(violations)} violation(s):\n" + "\n".join(violations)
    )
