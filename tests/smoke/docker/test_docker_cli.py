"""Docker-mode CLI smoke tests (BE-1, BE-2, BE-3, BE-4, BE-5, T-1).

Tests in this module exercise the ``archon-search`` CLI from a subprocess with
``ARCHON_SEARCH_CONTAINER=1`` injected into the environment, mirroring how the
CLI runs inside the Docker image.

Covers:
- S1 — ``--help`` and ``--version`` complete without error, exit 0
- S2 — ``serve`` starts and shuts down cleanly (``smoke_docker_server`` fixture)
- S3 — ``status`` with running server shows HTTP telemetry, exit 0 (BE-2, T-1)
- S4 — ``status`` with unreachable server exits 0 cleanly, no "stopped" line (BE-2)
- S5 — ``start`` in container mode emits clean message, exit 1 (BE-3)
- S6 — ``stop`` in container mode emits clean message, exit 1 (BE-3)
- S7 — ``install`` in container mode emits clean message, exit 1 (BE-4)
- S8 — ``uninstall`` in container mode emits clean message, exit 1 (BE-4)
- S9 — ``key list`` exits 0 (BE-5)
- S10 — ``collection list`` reports "smoke" in output, exit 0 (BE-5)
- S11 — ``collection add --wait`` completes with "ingested successfully.", exit 0 (BE-5)
- S12 — ``collection info smoke`` reports "name: smoke", exit 0 (BE-5)
- S13 — ``config show`` prints TOML config, exit 0, no server required
- S14 — ``ingest --wait`` completes with "Ingest complete for 'smoke'.", exit 0 (BE-5)
- S15 — ``jobs status <id>`` reports terminal status, exit 0 (BE-5)
- S16 — ``maintenance run`` exits 0 (BE-5)
- S18 — ``--help`` completes within 5 s (advisory)

T-1 manual spot-check checklist (human verification):
- Real container invocation: ``docker compose run --rm archon-test-runner archon-search status --api-url <url> --api-key <key>``
- Host-side approximation (env-var only, not a full container): ``ARCHON_SEARCH_CONTAINER=1 archon-search status --api-url <url> --api-key <key>``
- Fetch: ``GET /status`` with the same key and inspect the ``telemetry`` sub-object
- Confirm: the ``enabled`` field is rendered as the value word on the ``Telemetry:`` header
  line (e.g. ``Telemetry: enabled``); ``hash_doc_ids_enabled`` appears as an indented field
  label with its value (e.g. ``  hash_doc_ids_enabled: True``)
- Confirm: "stopped" is absent from the container-mode output (whole-line check)
- Status: completed — automated by ``test_docker_status_renders_telemetry_payload_fields``
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx
import pytest

from archon_search.cli._helpers import _CONTAINER_MSG
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


# ---------------------------------------------------------------------------
# T-1: telemetry parity — container mode output matches native telemetry fields
# ---------------------------------------------------------------------------


def test_docker_status_renders_telemetry_payload_fields(smoke_docker_server, tmp_path):
    """Telemetry payload-value coupling: rendered fields match HTTP payload values (T-1, S3).

    Sole unique contribution: asserts that ``enabled`` is rendered as the value
    word on the ``Telemetry:`` header line, and that ``hash_doc_ids_enabled`` is
    rendered as an indented field label with its actual payload value
    (C1 / C2-MAJOR-2).

    Telemetry rendering (``_print_telemetry_status``) is driven purely by the
    HTTP payload; it is identical in native and container mode.  This test uses
    ``ARCHON_SEARCH_CONTAINER=1`` only because the scenario (S3) calls for it —
    not because the assertions are container-specific.

    The "stopped" assertion below is a belt-and-suspenders corroboration (not a
    C2 guard): because ``smoke_docker_server`` has a running server
    (``svc_status.running is True``), the suppression branch in ``status.py``
    is never exercised here.  Primary C2 proof lives in the sibling
    ``test_status_without_server_clean_exit_0``.

    Manual spot-check documented in this module's docstring.
    """
    # --- Fetch the raw /status payload to obtain actual telemetry fields ---
    status_resp = httpx.get(
        f"{smoke_docker_server.base_url}/status",
        headers={"Authorization": f"Bearer {smoke_docker_server.api_key}"},
        timeout=5,
    )
    assert status_resp.status_code == 200, (
        f"GET /status returned {status_resp.status_code}: {status_resp.text}"
    )
    telemetry_payload = status_resp.json().get("telemetry")
    assert telemetry_payload is not None, (
        "GET /status returned null telemetry — smoke_docker_server must have telemetry enabled"
    )
    # Pre-condition: hash_doc_ids_enabled must be present in the payload;
    # if absent, the server schema has changed and we want a clear failure
    # before paying the ~30s subprocess cost.
    assert "hash_doc_ids_enabled" in telemetry_payload, (
        f"GET /status telemetry payload missing 'hash_doc_ids_enabled' field: {telemetry_payload}"
    )

    # --- Container-mode run ---
    container_env = _make_docker_env(
        port=smoke_docker_server.port,
        data_dir=tmp_path,
        api_key=smoke_docker_server.api_key,
    )

    container_result = subprocess.run(
        [
            "uv", "run", "archon-search", "status",
            "--api-url", smoke_docker_server.base_url,
            "--api-key", smoke_docker_server.api_key,
        ],
        env=container_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert container_result.returncode == 0, (
        f"container archon-search status exited {container_result.returncode}\n"
        f"stdout: {container_result.stdout}\nstderr: {container_result.stderr}"
    )
    assert "Traceback" not in (container_result.stdout + container_result.stderr), (
        f"container archon-search status printed a traceback:\n"
        f"{container_result.stdout + container_result.stderr}"
    )
    container_stdout = container_result.stdout

    # Belt-and-suspenders: "stopped" should be absent from output (running server
    # never reaches the suppression branch — see docstring for C2 proof location).
    assert "stopped" not in container_stdout.splitlines(), (
        f"'stopped' must be suppressed in container mode; stdout:\n{container_stdout}"
    )

    # 'enabled' is rendered as the value word on the Telemetry: header line,
    # not as a label — couple the payload value to the expected rendered header.
    # (Subsumes the bare "Telemetry:" header check — if this passes, the header is present.)
    expected_header = f"Telemetry: {'enabled' if telemetry_payload['enabled'] else 'disabled'}"
    assert expected_header in container_stdout, (
        f"Expected '{expected_header}' in container status stdout.\n"
        f"Container stdout:\n{container_stdout}"
    )
    # C2-MAJOR-2: hash_doc_ids_enabled is rendered as an indented label with its value.
    # The 2-space prefix is load-bearing: it couples to _print_telemetry_status's
    # indent format in status.py:121 — changing that indent breaks this assertion.
    expected_hash_line = f"  hash_doc_ids_enabled: {telemetry_payload['hash_doc_ids_enabled']}"
    assert expected_hash_line in container_stdout, (
        f"Expected '{expected_hash_line}' in container status stdout.\n"
        f"Container stdout:\n{container_stdout}"
    )


# ---------------------------------------------------------------------------
# BE-3: start/stop in container mode (S5, S6)
# ---------------------------------------------------------------------------


def test_start_emits_clean_container_mode_message(tmp_path):
    """``start`` in container mode exits 1 with clean message, no traceback (S5)."""
    env = _make_docker_env(data_dir=tmp_path)
    result = subprocess.run(
        ["uv", "run", "archon-search", "start"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, (
        f"archon-search start expected returncode 1, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search start printed a traceback:\n{combined}"
    )
    assert _CONTAINER_MSG in result.stderr, (
        f"Expected container-mode message in stderr; got:\n{result.stderr}"
    )


def test_stop_emits_clean_container_mode_message(tmp_path):
    """``stop`` in container mode exits 1 with clean message, no traceback (S6)."""
    env = _make_docker_env(data_dir=tmp_path)
    result = subprocess.run(
        ["uv", "run", "archon-search", "stop"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, (
        f"archon-search stop expected returncode 1, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search stop printed a traceback:\n{combined}"
    )
    assert _CONTAINER_MSG in result.stderr, (
        f"Expected container-mode message in stderr; got:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# BE-4: install/uninstall in container mode (S7, S8)
# ---------------------------------------------------------------------------


def test_install_emits_clean_container_mode_message(tmp_path):
    """``install`` in container mode exits 1 with clean message, no traceback (S7)."""
    env = _make_docker_env(data_dir=tmp_path)
    result = subprocess.run(
        ["uv", "run", "archon-search", "install"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, (
        f"archon-search install expected returncode 1, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search install printed a traceback:\n{combined}"
    )
    assert _CONTAINER_MSG in result.stderr, (
        f"Expected container-mode message in stderr; got:\n{result.stderr}"
    )


def test_uninstall_emits_clean_container_mode_message(tmp_path):
    """``uninstall`` in container mode exits 1 with clean message, no traceback (S8)."""
    env = _make_docker_env(data_dir=tmp_path)
    result = subprocess.run(
        ["uv", "run", "archon-search", "uninstall"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, (
        f"archon-search uninstall expected returncode 1, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search uninstall printed a traceback:\n{combined}"
    )
    assert _CONTAINER_MSG in result.stderr, (
        f"Expected container-mode message in stderr; got:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# BE-5: server-dependent proofs (S9, S10, S11, S12, S14, S15, S16)
# ---------------------------------------------------------------------------


def test_key_list_exits_0(smoke_docker_server_seeded, tmp_path):
    """``key list`` exits 0 against a running server (S9)."""
    env = _make_docker_env(
        port=smoke_docker_server_seeded.port,
        data_dir=tmp_path,
        api_key=smoke_docker_server_seeded.api_key,
    )
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "key", "list",
            "--api-url", smoke_docker_server_seeded.base_url,
            "--api-key", smoke_docker_server_seeded.api_key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search key list exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search key list printed a traceback:\n{combined}"
    )


def test_collection_list_exits_0(smoke_docker_server_seeded, tmp_path):
    """``collection list`` reports "smoke" in output, exits 0 (S10).

    ``collection list`` is an HTTP proxy (CSP120) — requires the server to be running.
    The "smoke" collection was seeded by the ``smoke_docker_server_seeded`` fixture.
    """
    env = _make_docker_env(
        port=smoke_docker_server_seeded.port,
        data_dir=smoke_docker_server_seeded.data_dir,
        api_key=smoke_docker_server_seeded.api_key,
    )
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "collection", "list",
            "--api-url", smoke_docker_server_seeded.base_url,
            "--api-key", smoke_docker_server_seeded.api_key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search collection list exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search collection list printed a traceback:\n{combined}"
    )
    assert "smoke" in result.stdout, (
        f"Expected 'smoke' collection in output; got:\n{result.stdout}"
    )


def test_collection_add_wait_completes(smoke_docker_server_seeded, tmp_path):
    """``collection add --wait`` completes with "ingested successfully.", exit 0 (S11)."""
    # Write a unique corpus directory so the server doesn't return 409 (already registered).
    new_corpus = tmp_path / "be5_add_corpus"
    new_corpus.mkdir()
    (new_corpus / "doc.txt").write_text(
        "BE-5 collection add proof: this document is ingested via the CLI --wait path "
        "inside the Docker-mode smoke test suite to verify the HTTP-backed command works."
    )

    env = _make_docker_env(
        port=smoke_docker_server_seeded.port,
        data_dir=tmp_path,
        api_key=smoke_docker_server_seeded.api_key,
    )
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "collection", "add",
            str(new_corpus),
            "--wait",
            "--api-url", smoke_docker_server_seeded.base_url,
            "--api-key", smoke_docker_server_seeded.api_key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search collection add --wait exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search collection add --wait printed a traceback:\n{combined}"
    )
    assert "ingested successfully." in result.stdout, (
        f"Expected 'ingested successfully.' in stdout; got:\n{result.stdout}"
    )


def test_collection_info_exits_0(smoke_docker_server_seeded, tmp_path):
    """``collection info smoke`` reports "name: smoke", exits 0 (S12)."""
    env = _make_docker_env(
        port=smoke_docker_server_seeded.port,
        data_dir=tmp_path,
        api_key=smoke_docker_server_seeded.api_key,
    )
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "collection", "info", "smoke",
            "--api-url", smoke_docker_server_seeded.base_url,
            "--api-key", smoke_docker_server_seeded.api_key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search collection info smoke exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search collection info smoke printed a traceback:\n{combined}"
    )
    assert "name: smoke" in result.stdout, (
        f"Expected 'name: smoke' in stdout; got:\n{result.stdout}"
    )


def test_ingest_wait_completes(smoke_docker_server_seeded, tmp_path):
    """``ingest --wait`` completes with "Ingest complete for 'smoke'.", exits 0 (S14)."""
    ingest_file = tmp_path / "be5_ingest.txt"
    ingest_file.write_text(
        "BE-5 ingest proof: this file is ingested via the CLI --wait path "
        "inside the Docker-mode smoke test suite to verify the HTTP-backed ingest command works."
    )

    env = _make_docker_env(
        port=smoke_docker_server_seeded.port,
        data_dir=tmp_path,
        api_key=smoke_docker_server_seeded.api_key,
    )
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "ingest",
            "--path", str(ingest_file),
            "--collection", "smoke",
            "--wait",
            "--api-url", smoke_docker_server_seeded.base_url,
            "--api-key", smoke_docker_server_seeded.api_key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search ingest --wait exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search ingest --wait printed a traceback:\n{combined}"
    )
    assert "Ingest complete for 'smoke'." in result.stdout, (
        f"Expected \"Ingest complete for 'smoke'.\" in stdout; got:\n{result.stdout}"
    )


def test_jobs_status_reports_status(smoke_docker_server_seeded, tmp_path):
    """``jobs status <id>`` reports "status:     DONE", exits 0 (S15).

    Submits a reindex job via REST, polls until it reaches a terminal status,
    then calls ``archon-search jobs status <id>`` and asserts the output
    contains "status:     DONE".
    """
    headers = {"Authorization": f"Bearer {smoke_docker_server_seeded.api_key}"}
    base_url = smoke_docker_server_seeded.base_url

    # Submit a reindex job for the smoke collection
    resp = httpx.post(
        f"{base_url}/collections/smoke/reindex",
        headers=headers,
        timeout=10,
    )
    assert resp.status_code == 202, (
        f"POST /collections/smoke/reindex failed: {resp.status_code} {resp.text}"
    )
    job_id = resp.json()["job_id"]

    # Poll until terminal
    deadline = time.monotonic() + 60.0
    job_status = "UNKNOWN"
    while time.monotonic() < deadline:
        job_resp = httpx.get(f"{base_url}/jobs/{job_id}", headers=headers, timeout=5)
        assert job_resp.status_code == 200, (
            f"GET /jobs/{job_id} failed: {job_resp.status_code} {job_resp.text}"
        )
        job_status = job_resp.json().get("status", "UNKNOWN")
        if job_status in {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}:
            break
        time.sleep(0.5)
    assert job_status == "DONE", (
        f"Reindex job {job_id} did not reach DONE within 60s; final status: {job_status}"
    )

    # Now call the CLI jobs status command
    env = _make_docker_env(
        port=smoke_docker_server_seeded.port,
        data_dir=tmp_path,
        api_key=smoke_docker_server_seeded.api_key,
    )
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "jobs", "status", job_id,
            "--api-url", smoke_docker_server_seeded.base_url,
            "--api-key", smoke_docker_server_seeded.api_key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search jobs status exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search jobs status printed a traceback:\n{combined}"
    )
    assert "status:     DONE" in result.stdout, (
        f"Expected 'status:     DONE' in stdout; got:\n{result.stdout}"
    )


def test_maintenance_run_exits_0(smoke_docker_server_seeded, tmp_path):
    """``maintenance run`` exits 0 (S16)."""
    env = _make_docker_env(
        port=smoke_docker_server_seeded.port,
        data_dir=tmp_path,
        api_key=smoke_docker_server_seeded.api_key,
    )
    result = subprocess.run(
        [
            "uv", "run", "archon-search", "maintenance", "run",
            "--api-url", smoke_docker_server_seeded.base_url,
            "--api-key", smoke_docker_server_seeded.api_key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"archon-search maintenance run exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in combined, (
        f"archon-search maintenance run printed a traceback:\n{combined}"
    )
