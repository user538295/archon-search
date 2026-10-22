"""MCP ``ingest_file`` / ``ingest_directory`` register as a collection writer for the whole pipeline call.

The tools create no job, so ``DELETE /collections/{name}`` cannot see them in the job store; it consults
``SearchStore.has_active_writer`` instead (CON-12). The registration is taken before the pipeline call, held for all of
it, and released however it ends. A registration the lock refuses is the tools' existing ``store_busy`` error.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import IngestResult
from archon_search.store import SearchStore
from tests._fastmcp_stub import stub_mcp_tool

_DEFAULT_COLLECTION = "default_col"
_EXPLICIT_COLLECTION = "explicit_col"
_SHORT_LOCK_TIMEOUT_S = 0.05
_BOOM = "pipeline blew up"

_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "ingest_file": {"path": "/tmp/archon-con12/doc.md"},
    "ingest_directory": {"path": "/tmp/archon-con12/"},
}
_TOOLS = pytest.mark.parametrize("tool_name", sorted(_TOOL_ARGS))


class _Probe:
    """A pipeline whose ingest calls record whether the collection had an active writer while they ran."""

    def __init__(self, store: SearchStore, *, error: Exception | None = None) -> None:
        self.active_during_call: list[bool] = []
        self._store = store
        self._error = error
        self.pipeline = MagicMock()
        self.pipeline.store = store
        self.pipeline._global_embedder = MagicMock()
        self.pipeline.get_collection_meta = AsyncMock(return_value=None)
        self.pipeline.ingest_file = AsyncMock(side_effect=self._ingest_file)
        self.pipeline.ingest_directory = AsyncMock(side_effect=self._ingest_directory)

    async def _record(self, collection: str) -> None:
        self.active_during_call.append(self._store.has_active_writer(collection))
        if self._error is not None:
            raise self._error

    async def _ingest_file(self, _path: Path, collection: str, **_kwargs: Any) -> IngestResult:
        await self._record(collection)
        return IngestResult(doc_id="d" * 64, chunks_created=1, status="ok")

    async def _ingest_directory(self, _path: Path, collection: str, **_kwargs: Any) -> list[IngestResult]:
        await self._record(collection)
        return [IngestResult(doc_id="d" * 64, chunks_created=1, status="ok")]


@pytest.fixture
def store(tmp_path: Path) -> SearchStore:
    return SearchStore(tmp_path / "db")


@_TOOLS
@pytest.mark.parametrize(
    ("collection_arg", "expected_collection"),
    [(None, _DEFAULT_COLLECTION), (_EXPLICIT_COLLECTION, _EXPLICIT_COLLECTION)],
    ids=["default-collection", "named-collection"],
)
async def test_tool_is_a_registered_writer_of_its_collection_during_the_pipeline_call_only(
    store: SearchStore, tool_name: str, collection_arg: str | None, expected_collection: str
) -> None:
    probe = _Probe(store)
    tool = stub_mcp_tool(tool_name, probe.pipeline, _DEFAULT_COLLECTION)

    await tool(collection=collection_arg, **_TOOL_ARGS[tool_name])

    assert probe.active_during_call == [True]
    assert not store.has_active_writer(expected_collection)
    assert not store.lock_for(expected_collection).locked()


@_TOOLS
async def test_tool_releases_its_registration_when_the_pipeline_raises(store: SearchStore, tool_name: str) -> None:
    probe = _Probe(store, error=RuntimeError(_BOOM))
    tool = stub_mcp_tool(tool_name, probe.pipeline, _DEFAULT_COLLECTION)

    result = await tool(**_TOOL_ARGS[tool_name])

    assert result["code"] == "internal_error"
    assert probe.active_during_call == [True]
    assert not store.has_active_writer(_DEFAULT_COLLECTION)


@_TOOLS
async def test_tool_answers_store_busy_without_calling_the_pipeline_when_the_lock_is_not_acquired(
    store: SearchStore, tool_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    probe = _Probe(store)
    tool = stub_mcp_tool(tool_name, probe.pipeline, _DEFAULT_COLLECTION)
    lock = store.lock_for(_DEFAULT_COLLECTION)
    await lock.acquire()
    try:
        result = await tool(**_TOOL_ARGS[tool_name])
    finally:
        lock.release()

    assert result["code"] == "store_busy"
    assert probe.active_during_call == []
    assert not store.has_active_writer(_DEFAULT_COLLECTION)
