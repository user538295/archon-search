"""``[graph].max_cross_collection_collections`` bounds one cross-collection graph call (SecReview #7f).

REST ``GET /graph/cross-collection`` answers 422 and the MCP tool a ``validation_error``, both with a fixed message
naming the limit and not the submitted names; a request at the limit passes the guard (it then reaches the existence
check, which answers 404 / ``not_found`` here because the stub pipeline knows no collection).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from tests._fastmcp_stub import stub_mcp_tool

_AUTH = {"Authorization": f"Bearer {'0' * 64}"}
_LIMIT = 3
_UNPROCESSABLE = 422
_NOT_FOUND = 404


def _names(count: int) -> list[str]:
    return [f"secret-name-{index}" for index in range(count)]


def _rest_client(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.graph.enabled = True
    config.graph.max_cross_collection_collections = _LIMIT
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    app.state.pipeline = MagicMock(get_collection_meta=AsyncMock(return_value=None))
    app.state.graph_store = MagicMock()
    return TestClient(app)


def test_rest_over_the_limit_is_422_and_does_not_look_up_collections(tmp_path: Path) -> None:
    client = _rest_client(tmp_path)

    response = client.get("/graph/cross-collection", params={"collections": ",".join(_names(_LIMIT + 1))}, headers=_AUTH)

    assert response.status_code == _UNPROCESSABLE
    assert str(_LIMIT) in response.json()["detail"]
    assert "secret-name" not in response.text
    client.app.state.pipeline.get_collection_meta.assert_not_called()


def test_rest_duplicates_do_not_count_toward_the_limit(tmp_path: Path) -> None:
    client = _rest_client(tmp_path)
    names = _names(_LIMIT)

    response = client.get("/graph/cross-collection", params={"collections": ",".join(names + names)}, headers=_AUTH)

    assert response.status_code == _NOT_FOUND


def test_rest_at_the_limit_passes_the_guard(tmp_path: Path) -> None:
    client = _rest_client(tmp_path)

    response = client.get("/graph/cross-collection", params={"collections": ",".join(_names(_LIMIT))}, headers=_AUTH)

    assert response.status_code == _NOT_FOUND


def _mcp_tool():
    config = SearchConfig()
    config.graph.enabled = True
    config.graph.max_cross_collection_collections = _LIMIT
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    return stub_mcp_tool("get_graph_cross_collection", pipeline, "col", config=config, graph_store=MagicMock()), pipeline


@pytest.mark.asyncio
async def test_mcp_over_the_limit_is_a_validation_error_without_lookups() -> None:
    tool, pipeline = _mcp_tool()

    result = await tool(collections=_names(_LIMIT + 1))

    assert result["code"] == "validation_error"
    assert str(_LIMIT) in result["error"]
    assert "secret-name" not in result["error"]
    pipeline.get_collection_meta.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_at_the_limit_passes_the_guard() -> None:
    tool, _pipeline = _mcp_tool()

    result = await tool(collections=_names(_LIMIT))

    assert result["code"] == "not_found"
