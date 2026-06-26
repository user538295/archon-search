"""Tests for pyproject.toml structure and optional extras."""

import tomllib
from pathlib import Path


PYPROJECT_PATH = Path(__file__).parent.parent / "pyproject.toml"


def _load_pyproject() -> dict:
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


def test_multilingual_extra_declared():
    data = _load_pyproject()
    extras = data["project"].get("optional-dependencies", {})
    assert "multilingual" in extras, "multilingual optional extra must be declared"
    packages = extras["multilingual"]
    assert any("fasttext-wheel" in pkg for pkg in packages), (
        "multilingual extra must include fasttext-wheel"
    )
    assert any("fasttext-wheel>=0.9.2" in pkg for pkg in packages), (
        "fasttext-wheel must have a version lower bound of >=0.9.2"
    )


def test_multilingual_extra_not_in_dev_or_all():
    data = _load_pyproject()
    optional = data["project"].get("optional-dependencies", {})
    dev_deps = data.get("dependency-groups", {}).get("dev", [])
    all_deps = optional.get("all", [])
    assert not any("fasttext-wheel" in dep for dep in dev_deps), (
        "fasttext-wheel must not appear in dev dependencies"
    )
    assert not any("fasttext-wheel" in dep for dep in all_deps), (
        "fasttext-wheel must not appear in all extras"
    )


def test_markitdown_declared_as_core_dep():
    """markitdown must appear in [project.dependencies], not in optional-dependencies."""
    data = _load_pyproject()
    core_deps = data["project"].get("dependencies", [])
    optional_deps = data["project"].get("optional-dependencies", {})
    dev_deps = data.get("dependency-groups", {}).get("dev", [])

    assert any("markitdown" in dep for dep in core_deps), (
        "markitdown must be declared in [project.dependencies] (core, not optional)"
    )
    assert any("markitdown>=0.1.6,<0.2" in dep for dep in core_deps), (
        "markitdown must have version spec >=0.1.6,<0.2"
    )
    assert not any("markitdown" in dep for dep in dev_deps), (
        "markitdown must not appear in dependency-groups.dev"
    )
    for extra_name, extra_deps in optional_deps.items():
        assert not any("markitdown" in dep for dep in extra_deps), (
            f"markitdown must not appear in optional-dependencies[{extra_name!r}]"
        )


def test_olefile_declared_as_core_dep():
    """olefile must appear in [project.dependencies].

    olefile is not a hard transitive dep of markitdown; it is only in markitdown's
    [all] optional extra. We declare it explicitly so .msg ingestion works on a
    fresh `uv sync --dev` without extra steps.
    """
    data = _load_pyproject()
    core_deps = data["project"].get("dependencies", [])
    optional_deps = data["project"].get("optional-dependencies", {})
    dev_deps = data.get("dependency-groups", {}).get("dev", [])

    assert any("olefile" in dep for dep in core_deps), (
        "olefile must be declared in [project.dependencies] (markitdown's .msg support)"
    )
    assert any("olefile>=0.46" in dep for dep in core_deps), (
        "olefile must have a version lower bound of >=0.46"
    )
    assert not any("olefile" in dep for dep in dev_deps), (
        "olefile must not appear in dependency-groups.dev"
    )
    for extra_name, extra_deps in optional_deps.items():
        assert not any("olefile" in dep for dep in extra_deps), (
            f"olefile must not appear in optional-dependencies[{extra_name!r}]"
        )
