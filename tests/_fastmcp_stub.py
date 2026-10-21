"""A ``FastMCP`` stand-in that records registered tools, so tests can call tool functions directly.

``create_app`` looks up the module-global ``FastMCP`` in ``archon_search.server.mcp`` at call
time, so patching that one name is enough — no ``sys.modules`` edit or module reload. Install it
only through :func:`stubbed_fastmcp` (or :func:`stub_mcp_tools` / :func:`stub_mcp_tool`, which wrap it):
``patch.object`` restores the real class on exit, so the stub never leaks into later tests
(``tests/test_no_leaked_fastmcp_stub.py`` guards this; ``tests/test_fastmcp_stub.py`` proves it).
"""
from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

_MCP_MODULE = "archon_search.server.mcp"


class StubFastMCP:
    """Captures ``@app.tool()`` functions in ``tools``; ``custom_route`` is a no-op."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register under ``name=`` when given (as real ``FastMCP.tool`` does), else ``fn.__name__``.

        Supports only the called-decorator forms ``create_app`` uses — ``@app.tool()`` and
        ``@app.tool(name=...)``; real ``FastMCP.tool`` also accepts a positional name
        (``@app.tool("x")``) and the bare ``@app.tool``, which this stub does not.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[kwargs.get("name") or fn.__name__] = fn
            return fn

        return decorator

    def custom_route(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn

        return decorator


@contextmanager
def stubbed_fastmcp() -> Iterator[type[StubFastMCP]]:
    """Make ``create_app`` build a :class:`StubFastMCP` inside the ``with`` block.

    The module is resolved on entry, so the patch lands on whatever ``sys.modules`` holds now
    rather than on a module object bound when this helper was first imported.
    """
    with patch.object(importlib.import_module(_MCP_MODULE), "FastMCP", StubFastMCP):
        yield StubFastMCP


def stub_mcp_tools(pipeline: Any, collection: str, **create_app_kwargs: Any) -> dict[str, Callable[..., Any]]:
    """Build a :class:`StubFastMCP`-backed MCP app over ``pipeline`` and return its tools by name."""
    with stubbed_fastmcp():
        app = importlib.import_module(_MCP_MODULE).create_app(pipeline, collection, **create_app_kwargs)
    return app.tools


def stub_mcp_tool(
    tool_name: str, pipeline: Any, collection: str, **create_app_kwargs: Any
) -> Callable[..., Any]:
    """Like :func:`stub_mcp_tools`, but return only the tool registered as ``tool_name``."""
    return stub_mcp_tools(pipeline, collection, **create_app_kwargs)[tool_name]
