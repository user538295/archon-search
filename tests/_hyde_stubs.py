"""HyDE stand-ins shared by the REST and MCP HyDE tests: an enabled ``[hyde]`` plus a stub generator.

HyDE resolves through ``server/_hyde.RequestHyDE``, which skips a request with no generator
or a disabled ``[hyde]`` before any LLM call — so a test that wants HyDE to run must supply
both, and controls the outcome through the stub's ``generate``.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from archon_search.config import SearchConfig
from archon_search.hyde import HyDEGenerator


def stub_hyde_generator(**generate: Any) -> MagicMock:
    """A ``HyDEGenerator`` stand-in whose ``generate`` is ``AsyncMock(**generate)``.

    ``return_value=`` a vector applies HyDE, ``return_value=None`` is a failed expansion,
    ``side_effect=`` raises or delays.
    """
    return MagicMock(spec=HyDEGenerator, generate=AsyncMock(**generate))


def hyde_enabled_config() -> SearchConfig:
    """A default ``SearchConfig`` with ``[hyde] enabled = true`` (for MCP ``create_app``)."""
    config = SearchConfig()
    config.hyde.enabled = True
    return config


@contextmanager
def rest_hyde_generating(app: Any, **generate: Any) -> Iterator[AsyncMock]:
    """Enable ``[hyde]`` on a REST *app* and serve HyDE from :func:`stub_hyde_generator`.

    Yields the stub's ``generate`` mock; restores the app's ``[hyde]`` state on exit.
    """
    hyde_config = app.state.config.hyde
    previous = (hyde_config.enabled, getattr(app.state, "hyde_generator", None))
    hyde_config.enabled = True
    app.state.hyde_generator = stub_hyde_generator(**generate)
    try:
        yield app.state.hyde_generator.generate
    finally:
        hyde_config.enabled, app.state.hyde_generator = previous
