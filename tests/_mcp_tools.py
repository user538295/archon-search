"""The tools a real ``FastMCP`` app registers, for doc-contract tests that derive wire truth.

Call these outside any ``stubbed_fastmcp()`` scope (``tests/_fastmcp_stub.py``):
they rely on ``archon_search.server.mcp.FastMCP`` being the real class.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from fastmcp.tools import Tool

from archon_search.key_manager import KeyStore
from archon_search.server import mcp as mcp_module

_DEFAULT_COLLECTION = "default"


def registered_tools(key_store: KeyStore | None) -> dict[str, Tool]:
    """Return ``{tool name: tool}`` as the real FastMCP ``tools/list`` advertises them."""
    app = mcp_module.create_app(MagicMock(), _DEFAULT_COLLECTION, key_store=key_store)
    return {tool.name: tool for tool in asyncio.run(app.list_tools())}


def input_properties(tool: Tool) -> set[str]:
    """Return the input property names the tool's advertised JSON schema carries."""
    return set(tool.to_mcp_tool().inputSchema.get("properties", {}))
