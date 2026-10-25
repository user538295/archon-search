"""Container tests for the GPU image (``:gpu``).

These tests build the ``:gpu`` image exactly as the release workflow does
(amd64 only, ``BASE_IMAGE`` from the workflow's ``GPU_BASE_IMAGE``) and check,
without a GPU, that it ships a CUDA build of PyTorch on the base image's CUDA
major and no ``onnxruntime-gpu``. Like ``tests/test_docker_smoke.py`` they are gated
behind ``@pytest.mark.docker`` and the ``ARCHON_SEARCH_RUN_DOCKER_SMOKE``
opt-in env var, and no CI workflow runs them (no GPU runner; the image build
step is the gate). ``release.sh``'s docker lane runs them as part of the local
pre-tag gate; to run them alone::

    ARCHON_SEARCH_RUN_DOCKER_SMOKE=1 uv run pytest tests/test_docker_gpu_image.py \\
        --no-cov -o addopts= --strict-markers --strict-config -n0 -m docker
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests._docker_support import (
    REPO_ROOT,
    SMOKE_OPT_IN_ENV,
    docker_available,
    smoke_opted_in,
)

RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "archon-search-release.yml"
GPU_IMAGE_TAG = "archon-search:gpu-smoke-test"
# The release workflow builds the GPU image for amd64 only.
GPU_PLATFORM = "linux/amd64"
# A cold build pulls the multi-GB CUDA base plus CUDA torch, under emulation on arm64 hosts.
GPU_BUILD_TIMEOUT_S = 3600
GPU_PROBE_TIMEOUT_S = 600

# Probe program sent to `python3 -` in the container over stdin (not a test module: no `test_` prefix).
GPU_IMAGE_PROBE = REPO_ROOT / "tests" / "_gpu_image_probe.py"
# Prefixes the probes' JSON line, so nothing else on stdout (entrypoint logs, pip output) can be mistaken for it.
PROBE_JSON_SENTINEL = "ARCHON_PROBE_JSON:"
# The Dockerfile's CUDA-major guard, as COPY . /app ships it into the image.
CUDA_MAJOR_GUARD = "/app/scripts/check-gpu-cuda-majors.sh"
CUDA_MAJOR_GUARD_FAILURE = "CUDA majors differ or are missing"
# A base image CUDA_VERSION whose major differs from the CUDA 13 stack the image ships.
MISMATCHED_BASE_CUDA_VERSION = "12.1.1"
# A stand-in for a CPU torch build, put first on PYTHONPATH: CPU builds report
# `torch.version.cuda` as None.
CPU_TORCH_SHIM_DIR = "/tmp/cpu-torch-shim"
CPU_TORCH_SHIM_SETUP = (
    f"mkdir -p {CPU_TORCH_SHIM_DIR}/torch && "
    f"printf 'class version:\\n    cuda = None\\n' > {CPU_TORCH_SHIM_DIR}/torch/__init__.py && "
)


def _read_release_gpu_base_image() -> str:
    """The ``GPU_BASE_IMAGE`` the release workflow's docker job builds the GPU image from."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    return workflow["jobs"]["docker"]["env"]["GPU_BASE_IMAGE"]


def _docker_run_json(args: list[str], timeout_s: int, probe: Path) -> dict:
    """``docker run -i --rm`` on the GPU platform with *probe*'s source on stdin;
    return the JSON of the stdout line the probe prefixes with :data:`PROBE_JSON_SENTINEL`."""
    proc = subprocess.run(
        ["docker", "run", "-i", "--rm", "--platform", GPU_PLATFORM, *args],
        input=probe.read_text(),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )
    assert proc.returncode == 0, (
        f"docker run exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    json_lines = [line for line in proc.stdout.splitlines() if line.startswith(PROBE_JSON_SENTINEL)]
    assert len(json_lines) == 1, f"expected one {PROBE_JSON_SENTINEL} line\n--- stdout ---\n{proc.stdout}"
    return json.loads(json_lines[0].removeprefix(PROBE_JSON_SENTINEL))


def _run_cuda_major_guard(gpu_image: str, *env_args: str, setup: str = "") -> subprocess.CompletedProcess[str]:
    """Run the Dockerfile's CUDA-major guard script inside the built GPU image,
    after the shell command prefix *setup* (empty: none)."""
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            GPU_PLATFORM,
            *env_args,
            "--entrypoint",
            "sh",
            gpu_image,
            "-c",
            f"{setup}exec sh {CUDA_MAJOR_GUARD}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=GPU_PROBE_TIMEOUT_S,
    )


def _parse_cuda_major(version: str) -> str:
    return version.split(".")[0]


@pytest.fixture(scope="module")
def gpu_image() -> Iterator[str]:
    """Build the GPU image once per module, as the release workflow does; yield the tag.

    Like the CPU image of ``tests/test_docker_smoke.py``, the image is kept so
    repeat runs reuse the layer cache. ``RELEASE_VERSION`` is the Dockerfile's
    own default, passed explicitly as the release does.
    """
    if not smoke_opted_in():
        pytest.skip(f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke build")
    if not docker_available():
        pytest.skip("docker not available")
    subprocess.run(
        [
            "docker", "build",
            "--platform", GPU_PLATFORM,
            "--build-arg", f"BASE_IMAGE={_read_release_gpu_base_image()}",
            "--build-arg", "RELEASE_VERSION=0.0.0",
            "-t", GPU_IMAGE_TAG,
            str(REPO_ROOT),
        ],
        check=True,
        timeout=GPU_BUILD_TIMEOUT_S,
    )
    yield GPU_IMAGE_TAG


@pytest.fixture(scope="module")
def gpu_image_probe(gpu_image: str) -> dict:
    """What ``tests/_gpu_image_probe.py`` reports inside the GPU image (no GPU needed)."""
    return _docker_run_json(
        [
            "--entrypoint", "python3", gpu_image, "-",
            PROBE_JSON_SENTINEL,
        ],
        GPU_PROBE_TIMEOUT_S,
        GPU_IMAGE_PROBE,
    )


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_gpu_image_torch_is_a_cuda_build(gpu_image_probe: dict) -> None:
    assert gpu_image_probe["torch_cuda_version"], f"torch has no CUDA version: {gpu_image_probe}"


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_gpu_image_cuda_majors_match(gpu_image_probe: dict) -> None:
    """Regression test for the Dockerfile's CUDA-major guard: while the guard
    exists a mismatched image fails to build, so this fails only once the guard
    is removed and the stacks drift apart."""
    majors = {
        "torch": _parse_cuda_major(gpu_image_probe["torch_cuda_version"]),
        "base image": _parse_cuda_major(gpu_image_probe["image_cuda_version"]),
    }
    assert len(set(majors.values())) == 1, f"CUDA majors differ: {majors}; probe: {gpu_image_probe}"


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_gpu_image_ships_no_onnxruntime_gpu(gpu_image_probe: dict) -> None:
    """archon runs on PyTorch; the CPU ``onnxruntime`` that RapidOCR and magika
    require is fine, a GPU build of it is not."""
    assert "onnxruntime-gpu" not in gpu_image_probe["onnxruntime_distributions"], gpu_image_probe


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_cuda_major_guard_passes_on_the_built_gpu_image(gpu_image: str) -> None:
    proc = _run_cuda_major_guard(gpu_image)

    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_cuda_major_guard_fails_when_the_base_cuda_major_differs(gpu_image: str) -> None:
    """The guard's fail branch: a base image on another CUDA major than the
    onnxruntime-gpu and torch builds must fail the GPU build."""
    proc = _run_cuda_major_guard(gpu_image, "-e", f"CUDA_VERSION={MISMATCHED_BASE_CUDA_VERSION}")

    assert proc.returncode == 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert CUDA_MAJOR_GUARD_FAILURE in proc.stderr, proc.stderr
    assert f"base image CUDA_VERSION '{MISMATCHED_BASE_CUDA_VERSION}'" in proc.stderr, proc.stderr


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_cuda_major_guard_fails_when_the_base_cuda_version_is_missing(gpu_image: str) -> None:
    """A base image without CUDA_VERSION must fail the GPU build."""
    proc = _run_cuda_major_guard(gpu_image, "-e", "CUDA_VERSION=")

    assert proc.returncode == 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert CUDA_MAJOR_GUARD_FAILURE in proc.stderr, proc.stderr
    assert "base image CUDA_VERSION ''" in proc.stderr, proc.stderr


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_cuda_major_guard_fails_on_a_cpu_torch_build(gpu_image: str) -> None:
    """A CPU torch build (no CUDA version) must fail the GPU build."""
    proc = _run_cuda_major_guard(gpu_image, "-e", f"PYTHONPATH={CPU_TORCH_SHIM_DIR}", setup=CPU_TORCH_SHIM_SETUP)

    assert proc.returncode == 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert CUDA_MAJOR_GUARD_FAILURE in proc.stderr, proc.stderr
    assert "torch ''" in proc.stderr, proc.stderr
