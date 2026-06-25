"""Multi-instance Docker e2e tests (T-1).

Verifies that ``archon-dev`` (port 18765) and ``archon-test`` (port 18766)
run side-by-side with isolated data volumes, independent API keys, and a
mounted MCP endpoint on each port.

These tests are gated behind ``ARCHON_SEARCH_RUN_DOCKER_SMOKE=1`` — the
same opt-in env var used by ``test_docker_smoke.py`` — so default
``uv run pytest`` invocations do not start Docker Compose services.

Run explicitly with::

    ARCHON_SEARCH_RUN_DOCKER_SMOKE=1 ARCHON_SEARCH_IMAGE=<image> \\
        uv run pytest tests/test_multi_instance_e2e.py -m docker -n0 -s

``xdist_group("docker")`` serialises this file with ``test_docker_smoke.py``
to prevent port-conflict races under parallel xdist execution. Both files
carry the ``docker`` xdist group so they never run concurrently.

Notes on key retrieval:
- API keys are retrieved via ``docker compose exec`` after auto-generation.
- Do NOT inject via ``-e ARCHON_SEARCH_API_KEY``.  ``docker-compose.yml``
  propagates that variable to EVERY service, so per-service injection is
  impossible and cross-auth tests would be vacuous (both services would
  share the same key).
- Do NOT export ``ARCHON_SEARCH_API_KEY`` in the shell environment before
  running these tests — see ``compose_stack`` for the assertion.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SMOKE_OPT_IN_ENV = "ARCHON_SEARCH_RUN_DOCKER_SMOKE"

DEV_HOST_PORT = 18765
TEST_HOST_PORT = 18766
READY_TIMEOUT_S = 60
JOB_POLL_TIMEOUT_S = 60


def _smoke_opted_in() -> bool:
    return os.environ.get(SMOKE_OPT_IN_ENV) == "1"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _wait_for_ready(url: str, timeout_s: int) -> int:
    """Poll *url* every second until it returns a status code or timeout.

    Returns the HTTP status code from the last successful response.
    Raises ``TimeoutError`` on timeout.
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


def _get_key_from_service(service: str) -> str:
    """Retrieve the auto-generated bare API key from *service* via compose exec.

    Reads ``/data/.search.env`` inside the container and returns the raw
    token (everything after the first ``=`` sign).  Using ``cut -d= -f2-``
    rather than ``grep -o '[^=]*$'`` handles any token that could contain
    ``=`` (e.g. base64-encoded variants), though archon-search currently
    generates 64-char hex tokens.
    """
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            service,
            "sh",
            "-c",
            "cut -d= -f2- /data/.search.env",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
        cwd=REPO_ROOT,
    )
    key = result.stdout.strip()
    assert key, (
        f"Could not retrieve API key from {service} — /data/.search.env may be "
        f"missing or malformed"
    )
    return key


def _http_get(url: str, *, bearer: str | None = None) -> tuple[int, bytes, dict[str, str]]:
    """Perform an HTTP GET and return ``(status_code, body_bytes, headers)``.

    Headers are normalised to lowercase keys so assertions are immune to
    wire-casing differences.  Starlette lowercases all header names before
    sending them on the wire (``starlette/responses.py`` ``raw_headers``
    construction), so ``www-authenticate`` arrives as ``www-authenticate``.
    Normalising here lets callers use the canonical lowercase form.

    Propagates non-HTTP errors (network, timeout) as exceptions — intentional
    fail-loud behaviour for e2e tests where a connection failure signals a
    broken environment rather than a testable condition.
    """
    req = urllib.request.Request(url)
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, exc.read(), headers


def _http_post_json(
    url: str, payload: dict, *, bearer: str | None = None
) -> tuple[int, bytes]:
    """Perform an HTTP POST with JSON body and return ``(status_code, body_bytes)``."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _poll_job_done(port: int, job_id: str, bearer: str, timeout_s: int) -> str:
    """Poll ``GET /jobs/{job_id}`` until the job reaches a terminal status.

    Returns the final status string (``"DONE"``, ``"FAILED"``, etc.).
    Raises ``TimeoutError`` if the job has not completed within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    terminal = {"DONE", "FAILED", "CANCELLED"}
    while time.monotonic() < deadline:
        status_code, body, _ = _http_get(
            f"http://localhost:{port}/jobs/{job_id}", bearer=bearer
        )
        if status_code == 200:
            data = json.loads(body)
            job_status = data.get("status", "").upper()
            if job_status in terminal:
                return job_status
        time.sleep(1)
    raise TimeoutError(
        f"Job {job_id} on port {port} did not reach a terminal status within {timeout_s}s"
    )


@pytest.fixture(scope="module")
def compose_stack() -> object:
    """Bring up ``archon-dev`` and ``archon-test`` for the module; tear down after.

    Skips the entire module if the opt-in env var is not set or Docker is
    unavailable.  Uses ``ARCHON_SEARCH_IMAGE`` from the environment (same
    precondition as ``test_docker_smoke.py``).

    Asserts that ``ARCHON_SEARCH_API_KEY`` is NOT set in the shell environment.
    If it is, both services receive the same key via ``${ARCHON_SEARCH_API_KEY:-}``
    interpolation in ``docker-compose.yml``, making key-isolation tests vacuous.
    """
    if not _smoke_opted_in():
        pytest.skip(
            f"{SMOKE_OPT_IN_ENV} not set; opt in to run the multi-instance e2e suite"
        )
    if not _docker_available():
        pytest.skip("docker not available")

    shell_key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    if shell_key:
        pytest.fail(
            "ARCHON_SEARCH_API_KEY is set in the shell environment. "
            "Unset it before running multi-instance tests — it would be injected "
            "into both containers, defeating key-isolation assertions."
        )

    # Start only archon-dev and archon-test — NOT archon-prod (port 8765).
    # archon-prod conflicts with a native prod instance running on port 8765.
    # ``--wait`` is passed for compatibility but provides no readiness guarantee
    # here because docker-compose.yml defines no healthcheck.  The explicit
    # ``_wait_for_ready`` polls below are the authoritative readiness gate.
    subprocess.run(
        [
            "docker",
            "compose",
            "up",
            "archon-dev",
            "archon-test",
            "-d",
            "--wait",
        ],
        check=True,
        timeout=120,
        cwd=REPO_ROOT,
    )

    try:
        # Poll both services until they serve HTTP 200 on /ready.
        status_dev = _wait_for_ready(
            f"http://localhost:{DEV_HOST_PORT}/ready", READY_TIMEOUT_S
        )
        assert status_dev == 200, (
            f"archon-dev /ready returned {status_dev} after startup; expected 200"
        )
        status_test = _wait_for_ready(
            f"http://localhost:{TEST_HOST_PORT}/ready", READY_TIMEOUT_S
        )
        assert status_test == 200, (
            f"archon-test /ready returned {status_test} after startup; expected 200"
        )
        yield
    finally:
        # ``down -v`` removes containers AND named volumes so the next run starts
        # clean (no stale keys or index data from a previous run).
        subprocess.run(
            [
                "docker",
                "compose",
                "down",
                "--volumes",
                "--remove-orphans",
                "archon-dev",
                "archon-test",
            ],
            check=False,
            timeout=60,
            cwd=REPO_ROOT,
        )


# ---------------------------------------------------------------------------
# Marker registration guard (always-on — does not require Docker)
# ---------------------------------------------------------------------------


def test_multi_instance_docker_marker_in_pyproject() -> None:
    """``--strict-markers`` requires the ``docker`` marker to be registered in
    ``pyproject.toml`` or pytest rejects it at collection time.

    Mirrors the guard in ``test_docker_smoke.py``.
    """
    import tomllib  # noqa: PLC0415 — stdlib, safe to import late

    with (REPO_ROOT / "pyproject.toml").open("rb") as fp:
        data = tomllib.load(fp)
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("docker:") or m.startswith("docker ") for m in markers), (
        "pyproject.toml [tool.pytest.ini_options].markers must register a "
        "'docker:' marker so --strict-markers does not reject @pytest.mark.docker"
    )


# ---------------------------------------------------------------------------
# Docker-gated multi-instance e2e tests
# ---------------------------------------------------------------------------


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the multi-instance e2e suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_archon_dev_starts_and_responds(compose_stack: object) -> None:  # noqa: ARG001
    """Both services start healthy and their data volumes are isolated.

    Steps:
    1. Both ``/health`` endpoints return HTTP 200.
    2. Retrieve each service's auto-generated API key via ``docker compose exec``.
    3. Ingest a document to ``archon-dev`` (port 18765); wait for the job to finish.
    4. Assert the collection IS present on archon-dev (positive control).
    5. List collections on ``archon-test`` (port 18766) — assert the collection is absent (data isolation).
    6. Confirm ``archon-dev-data`` and ``archon-test-data`` are separate Docker volumes.
    """
    # Step 1 — services are up (fixture already verified /ready; /health is a lighter check).
    status_dev, _, _ = _http_get(f"http://localhost:{DEV_HOST_PORT}/health")
    assert status_dev == 200, f"archon-dev /health returned {status_dev}"

    status_test, _, _ = _http_get(f"http://localhost:{TEST_HOST_PORT}/health")
    assert status_test == 200, f"archon-test /health returned {status_test}"

    # Step 2 — retrieve each service's key via exec (NOT -e ARCHON_SEARCH_API_KEY).
    dev_key = _get_key_from_service("archon-dev")
    test_key = _get_key_from_service("archon-test")

    # Keys must differ — each instance auto-generates from its own volume.
    assert dev_key != test_key, (
        "archon-dev and archon-test produced the same API key; "
        "ensure ARCHON_SEARCH_API_KEY is NOT set in .env or shell"
    )

    # Step 3 — enqueue an ingest job on archon-dev using a file that exists in the
    # container (/data/.search.env is written by every archon-search container on
    # first start; we ingest it only to create a collection, not for meaningful content).
    collection = "mis-isolation-test"
    ingest_status, ingest_body = _http_post_json(
        f"http://localhost:{DEV_HOST_PORT}/ingest",
        {
            "path": "/data/.search.env",
            "collection": collection,
        },
        bearer=dev_key,
    )
    assert ingest_status == 202, (
        f"POST /ingest to archon-dev returned {ingest_status}: {ingest_body.decode()}"
    )
    job_id = json.loads(ingest_body)["job_id"]

    # Wait for the async ingest job to reach a terminal status before checking isolation.
    final_status = _poll_job_done(DEV_HOST_PORT, job_id, dev_key, JOB_POLL_TIMEOUT_S)
    assert final_status == "DONE", (
        f"Ingest job {job_id} on archon-dev ended with status {final_status!r}; "
        f"expected DONE"
    )

    # Step 4 — positive control: collection must exist on archon-dev.
    list_dev_status, list_dev_body, _ = _http_get(
        f"http://localhost:{DEV_HOST_PORT}/collections/",
        bearer=dev_key,
    )
    assert list_dev_status == 200, (
        f"GET /collections/ on archon-dev returned {list_dev_status}: {list_dev_body.decode()}"
    )
    collections_on_dev = [c["name"] for c in json.loads(list_dev_body)]
    assert collection in collections_on_dev, (
        f"Positive control failed: collection '{collection}' not found on archon-dev "
        f"after ingest job completed. Collections: {collections_on_dev}"
    )

    # Step 5 — negative control: collection must NOT exist on archon-test.
    list_test_status, list_test_body, _ = _http_get(
        f"http://localhost:{TEST_HOST_PORT}/collections/",
        bearer=test_key,
    )
    assert list_test_status == 200, (
        f"GET /collections/ on archon-test returned {list_test_status}: {list_test_body.decode()}"
    )
    collections_on_test = [c["name"] for c in json.loads(list_test_body)]
    assert collection not in collections_on_test, (
        f"Data isolation failure: collection '{collection}' registered on archon-dev "
        f"is visible on archon-test. Collections on test: {collections_on_test}"
    )

    # Step 6 — confirm volumes are distinct via docker compose config.
    result = subprocess.run(
        [
            "docker",
            "compose",
            "config",
            "--volumes",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
        cwd=REPO_ROOT,
    )
    volumes_output = result.stdout
    assert "archon-dev-data" in volumes_output, (
        "archon-dev-data volume not found in compose config"
    )
    assert "archon-test-data" in volumes_output, (
        "archon-test-data volume not found in compose config"
    )


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the multi-instance e2e suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_cross_auth_fails(compose_stack: object) -> None:  # noqa: ARG001
    """API keys are instance-specific: each key is rejected on the other port.

    Proves both directions:
    - dev key → port 18766 (archon-test) returns 401
    - archon-test key → port 18765 (archon-dev) returns 401
    """
    dev_key = _get_key_from_service("archon-dev")
    test_key = _get_key_from_service("archon-test")

    # dev key must be rejected by archon-test (port 18766).
    status_dev_on_test, _, headers_dev_on_test = _http_get(
        f"http://localhost:{TEST_HOST_PORT}/status",
        bearer=dev_key,
    )
    assert status_dev_on_test == 401, (
        f"Expected 401 when using dev key on archon-test port {TEST_HOST_PORT}, "
        f"got {status_dev_on_test}"
    )
    assert headers_dev_on_test.get("www-authenticate") == "Bearer", (
        f"Expected www-authenticate: Bearer on 401 from archon-test, "
        f"got: {headers_dev_on_test.get('www-authenticate')!r}"
    )

    # test key must be rejected by archon-dev (port 18765).
    status_test_on_dev, _, headers_test_on_dev = _http_get(
        f"http://localhost:{DEV_HOST_PORT}/status",
        bearer=test_key,
    )
    assert status_test_on_dev == 401, (
        f"Expected 401 when using test key on archon-dev port {DEV_HOST_PORT}, "
        f"got {status_test_on_dev}"
    )
    assert headers_test_on_dev.get("www-authenticate") == "Bearer", (
        f"Expected www-authenticate: Bearer on 401 from archon-dev, "
        f"got: {headers_test_on_dev.get('www-authenticate')!r}"
    )


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the multi-instance e2e suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_mcp_endpoint_reachable(compose_stack: object) -> None:  # noqa: ARG001
    """MCP sub-app is mounted and its auth middleware is active on each port.

    A GET to ``/mcp`` with no auth must return HTTP 401 (not 404).
    - 401 proves the MCP sub-app is mounted and the auth middleware fires.
    - 404 would indicate the mount never happened (``mcp.enabled = false``
      or a lifespan mount failure).
    """
    status_dev, _, headers_dev = _http_get(f"http://localhost:{DEV_HOST_PORT}/mcp")
    assert status_dev == 401, (
        f"archon-dev /mcp returned {status_dev}; expected 401 "
        f"(401 = MCP mounted + auth active; 404 = mount absent)"
    )
    assert headers_dev.get("www-authenticate") == "Bearer", (
        f"archon-dev /mcp 401 missing www-authenticate: Bearer header; "
        f"got: {headers_dev.get('www-authenticate')!r}"
    )

    status_test, _, headers_test = _http_get(f"http://localhost:{TEST_HOST_PORT}/mcp")
    assert status_test == 401, (
        f"archon-test /mcp returned {status_test}; expected 401 "
        f"(401 = MCP mounted + auth active; 404 = mount absent)"
    )
    assert headers_test.get("www-authenticate") == "Bearer", (
        f"archon-test /mcp 401 missing www-authenticate: Bearer header; "
        f"got: {headers_test.get('www-authenticate')!r}"
    )
