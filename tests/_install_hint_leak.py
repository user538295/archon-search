"""Shared fixtures for the "missing optional extra" wire tests (REST, MCP, jobs).

In a uv tool env ``uv_tool.extra_install_hint`` returns a receipt-derived install
command that can carry ``--index-url`` credentials, ``--find-links`` and local
source paths. The dependency errors embed it, so their message goes to the server
log only and the wire gets the fixed ``*_DEPENDENCY_DETAIL`` constant.
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from archon_search.config import RAGFusionConfig
from tests._internal_error_leak import rag_fusion_generator_without_provider

INSTALL_HINT_SECRET = "https://ci:t0ken@pypi.corp.example/simple"
INSTALL_HINT_PATH = "/Users/secret/wheels"
INSTALL_HINT_SENTINEL = (
    f"uv tool install --index-url {INSTALL_HINT_SECRET} --find-links {INSTALL_HINT_PATH} 'archon-search[x]'"
)


def patch_install_hint(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Make *module*'s ``extra_install_hint`` return the credential-bearing sentinel command."""
    monkeypatch.setattr(f"{module}.extra_install_hint", lambda extra: INSTALL_HINT_SENTINEL)


def assert_install_hint_logged_not_sent(caplog: pytest.LogCaptureFixture, wire: str) -> None:
    """The install command reaches the server log as a WARNING and never the wire."""
    assert INSTALL_HINT_SECRET not in wire, f"install command leaked onto the wire: {wire!r}"
    assert INSTALL_HINT_PATH not in wire, f"install command leaked onto the wire: {wire!r}"
    assert any(
        r.levelno == logging.WARNING and INSTALL_HINT_SENTINEL in r.getMessage() for r in caplog.records
    ), f"install command not logged as a warning: {[r.getMessage() for r in caplog.records]!r}"


def rag_fusion_dependency_raiser() -> AsyncMock:
    """A pipeline-method stand-in failing at the real raise point: a provider-less RAG Fusion generator."""
    generator = rag_fusion_generator_without_provider(RAGFusionConfig(enabled=True))

    async def _generate(*args: Any, **kwargs: Any) -> None:
        await generator.generate_variants("q")

    return AsyncMock(side_effect=_generate)
