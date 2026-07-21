"""Tests for the MCP `explain` tool (Task 4.1)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Stub fastmcp so mcp.py can be imported without the real package interfering.
if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp

from archon_search._types import ChunkRecord
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.embedder import Embedder
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker
from archon_search.server.app import create_app as create_rest_app


# ---------------------------------------------------------------------------
# FastMCP stub (captures registered tools in a dict)
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func
        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> Any:
        def decorator(func: Any) -> Any:
            return func
        return decorator


class _FakeFastMCP:
    def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
        return _FakeApp(name)


def _make_mcp_app(pipeline: Any, *, config: SearchConfig | None = None, writer: Any = None) -> _FakeApp:
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(pipeline, "default", writer=writer, config=config)


# ---------------------------------------------------------------------------
# Mock backends + store helpers (mirrors tests/test_pipeline_explain.py)
# ---------------------------------------------------------------------------


class MockEmbedderBackend:
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class DistinctTextRerankerBackend:
    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [int(hashlib.sha256(t.encode()).hexdigest(), 16) % 100000 / 100000 for _, t in pairs]


def _make_doc_id(n: int) -> str:
    return hashlib.sha256(f"doc-{n:04d}".encode()).hexdigest()


def _chunk(doc_id: str, idx: int, text: str) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx + 1)] * 4,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )


def _make_records(n: int, *, id_offset: int = 0) -> list[ChunkRecord]:
    doc_id = _make_doc_id(id_offset)
    return [_chunk(doc_id, i, f"common alpha beta token unique{i + id_offset}") for i in range(n)]


async def _build_real_pipeline(tmp_path: Path, config: SearchConfig) -> SearchPipeline:
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await store.ensure_collection("docs", 4)
    await store.ingest_chunks("docs", _make_records(8))
    await store.rebuild_fts_index("docs")
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    return SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mcp_app_registers_explain_tool() -> None:
    """The MCP app registers `explain`; total tool count is 16 (13 base + 2 graph E2b + 1 graph_impact E2g BE-9)."""
    app = _make_mcp_app(MagicMock())
    assert "explain" in app.tools
    assert len(app.tools) == 16


@pytest.mark.asyncio
async def test_mcp_explain_rejects_empty_query() -> None:
    """Empty query → structured validation error (no pipeline call)."""
    pipeline = MagicMock()
    app = _make_mcp_app(pipeline)
    result = await app.tools["explain"](query="   ")
    assert result["code"] == "validation_error"


@pytest.mark.asyncio
async def test_explain_rerank_false_multi_mcp_returns_error() -> None:
    """MCP explain with collections=[a,b] and rerank=False → validation_error."""
    pipeline = MagicMock()
    app = _make_mcp_app(pipeline)
    result = await app.tools["explain"](query="q", collections=["a", "b"], rerank=False)
    assert result["code"] == "validation_error"
    assert result["error"] == "reranking cannot be disabled for multi-collection search in v1"


@pytest.mark.asyncio
async def test_mcp_explain_both_collection_and_collections_returns_error() -> None:
    """Supplying both collection and collections → validation_error."""
    pipeline = MagicMock()
    app = _make_mcp_app(pipeline)
    result = await app.tools["explain"](query="q", collection="x", collections=["y"])
    assert result["code"] == "validation_error"


@pytest.mark.asyncio
async def test_mcp_explain_multi_collection_happy_path() -> None:
    """MCP explain with collections returns results carrying per-collection provenance."""
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.pipeline import ExplainPipelineResult

    def _cand(doc: str, collection: str) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id=doc * 64,
            chunk_id=f"{doc * 64}-000000",
            text="t",
            source_path=f"/tmp/{doc}.md",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.5, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.5, reranker_score=0.7,
            ),
            collection=collection,
        )

    pipeline = MagicMock()
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(
            top_results=[_cand("a", "A"), _cand("b", "B")],
            near_misses=[],
            acl_filtered=False,
        )
    )
    app = _make_mcp_app(pipeline)

    result = await app.tools["explain"](query="q", collections=["A", "B"])

    assert "error" not in result
    assert {r["collection"] for r in result["results"]} == {"A", "B"}
    assert pipeline.explain.await_args.kwargs["collections"] == ["A", "B"]


@pytest.mark.asyncio
async def test_mcp_explain_missing_collection_returns_not_found() -> None:
    """Unknown pinned collection → not_found."""
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    app = _make_mcp_app(pipeline)
    result = await app.tools["explain"](query="hello", collection="missing")
    assert result["code"] == "not_found"


@pytest.mark.asyncio
async def test_mcp_explain_collectionless_no_collections_returns_not_found() -> None:
    """Collectionless with empty store → not_found."""
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])
    config = SearchConfig()
    app = _make_mcp_app(pipeline, config=config)
    result = await app.tools["explain"](query="hello")
    assert result["code"] == "not_found"


@pytest.mark.asyncio
async def test_mcp_explain_rest_parity(tmp_path: Path) -> None:
    """REST /explain and MCP explain return deep-equal payloads for the same inputs."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.observability.stage_timings_enabled = False  # timings are non-deterministic; exclude from parity check
    pipeline = await _build_real_pipeline(tmp_path, config)

    # MCP side
    mcp_app = _make_mcp_app(pipeline, config=config, writer=None)
    mcp_result = await mcp_app.tools["explain"](query="common alpha beta", collection="docs", top_k=3)

    # REST side — same pipeline injected onto the app state
    rest_app = create_rest_app(config, JobStore(path=tmp_path / "jobs.json"))
    rest_app.state.pipeline = pipeline
    rest_app.state.embedder = pipeline._global_embedder
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    transport = httpx.ASGITransport(app=rest_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Authorization": f"Bearer {key}"}) as ac:
        rest_resp = await ac.post("/explain", json={"query": "common alpha beta", "collection": "docs", "top_k": 3})

    assert rest_resp.status_code == 200, rest_resp.text
    # Deep-equal after a JSON round trip (dodges float-formatting drift).
    # embedding_model is excluded: REST populates it (Task 7.2/7.3); MCP does so in Task 7.4.
    mcp_cmp = {k: v for k, v in json.loads(json.dumps(mcp_result)).items() if k != "embedding_model"}
    rest_cmp = {k: v for k, v in rest_resp.json().items() if k != "embedding_model"}
    assert mcp_cmp == rest_cmp

    await pipeline.store.disconnect()


@pytest.mark.asyncio
async def test_mcp_explain_collectionless_rest_parity(tmp_path: Path) -> None:
    """Collectionless: routing block is populated AND REST<->MCP stay deep-equal."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.routing_confidence_threshold = 0.0
    config.observability.stage_timings_enabled = False  # timings are non-deterministic; exclude from parity check
    pipeline = await _build_real_pipeline(tmp_path, config)

    # Second collection so routing.candidates is non-trivial (and sorted).
    store = pipeline.store
    await store.ensure_collection("code", 4)
    await store.ingest_chunks("code", _make_records(5, id_offset=20))
    await store.rebuild_fts_index("code")
    await store.update_collection_meta(
        CollectionMeta(
            name="code",
            centroid=[0.4, 0.3, 0.2, 0.1],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    mcp_app = _make_mcp_app(pipeline, config=config, writer=None)
    mcp_result = await mcp_app.tools["explain"](query="common alpha beta", top_k=3)

    rest_app = create_rest_app(config, JobStore(path=tmp_path / "jobs.json"))
    rest_app.state.pipeline = pipeline
    rest_app.state.embedder = pipeline._global_embedder
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    transport = httpx.ASGITransport(app=rest_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Authorization": f"Bearer {key}"}) as ac:
        rest_resp = await ac.post("/explain", json={"query": "common alpha beta", "top_k": 3})

    assert rest_resp.status_code == 200, rest_resp.text
    # Routing must be populated over MCP (the config-driven collectionless path).
    assert mcp_result["routing"] is not None
    assert len(mcp_result["routing"]["candidates"]) == 2
    # embedding_model is excluded: REST populates it (Task 7.3); MCP does so in Task 7.4.
    mcp_cmp = {k: v for k, v in json.loads(json.dumps(mcp_result)).items() if k != "embedding_model"}
    rest_cmp = {k: v for k, v in rest_resp.json().items() if k != "embedding_model"}
    assert mcp_cmp == rest_cmp

    await store.disconnect()


@pytest.mark.asyncio
async def test_mcp_explain_telemetry_no_query(tmp_path: Path) -> None:
    """MCP explain telemetry must not contain the query key or the raw query string."""
    from archon_search.telemetry.reader import TelemetryReader
    from archon_search.telemetry.writer import TelemetryWriter

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    pipeline = await _build_real_pipeline(tmp_path, config)

    logs_dir = tmp_path / "telemetry-logs"
    logs_dir.mkdir()
    writer = TelemetryWriter(logs_dir)
    await writer.start()

    app = _make_mcp_app(pipeline, config=config, writer=writer)
    unique_query = "mcp-telemetry-xyzzy-7788"
    result = await app.tools["explain"](query=unique_query, collection="docs", top_k=3)
    result_count = len(result["results"])

    await writer.drain_and_stop()

    reader = TelemetryReader(logs_dir, retention_days=30)
    today = datetime.now(UTC).date()
    entries, skipped = reader.read_entries(today, today)
    explain_entries = [e for e in entries if e.endpoint == "explain"]
    assert len(explain_entries) >= 1
    assert explain_entries[0].collection == "docs"
    assert explain_entries[0].result_count == result_count

    for jsonl_file in logs_dir.glob("*.jsonl"):
        raw = jsonl_file.read_text(encoding="utf-8")
        assert unique_query not in raw
        for line in raw.splitlines():
            if line.strip():
                assert "query" not in json.loads(line)

    await pipeline.store.disconnect()


@pytest.mark.asyncio
async def test_mcp_explain_metadata_lookup_error_returns_metadata_store_error() -> None:
    """MetadataLookupError during multi-collection explain → code=metadata_store_error."""
    from archon_search.pipeline import MetadataLookupError

    pipeline = MagicMock()
    pipeline.explain = AsyncMock(side_effect=MetadataLookupError(RuntimeError("db boom")))
    app = _make_mcp_app(pipeline)

    result = await app.tools["explain"](query="q", collections=["a", "b"])

    assert result["code"] == "metadata_store_error"
    assert "metadata store" in result["error"]
