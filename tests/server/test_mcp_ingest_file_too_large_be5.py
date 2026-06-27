"""BE-5 — MCP ingest_file error code propagation + IngestResultSchema.code field.

Tests:
- test_mcp_ingest_result_schema_code_field_defaults_none
    IngestResultSchema.code is None when not set.
- test_mcp_ingest_result_schema_code_field_set
    IngestResultSchema.from_result() maps code="file_too_large" from IngestResult.
- test_mcp_ingest_file_too_large_code
    MCP ingest_file with oversized path and max_file_mb set → result dict has
    status="error", code="file_too_large", actionable message.

Scenario S4 (unit half): MCP ingest_file result carries code="file_too_large".
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Stub fastmcp only when the real package is unavailable.  When the real
# fastmcp IS importable (CI and local dev), prefer it so the module-level
# ``from fastmcp import FastMCP`` in mcp.py gets the real class and the
# integration tests in the same xdist_group("mcp") worker are not poisoned
# by a stub that does not accept constructor arguments.
if "fastmcp" not in sys.modules:
    try:
        import fastmcp as _real_fastmcp  # type: ignore[import]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _stub_fastmcp = types.ModuleType("fastmcp")
        _stub_fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _stub_fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _stub_fastmcp


# ---------------------------------------------------------------------------
# FastMCP stub (same pattern as test_mcp_ingest_503.py)
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


def _make_app(pipeline: MagicMock) -> _FakeApp:
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# IngestResultSchema.code field tests
# ---------------------------------------------------------------------------


def test_mcp_ingest_result_schema_code_field_defaults_none() -> None:
    """IngestResultSchema.code is None when IngestResult.code is not set."""
    from archon_search._types import IngestResult
    from archon_search.server.mcp_schemas import IngestResultSchema

    r = IngestResult(doc_id="d1", chunks_created=1, status="ok")
    schema = IngestResultSchema.from_result(r)
    assert schema.code is None
    dumped = schema.model_dump(mode="json")
    assert "code" in dumped, "code key must be present in model_dump output (not excluded)"
    assert dumped["code"] is None, "code=None must serialize as null, not be excluded"


def test_mcp_ingest_result_schema_code_field_set() -> None:
    """IngestResultSchema.from_result() maps code='file_too_large' from IngestResult."""
    from archon_search._types import IngestResult
    from archon_search.server.mcp_schemas import IngestResultSchema

    r = IngestResult(
        doc_id="d1",
        chunks_created=0,
        status="error",
        error="File size 150 MB exceeds the configured limit of 100 MB (`[ingest].max_file_mb`). "
               "Raise the limit in `archon-search.toml` or split the file.",
        code="file_too_large",
    )
    schema = IngestResultSchema.from_result(r)
    assert schema.code == "file_too_large"
    assert schema.status == "error"


def test_mcp_ingest_result_schema_fields_includes_code() -> None:
    """IngestResultSchema model fields include 'code'."""
    from archon_search.server.mcp_schemas import IngestResultSchema

    actual = set(IngestResultSchema.model_fields.keys())
    assert actual == {"doc_id", "chunks_created", "status", "error", "warnings", "code"}
    assert "needs_recompute" not in actual


def test_mcp_ingest_result_schema_rejects_invalid_code() -> None:
    """IngestResultSchema raises ValidationError for unknown code values.

    The Literal["file_too_large"] | None type acts as a schema-boundary guard:
    any future IngestResult.code value not yet added to the schema will fail
    loudly at the MCP boundary rather than passing through silently.
    """
    from pydantic import ValidationError
    from archon_search.server.mcp_schemas import IngestResultSchema

    with pytest.raises(ValidationError):
        IngestResultSchema(
            doc_id="d1",
            chunks_created=0,
            status="error",
            code="unknown_code",
        )


# ---------------------------------------------------------------------------
# MCP ingest_file tool — file_too_large result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_ingest_file_too_large_code() -> None:
    """MCP ingest_file with oversized path → result has status="error", code="file_too_large".

    The pipeline.ingest_file() returns an error IngestResult (not raises).
    IngestResultSchema.from_result() maps the code field to the response dict.
    """
    from archon_search._types import IngestResult

    error_msg = (
        "File size 150 MB exceeds the configured limit of 100 MB "
        "(`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."
    )
    oversized_result = IngestResult(
        doc_id="oversized-file",
        chunks_created=0,
        status="error",
        error=error_msg,
        code="file_too_large",
    )

    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(return_value=oversized_result)

    app = _make_app(pipeline)
    result = await app.tools["ingest_file"](path="/tmp/big.pdf", collection=None)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("status") == "error", f"Expected status='error': {result!r}"
    assert result.get("code") == "file_too_large", f"Expected code='file_too_large': {result!r}"
    assert "error" in result, f"'error' key missing: {result!r}"
    assert "[ingest].max_file_mb" in result["error"], (
        f"Actionable message not in error: {result['error']!r}"
    )
    assert result.get("chunks_created") == 0, f"Expected chunks_created=0: {result!r}"


@pytest.mark.asyncio
async def test_mcp_ingest_directory_code_propagates_in_list() -> None:
    """ingest_directory returns list where per-file code='file_too_large' propagates correctly.

    Verifies the list comprehension path in mcp.py serializes each IngestResult
    via IngestResultSchema.from_result(), including the code field.
    """
    from archon_search._types import IngestResult

    ok_result = IngestResult(doc_id="small-file", chunks_created=3, status="ok")
    error_result = IngestResult(
        doc_id="big-file",
        chunks_created=0,
        status="error",
        error="File size 150 MB exceeds the configured limit of 100 MB (`[ingest].max_file_mb`). "
              "Raise the limit in `archon-search.toml` or split the file.",
        code="file_too_large",
    )

    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(return_value=[ok_result, error_result])

    app = _make_app(pipeline)
    result = await app.tools["ingest_directory"](path="/tmp/somedir", collection=None)

    assert isinstance(result, list), f"Expected list, got {type(result)}: {result!r}"
    assert len(result) == 2, f"Expected 2 results, got {len(result)}: {result!r}"

    ok_item = result[0]
    assert ok_item.get("status") == "ok"
    assert ok_item.get("code") is None, f"Expected code=None for ok item: {ok_item!r}"

    error_item = result[1]
    assert error_item.get("status") == "error"
    assert error_item.get("code") == "file_too_large", (
        f"Expected code='file_too_large' for oversized item: {error_item!r}"
    )
