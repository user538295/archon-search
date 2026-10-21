"""Behavioural regression for ``tests/_fastmcp_stub.py``: the stub never outlives its scope.

``tests/test_no_leaked_fastmcp_stub.py`` only proves no test file uses a leaking install form;
these tests prove the sanctioned forms actually restore the real ``FastMCP`` class.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fastmcp
import pytest
from fastmcp.server.server import FastMCP as RealFastMCP

from archon_search.server import mcp as mcp_module
from tests import _fastmcp_stub as fastmcp_stub_module
from tests._fastmcp_stub import (
    StubFastMCP,
    stub_mcp_tool,
    stub_mcp_tools,
    stubbed_fastmcp,
)

_COLLECTION = "col1"
_STAND_IN_MODULE = "tests._stand_in_mcp_module"
_ORIGINAL = object()


class _BlockFailedError(Exception):
    """Raised inside a ``stubbed_fastmcp()`` block to exercise the exceptional exit."""


def _assert_real_fastmcp_installed() -> None:
    """The process-wide state a leaked stub used to corrupt is intact."""
    assert sys.modules[mcp_module.__name__] is mcp_module
    assert fastmcp.FastMCP is RealFastMCP
    assert mcp_module.FastMCP is RealFastMCP


def test_real_fastmcp_is_installed_outside_any_stub() -> None:
    _assert_real_fastmcp_installed()


def test_create_app_builds_real_fastmcp_after_a_stub_scope_exits() -> None:
    """The original leak made ``FastMCP("archon-search")`` raise ``TypeError: FastMCP() takes no arguments``."""
    with stubbed_fastmcp():
        pass

    app = mcp_module.create_app(MagicMock(), _COLLECTION)

    assert isinstance(app, RealFastMCP)
    _assert_real_fastmcp_installed()


def test_stubbed_fastmcp_makes_create_app_build_a_tool_capturing_stub() -> None:
    with stubbed_fastmcp() as stub_cls:
        assert stub_cls is StubFastMCP
        assert mcp_module.FastMCP is StubFastMCP
        app = mcp_module.create_app(MagicMock(), _COLLECTION)

    assert isinstance(app, StubFastMCP)
    assert "search" in app.tools


def test_stubbed_fastmcp_restores_real_class_when_block_raises() -> None:
    with pytest.raises(_BlockFailedError), stubbed_fastmcp():
        assert mcp_module.FastMCP is StubFastMCP
        raise _BlockFailedError

    _assert_real_fastmcp_installed()


def test_stubbed_fastmcp_patches_the_module_sys_modules_holds_at_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each entry re-resolves the module, so a module object replaced after import is the one patched.

    Stands in an unguarded ``sys.modules`` key for the MCP module: replacing the real
    ``archon_search.server.mcp`` entry is exactly what ``tests/test_no_leaked_fastmcp_stub.py`` forbids.
    """
    first, second = SimpleNamespace(FastMCP=_ORIGINAL), SimpleNamespace(FastMCP=_ORIGINAL)
    monkeypatch.setattr(fastmcp_stub_module, "_MCP_MODULE", _STAND_IN_MODULE)
    monkeypatch.setitem(sys.modules, _STAND_IN_MODULE, first)
    with stubbed_fastmcp():
        assert first.FastMCP is StubFastMCP

    monkeypatch.setitem(sys.modules, _STAND_IN_MODULE, second)
    with stubbed_fastmcp():
        assert second.FastMCP is StubFastMCP
        assert first.FastMCP is _ORIGINAL

    assert second.FastMCP is _ORIGINAL
    _assert_real_fastmcp_installed()


def test_stubbed_fastmcp_restores_an_enclosing_patch_not_the_real_class() -> None:
    outer = object()
    with patch.object(mcp_module, "FastMCP", outer):
        with stubbed_fastmcp():
            assert mcp_module.FastMCP is StubFastMCP
        assert mcp_module.FastMCP is outer

    _assert_real_fastmcp_installed()


def test_tool_registers_under_explicit_name() -> None:
    app = StubFastMCP("archon-search")

    @app.tool(name="renamed")
    def original() -> None:
        """Registered under the ``name=`` override, not its ``__name__``."""

    assert app.tools == {"renamed": original}


def test_tool_falls_back_to_function_name() -> None:
    app = StubFastMCP("archon-search")

    @app.tool()
    def search() -> None:
        """Registered under its ``__name__``."""

    assert app.tools == {"search": search}


def test_stub_mcp_tools_returns_every_registered_tool_and_restores_real_class() -> None:
    tools = stub_mcp_tools(MagicMock(), _COLLECTION)

    assert tools["search"].__name__ == "search"
    assert all(callable(fn) for fn in tools.values())
    _assert_real_fastmcp_installed()


def test_stub_mcp_tool_returns_named_tool_and_restores_real_class() -> None:
    tool_fn = stub_mcp_tool("search", MagicMock(), _COLLECTION)

    assert tool_fn.__name__ == "search"
    _assert_real_fastmcp_installed()
