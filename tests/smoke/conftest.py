"""Session-scoped fixture that spawns a real ``archon-search serve`` subprocess.

Shared by every module in ``tests/smoke/`` — a single server subprocess is
started once per test session, pre-seeded with a tiny real corpus, and torn
down at session end. See ``Documentation/Backlog/2026-07-15-010-live-smoke-test-team-plan.md``
(contract C1 — SmokeServerProcess) for the full startup/teardown contract this
fixture implements.

Helper functions (``_free_port``, ``_start_server``, ``_poll_health_and_ready``,
``_seed_corpus``) are module-level so they can be exercised directly by
``tests/smoke/test_conftest.py`` without going through the fixture.

``smoke_server_graph_enabled`` (BE-9) is a second, separate session-scoped
fixture for tests needing ``[graph] enabled = true`` (e.g. the S3
``graph build-communities --wait`` e2e test) — kept off ``smoke_server``
itself so the graph feature stays off by default for the rest of the suite.
It ``importorskip``s spaCy before starting the server (the graph extra is not
in the ``dev`` group) and seeds a multi-entity corpus so the extracted graph is
large enough for community clustering.
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


def _seed_corpus(
    base_url: str, api_key: str, corpus_dir: Path, proc: subprocess.Popen, *, collection: str = "smoke"
) -> None:
    """POST /collections/ {path} then poll the job to DONE, then assert doc_count > 0.

    ``collection`` must match the basename of ``corpus_dir`` — the server derives
    the collection name from the ingested directory's path (see
    ``path_to_collection_name`` in ``routes_collections.py``).
    """
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

    detail_resp = httpx.get(f"{base_url}/collections/{collection}", headers=headers, timeout=5)
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


# Graph-corpus docs: unlike ``_CORPUS_DOCS`` (generic prose that yields at most
# one entity per doc and therefore no co-occurrence edges), these sentences pack
# several spaCy-recognised named entities (PERSON/ORG/GPE) into each chunk so the
# real extraction pipeline produces MULTIPLE nodes AND co-occurrence edges. Only
# then does ``CommunityBuilder.build`` clear its ``len(nodes) < 2`` short-circuit
# and actually run Leiden clustering — the real S3 happy path T-1 exercises. A
# single-entity corpus would populate a node but never reach clustering.
_GRAPH_CORPUS_DOCS: dict[str, str] = {
    "team.txt": (
        "Alice works at Google in California. Bob also works at Google with "
        "Alice. Alice and Bob collaborate on the Kubernetes project at Google."
    ),
    "office.txt": (
        "Google is headquartered in Mountain View, California. Alice and Bob "
        "both moved to Mountain View to join the Kubernetes team at Google."
    ),
}


def _write_graph_corpus(corpus_dir: Path) -> None:
    for filename, text in _GRAPH_CORPUS_DOCS.items():
        (corpus_dir / filename).write_text(text)


def _write_graph_enabled_config(data_dir: Path) -> None:
    """Write ``[graph] enabled = true`` to the config path ``_subprocess_env``
    points the subprocess at, before the server starts.

    ``_subprocess_env`` always sets ``ARCHON_SEARCH_CONFIG`` to
    ``{data_dir}/archon-search.toml`` (a path that does not exist by default,
    so ``load_config()`` falls back to defaults — see that function's comment).
    Writing this file at that same path before ``_start_server`` is called is
    the only hook needed to turn the graph feature on for a smoke server.
    """
    (data_dir / "archon-search.toml").write_text("[graph]\nenabled = true\n")


@pytest.fixture(scope="session")
def smoke_server_graph_enabled(tmp_path_factory) -> Iterator[SmokeServer]:
    """Like ``smoke_server``, but with ``[graph] enabled = true``.

    A separate server subprocess and corpus from ``smoke_server`` — the graph
    feature must be off by default for the rest of ``tests/smoke/``, so this
    cannot just be a config tweak on the shared fixture.

    Memory note: both this and ``smoke_server`` are session-scoped, so once a
    session touches tests that use each, BOTH server subprocesses stay alive
    until session teardown (``xdist_group("smoke_e2e")`` serialises test
    *execution* onto one worker, not fixture *lifetime*). That is bounded — at
    most two servers on the single smoke worker, only when both fixtures are
    actually requested in a run — and the smoke suite is opt-in
    (``uv run pytest tests/smoke/``), never part of the default suite.

    Graph extras guard: ``graph.enabled = true`` makes the server raise
    ``ConfigError`` at startup if spaCy is absent (it lives in the optional
    ``archon-search[graph]`` extra, not the ``dev`` group). ``importorskip`` is
    therefore done HERE, before the subprocess is spawned — a guard in a
    consuming test body runs only after this session fixture has already been
    set up, too late to convert a startup failure into a clean skip.

    Seeds a multi-entity corpus (``_GRAPH_CORPUS_DOCS``) into a collection named
    ``smoke_graph`` via the real REST API, so extraction runs through the real
    pipeline. Asserts the resulting graph has >= 2 nodes AND >= 1 edge before
    yielding, so a consumer is guaranteed a graph large enough for
    ``CommunityBuilder.build`` to reach Leiden clustering rather than its
    single-node short-circuit — the S3 happy path, not the S8 empty-graph
    failure path.
    """
    pytest.importorskip("spacy")

    port = _free_port()
    data_dir = tmp_path_factory.mktemp("smoke_data_graph")
    corpus_dir = tmp_path_factory.mktemp("smoke_graph", numbered=False)
    api_key = secrets.token_hex(32)

    _write_graph_corpus(corpus_dir)
    _write_graph_enabled_config(data_dir)

    proc = _start_server(port=port, data_dir=data_dir, api_key=api_key)
    base_url = f"http://127.0.0.1:{port}"

    try:
        _seed_corpus(base_url, api_key, corpus_dir, proc, collection="smoke_graph")
        graph_resp = httpx.get(
            f"{base_url}/graph/smoke_graph",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        assert graph_resp.status_code == 200, (
            f"fixture sanity: GET /graph/smoke_graph failed: "
            f"{graph_resp.status_code} {graph_resp.text}"
        )
        graph = graph_resp.json()
        assert graph["node_count"] >= 2, (
            f"graph-enabled corpus pre-seed produced {graph['node_count']} node(s); "
            "need >= 2 so CommunityBuilder.build reaches Leiden clustering, not its "
            "single-node short-circuit — fixture misconfiguration"
        )
        assert graph["edge_count"] >= 1, (
            f"graph-enabled corpus pre-seed produced {graph['edge_count']} edge(s); "
            "need >= 1 co-occurrence edge for meaningful clustering — fixture misconfiguration"
        )
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
