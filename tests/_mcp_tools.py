"""The tools a real ``FastMCP`` app registers, for doc-contract tests that derive wire truth.

Sibling MCP tests (``test_keystore_be9.py``, ``test_integration_rag_fusion.py``) swap
``fastmcp.FastMCP`` for a stub and reload ``archon_search.server.mcp`` at module scope, so under
xdist the module object may still hold that ``_StubFastMCP``. ``create_app`` is therefore always
called with the defining-module class pinned on the module.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from fastmcp.server.server import FastMCP as _RealFastMCP
from fastmcp.tools import Tool

from archon_search.key_manager import KeyStore
from archon_search.server import mcp as mcp_module

_DEFAULT_COLLECTION = "default"


def registered_tools(key_store: KeyStore | None) -> dict[str, Tool]:
    """Return ``{tool name: tool}`` as the real FastMCP ``tools/list`` advertises them."""
    with patch.object(mcp_module, "FastMCP", _RealFastMCP):
        app = mcp_module.create_app(MagicMock(), _DEFAULT_COLLECTION, key_store=key_store)
    return {tool.name: tool for tool in asyncio.run(app.list_tools())}


def input_properties(tool: Tool) -> set[str]:
    """Return the input property names the tool's advertised JSON schema carries."""
    return set(tool.to_mcp_tool().inputSchema.get("properties", {}))
