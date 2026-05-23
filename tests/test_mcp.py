"""Tests for _needs_install_trigger() in archon_search.server.mcp (M12.13–M12.16)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# fastmcp is not installed in the test venv; stub it so mcp.py can be imported.
if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp

from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus, IndexingStateStore
from archon_search.server.mcp import _needs_install_trigger


# ---------------------------------------------------------------------------
# M12.13 — no state file → fresh install, must trigger
# ---------------------------------------------------------------------------

def test_M12_13_no_state_file_returns_true(tmp_path: Path) -> None:
    """M12.13: When no state file exists, _needs_install_trigger returns True."""
    store = IndexingStateStore(tmp_path)
    state = store.read()  # None — file doesn't exist yet

    assert _needs_install_trigger(state, {"docs": "/path/to/docs"}) is True


# ---------------------------------------------------------------------------
# M12.14 — state exists but a desired collection is absent → must trigger
# ---------------------------------------------------------------------------

def test_M12_14_new_collection_absent_returns_true(tmp_path: Path) -> None:
    """M12.14: State exists but a desired collection is not tracked — returns True."""
    store = IndexingStateStore(tmp_path)
    existing = IndexingState(
        collections={
            "old-col": CollectionProgress(status=IndexingStatus.DONE),
        }
    )
    store.write(existing)

    state = store.read()
    assert _needs_install_trigger(state, {"new-col": "/path/to/new"}) is True


# ---------------------------------------------------------------------------
# M12.15 — all desired collections are DONE → no trigger needed
# ---------------------------------------------------------------------------

def test_M12_15_all_done_returns_false(tmp_path: Path) -> None:
    """M12.15: All desired collections are DONE — returns False."""
    store = IndexingStateStore(tmp_path)
    existing = IndexingState(
        collections={
            "docs": CollectionProgress(status=IndexingStatus.DONE),
            "notes": CollectionProgress(status=IndexingStatus.DONE),
        }
    )
    store.write(existing)

    state = store.read()
    assert _needs_install_trigger(state, {"docs": "/d", "notes": "/n"}) is False


# ---------------------------------------------------------------------------
# M12.16 — a collection is IN_PROGRESS → implies prior crash, must trigger
# ---------------------------------------------------------------------------

def test_M12_16_in_progress_returns_true(tmp_path: Path) -> None:
    """M12.16: A desired collection is IN_PROGRESS (crash-recovery) — returns True."""
    store = IndexingStateStore(tmp_path)
    existing = IndexingState(
        collections={
            "docs": CollectionProgress(status=IndexingStatus.DONE),
            "notes": CollectionProgress(status=IndexingStatus.IN_PROGRESS),
        }
    )
    store.write(existing)

    state = store.read()
    assert _needs_install_trigger(state, {"docs": "/d", "notes": "/n"}) is True


# ---------------------------------------------------------------------------
# A5a — Path safety tests for MCP ingest_file and ingest_directory
# ---------------------------------------------------------------------------
# Uses the _FakeApp pattern from tests/server/test_mcp_search.py.
# fastmcp is already stubbed at module load above.

import sys as _sys
import types as _types

if "fastmcp" not in _sys.modules:
    _fm = _types.ModuleType("fastmcp")
    _fm.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fm.Context = type("Context", (), {})  # type: ignore[attr-defined]
    _sys.modules["fastmcp"] = _fm

from typing import Any as _Any
from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock, patch as _patch


class _FakeApp2:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, _Any] = {}

    def tool(self) -> _Any:
        def decorator(func: _Any) -> _Any:
            self.tools[func.__name__] = func
            return func
        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> _Any:
        def decorator(func: _Any) -> _Any:
            return func
        return decorator


class _FakeFastMCP2:
    def __new__(cls, name: str, **kwargs: _Any) -> _FakeApp2:  # type: ignore[misc]
        return _FakeApp2(name)


def _build_mcp_ingest_app(pipeline: _MagicMock) -> _FakeApp2:
    with _patch("archon_search.server.mcp.FastMCP", _FakeFastMCP2):
        from archon_search.server.mcp import create_app
        app = create_app(pipeline, "default-col", writer=None)
    return app  # type: ignore[return-value]


import asyncio as _asyncio


# ---------------------------------------------------------------------------
# _path_unsafe_message — all 5 reason codes
# ---------------------------------------------------------------------------


def test_mcp_path_unsafe_message_maps_all_reasons() -> None:
    """_path_unsafe_message returns non-empty string for all 5 reason codes."""
    from archon_search.server.mcp import _path_unsafe_message
    for code in ("empty", "whitespace_only", "nul_byte", "contains_dotdot", "not_absolute"):
        msg = _path_unsafe_message(code)
        assert msg, f"Expected non-empty message for reason {code!r}"


# ---------------------------------------------------------------------------
# ingest_file — path validation
# ---------------------------------------------------------------------------


def test_mcp_ingest_file_rejects_dotdot() -> None:
    """MCP ingest_file rejects dotdot path with code='path_unsafe'."""
    pipeline = _MagicMock()
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_file"](path="/foo/../bar"))
    assert result.get("code") == "path_unsafe"
    assert "contains_dotdot" in result.get("error", "").lower() or "dotdot" in result.get("error", "").lower() or ".." in result.get("error", "")


def test_mcp_ingest_file_rejects_relative() -> None:
    """MCP ingest_file rejects relative path with code='path_unsafe'."""
    pipeline = _MagicMock()
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_file"](path="./foo"))
    assert result.get("code") == "path_unsafe"
    # The message contains "not absolute" or "not_absolute" (LLM-readable phrase)
    assert "absolute" in result.get("error", "").lower()


def test_mcp_ingest_file_rejects_empty() -> None:
    """MCP ingest_file rejects empty path with code='path_unsafe'."""
    pipeline = _MagicMock()
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_file"](path=""))
    assert result.get("code") == "path_unsafe"
    assert "empty" in result.get("error", "")


def test_mcp_ingest_file_rejects_whitespace_only() -> None:
    """MCP ingest_file rejects whitespace-only path with code='path_unsafe'."""
    pipeline = _MagicMock()
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_file"](path="   "))
    assert result.get("code") == "path_unsafe"
    assert "whitespace" in result.get("error", "").lower()


def test_mcp_ingest_file_rejects_nul_byte() -> None:
    """MCP ingest_file rejects NUL byte path with code='path_unsafe'."""
    pipeline = _MagicMock()
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_file"](path="/tmp/x\x00.md"))
    assert result.get("code") == "path_unsafe"
    assert "nul" in result.get("error", "").lower()


def test_mcp_ingest_file_uses_validator_returned_path() -> None:
    """ingest_file passes the validator's Path directly to pipeline.ingest_file."""
    from pathlib import Path as _Path
    pipeline = _MagicMock()
    pipeline.ingest_file = _AsyncMock(return_value=_MagicMock())
    pipeline.ingest_file.return_value.__class__.__name__ = "IngestResult"
    from dataclasses import dataclass
    @dataclass
    class _IR:
        doc_id: str = "a" * 64
        chunk_count: int = 1
        collection: str = "default-col"
        source_path: str = "/sentinel/value"
        file_type: str = ""
        ingested_by: str = "http"
        indexed_at: str = "2024-01-01T00:00:00Z"
        metadata: dict = None  # type: ignore[assignment]
        def __post_init__(self): self.metadata = {}
    pipeline.ingest_file = _AsyncMock(return_value=_IR())

    sentinel = _Path("/sentinel/value")
    app = _build_mcp_ingest_app(pipeline)
    with _patch("archon_search.server.mcp.validate_ingest_path", return_value=sentinel):
        _asyncio.run(app.tools["ingest_file"](path="/some/valid/path"))
    # Assert pipeline.ingest_file received the sentinel Path
    pipeline.ingest_file.assert_called_once()
    call_args = pipeline.ingest_file.call_args
    assert call_args[0][0] == sentinel or call_args[1].get("path", call_args[0][0] if call_args[0] else None) == sentinel


def test_mcp_ingest_file_accepts_legitimate_absolute_path() -> None:
    """MCP ingest_file passes valid path through to pipeline (regression)."""
    from pathlib import Path as _Path
    from dataclasses import dataclass
    @dataclass
    class _IR:
        doc_id: str = "a" * 64
        chunk_count: int = 1
        collection: str = "default-col"
        source_path: str = "/tmp/legit.md"
        file_type: str = ""
        ingested_by: str = "http"
        indexed_at: str = "2024-01-01T00:00:00Z"
        metadata: dict = None  # type: ignore[assignment]
        def __post_init__(self): self.metadata = {}

    pipeline = _MagicMock()
    pipeline.ingest_file = _AsyncMock(return_value=_IR())
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_file"](path="/tmp/legit.md"))
    # On success, result should NOT have code="path_unsafe" or code="internal_error"
    assert result.get("code") not in ("path_unsafe", "internal_error")


# ---------------------------------------------------------------------------
# ingest_directory — path validation
# ---------------------------------------------------------------------------


def test_mcp_ingest_directory_rejects_dotdot() -> None:
    """MCP ingest_directory rejects dotdot path with code='path_unsafe'."""
    pipeline = _MagicMock()
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_directory"](path="/foo/../bar"))
    assert result.get("code") == "path_unsafe"


def test_mcp_ingest_directory_reuses_path_unsafe_message() -> None:
    """ingest_directory uses _path_unsafe_message (not a hardcoded copy)."""
    from archon_search.server.mcp import _path_unsafe_message
    pipeline = _MagicMock()
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_directory"](path="/tmp/x\x00.md"))
    expected_msg = _path_unsafe_message("nul_byte")
    assert result.get("error") == expected_msg


def test_mcp_ingest_directory_uses_validator_returned_path() -> None:
    """ingest_directory passes the validator's Path directly to pipeline.ingest_directory."""
    from pathlib import Path as _Path
    pipeline = _MagicMock()
    pipeline.ingest_directory = _AsyncMock(return_value=[])
    sentinel = _Path("/sentinel/value")
    app = _build_mcp_ingest_app(pipeline)
    with _patch("archon_search.server.mcp.validate_ingest_path", return_value=sentinel):
        _asyncio.run(app.tools["ingest_directory"](path="/some/valid/path"))
    pipeline.ingest_directory.assert_called_once()
    call_args = pipeline.ingest_directory.call_args
    assert call_args[0][0] == sentinel or call_args[1].get("path", call_args[0][0] if call_args[0] else None) == sentinel


def test_mcp_ingest_directory_accepts_legitimate_absolute_path() -> None:
    """MCP ingest_directory passes valid path through (regression)."""
    pipeline = _MagicMock()
    pipeline.ingest_directory = _AsyncMock(return_value=[])
    app = _build_mcp_ingest_app(pipeline)
    result = _asyncio.run(app.tools["ingest_directory"](path="/tmp/legit"))
    assert not isinstance(result, dict) or result.get("code") not in ("path_unsafe", "internal_error")
