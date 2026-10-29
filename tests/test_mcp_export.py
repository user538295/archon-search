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
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.store import COLLECTION_UNAVAILABLE_DETAIL, CollectionOwnershipError
from tests._fastmcp_stub import StubFastMCP, stubbed_fastmcp

pytestmark = pytest.mark.integration


def _make_mcp_app(
    pipeline: Any,
    *,
    config: Any = None,
    job_store: Any = None,
) -> StubFastMCP:
    with stubbed_fastmcp():
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
    store.assert_collection_access = AsyncMock()
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


def _own_dir(tmp_path: Path) -> Path:
    """The default namespace's export directory below *tmp_path*, which the import tests patch in as the data dir."""
    directory = tmp_path / "exports" / "default"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _make_valid_tar(
    tmp_path: Path,
    embedding_model: str = "BAAI/bge-small-en-v1.5",
    schema_version: object = 1,
    namespace: str = "default",
) -> Path:
    """Create a minimal valid .tar.gz archive with manifest.json and documents.jsonl."""
    manifest = {
        "schema_version": schema_version,
        "collection": "my-col",
        "namespace": namespace,
        "exported_at": "2024-01-01T00:00:00+00:00",
        "doc_count": 0,
        "active_embedding_model": embedding_model,
        "description": "",
        "archon_search_version": "dev",
    }
    archive_path = _own_dir(tmp_path) / "test.tar.gz"
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


def test_mcp_tool_count_is_15(tmp_path: Path) -> None:
    """create_app must register exactly 16 tools (13 base + 2 graph inspection E2b + 1 graph_impact E2g BE-9)."""
    pipeline = _make_pipeline()
    job_store = _make_job_store(tmp_path)
    app = _make_mcp_app(pipeline, job_store=job_store)
    assert len(app.tools) == 16, f"Expected 16 tools, got {len(app.tools)}: {list(app.tools)}"


# ---------------------------------------------------------------------------
# Test 1: export_collection happy path — returns job dict with status=QUEUED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_export_collection_returns_job(tmp_path: Path) -> None:
    """export_collection returns a dict with job_id and status=QUEUED."""
    meta = _make_collection_meta()
    pipeline = _make_pipeline(meta)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["export_collection"]

    result = await tool(collection="my-col")

    assert isinstance(result, dict)
    assert "job_id" in result
    assert result["status"] == "QUEUED"
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_mcp_export_collection_writes_to_the_namespace_exports_directory(tmp_path: Path) -> None:
    """The archive path is ``<data_dir>/exports/<namespace>/<collection>-<timestamp>.tar.gz``."""
    from archon_search.paths import get_data_dir

    meta = _make_collection_meta()
    pipeline = _make_pipeline(meta)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["export_collection"]

    result = await tool(collection="my-col")

    assert Path(result["output_path"]).parent == get_data_dir() / "exports" / "default"


# ---------------------------------------------------------------------------
# Test 2: export_collection path outside data dir → McpErrorResponse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_export_collection_takes_no_output_path(tmp_path: Path) -> None:
    """The export directory is pinned to the caller's namespace (SecReview #4): there is no ``output_path`` argument."""
    meta = _make_collection_meta()
    pipeline = _make_pipeline(meta)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["export_collection"]

    with pytest.raises(TypeError, match="output_path"):
        await tool(collection="my-col", output_path="/tmp/unsafe-export")


# ---------------------------------------------------------------------------
# Test 3: export_collection collection not found → McpErrorResponse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_export_collection_not_found(tmp_path: Path) -> None:
    """export_collection returns not_found error when collection does not exist."""
    # pipeline.store.get_collection_meta returns None
    pipeline = _make_pipeline(collection_meta=None)
    job_store = _make_job_store(tmp_path)
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    tool = app.tools["export_collection"]

    result = await tool(collection="ghost-col")

    assert isinstance(result, dict)
    assert "error" in result
    assert result.get("code") == "not_found"


@pytest.mark.asyncio
async def test_mcp_export_of_another_namespaces_collection_is_refused_with_the_fixed_text(tmp_path: Path) -> None:
    """A name another namespace owns answers the fixed refusal, never the owner, and queues no job."""
    pipeline = _make_pipeline(collection_meta=None)
    pipeline.store.assert_collection_access = AsyncMock(side_effect=CollectionOwnershipError("foreign-col", "default"))
    job_store = _make_job_store(tmp_path)
    app = _make_mcp_app(pipeline, config=_make_config(), job_store=job_store)

    result = await app.tools["export_collection"](collection="foreign-col")

    assert result == {"error": COLLECTION_UNAVAILABLE_DETAIL, "code": "collection_unavailable"}
    assert job_store.list() == []


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
# import_collection archive/manifest errors carry only text archon-search builds
# ---------------------------------------------------------------------------


async def _import_tool_result(tmp_path: Path, archive_path: Path) -> dict[str, Any]:
    app = _make_mcp_app(
        _make_pipeline(collection_meta=None), config=_make_config(), job_store=_make_job_store(tmp_path)
    )
    with patch("archon_search.server.mcp.get_data_dir", return_value=tmp_path):
        return await app.tools["import_collection"](collection="new-col", path=str(archive_path))


@pytest.mark.asyncio
async def test_mcp_import_corrupt_archive_sends_sanitized_error(tmp_path: Path) -> None:
    """tarfile/gzip error text (derived from the archive's bytes) never reaches the client."""
    from archon_search.jobs.export_archive import INVALID_ARCHIVE_DETAIL

    corrupt = _own_dir(tmp_path) / "corrupt.tar.gz"
    corrupt.write_bytes(b"not a real gzip archive")

    result = await _import_tool_result(tmp_path, corrupt)

    assert result == {"error": INVALID_ARCHIVE_DETAIL, "code": "validation_error"}


@pytest.mark.asyncio
async def test_mcp_import_rejected_manifest_keeps_its_own_message(tmp_path: Path) -> None:
    """A non-JSON manifest gets archon-search's wording, never the JSON parser's."""
    archive_path = _own_dir(tmp_path) / "bad-manifest.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        for name, payload in (("manifest.json", b"{not json"), ("documents.jsonl", b"")):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

    result = await _import_tool_result(tmp_path, archive_path)

    assert result == {
        "error": "invalid manifest: manifest.json is not valid UTF-8 JSON",
        "code": "validation_error",
    }


@pytest.mark.asyncio
async def test_mcp_import_unexpected_manifest_error_sends_sanitized_error(tmp_path: Path) -> None:
    from archon_search.jobs.export_archive import (
        INVALID_MANIFEST_DETAIL,
        ImportArchiveReader,
    )

    def _internal_failure(_self: ImportArchiveReader) -> dict:
        raise ValueError("internal: /var/lib/secret/state.db is locked")

    with patch.object(ImportArchiveReader, "read_manifest", _internal_failure):
        result = await _import_tool_result(tmp_path, _make_valid_tar(tmp_path))

    assert result == {"error": f"invalid manifest: {INVALID_MANIFEST_DETAIL}", "code": "validation_error"}


@pytest.mark.asyncio
async def test_mcp_import_deeply_nested_manifest_is_rejected_not_raised(tmp_path: Path) -> None:
    """Nesting deep enough to make the JSON parser raise RecursionError is a refusal, not a crash."""
    archive_path = _own_dir(tmp_path) / "deep-manifest.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        for name, payload in (("manifest.json", b"[" * 100_000), ("documents.jsonl", b"")):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

    result = await _import_tool_result(tmp_path, archive_path)

    assert result == {
        "error": "invalid manifest: manifest.json is not valid UTF-8 JSON",
        "code": "validation_error",
    }


@pytest.mark.asyncio
async def test_mcp_import_model_mismatch_truncates_archive_supplied_model(tmp_path: Path) -> None:
    """A crafted model name is quoted truncated, in the REST route's exact wording."""
    from archon_search.jobs.export_archive import (
        ARCHIVE_VALUE_MAX_CHARS,
        quote_archive_value,
    )

    crafted_model = "m" * 10_000
    result = await _import_tool_result(tmp_path, _make_valid_tar(tmp_path, embedding_model=crafted_model))

    assert result == {
        "error": (
            f"archive uses model {quote_archive_value(crafted_model)}; "
            f"server is configured with {_make_config().embedding_model!r}"
        ),
        "code": "embedding_model_mismatch",
    }
    assert len(quote_archive_value(crafted_model)) == ARCHIVE_VALUE_MAX_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crafted_schema", "quoted"),
    [
        (["v"] * 1_000, "['v', 'v', 'v', 'v', 'v', 'v', 'v', 'v', 'v', 'v', 'v', 'v', 'v', 'v', 'v', 'v'…"),
        ({"k": "x" * 200}, "{'k': '" + "x" * 72 + "…"),
    ],
    ids=["long-list", "long-dict"],
)
async def test_mcp_import_schema_mismatch_truncates_archive_supplied_value(
    tmp_path: Path, crafted_schema: object, quoted: str
) -> None:
    """A non-int schema_version is quoted truncated, in the REST route's exact wording."""
    from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION

    result = await _import_tool_result(tmp_path, _make_valid_tar(tmp_path, schema_version=crafted_schema))

    assert result == {
        "error": (
            f"archive has schema_version={quoted}; server expects {EXPORT_SCHEMA_VERSION!r}; "
            "use ignore_schema_version=true to bypass"
        ),
        "code": "schema_version_mismatch",
    }


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
