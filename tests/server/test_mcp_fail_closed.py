"""The MCP tools refuse a call that carries no authenticated identity (SecReview #9b).

``_get_request_identity`` used to fall back to the default namespace — and the default namespace is where the
default (admin) key lives. Outside an authenticated HTTP request every tool is now refused.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import archon_search.server.mcp as mcp_module
from archon_search.server.authz import ADMIN_REQUIRED_DETAIL, FORBIDDEN_CODE

pytestmark = pytest.mark.usefixtures("real_mcp_auth_context")

_REFUSED_TOOLS = [
    ("list_collections", {}),
    ("get_collections_meta", {}),
    ("get_collection_meta", {"name": "docs"}),
    ("list_documents", {"collection": "docs"}),
    ("search", {"query": "x"}),
    ("delete_document", {"doc_id": "d", "collection": "docs"}),
    ("ingest_file", {"path": "/tmp/x.md", "collection": "docs"}),
]


def _pipeline() -> MagicMock:
    pipeline = MagicMock()
    for name in ("get_all_collections_meta", "get_collection_meta", "list_documents", "search", "delete_document"):
        setattr(pipeline, name, AsyncMock(return_value=[]))
    return pipeline


@pytest.mark.parametrize(("tool", "arguments"), _REFUSED_TOOLS)
async def test_a_real_fastmcp_tool_call_without_an_http_request_is_refused(tool: str, arguments: dict) -> None:
    pipeline = _pipeline()
    app = mcp_module.create_app(pipeline, "docs")

    async with Client(app) as client:
        with pytest.raises(ToolError, match=mcp_module.NO_AUTH_CONTEXT_DETAIL):
            await client.call_tool(tool, arguments)

    pipeline.get_all_collections_meta.assert_not_called()
    pipeline.get_collection_meta.assert_not_called()
    pipeline.list_documents.assert_not_called()
    pipeline.delete_document.assert_not_called()


async def test_a_refused_call_logs_no_traceback(caplog: pytest.LogCaptureFixture) -> None:
    app = mcp_module.create_app(_pipeline(), "docs")

    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("list_collections", {})

    assert not [r for r in caplog.records if r.exc_info and r.name.startswith("archon_search")]


async def test_a_call_with_an_authenticated_request_is_served_in_that_namespace() -> None:
    pipeline = _pipeline()
    app = mcp_module.create_app(pipeline, "docs")
    request = types.SimpleNamespace(state=types.SimpleNamespace(namespace="ns-a", admin=False))

    with patch("archon_search.server.mcp.get_http_request", return_value=request):
        async with Client(app) as client:
            await client.call_tool("list_collections", {})

    pipeline.get_all_collections_meta.assert_awaited_once_with("ns-a")


async def test_key_tools_refuse_a_request_whose_state_has_a_namespace_but_no_admin_flag() -> None:
    """Absence of the admin flag never grants it."""
    key_store = MagicMock()
    key_store.rotation_lock = None
    app = mcp_module.create_app(_pipeline(), "docs", key_store=key_store)
    request = types.SimpleNamespace(state=types.SimpleNamespace(namespace="default"))

    with patch("archon_search.server.mcp.get_http_request", return_value=request):
        async with Client(app) as client:
            rotate = await client.call_tool("rotate_key", {})
            create = await client.call_tool("create_key", {"namespace": "other"})

    assert rotate.structured_content == {"error": ADMIN_REQUIRED_DETAIL, "code": FORBIDDEN_CODE}
    assert create.structured_content["code"] == FORBIDDEN_CODE
    key_store.create.assert_not_called()


async def test_key_tools_refuse_a_call_with_no_request_at_all() -> None:
    app = mcp_module.create_app(_pipeline(), "docs", key_store=MagicMock())

    async with Client(app) as client:
        for tool, arguments in (("create_key", {"namespace": "x"}), ("list_keys", {}), ("revoke_key", {"key_id": "k"}), ("rotate_key", {})):
            with pytest.raises(ToolError, match=mcp_module.NO_AUTH_CONTEXT_DETAIL):
                await client.call_tool(tool, arguments)
