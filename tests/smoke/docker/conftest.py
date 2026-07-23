"""Session-scoped fixture for Docker-mode smoke tests.

Provides:
- ``_docker_env()`` — thin wrapper around the parent conftest's
  ``_subprocess_env()`` that adds ``ARCHON_SEARCH_CONTAINER=1``.
- ``smoke_docker_server`` — a session-scoped fixture that spawns a real
  ``archon-search serve`` process with the Docker env, waits for it to
  become healthy+ready, then tears it down cleanly via SIGTERM.
- ``smoke_docker_server_seeded`` — like ``smoke_docker_server`` but also
  pre-seeds a "smoke" collection, for BE-5 server-dependent CLI proofs.
"""

from __future__ import annotations

import secrets
import subprocess
from collections.abc import Iterator

import pytest

from tests.smoke.conftest import (
    SmokeServer,
    _TEARDOWN_TIMEOUT_S,
    _free_port,
    _poll_health_and_ready,
    _seed_corpus,
    _subprocess_env,
    _terminate,
    _write_corpus,
)


def _docker_env(*, port: int, data_dir, api_key: str) -> dict[str, str]:
    """Return a subprocess env dict with ``ARCHON_SEARCH_CONTAINER=1`` added.

    Delegates to the parent conftest's ``_subprocess_env()`` and then injects
    the container-detection flag so the server (and CLI commands) behave as
    they would inside the Docker image.
    """
    env = _subprocess_env(port=port, data_dir=data_dir, api_key=api_key)
    env["ARCHON_SEARCH_CONTAINER"] = "1"
    return env


@pytest.fixture(scope="session")
def smoke_docker_server(tmp_path_factory) -> Iterator[SmokeServer]:
    """Spawn ``archon-search serve`` with Docker env; yield a handle; SIGTERM.

    Session-scoped and serialised via the ``xdist_group("smoke_e2e")`` marker
    each test module in ``tests/smoke/docker/`` carries.  No corpus is seeded
    here — this fixture exists only to prove the serve lifecycle (S2) and the
    status HTTP-fallback path (S3, BE-2).

    Telemetry is enabled via the config file so ``archon-search status`` has a
    visible field to print when the HTTP fallback fires — without this the
    default server returns ``telemetry: null`` and the CLI prints nothing even
    when the HTTP call succeeds, making S3's "≥1 telemetry field present"
    assertion impossible.
    """
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("smoke_docker_data")
    api_key = secrets.token_hex(32)

    # Write a config enabling telemetry so GET /status returns a non-null
    # telemetry sub-object and the CLI status command has something to print.
    (data_dir / "archon-search.toml").write_text("[telemetry]\nenabled = true\n")

    env = _docker_env(port=port, data_dir=data_dir, api_key=api_key)

    proc = subprocess.Popen(
        ["uv", "run", "archon-search", "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _poll_health_and_ready(proc, base_url)
    except RuntimeError:
        _terminate(proc)
        raise

    server = SmokeServer(
        proc=proc,
        port=port,
        base_url=base_url,
        api_key=api_key,
        data_dir=data_dir,
        corpus_dir=tmp_path_factory.mktemp("smoke_docker_corpus"),
    )

    yield server

    proc.terminate()
    try:
        proc.wait(timeout=_TEARDOWN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_TEARDOWN_TIMEOUT_S)
        pytest.fail("docker smoke server did not stop cleanly on SIGTERM")


@pytest.fixture(scope="session")
def smoke_docker_server_seeded(tmp_path_factory) -> Iterator[SmokeServer]:
    """Like ``smoke_docker_server`` but with a pre-seeded "smoke" collection.

    Session-scoped fixture for BE-5 server-dependent CLI proofs.  Seeds the
    same three-document corpus as the parent ``smoke_server`` fixture so
    ``collection list`` reports "smoke" in its output and ``collection info``
    can find the collection.

    Teardown: SIGTERM, wait up to 10s, SIGKILL on timeout.
    """
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("smoke_docker_seeded_data")
    # corpus_dir must have basename "smoke" so path_to_collection_name() derives
    # "smoke" — matching the _seed_corpus default collection= kwarg.
    corpus_dir = tmp_path_factory.mktemp("smoke_docker_seeded_corpus_parent") / "smoke"
    corpus_dir.mkdir()
    api_key = secrets.token_hex(32)

    _write_corpus(corpus_dir)
    (data_dir / "archon-search.toml").write_text("[telemetry]\nenabled = true\n")

    env = _docker_env(port=port, data_dir=data_dir, api_key=api_key)

    proc = subprocess.Popen(
        ["uv", "run", "archon-search", "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _poll_health_and_ready(proc, base_url)
        _seed_corpus(base_url, api_key, corpus_dir, proc)
    except Exception:
        _terminate(proc)
        raise

    server = SmokeServer(
        proc=proc,
        port=port,
        base_url=base_url,
        api_key=api_key,
        data_dir=data_dir,
        corpus_dir=corpus_dir,
    )

    yield server

    proc.terminate()
    try:
        proc.wait(timeout=_TEARDOWN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_TEARDOWN_TIMEOUT_S)
        pytest.fail("docker smoke seeded server did not stop cleanly on SIGTERM")
