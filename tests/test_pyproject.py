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
    assert any("markitdown[docx,pptx,xls,xlsx,outlook]>=0.1.6,<0.2" in dep for dep in core_deps), (
        "markitdown must declare extras [docx,pptx,xls,xlsx,outlook] to pull in: "
        "mammoth+lxml (docx), python-pptx (pptx), xlrd+pandas (xls), openpyxl+pandas (xlsx), olefile (outlook/.msg)"
    )
    assert not any("markitdown" in dep for dep in dev_deps), (
        "markitdown must not appear in dependency-groups.dev"
    )
    for extra_name, extra_deps in optional_deps.items():
        assert not any("markitdown" in dep for dep in extra_deps), (
            f"markitdown must not appear in optional-dependencies[{extra_name!r}]"
        )


def test_olefile_covered_via_markitdown_outlook_extra():
    """olefile (needed for .msg ingestion) must be reachable via markitdown[outlook].

    markitdown's [outlook] extra declares olefile as its dependency. We include
    the [outlook] extra in our markitdown dep spec so .msg ingestion works on a
    fresh `uv sync --dev` without a separate olefile declaration.
    """
    data = _load_pyproject()
    core_deps = data["project"].get("dependencies", [])

    # olefile is pulled in transitively via markitdown[outlook]; no standalone line needed
    assert any("markitdown[" in dep and "outlook" in dep for dep in core_deps), (
        "markitdown dep must include [outlook] extra so olefile is available for .msg ingestion"
    )
