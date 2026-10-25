"""Tests for pyproject.toml structure and optional extras."""

import tomllib
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement

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


# First release that clears pip-audit PYSEC-2026-3929 (5.8.1 carried it; PYSEC-2026-4174 has no listed fix).
_TRANSFORMERS_ADVISORY_FIXED = (5, 10, 0)


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
    (locked,) = versions
    assert tuple(int(x) for x in locked.split(".")[:3]) >= _TRANSFORMERS_ADVISORY_FIXED, (
        f"transformers {locked} carries PYSEC-2026-3929/-4174; the lock must resolve to "
        f">= {'.'.join(map(str, _TRANSFORMERS_ADVISORY_FIXED))}"
    )


_TORCH_FLOOR = "2.4.0"
_TORCH_BELOW_FLOOR = "2.3.1"
_TORCH_LOCKED = "2.13.0"


def test_torch_floor_is_the_oldest_torch_the_runtime_works_on():
    """The project needs Python >= 3.12, where torch < 2.4 cannot ``torch.compile``: ModernBERT
    (``granite-embedding-small-english-r2``) raises 'Dynamo is not supported on Python 3.12+' when
    it loads (torch 2.2.2 and 2.3.1 measured), and transformers 5.13 treats torch < 2.4 as absent.
    The declared range must not admit them (docling alone would allow torch >= 2.2.2)."""
    core_deps = _load_pyproject()["project"]["dependencies"]
    torch_specs = [Requirement(dep) for dep in core_deps if _pep508_package_name(dep) == "torch"]

    assert len(torch_specs) == 1
    specifier = torch_specs[0].specifier
    assert _TORCH_BELOW_FLOOR not in specifier
    assert _TORCH_FLOOR in specifier
    assert _TORCH_LOCKED in specifier


# (sys_platform, platform_machine) -> whether uv must take torch from the PyTorch CPU index.
# The CPU index serves `+cpu` wheels for linux x86_64 AND aarch64; PyPI's linux wheels (both
# archs) are the CUDA build. macOS and Windows PyPI wheels are already CPU-only.
_TORCH_CPU_INDEX_BY_PLATFORM = {
    ("linux", "x86_64"): True,
    ("linux", "aarch64"): True,
    ("darwin", "arm64"): False,
    ("darwin", "x86_64"): False,
    ("win32", "AMD64"): False,
}


@pytest.mark.parametrize("package", ["torch", "torchvision"])
@pytest.mark.parametrize(("platform", "machine"), list(_TORCH_CPU_INDEX_BY_PLATFORM))
def test_uv_torch_sources_pin_cpu_index_on_every_linux_arch(package, platform, machine):
    """`uv sync` on linux/arm64 (test-runner, dev-shell images) must not resolve CUDA torch."""
    source = _load_pyproject()["tool"]["uv"]["sources"][package]

    assert source["index"] == "pytorch-cpu"
    environment = {
        "sys_platform": platform,
        "platform_machine": machine,
        "platform_system": {"linux": "Linux", "darwin": "Darwin", "win32": "Windows"}[platform],
    }
    assert Marker(source["marker"]).evaluate(environment) is _TORCH_CPU_INDEX_BY_PLATFORM[
        (platform, machine)
    ]


def test_gliner_present():
    # The removed engine's absence from pyproject.toml and uv.lock is asserted by
    # test_removed_engine_absent_from_dependency_manifests (tests/test_graph_engine_stub.py),
    # not restated here — a test file may not name the removed engine (BE-16's
    # meta-guard). BE-21 later folds this into the repo-wide structural scan (S42).
    data = _load_pyproject()
    optional = data["project"].get("optional-dependencies", {})

    graph_deps = optional.get("graph", [])
    gliner_specs = [d for d in graph_deps if d.lower().startswith("gliner")]
    assert gliner_specs, "gliner must be present in [graph]"
    assert any(">=0.2.26" in d for d in gliner_specs), (
        f"gliner must have a version lower bound of >=0.2.26, got: {gliner_specs}"
    )

    with open(UV_LOCK_PATH, "rb") as f:
        lock = tomllib.load(f)
    lock_packages = lock.get("package", [])
    gliner_lock_pkgs = [p for p in lock_packages if p.get("name") == "gliner"]
    assert gliner_lock_pkgs, "gliner must be present in uv.lock"
    for pkg in gliner_lock_pkgs:
        version = tuple(int(x) for x in pkg["version"].split(".")[:3])
        assert version >= (0, 2, 26), f"gliner {pkg['version']} is below the >=0.2.26 floor"
