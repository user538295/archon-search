"""Docker-mode CLI smoke tests (BE-1, BE-2).

Tests in this module exercise the ``archon-search`` CLI from a subprocess with
``ARCHON_SEARCH_CONTAINER=1`` injected into the environment, mirroring how the
CLI runs inside the Docker image.

Covers:
- S1 — ``--help`` and ``--version`` complete without error, exit 0
- S2 — ``serve`` starts and shuts down cleanly (``smoke_docker_server`` fixture)
- S3 — ``status`` with running server shows HTTP telemetry, exit 0 (BE-2)
- S4 — ``status`` with unreachable server exits 0 cleanly, no "stopped" line (BE-2)
- S13 — ``config show`` prints TOML config, exit 0, no server required
- S18 — ``--help`` completes within 5 s (advisory)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.smoke.conftest import _free_port

pytestmark = [pytest.mark.smoke, pytest.mark.xdist_group("smoke_e2e")]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_docker_env(*, port: int | None = None, data_dir: Path, api_key: str | None = None) -> dict[str, str]:
    """Return a subprocess env dict with ARCHON_SEARCH_CONTAINER=1.

    Callers that need isolation for offline commands (help, version, config
    show) must pass an explicit ``data_dir`` (e.g. the ``tmp_path`` fixture) to
    avoid xdist collisions via shared fixed paths.  ``port`` and ``api_key``
    default to safe dummy values for commands that never contact a server.
    """
    from tests.smoke.docker.conftest import _docker_env

    # Provide dummy values for offline commands that never use them
    _port = port or _free_port()
    _api_key = api_key or "a" * 64

    return _docker_env(port=_port, data_dir=data_dir, api_key=_api_key)


# ---------------------------------------------------------------------------
# Structural guard
# ---------------------------------------------------------------------------

def test_docker_module_has_correct_markers():
    """Verify pytestmark contains both required markers (structural guard).

    Uses the AST to verify the module-level ``pytestmark`` assignment so that
    a source-level substring match cannot be fooled by comments or strings.
    """
    import ast

    source = Path(__file__).read_text()
    tree = ast.parse(source)

    # Find the module-level pytestmark assignment
    pytestmark_value = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "pytestmark"
                for t in node.targets
            )
        ):
            pytestmark_value = node.value
            break

    assert pytestmark_value is not None, "No module-level pytestmark assignment found"
    assert isinstance(pytestmark_value, ast.List), "pytestmark must be a list"

    source_text = ast.unparse(pytestmark_value)
    assert "pytest.mark.smoke" in source_text, (
        f"pytestmark must contain pytest.mark.smoke; got: {source_text}"
    )
    assert "xdist_group" in source_text and "smoke_e2e" in source_text, (
        f"pytestmark must contain xdist_group('smoke_e2e'); got: {source_text}"
    )


# ---------------------------------------------------------------------------
# Offline CLI tests (S1, S13, S18)
# ---------------------------------------------------------------------------

def test_help_exits_0(tmp_path):
    """``archon-search --help`` exits 0 and produces no traceback (S1)."""
    env = _make_docker_env(data_dir=tmp_path)
    result = subprocess.run(
        ["uv", "run", "archon-search", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search --help exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search --help printed a traceback:\n{combined}"
    )


def test_version_exits_0(tmp_path):
    """``archon-search --version`` exits 0 (S1)."""
    env = _make_docker_env(data_dir=tmp_path)
    result = subprocess.run(
        ["uv", "run", "archon-search", "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"archon-search --version exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_config_show_exits_0(tmp_path):
    """``archon-search config show`` exits 0 and echoes the written TOML (S13).

    Writes a minimal TOML to a temp path so the command reads a real file
    rather than falling back to ``_default_toml()``.  No server is required.
    """
    # Use a non-default host value to prove config show reads the real file
    # rather than falling back to _default_toml() (whose default host is
    # "127.0.0.1" — indistinguishable from a default-only assertion).
    config_path = tmp_path / "archon-search.toml"
    config_path.write_text('[server]\nhost = "10.20.30.40"\n', encoding="utf-8")

    env = _make_docker_env(data_dir=tmp_path / "data")
    env["ARCHON_SEARCH_CONFIG"] = str(config_path)
    env["ARCHON_SEARCH_DATA_DIR"] = str(tmp_path / "data")

    result = subprocess.run(
        ["uv", "run", "archon-search", "config", "show"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"archon-search config show exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "[server]" in result.stdout, (
        f"Expected '[server]' section in config show output:\n{result.stdout}"
    )
    assert 'host = "10.20.30.40"' in result.stdout, (
        f"Expected written non-default host value in config show output:\n{result.stdout}"
    )


@pytest.mark.xfail(reason="advisory timing; may exceed 5s under load", strict=False)
def test_help_completes_within_5s(tmp_path):
    """``archon-search --help`` completes within 5 seconds (advisory, S18)."""
    env = _make_docker_env(data_dir=tmp_path)
    start = time.monotonic()
    subprocess.run(
        ["uv", "run", "archon-search", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"archon-search --help took {elapsed:.2f}s (limit: 5.0s)"


# ---------------------------------------------------------------------------
# Serve lifecycle test (S2)
# ---------------------------------------------------------------------------

def test_serve_health_and_ready(smoke_docker_server):
    """Server started with Docker env responds to /health and /ready (S2).

    Verifies:
    1. ``GET /health`` returns HTTP 200.
    2. ``GET /ready`` returns ``{"ready": true}``.
    3. The server process exits cleanly after SIGTERM (tested implicitly via
       fixture teardown, which calls ``pytest.fail`` if SIGTERM times out).
    """
    base_url = smoke_docker_server.base_url

    health_resp = httpx.get(f"{base_url}/health", timeout=5)
    assert health_resp.status_code == 200, (
        f"GET /health returned {health_resp.status_code}: {health_resp.text}"
    )

    ready_resp = httpx.get(f"{base_url}/ready", timeout=5)
    assert ready_resp.status_code == 200, (
        f"GET /ready returned {ready_resp.status_code}: {ready_resp.text}"
    )
    assert ready_resp.json().get("ready") is True, (
        f"GET /ready did not report ready=true: {ready_resp.text}"
    )


# ---------------------------------------------------------------------------
# BE-2: status in container mode (S3, S4)
# ---------------------------------------------------------------------------


def test_status_with_server_shows_http_telemetry(smoke_docker_server, tmp_path):
    """``status`` with server running shows HTTP telemetry, 'stopped' absent, exit 0 (S3).

    Runs ``archon-search status --api-url <url> --api-key <key>`` with
    ``ARCHON_SEARCH_CONTAINER=1`` injected.  Asserts:
    - ``returncode == 0``
    - ``"stopped"`` is NOT present (service-section line suppressed in container mode)
    - At least one telemetry-like field from the HTTP /status response is present
    """
    env = _make_docker_env(
        port=smoke_docker_server.port,
        data_dir=tmp_path,
        api_key=smoke_docker_server.api_key,
    )
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "status",
            "--api-url", smoke_docker_server.base_url,
            "--api-key", smoke_docker_server.api_key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search status exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search status printed a traceback:\n{combined}"
    )
    assert "stopped" not in result.stdout, (
        f"'stopped' should be suppressed in container mode; stdout:\n{result.stdout}"
    )
    # The smoke_docker_server fixture enables telemetry ([telemetry] enabled=true)
    # so GET /status returns a non-null telemetry sub-object and the CLI prints
    # the "Telemetry:" section header.  "Collections:" is never present because
    # the docker fixture seeds no corpus.  We assert on result.stdout (not
    # combined) because _print_telemetry_status uses click.echo without err=True
    # — telemetry output is a stdout contract, not a stderr one.
    assert "Telemetry:" in result.stdout, (
        f"Expected 'Telemetry:' in stdout from the HTTP /status response; got:\n{result.stdout}"
    )


def test_status_without_server_clean_exit_0(tmp_path):
    """``status`` with unreachable server exits 0 with no traceback, no "stopped" (S4).

    Points ``--api-url`` at a port with no listener so ``_fetch_server_status``
    returns ``None`` via the ``ConnectError`` path.  In container mode the
    service-section line is suppressed and the telemetry section is silently
    omitted — the result is empty stdout, exit 0, no traceback.
    """
    from tests.smoke.conftest import _free_port

    dead_port = _free_port()
    env = _make_docker_env(data_dir=tmp_path, port=dead_port)
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "status",
            "--api-url", f"http://127.0.0.1:{dead_port}",
            "--api-key", "a" * 64,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search status exited {result.returncode} (expected 0);\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search status printed a traceback:\n{combined}"
    )
    assert "stopped" not in result.stdout, (
        f"'stopped' should be suppressed in container mode; stdout:\n{result.stdout}"
    )
    # Positive assertion: the unreachable path results in empty stdout (the
    # service-section line is suppressed and the telemetry section is silently
    # omitted).  An empty stdout distinguishes this path from every other exit-0
    # branch (401, empty-but-non-None payload, etc. all produce some output).
    assert result.stdout.strip() == "", (
        f"Expected empty stdout when server unreachable in container mode; got:\n{result.stdout}"
    )
