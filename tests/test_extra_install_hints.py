"""Runtime "optional extra missing" errors name the install command for the
running install method.

In a ``uv tool install archon-search`` environment ``pip install`` is not
actionable (the env ships no pip, and mutating it is undone by the next
``uv tool upgrade``), so these errors must carry the uv tool command there and
``pip install`` everywhere else. The ``uv_tool_prefix`` / ``pip_prefix``
fixtures live in ``tests/conftest.py``.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.community_builder import (
    CommunityDependencyError,
    _run_leiden_partition_sync,
)
from archon_search.config import HyDEConfig, RAGFusionConfig
from archon_search.graph_inspector import (
    CollectionGraphView,
    GraphMLDependencyError,
    to_graphml,
)
from archon_search.hyde import HyDEGenerator
from archon_search.llm_client.anthropic_client import AnthropicClient
from archon_search.rag_fusion import (
    RAG_FUSION_DEPENDENCY_DETAIL,
    RAGFusionDependencyError,
)
from tests._internal_error_leak import rag_fusion_generator_without_provider

_INSTALL_METHODS = pytest.mark.parametrize(
    ("prefix_fixture", "command"),
    [("uv_tool_prefix", "uv tool install"), ("pip_prefix", "pip install")],
)


@_INSTALL_METHODS
def test_missing_leiden_names_the_install_command(
    request: pytest.FixtureRequest, prefix_fixture: str, command: str
) -> None:
    request.getfixturevalue(prefix_fixture)
    with patch.dict(sys.modules, {"igraph": None, "leidenalg": None}), \
            pytest.raises(CommunityDependencyError) as excinfo:
        _run_leiden_partition_sync([], [], resolution=1.0)
    assert str(excinfo.value) == (
        f"leidenalg is required for community detection. Install it with: {command} 'archon-search[graph]'"
    )


@_INSTALL_METHODS
def test_missing_networkx_names_the_install_command(
    request: pytest.FixtureRequest, prefix_fixture: str, command: str
) -> None:
    request.getfixturevalue(prefix_fixture)
    view = CollectionGraphView(nodes=[], edges=[], node_count=0, edge_count=0, truncated=False)
    with patch.dict(sys.modules, {"networkx": None}), pytest.raises(GraphMLDependencyError) as excinfo:
        to_graphml(view)
    assert str(excinfo.value) == f"GraphML export requires networkx. Install it with: {command} 'archon-search[graph]'"
    assert isinstance(excinfo.value.__cause__, ImportError)


@_INSTALL_METHODS
def test_missing_fasttext_names_the_install_command(
    request: pytest.FixtureRequest, prefix_fixture: str, command: str, tmp_path: Path
) -> None:
    import archon_search.language_detector as ld_module

    request.getfixturevalue(prefix_fixture)
    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()
    with patch.object(ld_module, "fasttext", None), pytest.raises(RuntimeError) as excinfo:
        ld_module.LanguageDetector(model_path)
    assert str(excinfo.value) == f"fasttext-predict not installed; run: {command} 'archon-search[multilingual]'"


@_INSTALL_METHODS
async def test_missing_hyde_provider_names_the_install_command(
    request: pytest.FixtureRequest,
    prefix_fixture: str,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A HyDE call on an Anthropic client without its package searches without HyDE and logs the install command."""
    request.getfixturevalue(prefix_fixture)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    generator = HyDEGenerator(HyDEConfig(enabled=True), AnthropicClient(model="claude-haiku-4-5-20251001"))
    embedder = MagicMock()
    embedder.embed_one = AsyncMock()

    with patch.dict(sys.modules, {"anthropic": None}), caplog.at_level(logging.WARNING):
        result = await generator.generate("query", embedder=embedder)

    assert result is None
    embedder.embed_one.assert_not_awaited()
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert f"run: {command} 'archon-search[hyde]'" in warning.getMessage()


@_INSTALL_METHODS
async def test_missing_rag_fusion_provider_names_the_install_command(
    request: pytest.FixtureRequest, prefix_fixture: str, command: str
) -> None:
    request.getfixturevalue(prefix_fixture)
    generator = rag_fusion_generator_without_provider(RAGFusionConfig(enabled=True))
    with pytest.raises(RAGFusionDependencyError) as excinfo:
        await generator.generate_variants("query")
    assert str(excinfo.value) == (
        f"Install archon-search[rag_fusion] to use RAG Fusion ({command} 'archon-search[rag_fusion]')"
    )
    # The wire-safe half is the message minus the install command (G2).
    assert str(excinfo.value).startswith(f"{RAG_FUSION_DEPENDENCY_DETAIL} (")
