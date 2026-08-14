"""Shared helpers for integration and e2e tests.

New file — do NOT modify the existing ``tests/conftest.py``.

``make_real_app`` is a context manager that yields ``(TestClient, config,
api_key)`` backed by real ``SearchStore`` + ``SearchPipeline`` in
``tmp_path``.  All env vars are set via ``monkeypatch`` so they auto-revert
after each test.  Usage::

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        ...

``ingest_doc`` POSTs to /ingest and polls GET /jobs/{id} until done.

``search`` POSTs to /search and returns the result items.

``make_real_pipeline`` creates an async store+pipeline pair for tests that
call async pipeline/store methods directly without going through TestClient.
"""
from __future__ import annotations

import json
import sys
import time
import types
from contextlib import contextmanager
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _stub_anthropic_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """anthropic is an optional extra absent from the test env. create_app's
    provider guard (_check_provider_deps) imports it when anthropic-backed
    HyDE/RAG Fusion is enabled, and AnthropicQueryExpansionProvider.__init__
    reads anthropic.APIError, so provide a bare stub for every integration test
    (make_real_app enables features with the default anthropic provider). No
    integration test asserts anthropic-absence behavior — those unit tests live
    in tests/test_hyde.py / tests/test_rag_fusion.py and are unaffected."""
    stub = types.ModuleType("anthropic")
    stub.APIError = type("APIError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", stub)


@contextmanager
def make_real_app(
    tmp_path,
    monkeypatch,
    *,
    backup_enabled: bool = False,
    maintenance_enabled: bool = False,
    mcp_enabled: bool = False,
    telemetry_enabled: bool = False,
    hash_doc_ids_enabled: bool = False,
    namespaces: dict[str, str] | None = None,
    hyde_enabled: bool = False,
    rag_fusion_enabled: bool = False,
    max_fanout: int | None = None,
    top_k_max: int | None = None,
    toml_content: str | None = None,
    graph_enabled: bool = False,
    openai_shim_enabled: bool = False,
) -> Iterator[tuple[TestClient, Any, str]]:
    """Context manager yielding ``(TestClient, config, api_key)`` backed by real store+pipeline.

    Uses ``monkeypatch.setenv`` for env vars so they auto-revert after each test.
    Pass ``backup_enabled=True`` only in Task 2.2 backup tests.
    Pass ``maintenance_enabled=True`` to enable the MaintenanceLoop with interval_hours=1.
    Pass ``telemetry_enabled=True`` to enable the TelemetryWriter (writes JSONL to
    ``tmp_path/search-logs/``).  The log_dir is always set to ``tmp_path/search-logs``
    so any accidental write in a disabled-telemetry test is caught in the temp dir.
    Pass ``hash_doc_ids_enabled=True`` to enable HMAC hashing of result_doc_ids in telemetry
    (D8 / BE-4). Requires ``telemetry_enabled=True`` to produce JSONL output.
    Pass ``namespaces={'key_hex': 'ns-name', ...}`` for multi-namespace tests.
    Pass ``hyde_enabled=True`` to enable HyDE in config (E0b / BE-8, T-3).
    Pass ``rag_fusion_enabled=True`` to enable RAG Fusion in config (E0b / BE-8, T-3).
    Pass ``toml_content='[section]\\nkey = value\\n'`` to exercise the full TOML loading path
    (write TOML → load_config(path) → create_app).  Env vars are set before load_config so
    ARCHON_SEARCH_API_KEY is picked up by _apply_env_overrides.  Note: db_path and
    telemetry.log_dir are always force-overridden after load_config by this helper so
    test isolation is guaranteed regardless of what TOML specifies.
    Cannot be combined with ``max_fanout`` / ``top_k_max`` kwargs; pass those values via
    the TOML string instead.
    Pass ``graph_enabled=True`` to enable the graph feature (``config.graph.enabled=True``).
    Callers must patch spaCy in sys.modules BEFORE entering the context manager (create_app
    calls _check_graph_deps which imports spacy synchronously).
    The TestClient lifespan (startup + shutdown) is managed by the context block.
    """
    import secrets

    from archon_search.config import SearchConfig, load_config
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    if toml_content is not None and (max_fanout is not None or top_k_max is not None):
        raise ValueError(
            "Pass toml_content OR max_fanout/top_k_max kwargs, not both. "
            "Encode the values inside the TOML string instead."
        )

    api_key = secrets.token_hex(32)
    # Env vars must be set before load_config so _apply_env_overrides picks them up.
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    if toml_content is not None:
        toml_path = tmp_path / "archon-search.toml"
        toml_path.write_text(toml_content, encoding="utf-8")
        cfg = load_config(path=toml_path)
    else:
        cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0  # disabled by default; trigger loop self-exits immediately
    # MCP is mounted only when explicitly requested; default off keeps unrelated
    # integration tests fast and avoids the FastMCP session-manager startup cost.
    cfg.mcp.enabled = mcp_enabled
    # Always point telemetry log_dir to tmp_path so any accidental write is caught
    # in the test's temp dir, not the developer's home directory.
    cfg.telemetry.log_dir = str(tmp_path / "search-logs")

    if backup_enabled:
        cfg.backup.interval_hours = 1
        cfg.backup.output_dir = str(tmp_path / "backups")

    if maintenance_enabled:
        cfg.maintenance.interval_hours = 1  # enabled; manual trigger still fires immediately

    cfg.telemetry.enabled = telemetry_enabled
    if hash_doc_ids_enabled:
        cfg.telemetry.hash_doc_ids = True

    if namespaces is not None:
        cfg.namespaces = namespaces

    if hyde_enabled:
        cfg.hyde.enabled = True

    if rag_fusion_enabled:
        cfg.rag_fusion.enabled = True

    if max_fanout is not None:
        cfg.max_fanout = max_fanout

    if top_k_max is not None:
        cfg.top_k_max = top_k_max

    if graph_enabled:
        cfg.graph.enabled = True

    if openai_shim_enabled:
        cfg.openai_shim.enabled = True

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,  # replaced in lifespan startup
    )

    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app) as client:
        _await_startup_sync(app)
        yield client, cfg, api_key


def _await_startup_sync(app, timeout_s: float = 30.0) -> None:
    """Block until the lifespan's background startup sync has finished.

    The lifespan hands ``collection_sync.sync()`` to ``asyncio.create_task`` so uvicorn
    can bind the port immediately. That makes the sync concurrent with the first
    requests, which every integration test would otherwise race — the sync ingests the
    same configured directories the tests ingest explicitly. The task lives on
    TestClient's portal-thread loop, so it cannot be awaited from here; poll ``.done()``.
    """
    task = getattr(app.state, "_startup_sync_task", None)
    if task is None:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if task.done():
            return
        time.sleep(0.01)
    raise TimeoutError(f"startup sync did not finish within {timeout_s}s")


def ingest_doc(
    client: TestClient,
    col: str,
    text: str,
    path: str,
    *,
    api_key: str,
    timeout_s: float = 10.0,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """POST /ingest with inline documents, poll until done. Returns job_id.

    NOTE: ``SearchPipeline`` does not implement ``ingest_documents``. The route
    handler logs a warning and marks the job DONE without writing any data.
    Use ``ingest_file_via_path`` instead when you need real data in the store.
    This helper is retained for tests that exercise the /ingest path validation
    (auth, path-safety) rather than actual data ingestion.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)
    body = {
        "collection": col,
        "documents": [{"text": text, "source_path": path}],
    }
    resp = client.post("/ingest", json=body, headers=headers)
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        status = r.json()["status"]
        if status == "DONE":
            return job_id
        if status == "FAILED":
            pytest.fail(f"ingest job failed (job_id={job_id}): {r.json()}")
        time.sleep(0.1)
    pytest.fail(f"ingest did not complete in {timeout_s}s (job_id={job_id})")


def ingest_file_via_path(
    client: TestClient,
    col: str,
    file_path: str,
    *,
    api_key: str,
    timeout_s: float = 10.0,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """POST /ingest with a file path, poll until done. Returns job_id."""
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)
    body = {"collection": col, "path": file_path}
    resp = client.post("/ingest", json=body, headers=headers)
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        status = r.json()["status"]
        if status == "DONE":
            return job_id
        if status == "FAILED":
            pytest.fail(f"ingest job failed (job_id={job_id}): {r.json()}")
        time.sleep(0.1)
    pytest.fail(f"ingest did not complete in {timeout_s}s (job_id={job_id})")


def search(
    client: TestClient,
    col: str,
    query: str,
    *,
    api_key: str,
    **filters: Any,
) -> list[dict]:
    """POST /search, assert 200, return items."""
    headers = {"Authorization": f"Bearer {api_key}"}
    body: dict[str, Any] = {"collection": col, "query": query}
    if filters:
        body["filters"] = filters
    resp = client.post("/search", json=body, headers=headers)
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()["results"]


async def make_real_pipeline(tmp_path, monkeypatch):
    """Create a real SearchStore + SearchPipeline for async pipeline tests.

    Callers must be ``async def`` and call ``await make_real_pipeline(...)``.
    Returns ``(store, pipeline)``.

    Uses stub embedder (same stubs as the test suite's ``install_stubs()``).
    Calls ``await store.connect()`` before returning — missing this is the
    most likely failure since ``SearchStore`` raises ``RuntimeError("not
    connected")`` without it.
    """
    from unittest.mock import MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    store = SearchStore(str(tmp_path / "db"))
    await store.connect()

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    return store, pipeline


# ---------------------------------------------------------------------------
# spaCy stub helpers — used by graph-related e2e tests (T-1, T-2, T-3, T-4).
# ---------------------------------------------------------------------------


def install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy NLP model that recognizes entity names in text.

    Recognises: "Alice" → PERSON, "Bob" → PERSON, "Google" → ORG.

    Must be called BEFORE make_real_app(graph_enabled=True) because create_app
    calls _check_graph_deps which imports spaCy synchronously.

    Usage::
        install_spacy_stub(monkeypatch)
        with make_real_app(..., graph_enabled=True) as (client, cfg, api_key):
            ...
    """
    import sys
    import types

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list) -> None:
            self.ents = ents

    _ENTITY_MAP = [
        ("Alice", "PERSON"),
        ("Bob", "PERSON"),
        ("Google", "ORG"),
    ]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents = [
                _FakeEnt(name, label)
                for name, label in _ENTITY_MAP
                if name in text
            ]
            return _FakeDoc(ents)

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


# ---------------------------------------------------------------------------
# MCP JSON-RPC test-client helpers — generic across fixtures/tool names, used
# by graph-related e2e tests (e.g. T-3's graph_impact e2e test).
# ---------------------------------------------------------------------------


def mcp_headers(token: str, session_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


def mcp_initialize(client: TestClient, token: str) -> str:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "integration-test", "version": "1.0"},
            },
        },
        headers=mcp_headers(token),
    )
    assert resp.status_code == 200, f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    session_id = resp.headers.get("mcp-session-id")
    assert session_id is not None, "MCP initialize did not return mcp-session-id header"

    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers=mcp_headers(token, session_id),
    )
    assert resp.status_code in (200, 202), (
        f"MCP notifications/initialized failed: {resp.status_code} {resp.text[:300]}"
    )
    return session_id


def mcp_tool_call(
    client: TestClient, token: str, session_id: str, tool_name: str, arguments: dict
) -> dict:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/call {tool_name} failed: {resp.status_code} {resp.text[:300]}"
    )
    data_lines = [
        line[5:].strip() for line in resp.text.split("\n") if line.startswith("data:")
    ]
    assert data_lines, f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    body = json.loads(data_lines[-1])
    assert body.get("jsonrpc") == "2.0"

    rpc_result = body.get("result", {})
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


def install_k8s_synonym_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a content-dependent spaCy stub for K8s/Kubernetes synonym e2e tests.

    Returns "K8s" (label "ORG" → EntityType.system) only when "K8s" appears in text.
    Returns "Kubernetes" (label "ORG" → EntityType.system) only when "Kubernetes" appears.
    Both entities get the same entity_type so SynonymDetector groups them together.

    Must be called BEFORE make_real_app(graph_enabled=True) because create_app
    calls _check_graph_deps which imports spaCy synchronously.

    Used by T-1 (synonym_search_e2e), T-2 (alias_file_manual_synonym_edge), and
    T-3 (health_metrics_synonym_e2e).

    Usage::
        install_k8s_synonym_spacy_stub(monkeypatch)
        with make_real_app(..., graph_enabled=True) as (client, cfg, api_key):
            ...
    """
    import sys
    import types

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list) -> None:
            self.ents = ents

    # ORG → EntityType.system in graph_extractor._LABEL_TO_ENTITY_TYPE.
    # Content-dependent: each document produces exactly the entity named in its text.
    _ENTITY_MAP = [
        ("K8s", "ORG"),
        ("Kubernetes", "ORG"),
    ]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents = [
                _FakeEnt(name, label)
                for name, label in _ENTITY_MAP
                if name in text
            ]
            return _FakeDoc(ents)

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)
