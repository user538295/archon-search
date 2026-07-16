"""Session-scoped fixture that spawns a real ``archon-search serve`` subprocess.

Shared by every module in ``tests/smoke/`` — a single server subprocess is
started once per test session, pre-seeded with a tiny real corpus, and torn
down at session end. See ``Documentation/Backlog/2026-07-15-010-live-smoke-test-team-plan.md``
(contract C1 — SmokeServerProcess) for the full startup/teardown contract this
fixture implements.

Helper functions (``_free_port``, ``_start_server``, ``_poll_health_and_ready``,
``_seed_corpus``) are module-level so they can be exercised directly by
``tests/smoke/test_conftest.py`` without going through the fixture.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

# --- Timing budgets (seconds) ---
_HEALTH_READY_TIMEOUT_S: float = 30.0
_JOB_POLL_TIMEOUT_S: float = 60.0
_POLL_INTERVAL_S: float = 0.5
_TEARDOWN_TIMEOUT_S: float = 10.0

_TERMINAL_JOB_STATUSES = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}

# Corpus files: each must contain real prose so ingestion produces > 0 chunks
# (empty files produce 0 chunks and mask the bug under test — see plan Data section).
_CORPUS_DOCS: dict[str, str] = {
    "doc1.txt": (
        "Archon Search is a hybrid retrieval server that combines dense vector "
        "search with full-text search to find relevant passages in a document "
        "collection. It uses a cross-encoder reranker to refine the top results."
    ),
    "doc2.txt": (
        "The router pre-ranks collections by comparing a query embedding against "
        "each collection's centroid vector. This lets a single query fan out "
        "across many collections without scoring every document up front."
    ),
    "doc3.txt": (
        "Chunking splits long documents into overlapping windows before they are "
        "embedded. Good chunk boundaries preserve enough context for the "
        "reranker to judge relevance without truncating the original sentence."
    ),
}


@dataclass(frozen=True)
class SmokeServer:
    """Handle to the running smoke-test server subprocess."""

    proc: subprocess.Popen
    port: int
    base_url: str
    api_key: str
    data_dir: Path
    corpus_dir: Path


def _free_port() -> int:
    """Bind to port 0 to obtain a free ephemeral port, then release it.

    No TOCTOU risk in practice for this fixture: the port is handed
    immediately to the subprocess we spawn next.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _subprocess_env(*, port: int, data_dir: Path, api_key: str) -> dict[str, str]:
    env = {
        **os.environ,
        "ARCHON_SEARCH_PORT": str(port),
        "ARCHON_SEARCH_DATA_DIR": str(data_dir),
        # Point at a non-existent path under the isolated data dir so the
        # subprocess never reads the developer's real
        # ~/.archon-search/archon-search.toml (load_config() treats a missing
        # file as "use defaults") — without this, an operator TOML enabling
        # e.g. [database].multilingual = true with fasttext-wheel uninstalled
        # would make the smoke server fail to start on that machine.
        "ARCHON_SEARCH_CONFIG": str(data_dir / "archon-search.toml"),
        "ARCHON_SEARCH_API_KEY": api_key,
        "FASTEMBED_CACHE_PATH": str(Path.home() / ".cache/fastembed"),
        "PYTEST_ADDOPTS": "",
    }
    # An operator's exported ARCHON_SEARCH_HOST would otherwise leak in via
    # **os.environ above and change which interface serve mode binds to,
    # breaking the 0.0.0.0-default assumption the S15 bind-collision test
    # relies on. Force serve mode's 0.0.0.0 default to always apply.
    env.pop("ARCHON_SEARCH_HOST", None)
    return env


def _start_server(*, port: int, data_dir: Path, api_key: str) -> subprocess.Popen:
    """Spawn ``archon-search serve`` and wait for it to become healthy+ready.

    Raises ``RuntimeError`` (message containing "server did not start" and the
    captured stderr) if the process exits early or ``/health``/``/ready`` do
    not succeed within ``_HEALTH_READY_TIMEOUT_S``.
    """
    proc = subprocess.Popen(
        ["uv", "run", "archon-search", "serve"],
        env=_subprocess_env(port=port, data_dir=data_dir, api_key=api_key),
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
    return proc


def _poll_health_and_ready(proc: subprocess.Popen, base_url: str) -> None:
    """Poll GET /health then GET /ready until both succeed, or raise RuntimeError.

    /health is auth-exempt and returns 200 unconditionally once the app is up.
    /ready returns 503 (with a JSON body) until storage is initialised; success
    is `response.json()["ready"] is True`.
    """
    # Each phase gets its own full budget — otherwise a slow /health phase
    # would starve /ready of poll time within a single shared deadline.
    deadline = time.monotonic() + _HEALTH_READY_TIMEOUT_S

    # --- Phase 1: GET /health ---
    healthy = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise _startup_failure(proc, "process exited before /health succeeded")
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2)
            if resp.status_code == 200:
                healthy = True
                break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            pass
        time.sleep(_POLL_INTERVAL_S)
    if not healthy:
        raise _startup_failure(proc, "server did not start: /health did not return 200 within timeout")

    # --- Phase 2: GET /ready ---
    # Fresh deadline: /health succeeding must not eat into /ready's budget.
    deadline = time.monotonic() + _HEALTH_READY_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise _startup_failure(proc, "process exited before /ready succeeded")
        try:
            resp = httpx.get(f"{base_url}/ready", timeout=2)
            if resp.json().get("ready") is True:
                ready = True
                break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            pass
        time.sleep(_POLL_INTERVAL_S)
    if not ready:
        raise _startup_failure(proc, "server did not start: /ready did not report ready=true within timeout")


def _startup_failure(proc: subprocess.Popen, reason: str) -> RuntimeError:
    """Build the RuntimeError raised on startup failure, capturing stderr.

    On the health/ready timeout path the subprocess is still alive, so a bare
    ``communicate()`` would time out and yield empty stderr. Kill it first so
    the buffered output can be drained.
    """
    if proc.poll() is None:
        proc.kill()
    stderr = ""
    try:
        _, stderr = proc.communicate(timeout=2)
    except (subprocess.TimeoutExpired, ValueError):
        pass
    return RuntimeError(f"server did not start: {reason}\n--- captured stderr ---\n{stderr}")


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort cleanup for a subprocess that failed to start cleanly."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=_TEARDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_TEARDOWN_TIMEOUT_S)


def _write_corpus(corpus_dir: Path) -> None:
    for filename, text in _CORPUS_DOCS.items():
        (corpus_dir / filename).write_text(text)


def _seed_corpus(base_url: str, api_key: str, corpus_dir: Path, proc: subprocess.Popen) -> None:
    """POST /collections/ {path} then poll the job to DONE, then assert doc_count > 0."""
    headers = {"Authorization": f"Bearer {api_key}"}

    resp = httpx.post(
        f"{base_url}/collections/",
        json={"path": str(corpus_dir)},
        headers=headers,
        timeout=10,
    )
    assert resp.status_code == 202, (
        f"POST /collections/ failed to enqueue ingest: {resp.status_code} {resp.text}"
    )
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + _JOB_POLL_TIMEOUT_S
    job: dict = {}
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"corpus pre-seed job {job_id} failed: server process exited "
                f"during ingest (returncode={proc.returncode})"
            )
        try:
            job_resp = httpx.get(f"{base_url}/jobs/{job_id}", headers=headers, timeout=5)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise RuntimeError(f"corpus pre-seed job {job_id} failed: server unreachable mid-ingest ({exc})") from exc
        job = job_resp.json()
        if job.get("status") in _TERMINAL_JOB_STATUSES:
            break
        time.sleep(_POLL_INTERVAL_S)
    else:
        raise RuntimeError(f"corpus pre-seed job {job_id} did not reach a terminal status within {_JOB_POLL_TIMEOUT_S}s")

    if job.get("status") != "DONE":
        raise RuntimeError(
            f"corpus pre-seed job {job_id} ended in status {job.get('status')} (error={job.get('error')})"
        )

    detail_resp = httpx.get(f"{base_url}/collections/smoke", headers=headers, timeout=5)
    assert detail_resp.status_code == 200
    doc_count = detail_resp.json()["doc_count"]
    assert doc_count > 0, "corpus pre-seed produced doc_count == 0 — fixture misconfiguration"


@pytest.fixture(scope="session")
def smoke_server(tmp_path_factory) -> Iterator[SmokeServer]:
    """Start a real ``archon-search serve`` subprocess, seed it, yield a handle.

    Session-scoped: one server subprocess is shared by every test in
    ``tests/smoke/`` (serialised via the ``xdist_group("smoke_e2e")`` marker
    each test module carries).

    Teardown: SIGTERM, wait up to 10s, escalate to SIGKILL and fail the test
    if the process does not exit cleanly.
    """
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("smoke_data")
    corpus_dir = tmp_path_factory.mktemp("smoke", numbered=False)
    # secrets.token_hex(32) yields a 64-char lowercase-hex string; the
    # validator (key_manager.py:497 _validate_key) only requires lowercase
    # hex (no length constraint) — 64 chars is a property of token_hex(32),
    # not a validator requirement.
    api_key = secrets.token_hex(32)

    _write_corpus(corpus_dir)

    proc = _start_server(port=port, data_dir=data_dir, api_key=api_key)
    base_url = f"http://127.0.0.1:{port}"

    try:
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
        pytest.fail("server did not stop cleanly on SIGTERM")
