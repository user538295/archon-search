"""Integration tests for MCP export_collection and import_collection tools (Task 7.1).

Verifies:
- export_collection returns a job dict with status=QUEUED on success.
- export_collection returns McpErrorResponse on PathUnsafeError (path outside data dir).
- import_collection returns a job dict with status=QUEUED on success.
- import_collection returns McpErrorResponse when on_error is invalid.
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


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


def _make_mcp_app(
    pipeline: Any,
    *,
    config: Any = None,
    job_store: Any = None,
) -> _FakeApp:
    # Ensure fastmcp is in sys.modules before patching so the import in mcp.py works.
    if "fastmcp" not in sys.modules:
        _fastmcp_stub = types.ModuleType("fastmcp")
        _fastmcp_stub.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp_stub.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp_stub

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(
            pipeline, "default", config=config, job_store=job_store
        )


def _make_config(embedding_model: str = "BAAI/bge-small-en-v1.5") -> Any:
    from archon_search.config import SearchConfig

    cfg = SearchConfig()
    cfg.embedding_model = embedding_model
    return cfg


def _make_pipeline(collection_meta: Any = None) -> MagicMock:
    pipeline = MagicMock()
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=collection_meta)
    pipeline.store = store
    return pipeline


def _make_job_store(tmp_path: Path) -> Any:
    from archon_search.jobs.store import JobStore

    return JobStore(path=tmp_path / "jobs.json")


def _make_collection_meta(
    name: str = "my-col",
    embedding_model: str = "BAAI/bge-small-en-v1.5",
) -> Any:
    from archon_search.collection_meta import CollectionMeta

    return CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model=embedding_model,
    )


def _make_valid_tar(tmp_path: Path, embedding_model: str = "BAAI/bge-small-en-v1.5") -> Path:
    """Create a minimal valid .tar.gz archive with manifest.json and documents.jsonl."""
    manifest = {
        "schema_version": 1,
        "collection": "my-col",
        "exported_at": "2024-01-01T00:00:00+00:00",
        "doc_count": 0,
        "active_embedding_model": embedding_model,
        "description": "",
        "archon_search_version": "dev",
    }
    archive_path = tmp_path / "test.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        manifest_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))

        docs_bytes = b""
        info2 = tarfile.TarInfo(name="documents.jsonl")
        info2.size = len(docs_bytes)
        tf.addfile(info2, io.BytesIO(docs_bytes))
    return archive_path


# ---------------------------------------------------------------------------
# Test: tool count is 13 (11 previous + export_collection + import_collection)
# ---------------------------------------------------------------------------


def test_mcp_tool_count_is_13(tmp_path: Path) -> None:
    """create_app must register exactly 13 tools after adding export/import."""
    pipeline = _make_pipeline()
    job_store = _make_job_store(tmp_path)
    app = _make_mcp_app(pipeline, job_store=job_store)
    assert len(app.tools) == 13, f"Expected 13 tools, got {len(app.tools)}: {list(app.tools)}"


# ---------------------------------------------------------------------------
# Test 1: export_collection happy path — returns job dict with status=QUEUED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_export_collection_returns_job(tmp_path: Path) -> None:
    """export_collection returns a dict with job_id and status=QUEUED."""
    from archon_search.paths import get_data_dir

    meta = _make_collection_meta()
    pipeline = _make_pipeline(meta)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["export_collection"]

    # Use the data dir as the output_path so it passes validation
    output_dir = get_data_dir() / "exports"
    result = await tool(collection="my-col", output_path=str(output_dir))

    assert isinstance(result, dict)
    assert "job_id" in result
    assert result["status"] == "QUEUED"
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_mcp_export_collection_default_output_path(tmp_path: Path) -> None:
    """export_collection with empty output_path uses the data dir exports directory."""
    meta = _make_collection_meta()
    pipeline = _make_pipeline(meta)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["export_collection"]

    result = await tool(collection="my-col", output_path="")

    assert isinstance(result, dict)
    assert "job_id" in result
    assert result["status"] == "QUEUED"


# ---------------------------------------------------------------------------
# Test 2: export_collection path outside data dir → McpErrorResponse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_export_path_unsafe(tmp_path: Path) -> None:
    """export_collection with path outside data dir returns McpErrorResponse."""
    meta = _make_collection_meta()
    pipeline = _make_pipeline(meta)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["export_collection"]

    # /tmp is not inside get_data_dir()
    result = await tool(collection="my-col", output_path="/tmp/unsafe-export")

    assert isinstance(result, dict)
    assert "error" in result
    assert result.get("code") == "path_unsafe"


# ---------------------------------------------------------------------------
# Test 3: export_collection collection not found → McpErrorResponse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_export_collection_not_found(tmp_path: Path) -> None:
    """export_collection returns not_found error when collection does not exist."""
    from archon_search.paths import get_data_dir

    # pipeline.store.get_collection_meta returns None
    pipeline = _make_pipeline(collection_meta=None)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["export_collection"]

    output_dir = get_data_dir() / "exports"
    result = await tool(collection="ghost-col", output_path=str(output_dir))

    assert isinstance(result, dict)
    assert "error" in result
    assert result.get("code") == "not_found"


# ---------------------------------------------------------------------------
# Test 4: import_collection happy path — returns job dict with status=QUEUED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_import_collection_returns_job(tmp_path: Path) -> None:
    """import_collection returns a dict with job_id and status=QUEUED."""
    archive_path = _make_valid_tar(tmp_path)

    # No existing collection
    pipeline = _make_pipeline(collection_meta=None)
    store = pipeline.store
    store.count_chunks = AsyncMock(return_value=0)

    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["import_collection"]

    # Patch get_data_dir to include tmp_path so path validation passes
    with patch("archon_search.server.mcp.get_data_dir", return_value=tmp_path):
        result = await tool(collection="new-col", path=str(archive_path))

    assert isinstance(result, dict)
    assert "job_id" in result
    assert result["status"] == "QUEUED"


# ---------------------------------------------------------------------------
# Test 5: import_collection invalid on_error → McpErrorResponse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_import_invalid_on_error(tmp_path: Path) -> None:
    """import_collection returns McpErrorResponse for invalid on_error value."""
    archive_path = _make_valid_tar(tmp_path)

    pipeline = _make_pipeline(collection_meta=None)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["import_collection"]

    with patch("archon_search.server.mcp.get_data_dir", return_value=tmp_path):
        result = await tool(
            collection="new-col",
            path=str(archive_path),
            on_error="invalid",
        )

    assert isinstance(result, dict)
    assert "error" in result
    assert result.get("code") == "validation_error"


# ---------------------------------------------------------------------------
# Test 6: import_collection path outside data dir → McpErrorResponse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_import_path_unsafe(tmp_path: Path) -> None:
    """import_collection with path outside allowed dirs returns McpErrorResponse."""
    pipeline = _make_pipeline(collection_meta=None)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["import_collection"]

    # Use a path inside /tmp — validate_export_path will reject it since
    # get_data_dir() is not /tmp
    result = await tool(collection="new-col", path="/tmp/archive.tar.gz")

    assert isinstance(result, dict)
    assert "error" in result
    assert result.get("code") == "path_unsafe"
