"""End-to-end telemetry tests — FEAT-039b Task 3.5.

Exercises every entry variant so the union of observed JSONL keys equals
DOCUMENTED_SCHEMA_FIELDS, and asserts the privacy sentinel never leaks into
log files or logger message strings.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search._types import SearchResult
from archon_search.config import SearchConfig, TelemetryConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.telemetry.entry import DOCUMENTED_SCHEMA_FIELDS, TelemetryEntry
from archon_search.telemetry.pruner import Pruner
from archon_search.telemetry.writer import TelemetryWriter

# ---------------------------------------------------------------------------
# Privacy sentinel — hyphens ensure it cannot collide with hex query_id or
# filesystem-path-derived doc_id values.
# ---------------------------------------------------------------------------
SENTINEL = "PRIVACY-LEAK-SENTINEL-7f3a-feat-039b"

# ---------------------------------------------------------------------------
# FastMCP stub — same pattern as test_mcp_telemetry.py so mcp.py can be
# imported without installing the real fastmcp package.
# ---------------------------------------------------------------------------
if "fastmcp" not in sys.modules:
    _fastmcp_stub = types.ModuleType("fastmcp")
    _fastmcp_stub.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp_stub.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp_stub


class _FakeApp:
    """Minimal FastMCP substitute that registers @app.tool() decorated functions."""

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


# ---------------------------------------------------------------------------
# Minimal fake MultiCollectionRouter for route-handler patching
# ---------------------------------------------------------------------------
class _FakeColRouter:
    def __init__(
        self,
        *,
        pre_context: str | None = "context",
        routable: list[str] | None = None,
        decomposer: bool = False,
        raise_on_get: BaseException | None = None,
    ) -> None:
        self._pre_context = pre_context
        self.last_routable_names: list[str] = routable or []
        self.decomposer_was_invoked: bool = decomposer
        self._raise = raise_on_get

    async def get_pre_context(self, **_kwargs: object) -> str | None:
        if self._raise is not None:
            raise self._raise
        return self._pre_context


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, *, enabled: bool) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.telemetry = TelemetryConfig(
        enabled=enabled,
        retention_days=30,
        log_dir=str(tmp_path / "search-logs"),
    )
    return cfg


def _make_job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _read_all_jsonl(log_dir: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not log_dir.exists():
        return entries
    for f in sorted(log_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _make_ok_pipeline(results: list[SearchResult]) -> MagicMock:
    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=results)
    swc_results = [{"result": r, "context_before": [], "context_after": []} for r in results]
    pipeline.search_with_context = AsyncMock(return_value=swc_results)
    return pipeline


def _make_error_pipeline(exc: Exception) -> MagicMock:
    pipeline = MagicMock()
    pipeline.search = AsyncMock(side_effect=exc)
    pipeline.search_with_context = AsyncMock(side_effect=exc)
    return pipeline


# ---------------------------------------------------------------------------
# E2E Test 1: key-set equality with DOCUMENTED_SCHEMA_FIELDS
# ---------------------------------------------------------------------------


def test_jsonl_key_set_equals_documented_schema(tmp_path: Path) -> None:
    """Union of all JSONL keys must EQUAL DOCUMENTED_SCHEMA_FIELDS (not just a subset).

    Every documented field must appear in at least one emitted entry variant:
    - search ok        → collection, result_count, result_doc_ids
    - route ok         → collections, decomposer_invoked
    - any error        → error_kind
    - oversized search → truncated (set to True, so it's serialized)
    - all entries      → query_id, timestamp, endpoint, latency_ms, status
    """
    cfg = _make_config(tmp_path, enabled=True)
    log_dir = Path(cfg.telemetry.log_dir)
    app = create_app(cfg, _make_job_store(tmp_path))

    results = [
        SearchResult(doc_id="doc1", chunk_id="c1", text="hello", score=0.9, source_path="/a/b.md"),
    ]
    ok_pipeline = _make_ok_pipeline(results)
    err_pipeline = _make_error_pipeline(RuntimeError("search error"))

    async def _call_mcp_tools(writer: TelemetryWriter) -> None:
        with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
            from archon_search.server import mcp as mcp_module

            ok_mcp = mcp_module.create_app(ok_pipeline, "col1", writer=writer)
            err_mcp = mcp_module.create_app(err_pipeline, "col1", writer=writer)

            # search ok → provides: collection, result_count, result_doc_ids
            await ok_mcp.tools["search"](query="q1", collection=None)
            # search error → provides: error_kind
            await err_mcp.tools["search"](query="q2", collection=None)
            # search_with_context ok → provides: endpoint="search_with_context"
            await ok_mcp.tools["search_with_context"](query="q3", collection=None)
            # search_with_context error
            await err_mcp.tools["search_with_context"](query="q4", collection=None)

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
        writer = app.state.telemetry_writer
        assert writer is not None, "Writer must be set when telemetry is enabled"

        # MCP tool calls need an event loop; run them in a fresh loop (not the
        # TestClient's thread-loop) — writer.enqueue() is synchronous and
        # safe to call from any thread.
        asyncio.run(_call_mcp_tools(writer))

        # route ok → provides: collections, decomposer_invoked
        fake_router = _FakeColRouter(routable=["col_b"], decomposer=True)
        with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
            resp = client.post("/route", json={"query": "q5"})
        assert resp.status_code == 200

        # route validation error → provides: error_kind via validation_error path
        resp = client.post("/route", json={"query": ""})
        assert resp.status_code == 400

        # route timeout → provides: error_kind via timeout path
        timeout_router = _FakeColRouter(raise_on_get=asyncio.TimeoutError())
        with patch("archon_search.server.routes_route._build_router", return_value=timeout_router):
            resp = client.post("/route", json={"query": "q7"})
        assert resp.status_code == 504

        # Oversized entry → triggers truncation; serialized form gains truncated=True key.
        # The writer's drain task calls _truncate_to_fit() which adds truncated=True.
        big_entry = TelemetryEntry(
            query_id=uuid.uuid4().hex,
            timestamp="2024-01-01T00:00:00Z",
            endpoint="search",
            latency_ms=1.0,
            status="ok",
            collection="col1",
            result_count=1000,
            result_doc_ids=["x" * 50] * 1000,
        )
        writer.enqueue(big_entry)

    # lifespan exit has drained the writer

    entries = _read_all_jsonl(log_dir)
    assert len(entries) > 0, "Expected at least one JSONL entry"

    observed_keys: set[str] = set()
    for entry in entries:
        observed_keys |= set(entry.keys())

    missing = DOCUMENTED_SCHEMA_FIELDS - observed_keys
    extra = observed_keys - DOCUMENTED_SCHEMA_FIELDS
    assert observed_keys == DOCUMENTED_SCHEMA_FIELDS, (
        f"Key mismatch.\n"
        f"Missing (not in any entry): {missing}\n"
        f"Extra   (undocumented keys): {extra}"
    )


# ---------------------------------------------------------------------------
# E2E Test 2: privacy — sentinel not in JSONL files or logger messages
# ---------------------------------------------------------------------------


def test_handler_does_not_leak_query_text_into_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sentinel query text must not appear in any JSONL file or archon.search log message.

    Exercises both a search call (via MCP tool) and a route call (via HTTP), per
    the acceptance criteria requirement to issue both types of call with the sentinel.

    Note: exc_info tracebacks in logger records may legitimately contain the sentinel
    (the exception message echoes the input). The assertion checks record.getMessage()
    — the developer-controlled format string — to detect cases like
    logger.exception(f"search failed for query: {query}").
    """
    cfg = _make_config(tmp_path, enabled=True)
    log_dir = Path(cfg.telemetry.log_dir)
    app = create_app(cfg, _make_job_store(tmp_path))

    results = [
        SearchResult(doc_id="doc1", chunk_id="c1", text="x", score=0.9, source_path="/a/b.md"),
    ]
    ok_pipeline = _make_ok_pipeline(results)

    async def _mcp_search_with_sentinel(writer: TelemetryWriter) -> None:
        """Call MCP search and search_with_context with SENTINEL as query."""
        with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
            from archon_search.server import mcp as mcp_module

            mcp_app = mcp_module.create_app(ok_pipeline, "col1", writer=writer)
            await mcp_app.tools["search"](query=SENTINEL, collection=None)
            await mcp_app.tools["search_with_context"](query=SENTINEL, collection=None)

    with caplog.at_level(logging.WARNING, logger="archon.search"):
        # raise_server_exceptions=False so internal errors return 500 instead of
        # propagating as Python exceptions and short-circuiting the test.
        key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
        with TestClient(app, headers={"Authorization": f"Bearer {key}"}, raise_server_exceptions=False) as client:
            writer = app.state.telemetry_writer
            assert writer is not None

            # MCP search + search_with_context with SENTINEL as query (success paths)
            asyncio.run(_mcp_search_with_sentinel(writer))

            # Route success path — sentinel is the query
            fake_router = _FakeColRouter(routable=["col_a"], decomposer=False)
            with patch(
                "archon_search.server.routes_route._build_router", return_value=fake_router
            ):
                resp = client.post("/route", json={"query": SENTINEL})
            assert resp.status_code == 200

            # Route internal error path — router raises unexpectedly (500)
            crash_router = _FakeColRouter(raise_on_get=RuntimeError("disk error"))
            with patch(
                "archon_search.server.routes_route._build_router", return_value=crash_router
            ):
                resp = client.post("/route", json={"query": SENTINEL})
            assert resp.status_code == 500

    # (a) Sentinel must not appear anywhere in any JSONL file
    for f in log_dir.glob("*.jsonl"):
        content = f.read_bytes()
        assert SENTINEL.encode() not in content, (
            f"Sentinel found in log file {f.name}: {content[:200]}"
        )

    # (b) Sentinel must not appear in any logger MESSAGE from archon.search.
    # We check record.getMessage() (the format string with args substituted) —
    # not caplog.text — because exc_info tracebacks may contain the sentinel by
    # design (the exception's own message echoes the input). The relevant privacy
    # invariant is: the developer never writes logger.xxx(f"... {query} ...").
    for record in caplog.records:
        if record.name.startswith("archon.search"):
            msg = record.getMessage()
            assert SENTINEL not in msg, (
                f"Sentinel found in archon.search log message: {msg!r}"
            )


# ---------------------------------------------------------------------------
# E2E Test 3: privacy via exception message — sentinel in exc msg, not in JSONL
# ---------------------------------------------------------------------------


def test_handler_does_not_leak_query_text_via_exception_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the pipeline raises an exception whose message contains the sentinel,
    the sentinel must still not appear in JSONL or in archon.search log messages.

    This proves the error-path factory uses error_kind="other" (a closed Literal)
    rather than str(exc) — so no user-input-echoing data reaches the JSONL line.
    """
    log_dir = tmp_path / "search-logs"
    log_dir.mkdir()

    # Pipeline raises an exception whose message contains the sentinel
    exc_with_sentinel = RuntimeError(f"failed for query: {SENTINEL}")
    err_pipeline = _make_error_pipeline(exc_with_sentinel)

    async def _run() -> None:
        writer = TelemetryWriter(log_dir)
        await writer.start()

        with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
            from archon_search.server import mcp as mcp_module

            mcp_app = mcp_module.create_app(err_pipeline, "col1", writer=writer)

            with caplog.at_level(logging.ERROR, logger="archon.search"):
                # Raises internally; tool catches and enqueues an error entry
                await mcp_app.tools["search"](query=SENTINEL, collection=None)
                await mcp_app.tools["search_with_context"](query=SENTINEL, collection=None)

        await writer.drain_and_stop()

    asyncio.run(_run())

    # (a) Sentinel must not appear in any JSONL file
    for f in log_dir.glob("*.jsonl"):
        content = f.read_bytes()
        assert SENTINEL.encode() not in content, (
            f"Sentinel found in log file {f.name}: {content[:200]}"
        )

    # (b) Sentinel must not appear in any archon.search log MESSAGE
    # (The exception traceback WILL contain the sentinel — that is expected and
    # acceptable; the privacy invariant is about the MESSAGE field, not exc_info.)
    for record in caplog.records:
        if record.name.startswith("archon.search"):
            msg = record.getMessage()
            assert SENTINEL not in msg, (
                f"Sentinel found in archon.search log message: {msg!r}"
            )


# ---------------------------------------------------------------------------
# E2E Test 4: disabled telemetry writes no files
# ---------------------------------------------------------------------------


def test_disabled_telemetry_writes_no_files(tmp_path: Path) -> None:
    """When telemetry is disabled, no files are created under log_dir."""
    cfg = _make_config(tmp_path, enabled=False)
    log_dir = Path(cfg.telemetry.log_dir)
    app = create_app(cfg, _make_job_store(tmp_path))

    fake_router = _FakeColRouter(routable=["col_a"], decomposer=False)

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
        # Success path
        with patch(
            "archon_search.server.routes_route._build_router", return_value=fake_router
        ):
            resp = client.post("/route", json={"query": "hello"})
        assert resp.status_code == 200

        # Validation error
        resp = client.post("/route", json={"query": ""})
        assert resp.status_code == 400

        # Timeout
        timeout_router = _FakeColRouter(raise_on_get=asyncio.TimeoutError())
        with patch(
            "archon_search.server.routes_route._build_router", return_value=timeout_router
        ):
            resp = client.post("/route", json={"query": "timeout"})
        assert resp.status_code == 504

    # log_dir must not exist, or must be empty
    if log_dir.exists():
        jsonl_files = list(log_dir.glob("*.jsonl"))
        assert jsonl_files == [], f"Expected no JSONL files, found: {jsonl_files}"


# ---------------------------------------------------------------------------
# E2E Test 5: full 32-day cycle with rotation and pruning
# ---------------------------------------------------------------------------


def test_full_telemetry_cycle_with_rotation_and_pruning(tmp_path: Path) -> None:
    """Simulate 32 days of traffic: verify file rotation and retention pruning.

    Uses an injected clock callable to advance time deterministically.
    Each day's entry is written to a dated file; the pruner then deletes files
    older than retention_days (30) when called with now=day-31.
    """
    log_dir = tmp_path / "search-logs"
    log_dir.mkdir()

    base_date = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    current_dt: list[datetime] = [base_date]

    def fake_clock() -> datetime:
        return current_dt[0]

    async def _run() -> None:
        writer = TelemetryWriter(log_dir, clock=fake_clock, queue_size=2048)
        await writer.start()

        for day in range(32):
            current_dt[0] = base_date + timedelta(days=day)
            entry = TelemetryEntry.from_search_tool_result(
                endpoint="search",
                collection="col",
                result_doc_ids=[f"doc-{day}"],
                latency_ms=float(day),
            )
            writer.enqueue(entry)
            # Wait for the drain task to consume this entry (and call task_done)
            # before advancing the clock so the entry lands in the correct dated file.
            await asyncio.wait_for(writer._queue.join(), timeout=2.0)

        await writer.drain_and_stop()

    asyncio.run(_run())

    # Should have exactly 32 files: 2024-01-01.jsonl … 2024-02-01.jsonl
    files = sorted(log_dir.glob("*.jsonl"))
    assert len(files) == 32, f"Expected 32 daily files, got {len(files)}: {files}"

    # Each file should have exactly one entry
    for f in files:
        lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1, f"Expected 1 entry in {f.name}, got {len(lines)}"

    # Prune from the perspective of day 31 (2024-02-01).
    # cutoff = 2024-02-01 - 30 days = 2024-01-02
    # Day 0 (2024-01-01) < 2024-01-02 → deleted
    # Day 1 (2024-01-02) = cutoff      → kept (< is strict)
    # Days 2-31                        → kept
    pruner = Pruner(log_dir, retention_days=30)
    now_date = (base_date + timedelta(days=31)).date()
    deleted = pruner.prune_once(now=now_date)

    assert deleted == 1, f"Expected 1 file deleted (day 0), got {deleted}"

    remaining = sorted(log_dir.glob("*.jsonl"))
    assert len(remaining) == 31, f"Expected 31 files remaining, got {len(remaining)}"

    # Day 0's file must be gone
    day0_file = log_dir / f"{base_date.date().isoformat()}.jsonl"
    assert not day0_file.exists(), f"Day 0 file should have been pruned: {day0_file}"

    # Day 1's file must still exist
    day1_file = log_dir / f"{(base_date + timedelta(days=1)).date().isoformat()}.jsonl"
    assert day1_file.exists(), f"Day 1 file should be retained: {day1_file}"

    # Day 31 (today for pruner perspective) must still exist
    day31_file = log_dir / f"{now_date.isoformat()}.jsonl"
    assert day31_file.exists(), f"Day 31 (today) file should never be pruned: {day31_file}"
