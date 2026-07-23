"""Session-scoped fixture for Docker-mode smoke tests.

Provides:
- ``_docker_env()`` — thin wrapper around the parent conftest's
  ``_subprocess_env()`` that adds ``ARCHON_SEARCH_CONTAINER=1``.
- ``smoke_docker_server`` — a session-scoped fixture that spawns a real
  ``archon-search serve`` process with the Docker env, waits for it to
  become healthy+ready, then tears it down cleanly via SIGTERM.

Unlike the parent ``smoke_server`` fixture, this fixture does NOT seed a
corpus.  Server-dependent tests requiring a pre-seeded corpus live in BE-5
and will use a separate fixture once that task is implemented.
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
    _subprocess_env,
    _terminate,
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
    here — this fixture exists only to prove the serve lifecycle (S2).
    """
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("smoke_docker_data")
    api_key = secrets.token_hex(32)

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
