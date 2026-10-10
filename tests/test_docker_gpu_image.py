"""Container tests for the GPU image (``:gpu``).

These tests build the ``:gpu`` image exactly as the release workflow does
(amd64 only, ``BASE_IMAGE`` from the workflow's ``GPU_BASE_IMAGE``) and check,
without a GPU, that its onnxruntime CUDA build can load on the image's CUDA
stack, that the stack holds one cuDNN, and that the extras volume never
shadows onnxruntime-gpu. Like ``tests/test_docker_smoke.py`` they are gated
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
import uuid
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
from tests._entrypoint_harness import ONNXRUNTIME_DISTRIBUTIONS

RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "archon-search-release.yml"
GPU_IMAGE_TAG = "archon-search:gpu-smoke-test"
# The release workflow builds the GPU image for amd64 only.
GPU_PLATFORM = "linux/amd64"
# A cold build pulls the multi-GB CUDA base plus CUDA torch and onnxruntime-gpu,
# under emulation on arm64 hosts.
GPU_BUILD_TIMEOUT_S = 3600
GPU_PROBE_TIMEOUT_S = 600
# The first start installs the graph extra's packages over the network.
GPU_EXTRAS_START_TIMEOUT_S = 1800
# Budget for the quick docker CLI calls that create and remove the test's container and volume.
DOCKER_CLI_TIMEOUT_S = 30
# Hex characters of a uuid4 that name one test run's container and volume.
RUN_ID_HEX_CHARS = 12
CUDA_EXECUTION_PROVIDER = "CUDAExecutionProvider"
EXTRAS_VOLUME = "/pip-packages"
# The NVIDIA driver library: the container toolkit mounts it in only at
# `docker run --gpus` time, so it can never resolve in a GPU-less run.
CUDA_DRIVER_LIBRARY = "libcuda.so.1"
# cuFFT's soname major per CUDA major, as ORT 1.31 maps it (`_get_cufft_version`,
# https://github.com/microsoft/onnxruntime/blob/v1.31.0/onnxruntime/__init__.py lines 201-202).
CUFFT_MAJOR_BY_CUDA_MAJOR = {12: 11, 13: 12}
# The cuDNN library ORT's `preload_dlls` loads on Linux (same file, line 276).
CUDNN_SONAME = "libcudnn.so.9"

# Probe programs sent to `python3 -` in the container over stdin (not test modules: no `test_` prefix).
TESTS_DIR = REPO_ROOT / "tests"
GPU_IMAGE_PROBE = TESTS_DIR / "_gpu_image_probe.py"
DLOPEN_PROBE = TESTS_DIR / "_dlopen_probe.py"
EXTRAS_VOLUME_ONNXRUNTIME_PROBE = TESTS_DIR / "_extras_volume_onnxruntime_probe.py"
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
            DLOPEN_PROBE.read_text(),
            json.dumps(CUFFT_MAJOR_BY_CUDA_MAJOR),
            CUDNN_SONAME,
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
def test_gpu_image_onnxruntime_ships_the_cuda_provider(gpu_image_probe: dict) -> None:
    assert CUDA_EXECUTION_PROVIDER in gpu_image_probe["providers"], gpu_image_probe


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_gpu_image_cuda_provider_libraries_resolve(gpu_image_probe: dict) -> None:
    """With the default ``[database].providers = []`` fastembed picks CUDA and
    preloads nothing, so the CUDA provider's libraries must resolve from the
    image's own loader path or the models silently fall back to CPU."""
    unresolved = [line for line in gpu_image_probe["ldd_not_found"] if line.split()[0] != CUDA_DRIVER_LIBRARY]
    assert unresolved == [], f"unresolved CUDA provider libraries: {unresolved}; probe: {gpu_image_probe}"


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_gpu_image_cuda_provider_libraries_load_without_preload(gpu_image_probe: dict) -> None:
    """ldd sees only the CUDA provider's link-time libraries; cuDNN, cuFFT and
    NVRTC it opens at run time. Every library ORT's ``preload_dlls`` would load
    must therefore open by soname from the image's loader path alone."""
    assert gpu_image_probe["dlopen_failures"] == {}, (
        f"CUDA provider libraries that do not open without preload_dlls: {gpu_image_probe['dlopen_failures']}"
    )


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_gpu_image_cudnn_loads_from_the_wheel_torch_shares(gpu_image_probe: dict) -> None:
    """Loaded by soname without ``preload_dlls``, ``libcudnn.so.9`` must be the
    one copy in the ``nvidia/cudnn/lib`` wheel directory (torch's
    nvidia-cudnn-cu13): a second, system cuDNN that the loader found first would
    serve torch as well, which refuses a cuDNN older than the one it was built
    against."""
    loaded = gpu_image_probe["dlopen_loaded_files"][CUDNN_SONAME]
    assert loaded == [gpu_image_probe["cudnn_wheel_library"]], (
        f"{CUDNN_SONAME} loaded from {loaded}, expected only {gpu_image_probe['cudnn_wheel_library']}"
    )


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
        "onnxruntime": _parse_cuda_major(gpu_image_probe["onnxruntime_cuda_version"]),
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

@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_gpu_image_ships_no_cpu_onnxruntime(gpu_image_probe: dict) -> None:
    assert "onnxruntime" not in gpu_image_probe["onnxruntime_distributions"], gpu_image_probe


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_gpu_image_extras_volume_does_not_shadow_onnxruntime_gpu(gpu_image: str) -> None:
    """First start with ``ARCHON_EXTRAS=graph`` on a fresh volume (needs network):
    the volume, first on ``PYTHONPATH``, must not bring a second onnxruntime."""
    run_id = uuid.uuid4().hex[:RUN_ID_HEX_CHARS]
    container = f"archon-gpu-extras-{run_id}"
    volume = f"archon-gpu-extras-{run_id}"
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True, timeout=DOCKER_CLI_TIMEOUT_S)
    try:
        report = _docker_run_json(
            [
                "--name", container,
                "-v", f"{volume}:{EXTRAS_VOLUME}",
                "-e", "ARCHON_EXTRAS=graph",
                gpu_image,
                "python3", "-", PROBE_JSON_SENTINEL, EXTRAS_VOLUME,
            ],
            GPU_EXTRAS_START_TIMEOUT_S,
            EXTRAS_VOLUME_ONNXRUNTIME_PROBE,
        )
        in_volume = ONNXRUNTIME_DISTRIBUTIONS & {name.lower() for name in report["volume_distributions"]}
        assert not in_volume, f"onnxruntime distributions in {EXTRAS_VOLUME}: {in_volume}; report: {report}"
        assert not report["onnxruntime_file"].startswith(f"{EXTRAS_VOLUME}/"), report
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True, timeout=DOCKER_CLI_TIMEOUT_S)
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume], check=False, capture_output=True, timeout=DOCKER_CLI_TIMEOUT_S,
        )
