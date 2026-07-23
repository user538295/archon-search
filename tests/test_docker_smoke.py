"""Container smoke test for the CPU image (Task 4.3).

These tests build the CPU image from the current source tree, start a
container, and verify it serves traffic. They are gated behind
``@pytest.mark.docker`` and the ``ARCHON_SEARCH_RUN_DOCKER_SMOKE`` opt-in
env var so the default ``uv run pytest`` invocation does not pay the
~5-minute build cost on dev machines that happen to have a docker daemon
installed. CI runs them explicitly with the env var set and
``-m docker``.

The marker is registered both here and in ``pyproject.toml`` because
``addopts`` uses ``--strict-markers`` — without the ``pyproject.toml``
entry, pytest would reject the marker at collection time and silently
skip every test in this file. ``test_docker_marker_in_pyproject`` guards
that registration.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

IMAGE_TAG = "archon-search:smoke-test"
# Port 28765 avoids the docker-compose dev port (18765:8765) and the
# production port (8765) so the smoke test can coexist with a running
# compose stack on the same host.
HOST_PORT = 28765
CONTAINER_PORT = 8765
SMOKE_API_KEY = "smoketest"
READY_TIMEOUT_S = 30


SMOKE_OPT_IN_ENV = "ARCHON_SEARCH_RUN_DOCKER_SMOKE"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _smoke_opted_in() -> bool:
    """Smoke tests are skipped unless the operator opts in.

    Without this gate every ``uv run pytest`` on a developer laptop with
    Docker Desktop installed would trigger the ~5-minute image build.
    The C9 plan requires the suite to run via ``-m docker`` in CI; this
    env var is the project-convention equivalent of marker exclusion,
    matching how ``live`` / ``live_eval`` skip when their infrastructure
    is absent.
    """
    return os.environ.get(SMOKE_OPT_IN_ENV) == "1"


def _wait_for_ready(url: str, timeout_s: int) -> int:
    """Poll ``url`` until it returns a status code or ``timeout_s`` expires.

    Returns the HTTP status code from the last successful response. Raises
    ``TimeoutError`` if the URL never responds within the budget. Uses a
    1-second sleep between attempts per the Task 4.3 spec.
    """
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError(
        f"{url} did not become ready within {timeout_s}s; last error: {last_error!r}"
    )


@pytest.fixture(scope="module")
def cpu_image() -> Iterator[str]:
    """Build the CPU image once per module; yield the tag.

    The image is intentionally NOT removed in teardown — repeat smoke
    runs on the same host reuse the build layer cache. CI runners are
    ephemeral so leftover images don't accumulate.
    """
    if not _smoke_opted_in():
        pytest.skip(
            f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke build"
        )
    if not _docker_available():
        pytest.skip("docker not available")
    # Build budget: 25 min. The Task 4.3 spec suggested 300s, but a cold
    # `pip install .` pulls torch + onnxruntime + fastembed model deps,
    # which on a clean BuildKit cache exceeds five minutes on both
    # GitHub-hosted runners and developer laptops. We still fail if the
    # build truly stalls, just with a budget that accommodates a fresh
    # transitive-dep download.
    subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, str(REPO_ROOT)],
        check=True,
        timeout=1500,
    )
    yield IMAGE_TAG


# ---------------------------------------------------------------------------
# Marker registration guard (always-on)
# ---------------------------------------------------------------------------


def test_docker_marker_in_pyproject() -> None:
    """``addopts`` uses ``--strict-markers`` — the docker marker must be
    registered in ``pyproject.toml`` or pytest rejects the marker at
    collection time and every smoke test silently disappears.
    """
    with PYPROJECT.open("rb") as fp:
        data = tomllib.load(fp)
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("docker:") or m.startswith("docker ") for m in markers), (
        "pyproject.toml [tool.pytest.ini_options].markers must register a "
        "'docker:' marker so --strict-markers does not reject @pytest.mark.docker"
    )


# ---------------------------------------------------------------------------
# Docker-gated smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_cpu_image_starts_and_serves_ready(cpu_image: str) -> None:
    """Build the CPU image, run it as the default user with an anonymous
    volume, and assert ``/ready`` returns HTTP 200 within 30s.

    The container runs detached (``-d``) so the test process can poll
    readiness. ``--rm`` is intentionally omitted: when combined with
    ``-d`` the container would be removed before the ``docker rm -f``
    teardown could log diagnostics on failure.
    """
    container_id = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "-e",
            f"ARCHON_SEARCH_API_KEY={SMOKE_API_KEY}",
            "-p",
            f"{HOST_PORT}:{CONTAINER_PORT}",
            cpu_image,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()
    assert container_id, "docker run -d did not return a container id"

    try:
        status = _wait_for_ready(
            f"http://localhost:{HOST_PORT}/ready", READY_TIMEOUT_S
        )
        assert status == 200, f"/ready returned HTTP {status}, expected 200"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            check=False,
            timeout=30,
        )


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_uid_1000_can_write_data_dir(cpu_image: str) -> None:
    """Mount a host directory at ``/data`` with UID 1000 ownership and
    assert that the auto-generated key file lands on the volume.

    ``tempfile.mkdtemp()`` creates the dir owned by the current process
    UID; ``os.chmod(..., 0o777)`` is required so UID 1000 inside the
    container can write to it. Without the chmod, ``load_or_generate_key()``
    silently fails and no ``.search.env`` file appears.
    """
    tmp_dir = tempfile.mkdtemp(prefix="archon-smoke-")
    os.chmod(tmp_dir, 0o777)

    container_id: str | None = None
    try:
        container_id = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--user",
                "1000",
                "-v",
                f"{tmp_dir}:/data",
                "-e",
                f"ARCHON_SEARCH_API_KEY={SMOKE_API_KEY}",
                "-p",
                f"{HOST_PORT + 1}:{CONTAINER_PORT}",
                cpu_image,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.strip()
        assert container_id, "docker run -d did not return a container id"

        status = _wait_for_ready(
            f"http://localhost:{HOST_PORT + 1}/ready", READY_TIMEOUT_S
        )
        assert status == 200, f"/ready returned HTTP {status}, expected 200"

        key_file = Path(tmp_dir) / ".search.env"
        assert key_file.exists(), (
            f"Expected {key_file} to be created by UID 1000 on the mounted "
            f"volume; container could not write to /data"
        )
    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                check=False,
                timeout=30,
            )
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# T-2: whole-feature Docker compose smoke suite (DCS)
# ---------------------------------------------------------------------------


# 20 test functions in tests/smoke/docker/test_docker_cli.py; 1 is xfail(strict=False)
# for the advisory timing test. xfail counts as "1 xfailed", not "1 passed", so a
# fully-green run reports 19 passed. Floor is 19 to catch any silent skip regression.
_DOCKER_SMOKE_MIN_PASSED = 19


@pytest.fixture(scope="module")
def test_runner_image() -> str:
    """Build the Dockerfile.test image once per module; return a sentinel.

    Uses ``docker compose build archon-test-runner`` so the compose service
    definition (volumes, env vars) is fully honoured.  Image is not removed on
    teardown — the named volumes (``archon-docker-venv``, ``archon-uv-cache``)
    persist and speed up subsequent runs.
    """
    if not _smoke_opted_in():
        pytest.skip(f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker compose smoke suite")
    if not _docker_available():
        pytest.skip("docker not available")
    subprocess.run(
        ["docker", "compose", "build", "archon-test-runner"],
        check=True,
        cwd=str(REPO_ROOT),
        timeout=1500,
    )
    return "archon-test-runner"


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker compose smoke suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_docker_smoke_suite_exits_0(test_runner_image: str) -> None:
    """Run ``tests/smoke/docker/`` inside the compose test-runner; assert exit 0.

    Builds the ``archon-test-runner`` image from ``Dockerfile.test``, then
    runs::

        docker compose run --rm archon-test-runner \\
            sh -c "uv sync ... && uv run pytest tests/smoke/docker/ ..."

    inside the container.  The ``-o addopts=`` flag strips the ini-level
    ``addopts`` (which contains ``-m "not smoke"`` and ``-n 8``) so the
    smoke-marked tests are collected and run serially on a single worker.

    Asserts:
    - ``returncode == 0`` (the whole suite passed)
    - at least ``_DOCKER_SMOKE_MIN_PASSED`` tests reported as passed (guards
      against silent collection-failure where pytest exits 0 with 0 tests)

    ``--extra graph`` and the spaCy model download are intentionally omitted:
    the docker smoke tests spawn ``archon-search serve`` without graph enabled
    (the default), so spaCy is never imported during these tests.
    """
    result = subprocess.run(
        [
            "docker", "compose",
            "run", "--rm", "archon-test-runner",
            "sh", "-c",
            (
                "uv sync --dev --extra hyde --extra rag-fusion --quiet && "
                "uv run pytest tests/smoke/docker/ --no-cov -o addopts= -v"
            ),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=600,
    )
    combined = result.stdout + result.stderr

    # Parse pytest summary counts for diagnostic output in the failure message.
    # returncode is the authoritative pass/fail signal; parsed counts add context.
    passed_match = re.search(r"(\d+) passed", combined)
    passed_count = int(passed_match.group(1)) if passed_match else 0
    failed_match = re.search(r"(\d+) failed\b", combined)
    error_match = re.search(r"(\d+) error[s]?\b", combined)
    failed_count = int(failed_match.group(1)) if failed_match else 0
    error_count = int(error_match.group(1)) if error_match else 0

    assert result.returncode == 0, (
        f"docker compose smoke suite exited {result.returncode} "
        f"(passed={passed_count}, failed={failed_count}, errors={error_count})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert passed_count >= _DOCKER_SMOKE_MIN_PASSED, (
        f"Expected >= {_DOCKER_SMOKE_MIN_PASSED} passed tests; got {passed_count}. "
        f"If passed_count=0, the pytest summary line was not found in the output.\n"
        f"combined output:\n{combined}"
    )
