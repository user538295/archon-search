"""Graph client composition in ``server/app.py`` — LCD BE-16 (C12, S5, S16, S20b).

Replaces the removed ``EnrichmentClientFactory`` tests: ``_build_graph_client`` routes ``[graph].provider`` to the shared
LLM client (all five providers, ``claude_cli`` included), and ``_missing_graph_package`` is the C12 guard that decides when
the graph feature gets no client because its provider package is not installed.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

from archon_search.config import GraphConfig
from archon_search.llm_client.anthropic_client import AnthropicClient
from archon_search.llm_client.claude_cli_client import ClaudeCliClient
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.openai_client import OpenAIClient
from archon_search.llm_client.protocol import LlmClient
from archon_search.server import app as app_module
from archon_search.server import provider_dependency
from archon_search.uv_tool import extra_install_hint

_MODEL = "some-model"
_CLIENT_CLASS = {
    "llama_cpp": LlamaCppClient,
    "ollama": OllamaClient,
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
    "claude_cli": ClaudeCliClient,
}


def test_no_graph_client_for_none_provider() -> None:
    config = GraphConfig(provider=None, extraction_model=_MODEL)
    assert app_module._build_graph_client(config, rate_limit_rpm=None) is None


@pytest.mark.parametrize("provider", sorted(_CLIENT_CLASS))
def test_graph_client_is_the_shared_client_of_the_configured_provider(provider: str) -> None:
    """All five providers, ``claude_cli`` included (the removed factory built none for it), give a shared client."""
    config = GraphConfig(provider=provider, extraction_model=_MODEL)
    client = app_module._build_graph_client(config, rate_limit_rpm=None)
    assert isinstance(client, _CLIENT_CLASS[provider])
    assert isinstance(client, LlmClient)
    assert not hasattr(client, "label_relationships")


#: Submodules only: a leftover ``__pycache__`` directory could make the bare package importable as a namespace package.
@pytest.mark.parametrize(
    "module",
    [
        "archon_search.enrichment.factory",
        "archon_search.enrichment.anthropic",
        "archon_search.enrichment.llama_cpp",
        "archon_search.enrichment.ollama",
        "archon_search.enrichment.openai",
        "archon_search.graph_enrichment_protocol",
    ],
)
def test_the_enrichment_adapters_and_their_protocol_are_gone(module: str) -> None:
    """Clean cutover (BE-16): the shared client is the only graph LLM path; no adapter, factory or protocol is left."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_eval_runner_graph_config_literal_has_no_provider() -> None:
    """S20b: eval/runner.py's community-build GraphConfig(...) literal never sets `provider`, so CommunityBuilder there
    always receives no graph client (eval must be deterministic; no LLM enrichment in the eval harness)."""
    # Mirrors the exact GraphConfig(...) call in archon_search/eval/runner.py:~1124.
    _graph_config = GraphConfig(
        enabled=True,
        leiden_resolution=1.0,
        community_summary_chunks=3,
        max_community_size=10,
    )
    assert _graph_config.provider is None
    assert app_module._build_graph_client(_graph_config, rate_limit_rpm=None) is None


# C12: provider -> (package, extra named in the install hint)
_GUARDED = {"ollama": ("ollama", "ollama"), "openai": ("openai", "openai-provider"), "anthropic": ("anthropic", "hyde")}


@pytest.mark.parametrize("provider", sorted(_GUARDED))
def test_missing_package_is_reported_with_its_install_command(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    package, extra = _GUARDED[provider]
    monkeypatch.setitem(sys.modules, package, None)
    config = GraphConfig(enabled=True, provider=provider, extraction_model=_MODEL)
    message = app_module._missing_graph_package(config)
    assert message is not None
    assert f"'{package}' package is not installed" in message
    assert f"run: {extra_install_hint(extra)}" in message
    assert "[graph]" in message
    assert "graph enrichment stays off" in message


@pytest.mark.parametrize("provider", sorted(_GUARDED))
def test_installed_package_is_not_reported(provider: str) -> None:
    pytest.importorskip(_GUARDED[provider][0])
    config = GraphConfig(enabled=True, provider=provider, extraction_model=_MODEL)
    assert app_module._missing_graph_package(config) is None


@pytest.mark.parametrize("provider", sorted(_GUARDED))
def test_missing_package_composes_no_graph_client(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard sits in the builder itself, so no caller (production client, startup probe) can get a client for a
    provider whose package is missing."""
    monkeypatch.setitem(sys.modules, _GUARDED[provider][0], None)
    config = GraphConfig(enabled=True, provider=provider, extraction_model=_MODEL)
    assert app_module._build_graph_client(config, rate_limit_rpm=None) is None


def _import_raising(error: BaseException) -> types.SimpleNamespace:
    """A stand-in for the ``importlib`` the shared package guard sees, whose ``import_module`` raises *error* (patched on
    the guard module, not on the process-wide ``importlib``)."""

    def _raise(name: str) -> None:
        raise error

    return types.SimpleNamespace(import_module=_raise)


@pytest.mark.parametrize(
    "error",
    [
        ImportError("cannot import name 'x' from partially initialised module"),
        ModuleNotFoundError("No module named 'pydantic'", name="pydantic"),  # a dependency is absent, not the SDK
        RuntimeError("the SDK refuses this pydantic version"),
    ],
    ids=["import-error", "other-module-missing", "non-import-error"],
)
def test_a_broken_install_is_reported_without_an_install_command(
    error: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything that is not "this package is absent" (a broken transitive import, an SDK that raises while importing)
    keeps the server booting, says so and offers no install command, which would not help."""
    monkeypatch.setattr(provider_dependency, "importlib", _import_raising(error))
    config = GraphConfig(enabled=True, provider="ollama", extraction_model=_MODEL)
    message = app_module._missing_graph_package(config)
    assert message is not None
    assert f"failed to import ({type(error).__name__})" in message
    assert "run:" not in message
    assert "graph enrichment stays off" in message


@pytest.mark.parametrize("provider", ["llama_cpp", "claude_cli", None])
def test_providers_without_a_package_are_never_reported(provider: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    for package in ("anthropic", "openai", "ollama"):
        monkeypatch.setitem(sys.modules, package, None)
    config = GraphConfig(enabled=True, provider=provider, extraction_model=_MODEL)
    assert app_module._missing_graph_package(config) is None


def test_disabled_graph_is_never_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ollama", None)
    config = GraphConfig(enabled=False, provider="ollama", extraction_model=_MODEL)
    assert app_module._missing_graph_package(config) is None


def test_graph_warning_texts_are_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The texts the shared package guard produced for graph before it was shared (UserManual troubleshooting quotes them)."""
    config = GraphConfig(enabled=True, provider="ollama", extraction_model=_MODEL)
    monkeypatch.setitem(sys.modules, "ollama", None)
    assert app_module._missing_graph_package(config) == (
        "[graph] provider='ollama' but the 'ollama' package is not installed; graph enrichment stays off "
        f"(communities are built without summaries); run: {extra_install_hint('ollama')}"
    )

    monkeypatch.undo()
    monkeypatch.setattr(provider_dependency, "importlib", _import_raising(RuntimeError("boom")))
    assert app_module._missing_graph_package(config) == (
        "[graph] provider='ollama' but the 'ollama' package failed to import (RuntimeError); graph enrichment stays off "
        "(communities are built without summaries)"
    )
