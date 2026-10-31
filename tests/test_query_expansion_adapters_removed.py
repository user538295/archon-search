"""BE-13 (collection descriptions, S65) — the query-expansion adapter layer is gone, only the shared client remains.

HyDE and RAG Fusion call ``archon_search.llm_client``; the five ``archon_search/providers/`` adapters, the
``QueryExpansionProvider`` protocol and the ``_build_query_expansion_provider`` factory had no caller left after
BE-11 and BE-12 and were removed. Nothing in the server raises ``HyDEDependencyError`` or
``RAGFusionDependencyError`` any more (a missing package refuses to start in ``_check_provider_deps``; at call time
the shared client reports it as a call failure and the search falls back), so the errors and the transports' 422
mappings went with them.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import archon_search

_PACKAGE_ROOT = Path(archon_search.__file__).resolve().parent

_REMOVED_MODULES = (
    "archon_search.providers.anthropic_provider",
    "archon_search.providers.claude_cli_provider",
    "archon_search.providers.llama_cpp_provider",
    "archon_search.providers.ollama_provider",
    "archon_search.providers.openai_provider",
    "archon_search.query_expansion_protocol",
)


@pytest.mark.parametrize("module", _REMOVED_MODULES)
def test_old_query_expansion_module_is_not_importable(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_providers_package_holds_no_python_file() -> None:
    # A leftover ``__pycache__`` directory of a pre-removal checkout is not source: judge ``*.py`` only.
    assert not list((_PACKAGE_ROOT / "providers").rglob("*.py"))


def test_query_expansion_provider_builder_is_gone() -> None:
    app_module = importlib.import_module("archon_search.server.app")

    assert not hasattr(app_module, "_build_query_expansion_provider")


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("archon_search.hyde", "HyDEDependencyError"),
        ("archon_search.hyde", "HYDE_DEPENDENCY_DETAIL"),
        ("archon_search.hyde", "HYDE_DEPENDENCY_CODE"),
        ("archon_search.rag_fusion", "RAGFusionDependencyError"),
        ("archon_search.rag_fusion", "RAG_FUSION_DEPENDENCY_DETAIL"),
        ("archon_search.rag_fusion", "RAG_FUSION_DEPENDENCY_CODE"),
        ("archon_search.server.routes_search", "dependency_error_response"),
        ("archon_search.server.mcp", "_dependency_error_response"),
    ],
)
def test_unreachable_dependency_error_is_gone(module: str, name: str) -> None:
    assert not hasattr(importlib.import_module(module), name)
