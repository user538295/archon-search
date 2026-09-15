"""S582 regression — HyDE generation is not bounded by the per-request search budget.

``routes_search.search`` resolves HyDE (``resolve_hyde_vector``, routes_search.py:262)
*before* it enters ``run_within_budget(..., timeout=_SEARCH_TIMEOUT_SECONDS)``
(routes_search.py:283/298). ``hyde_may_embed`` only guards a cold *embedder*; nothing
bounds the provider's HTTP call to the LLM. With ``[hyde] provider = "llama_cpp"``
pointed at a llama-server that accepts the connection but never answers — a server
still loading its model, the exact case ``llama_cpp_provider._post_chat_completion``
documents — the request parks for the whole ``[hyde].timeout_seconds`` (unclamped by
``config.py`` ``_apply_toml``) outside any server-side budget, and a client whose read
timeout is shorter gets no response at all: the ``status=0``/``body=None`` symptom of
the S278/S288/S290 out-of-budget-wait family, this time via the HyDE leg.

Run with:
    uv run pytest tests/integration/test_s582_hyde_outside_search_budget.py --no-cov -q
"""
from __future__ import annotations

import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search._types import ChunkRecord
from archon_search.collection_meta import CollectionMeta
from archon_search.config import HyDEConfig, SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server import routes_search
from archon_search.server.app import create_app
from archon_search.store import SearchStore

pytestmark = pytest.mark.integration

_VECTOR_DIM = 384  # must match the stub fastembed dimension (zeros(384))
_COLLECTION = "s582col"

#: What the operator configured as the HyDE LLM call timeout. Unclamped by config
#: validation — any value > 0 is accepted ([hyde].timeout_seconds, config.py:744).
_HYDE_TIMEOUT_SECONDS = 8.0

#: The server's own per-request search budget for this test, shrunk from the 30 s
#: default so the assertion below does not need a 30 s wait to be meaningful.
_SEARCH_BUDGET_SECONDS = 1.0

#: Wall-clock ceiling for the request. Generous multiple of the budget above — it is
#: not a latency SLA, it only has to sit well below _HYDE_TIMEOUT_SECONDS so that
#: "the HyDE call ran outside the budget" is the only thing it can catch.
_RESPONSE_DEADLINE_SECONDS = 4.0


class _SilentLlamaServer:
    """A socket that accepts connections and never writes a byte back.

    Models a llama-server that is up but not answering (still loading its model).
    Connections are held open, so the provider's httpx call blocks on the read
    until its own timeout — it does not fail fast the way a closed port does.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port: int = self._sock.getsockname()[1]
        self.connections: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self.connections.append(conn)  # held open, never answered

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        for conn in self.connections:
            conn.close()
        self._sock.close()


async def _ingest_chunk(tmp_path: Path) -> None:
    """Create a LanceDB store, ingest one chunk, and disconnect."""
    chunk = ChunkRecord(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000000",
        text="hello world documentation",
        vector=[0.0] * _VECTOR_DIM,
        source_path="/docs/hello.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )
    store = SearchStore(str(tmp_path / "search"))
    await store.connect()
    await store.ensure_collection(_COLLECTION, _VECTOR_DIM)
    await store.ingest_chunks(_COLLECTION, [chunk])
    await store.update_collection_meta(
        CollectionMeta(
            name=_COLLECTION,
            active_embedding_model="BAAI/bge-small-en-v1.5",
            namespace="default",
        )
    )
    await store.disconnect()


@pytest.mark.asyncio
async def test_search_hyde_llama_cpp_unresponsive_server_stays_within_search_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search with hyde=true must answer within the search budget when the
    configured llama-server accepts connections but never responds."""
    await _ingest_chunk(tmp_path)
    llama = _SilentLlamaServer()
    try:
        config = SearchConfig()
        config.db_path = str(tmp_path / "search")
        config.hyde = HyDEConfig(
            enabled=True,
            provider="llama_cpp",
            model="test-model",
            timeout_seconds=_HYDE_TIMEOUT_SECONDS,
            llama_cpp_base_url=f"http://127.0.0.1:{llama.port}",
        )
        app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
        monkeypatch.setattr(
            routes_search, "_SEARCH_TIMEOUT_SECONDS", _SEARCH_BUDGET_SECONDS
        )
        key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

        with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
            started = time.monotonic()
            response = client.post(
                "/search",
                json={"query": "test query", "collection": _COLLECTION, "hyde": True},
            )
            elapsed = time.monotonic() - started

        # Guards the assertion below against passing for the wrong reason: if HyDE
        # were skipped (cold-embedder gate, disabled config), nothing would ever
        # reach the llama-server and the request would be fast regardless.
        assert llama.connections, (
            "the llama_cpp HyDE provider never connected — HyDE was skipped, so this "
            "test would pass without exercising the out-of-budget wait"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["hyde_applied"] is False
        assert body["expansion_used"] is False
        assert body["expansion_warning"] == "HyDE expansion failed"
        assert elapsed < _RESPONSE_DEADLINE_SECONDS, (
            f"POST /search with hyde=true took {elapsed:.1f}s against a "
            f"{_SEARCH_BUDGET_SECONDS:.1f}s search budget — the HyDE provider call "
            f"(timeout_seconds={_HYDE_TIMEOUT_SECONDS:.1f}) runs outside "
            "run_within_budget, so an unresponsive llama-server parks the connection "
            "past any client read timeout (status=0, body=None)"
        )
    finally:
        llama.close()
