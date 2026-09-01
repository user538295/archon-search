"""Tests for pyproject.toml structure and optional extras."""

import tomllib
from pathlib import Path


PYPROJECT_PATH = Path(__file__).parent.parent / "pyproject.toml"
UV_LOCK_PATH = Path(__file__).parent.parent / "uv.lock"


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


def _pep508_package_name(spec: str) -> str:
    """Extract the package name from a PEP 508 spec like 'transformers>=4.51.3,<5.14.0'."""
    for sep in (">=", "<", ">", "==", "!="):
        if sep in spec:
            return spec.split(sep, 1)[0].strip()
    return spec.strip()


def test_transformers_constraint_present_via_tomllib():
    data = _load_pyproject()
    constraints = data.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    transformers_specs = [c for c in constraints if _pep508_package_name(c) == "transformers"]
    assert transformers_specs, "transformers must be pinned in [tool.uv] constraint-dependencies"
    assert len(transformers_specs) == 1, (
        f"expected exactly one transformers constraint entry, got: {transformers_specs}"
    )
    spec = transformers_specs[0]
    assert ";" not in spec, (
        f"transformers constraint must be cross-platform, not marker-scoped, got: {spec}"
    )
    assert ">=4.51.3" in spec, f"transformers constraint must have lower bound >=4.51.3, got: {spec}"
    assert "<5.14.0" in spec, f"transformers constraint must have upper bound <5.14.0, got: {spec}"


def test_constraint_reason_comment_present_in_raw_text():
    lines = PYPROJECT_PATH.read_text().splitlines()
    target_idx = next(i for i, line in enumerate(lines) if "constraint-dependencies = [" in line)
    comment_lines = []
    i = target_idx - 1
    while i >= 0 and lines[i].strip().startswith("#"):
        comment_lines.append(lines[i])
        i -= 1
    comment_text = "\n".join(comment_lines).lower()
    assert "gliner" in comment_text, "the comment above constraint-dependencies must mention gliner"
    assert "transformers" in comment_text, (
        "the comment above constraint-dependencies must mention transformers"
    )


def test_onnxruntime_absent_from_graph_extra():
    data = _load_pyproject()
    extras = data["project"].get("optional-dependencies", {})
    graph_deps = extras.get("graph", [])
    assert not any("onnxruntime" in dep.lower() for dep in graph_deps), (
        "onnxruntime must not be declared in the graph extra (gliner pulls it in transitively)"
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


def test_lock_matches_the_chosen_transformers_strategy():
    """uv.lock must resolve transformers to one converged version within the ceiling."""
    with open(UV_LOCK_PATH, "rb") as f:
        lock = tomllib.load(f)
    packages = [p for p in lock.get("package", []) if p.get("name") == "transformers"]
    assert packages, "transformers must be present in uv.lock"
    for pkg in packages:
        version = tuple(int(x) for x in pkg["version"].split(".")[:3])
        assert version >= (4, 51, 3), f"transformers {pkg['version']} is below the >=4.51.3 floor"
        assert version < (5, 14, 0), f"transformers {pkg['version']} is at/above the <5.14.0 ceiling"
    versions = {pkg["version"] for pkg in packages}
    assert len(versions) == 1, (
        f"transformers must converge to a single version across all platforms, got: {versions}"
    )
    assert versions == {"5.8.1"}, (
        f"transformers must resolve to the K2 spike's converged version 5.8.1, got: {versions}"
    )


def test_gliner_present_and_spacy_absent():
    data = _load_pyproject()
    optional = data["project"].get("optional-dependencies", {})
    core_deps = data["project"].get("dependencies", [])
    dev_deps = data.get("dependency-groups", {}).get("dev", [])

    graph_deps = optional.get("graph", [])
    gliner_specs = [d for d in graph_deps if d.lower().startswith("gliner")]
    assert gliner_specs, "gliner must be present in [graph]"
    assert any(">=0.2.26" in d for d in gliner_specs), (
        f"gliner must have a version lower bound of >=0.2.26, got: {gliner_specs}"
    )

    def _no_spacy(deps: list[str], where: str) -> None:
        assert not any(d.lower().startswith("spacy") for d in deps), f"spacy must not appear in {where}"

    _no_spacy(core_deps, "[project.dependencies]")
    _no_spacy(dev_deps, "dependency-groups.dev")
    for extra_name, extra_deps in optional.items():
        _no_spacy(extra_deps, f"optional-dependencies[{extra_name!r}]")

    assert not any("en-core-web-sm" in d for d in dev_deps), (
        "en-core-web-sm must not appear in dependency-groups.dev"
    )
    uv_sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    assert "en-core-web-sm" not in uv_sources, "en-core-web-sm must not appear in [tool.uv.sources]"

    with open(UV_LOCK_PATH, "rb") as f:
        lock = tomllib.load(f)
    lock_packages = lock.get("package", [])
    lock_names = {p.get("name") for p in lock_packages}
    assert "spacy" not in lock_names, "spacy must not appear in uv.lock"
    assert "en-core-web-sm" not in lock_names, "en-core-web-sm must not appear in uv.lock"

    gliner_lock_pkgs = [p for p in lock_packages if p.get("name") == "gliner"]
    assert gliner_lock_pkgs, "gliner must be present in uv.lock"
    for pkg in gliner_lock_pkgs:
        version = tuple(int(x) for x in pkg["version"].split(".")[:3])
        assert version >= (0, 2, 26), f"gliner {pkg['version']} is below the >=0.2.26 floor"
