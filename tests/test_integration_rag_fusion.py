"""C5 Task 6.2 — integration tests for RAG Fusion over the full HTTP stack.

Uses the real ``create_app()`` + ``TestClient`` pattern (matching
``tests/test_integration_hyde.py``) with a real LanceDB store but mocks
``RAGFusionGenerator.generate_variants`` to return deterministic variants,
avoiding real Anthropic API calls.

MCP tool tests build a FastMCP app backed by the real pipeline extracted from
``app.state``, using the same ``_StubFastMCP``+``importlib.reload`` pattern as
``tests/test_mcp.py``, then invoke tool functions directly.

Run with:
    uv run pytest tests/test_integration_rag_fusion.py -m integration --no-cov -q
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search._types import ChunkRecord
from archon_search.collection_meta import CollectionMeta
from archon_search.config import RAGFusionConfig, SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.store import SearchStore

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VECTOR_DIM = 384  # Must match the stub fastembed dimension (zeros(384))
_FIXED_VARIANTS = ["documentation alternative query", "hello world related search"]
_COLLECTION = "ragfusioncol"

# Fixed API key set by conftest.py
_TEST_API_KEY = "0" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ingest_chunk(tmp_path: Path) -> None:
    """Create a LanceDB store, ingest one chunk, and disconnect."""
    db_path = str(tmp_path / "search")
    chunk = ChunkRecord(
        doc_id="b" * 64,
        chunk_id="b" * 64 + "-000000",
        text="hello world documentation",
        vector=[0.0] * _VECTOR_DIM,
        source_path="/docs/hello.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )
    store = SearchStore(db_path)
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


def _make_app(tmp_path: Path, *, rag_fusion_enabled: bool = True):
    """Return a create_app() instance with RAG Fusion optionally enabled."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.rag_fusion = RAGFusionConfig(enabled=rag_fusion_enabled)
    job_store = JobStore(path=tmp_path / "jobs.json")
    return create_app(config, job_store)


# ---------------------------------------------------------------------------
# Stub FastMCP (mirrors the one in test_mcp.py) for MCP tool invocation
# ---------------------------------------------------------------------------

class _StubFastMCP:
    def __init__(self, *args, **kwargs):
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


def _get_mcp_tool_fn(tool_name: str, pipeline, config, rf_generator=None, writer=None):
    """Build a stub-backed MCP app with the real pipeline and return the named tool."""
    _fastmcp_module = "fastmcp"
    # Save current state
    _prev = sys.modules.get(_fastmcp_module)
    _prev_class = getattr(_prev, "FastMCP", None) if _prev else None

    # Install stub
    if _fastmcp_module not in sys.modules:
        _mod = types.ModuleType(_fastmcp_module)
        _mod.FastMCP = _StubFastMCP  # type: ignore[attr-defined]
        _mod.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules[_fastmcp_module] = _mod
    else:
        sys.modules[_fastmcp_module].FastMCP = _StubFastMCP  # type: ignore[attr-defined]

    _mcp_module = "archon_search.server.mcp"
    sys.modules.pop(_mcp_module, None)
    mcp_mod = importlib.import_module(_mcp_module)

    app = mcp_mod.create_app(
        pipeline,
        _COLLECTION,
        config=config,
        rag_fusion_generator=rf_generator,
        writer=writer,
    )

    # Restore real FastMCP
    if _prev_class is not None:
        sys.modules[_fastmcp_module].FastMCP = _prev_class  # type: ignore[attr-defined]
    sys.modules.pop(_mcp_module, None)

    return app._tools[tool_name]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_rag_fusion_true_returns_200_applied_true(tmp_path: Path) -> None:
    """POST /search with rag_fusion=true → 200 with rag_fusion_applied=true and rag_fusion_queries_used=2."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    with patch(
        "archon_search.rag_fusion.RAGFusionGenerator.generate_variants",
        new=AsyncMock(return_value=_FIXED_VARIANTS),
    ):
        with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as client:
            response = client.post(
                "/search",
                json={"query": "hello world", "collection": _COLLECTION, "rag_fusion": True},
            )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rag_fusion_applied"] is True
    assert data["rag_fusion_queries_used"] == 2
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_search_rag_fusion_false_returns_200_applied_false(tmp_path: Path) -> None:
    """POST /search with rag_fusion=false → 200 with rag_fusion_applied=false and rag_fusion_attempted=false."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as client:
        response = client.post(
            "/search",
            json={"query": "hello world", "collection": _COLLECTION, "rag_fusion": False},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rag_fusion_applied"] is False
    assert data["rag_fusion_attempted"] is False
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_search_rag_fusion_true_hyde_true_mutual_exclusion(tmp_path: Path) -> None:
    """POST /search with rag_fusion=true and hyde=true → rag_fusion wins, hyde_applied=false."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    mock_generate_variants = AsyncMock(return_value=_FIXED_VARIANTS)
    with patch(
        "archon_search.rag_fusion.RAGFusionGenerator.generate_variants",
        new=mock_generate_variants,
    ):
        with patch("archon_search.server.routes_search.resolve_hyde_vector") as mock_resolve_hyde:
            with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as client:
                response = client.post(
                    "/search",
                    json={
                        "query": "hello world",
                        "collection": _COLLECTION,
                        "rag_fusion": True,
                        "hyde": True,
                    },
                )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rag_fusion_applied"] is True
    assert data["hyde_applied"] is False
    mock_resolve_hyde.assert_not_called()


@pytest.mark.asyncio
async def test_search_rag_fusion_generator_returns_empty_fallback(tmp_path: Path) -> None:
    """POST /search with rag_fusion=true but generator returns [] → rag_fusion_applied=false, rag_fusion_attempted=true."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    with patch(
        "archon_search.rag_fusion.RAGFusionGenerator.generate_variants",
        new=AsyncMock(return_value=[]),
    ):
        with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as client:
            response = client.post(
                "/search",
                json={"query": "hello world", "collection": _COLLECTION, "rag_fusion": True},
            )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rag_fusion_applied"] is False
    assert data["rag_fusion_attempted"] is True
    assert data["rag_fusion_queries_used"] == 0
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_search_rag_fusion_disabled_config_skips(tmp_path: Path) -> None:
    """When config kill-switch is off, generator is never called even if rag_fusion=true in request."""
    await _ingest_chunk(tmp_path)
    # Intentionally disabled
    app = _make_app(tmp_path, rag_fusion_enabled=False)

    mock_generate_variants = AsyncMock(return_value=_FIXED_VARIANTS)
    with patch(
        "archon_search.rag_fusion.RAGFusionGenerator.generate_variants",
        new=mock_generate_variants,
    ):
        with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as client:
            response = client.post(
                "/search",
                json={"query": "hello world", "collection": _COLLECTION, "rag_fusion": True},
            )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rag_fusion_applied"] is False
    # Generator was never invoked (kill-switch prevents the LLM call)
    mock_generate_variants.assert_not_called()


@pytest.mark.asyncio
async def test_explain_rag_fusion_true_returns_sub_queries(tmp_path: Path) -> None:
    """POST /explain with rag_fusion=true → rag_fusion_applied=true and 3-entry rag_fusion_sub_queries list."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    with patch(
        "archon_search.rag_fusion.RAGFusionGenerator.generate_variants",
        new=AsyncMock(return_value=_FIXED_VARIANTS),
    ):
        with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as client:
            response = client.post(
                "/explain",
                json={"query": "hello world", "collection": _COLLECTION, "rag_fusion": True},
            )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rag_fusion_applied"] is True
    sub_queries = data["rag_fusion_sub_queries"]
    assert isinstance(sub_queries, list)
    # Expect original (index 0) + 2 variants = 3 entries
    assert len(sub_queries) == 3
    variant_indices = [entry["variant_index"] for entry in sub_queries]
    assert sorted(variant_indices) == [0, 1, 2], f"Expected variant_index 0,1,2 but got {variant_indices}"
    for entry in sub_queries:
        assert "variant_index" in entry
        assert "result_count" in entry
        assert isinstance(entry["result_count"], int)
        assert "top_doc_ids" in entry
        assert isinstance(entry["top_doc_ids"], list)


@pytest.mark.asyncio
async def test_explain_rag_fusion_true_hyde_true_mutual_exclusion(tmp_path: Path) -> None:
    """POST /explain with rag_fusion=true and hyde=true → rag_fusion wins, hyde_applied=false."""
    await _ingest_chunk(tmp_path)
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    with patch(
        "archon_search.rag_fusion.RAGFusionGenerator.generate_variants",
        new=AsyncMock(return_value=_FIXED_VARIANTS),
    ):
        with patch("archon_search.server.routes_explain.resolve_hyde_vector") as mock_resolve_hyde:
            with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as client:
                response = client.post(
                    "/explain",
                    json={
                        "query": "hello world",
                        "collection": _COLLECTION,
                        "rag_fusion": True,
                        "hyde": True,
                    },
                )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rag_fusion_applied"] is True
    assert data["hyde_applied"] is False
    mock_resolve_hyde.assert_not_called()


@pytest.mark.asyncio
async def test_search_rag_fusion_telemetry_entry_written(tmp_path: Path) -> None:
    """POST /search with rag_fusion=true writes a telemetry entry with rag_fusion_applied=true."""
    await _ingest_chunk(tmp_path)

    # Build a config with telemetry enabled, pointing to a temp dir
    log_dir = tmp_path / "search-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.rag_fusion = RAGFusionConfig(enabled=True)
    config.telemetry.enabled = True
    config.telemetry.log_dir = str(log_dir)

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)

    with patch(
        "archon_search.rag_fusion.RAGFusionGenerator.generate_variants",
        new=AsyncMock(return_value=_FIXED_VARIANTS),
    ):
        with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as client:
            response = client.post(
                "/search",
                json={"query": "hello world", "collection": _COLLECTION, "rag_fusion": True},
            )

    assert response.status_code == 200, response.text

    # Find the written JSONL file
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert len(jsonl_files) >= 1, "Expected at least one telemetry JSONL file"

    entries = []
    for jsonl_file in jsonl_files:
        for line in jsonl_file.read_text().splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    assert entries, "Expected at least one telemetry entry"
    # Find the search entry
    search_entries = [e for e in entries if e.get("endpoint") == "search"]
    assert search_entries, f"Expected a 'search' telemetry entry; got: {entries}"
    entry = search_entries[-1]
    assert entry.get("rag_fusion_applied") is True
    assert entry.get("rag_fusion_queries_used") == 2


def test_mcp_search_rag_fusion_true_applied_in_result(tmp_path: Path) -> None:
    """MCP search tool with rag_fusion=True returns rag_fusion_applied=True and rag_fusion_queries_used=2."""
    asyncio.run(_ingest_chunk(tmp_path))
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as _client:
        # Access pipeline/config while store is still connected (inside TestClient context)
        pipeline = app.state.pipeline
        config = app.state.config
        rf_generator = app.state.rag_fusion_generator

        mock_generate_variants = AsyncMock(return_value=_FIXED_VARIANTS)
        with patch.object(rf_generator, "generate_variants", new=mock_generate_variants):
            tool_fn = _get_mcp_tool_fn("search", pipeline, config, rf_generator=rf_generator)
            result = asyncio.run(
                tool_fn(
                    query="hello world",
                    collection=_COLLECTION,
                    rag_fusion=True,
                )
            )

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result}"
    assert result.get("rag_fusion_applied") is True
    assert result.get("rag_fusion_queries_used") == 2
    assert result.get("rag_fusion_attempted") is True


def test_mcp_search_rag_fusion_true_hyde_true_mutual_exclusion(tmp_path: Path) -> None:
    """MCP search tool with rag_fusion=True and hyde=True → rag_fusion wins, hyde_applied=False.

    Verifies mutual exclusion via result fields: rag_fusion_applied=True, hyde_applied=False.
    The resolve_hyde_vector call is guarded inside the mcp module. Since mcp is reloaded
    inside _get_mcp_tool_fn (to use the stub FastMCP), we verify the outcome via the
    response fields rather than patching the module-level import.
    """
    asyncio.run(_ingest_chunk(tmp_path))
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as _client:
        pipeline = app.state.pipeline
        config = app.state.config
        rf_generator = app.state.rag_fusion_generator

        mock_generate_variants = AsyncMock(return_value=_FIXED_VARIANTS)
        with patch.object(rf_generator, "generate_variants", new=mock_generate_variants):
            tool_fn = _get_mcp_tool_fn("search", pipeline, config, rf_generator=rf_generator)
            result = asyncio.run(
                tool_fn(
                    query="hello world",
                    collection=_COLLECTION,
                    rag_fusion=True,
                    hyde=True,
                )
            )

    assert isinstance(result, dict)
    assert result.get("rag_fusion_applied") is True
    assert result.get("hyde_applied") is False


def test_mcp_search_with_context_rag_fusion_result_and_telemetry(tmp_path: Path) -> None:
    """MCP search_with_context with rag_fusion=True has rag_fusion fields and closes the HyDE telemetry gap.

    Verifies: result dict has rag_fusion_applied, rag_fusion_queries_used, rag_fusion_attempted;
    telemetry entry has both hyde_applied and rag_fusion_applied fields (pre-existing gap closed).
    """
    asyncio.run(_ingest_chunk(tmp_path))
    app = _make_app(tmp_path, rag_fusion_enabled=True)

    # Use a mock writer to capture the telemetry entry
    from archon_search.telemetry.writer import TelemetryWriter
    writer = MagicMock(spec=TelemetryWriter)

    with TestClient(app, headers={"Authorization": f"Bearer {_TEST_API_KEY}"}) as _client:
        pipeline = app.state.pipeline
        config = app.state.config
        rf_generator = app.state.rag_fusion_generator

        mock_generate_variants = AsyncMock(return_value=_FIXED_VARIANTS)
        with patch.object(rf_generator, "generate_variants", new=mock_generate_variants):
            tool_fn = _get_mcp_tool_fn(
                "search_with_context", pipeline, config, rf_generator=rf_generator, writer=writer
            )
            result = asyncio.run(
                tool_fn(
                    query="hello world",
                    collection=_COLLECTION,
                    rag_fusion=True,
                )
            )

    assert isinstance(result, dict)
    assert "rag_fusion_applied" in result
    assert "rag_fusion_queries_used" in result
    assert "rag_fusion_attempted" in result

    # Verify telemetry was written with rag_fusion fields
    # (the pre-existing search_with_context telemetry gap is now closed — Task 5.1 fix:
    #  search_with_context now passes rag_fusion_applied and rag_fusion_queries_used to telemetry)
    writer.enqueue.assert_called_once()
    entry = writer.enqueue.call_args[0][0]
    assert hasattr(entry, "rag_fusion_applied"), "telemetry entry must have rag_fusion_applied (gap closed)"
    assert hasattr(entry, "rag_fusion_queries_used"), "telemetry entry must have rag_fusion_queries_used (gap closed)"
    assert entry.rag_fusion_applied is True
    assert entry.rag_fusion_queries_used == 2
