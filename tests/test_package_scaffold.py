"""Tests for Task 1.1: package scaffold verification."""

from pathlib import Path


def test_package_importable() -> None:
    """import archon_search succeeds after uv sync."""
    import archon_search  # noqa: F401

    assert archon_search is not None


def test_entry_point_defined() -> None:
    """pyproject.toml contains the correct entry point."""
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject_path.read_text()
    assert 'archon-search = "archon_search.cli.main:main"' in content
