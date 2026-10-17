"""Runtime "optional extra missing" errors name the install command for the
running install method.

In a ``uv tool install archon-search`` environment ``pip install`` is not
actionable (the env ships no pip, and mutating it is undone by the next
``uv tool upgrade``), so these errors must carry the uv tool command there and
``pip install`` everywhere else. The ``uv_tool_prefix`` / ``pip_prefix``
fixtures live in ``tests/conftest.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.community_builder import _run_leiden_partition_sync
from archon_search.config import HyDEConfig, RAGFusionConfig
# Module, not names: tests/test_hyde.py reloads archon_search.hyde, rebinding
# HyDEDependencyError (same reason as rag_fusion below).
import archon_search.hyde as hyde_mod
# Module, not names: tests/test_rag_fusion.py reloads archon_search.rag_fusion, which
# rebinds RAGFusionDependencyError — a name imported here at collection time would then
# no longer match the class the generator raises when both run on one xdist worker.
import archon_search.rag_fusion as rag_fusion_mod

_INSTALL_METHODS = pytest.mark.parametrize(
    ("prefix_fixture", "command"),
    [("uv_tool_prefix", "uv tool install"), ("pip_prefix", "pip install")],
)


@_INSTALL_METHODS
def test_missing_leiden_names_the_install_command(
    request: pytest.FixtureRequest, prefix_fixture: str, command: str
) -> None:
    request.getfixturevalue(prefix_fixture)
    with patch.dict(sys.modules, {"igraph": None, "leidenalg": None}), pytest.raises(ImportError) as excinfo:
        _run_leiden_partition_sync([], [], resolution=1.0)
    assert str(excinfo.value) == (
        f"leidenalg is required for community detection. Install it with: {command} 'archon-search[graph]'"
    )


@_INSTALL_METHODS
def test_missing_fasttext_names_the_install_command(
    request: pytest.FixtureRequest, prefix_fixture: str, command: str, tmp_path: Path
) -> None:
    import archon_search.language_detector as ld_module  # noqa: PLC0415

    request.getfixturevalue(prefix_fixture)
    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()
    with patch.object(ld_module, "fasttext", None), pytest.raises(RuntimeError) as excinfo:
        ld_module.LanguageDetector(model_path)
    assert str(excinfo.value) == f"fasttext-wheel not installed; run: {command} 'archon-search[multilingual]'"


# Constructing a generator without the provider module is how a missing
# provider extra reaches the generators (hyde.py / rag_fusion.py __init__).
_NO_ANTHROPIC_PROVIDER = {"archon_search.providers.anthropic_provider": None}


@_INSTALL_METHODS
async def test_missing_hyde_provider_names_the_install_command(
    request: pytest.FixtureRequest, prefix_fixture: str, command: str
) -> None:
    request.getfixturevalue(prefix_fixture)
    with patch.dict(sys.modules, _NO_ANTHROPIC_PROVIDER):
        generator = hyde_mod.HyDEGenerator(HyDEConfig(enabled=True))
    with pytest.raises(hyde_mod.HyDEDependencyError) as excinfo:
        await generator.generate("query", embedder=MagicMock())
    assert str(excinfo.value) == f"Install archon-search[hyde] to use HyDE ({command} 'archon-search[hyde]')"
    # The wire-safe half is the message minus the install command (C2-T-1).
    assert str(excinfo.value).startswith(f"{hyde_mod.HYDE_DEPENDENCY_DETAIL} (")


@_INSTALL_METHODS
async def test_missing_rag_fusion_provider_names_the_install_command(
    request: pytest.FixtureRequest, prefix_fixture: str, command: str
) -> None:
    request.getfixturevalue(prefix_fixture)
    with patch.dict(sys.modules, _NO_ANTHROPIC_PROVIDER):
        generator = rag_fusion_mod.RAGFusionGenerator(RAGFusionConfig(enabled=True))
    with pytest.raises(rag_fusion_mod.RAGFusionDependencyError) as excinfo:
        await generator.generate_variants("query")
    assert str(excinfo.value) == (
        f"Install archon-search[rag_fusion] to use RAG Fusion ({command} 'archon-search[rag_fusion]')"
    )
    # The wire-safe half is the message minus the install command (G2).
    assert str(excinfo.value).startswith(f"{rag_fusion_mod.RAG_FUSION_DEPENDENCY_DETAIL} (")
