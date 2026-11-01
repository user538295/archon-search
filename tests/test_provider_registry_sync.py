"""BE-10 — structural CI guard: provider registry stays in sync across derived sites.

`_PROVIDER_REGISTRY` (archon_search/config.py) is the single source of truth for
supported HyDE/RAG-Fusion/graph providers (Q10=A, C4). Four other sites must be kept in
lock-step with it by hand:

  1. `_VALID_PROVIDERS` — the frozenset config.py derives from the registry.
  2. The wizard's `_prompt_provider` valid-choice set (archon_search/install/wizard.py)
     — the interactive HyDE/RAG-Fusion provider picker.
  3. The wizard's `_prompt_graph_provider` valid-choice set — the graph-enrichment
     picker; all five providers, including `claude_cli` (the graph client is built by
     the same `build_llm_client` for every registered provider).
  4. The `WizardFeatures.hyde_provider` type-annotation comment (archon_search/
     install/config_writer.py) documenting which providers
     `_apply_wizard_features_to_toml` knows how to write to `[hyde]`/`[rag_fusion]`.

If a future provider is added to the registry and one of these derived sites is not
updated, this guard fails loudly instead of shipping a half-wired provider.

`_PROVIDER_EXTRA` (archon_search/install/extras.py) is asserted to be a *subset* of
the registry, not equal to it — `claude_cli` (binary on PATH) and `llama_cpp`
(httpx, core dep) legitimately have no pip extra (llama-cpp plan S14).

Mirrors `tests/test_no_fstring_sql.py`: real guards plus meta-tests proving the
extraction regexes are neither too loose (false pass) nor too tight (false fail).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from archon_search.config import _PROVIDER_REGISTRY, _VALID_PROVIDERS
from archon_search.install.extras import _PROVIDER_EXTRA

_REPO_ROOT = Path(__file__).parent.parent
_WIZARD_PATH = _REPO_ROOT / "archon_search" / "install" / "wizard.py"
_CONFIG_WRITER_PATH = _REPO_ROOT / "archon_search" / "install" / "config_writer.py"

_QUOTED_NAME = re.compile(r'"([a-z_]+)"')


def _extract_choice_set(source: str, function_name: str) -> set[str]:
    """Pull the valid-choice set literal out of one wizard function's body.

    Bounded to *function_name* alone by slicing from its `def` to the next
    top-level `def`, so a sibling prompt's literal is never picked up.
    """
    func_match = re.search(rf"\ndef {function_name}\(.*?(?=\ndef )", source, re.DOTALL)
    assert func_match is not None, f"{function_name} function not found"
    body = func_match.group(0)
    set_match = re.search(r"ask_choice\(.*?,\s*\{([^}]*)\}", body, re.DOTALL)
    assert set_match is not None, "ask_choice(...) valid-choice set literal not found"
    return set(_QUOTED_NAME.findall(set_match.group(1)))


def _extract_prompt_provider_choice_set(source: str) -> set[str]:
    """The HyDE/RAG-Fusion picker's valid-choice set (`_prompt_provider`)."""
    return _extract_choice_set(source, "_prompt_provider")


def _extract_graph_prompt_provider_choice_set(source: str) -> set[str]:
    """The graph-enrichment picker's valid-choice set (`_prompt_graph_provider`)."""
    return _extract_choice_set(source, "_prompt_graph_provider")


def _extract_hyde_provider_comment_set(source: str) -> set[str]:
    """Pull the pipe-separated provider list out of the `hyde_provider` field comment."""
    match = re.search(r'hyde_provider:\s*str\s*=\s*"anthropic"\s*#\s*(.+)', source)
    assert match is not None, "hyde_provider field comment not found"
    return set(_QUOTED_NAME.findall(match.group(1)))


# ---------------------------------------------------------------------------
# Meta-tests: verify the extraction regexes behave correctly
# ---------------------------------------------------------------------------


def test_meta_extract_prompt_provider_choice_set_finds_literal() -> None:
    fixture = """
def _prompt_provider(feature_label, ask_choice, ollama_base_url_default):
    provider = ask_choice(
        f"Which provider? ",
        {"anthropic", "openai", "ollama"},
        "anthropic",
    )
    return provider


def _prompt_graph_provider(ask_yn, ask_choice, ollama_base_url_default):
    provider = ask_choice(
        "graph? ",
        {"anthropic", "openai", "ollama", "llama_cpp"},
        "anthropic",
    )
    return provider
"""
    assert _extract_prompt_provider_choice_set(fixture) == {"anthropic", "openai", "ollama"}


def test_meta_extract_prompt_provider_choice_set_ignores_other_functions() -> None:
    """The extractor must not accidentally pick up a later function's set literal."""
    fixture = """
def _prompt_provider(feature_label, ask_choice, ollama_base_url_default):
    provider = ask_choice(
        f"Which provider? ",
        {"anthropic"},
        "anthropic",
    )
    return provider


def _prompt_graph_provider(ask_yn, ask_choice, ollama_base_url_default):
    provider = ask_choice(
        "graph? ",
        {"anthropic", "openai", "ollama", "llama_cpp", "claude_cli", "extra_provider"},
        "anthropic",
    )
    return provider
"""
    assert _extract_prompt_provider_choice_set(fixture) == {"anthropic"}


def test_graph_extractor_detects_drift() -> None:
    """The extractor reports the reduced set when a provider is dropped from the graph prompt."""
    fixture = """
def _prompt_provider(feature_label, ask_choice, ollama_base_url_default):
    provider = ask_choice(
        "p? ",
        {"anthropic", "openai", "ollama", "claude_cli", "llama_cpp"},
        "anthropic",
    )
    return provider


def _prompt_graph_provider(ask_yn, ask_choice, ollama_base_url_default):
    provider = ask_choice(
        "graph? ",
        {"anthropic", "openai", "ollama", "claude_cli"},
        "anthropic",
    )
    return provider


def _next_function():
    return None
"""
    graph_set = _extract_graph_prompt_provider_choice_set(fixture)
    assert graph_set == {"anthropic", "openai", "ollama", "claude_cli"}
    assert graph_set != set(_PROVIDER_REGISTRY)


def test_meta_extract_choice_set_does_not_read_a_later_function() -> None:
    """A target without its own set literal fails loudly instead of reading the next function's."""
    fixture = """
def _prompt_graph_provider(ask_yn, ask_choice, ollama_base_url_default):
    return "anthropic"


def _prompt_provider(feature_label, ask_choice, ollama_base_url_default):
    return ask_choice("p? ", {"anthropic", "openai"}, "anthropic")
"""
    with pytest.raises(AssertionError, match="literal not found"):
        _extract_graph_prompt_provider_choice_set(fixture)


def test_meta_extract_hyde_provider_comment_set_finds_literal() -> None:
    fixture = '    hyde_provider: str = "anthropic"          # "anthropic" | "openai" | "ollama"\n'
    assert _extract_hyde_provider_comment_set(fixture) == {"anthropic", "openai", "ollama"}


def test_meta_extract_hyde_provider_comment_set_ignores_unrelated_lines() -> None:
    """Only the `hyde_provider` field's own comment is captured, not a sibling field's."""
    fixture = (
        '    rag_fusion_provider: str = "anthropic"     # not this one: "wrong_provider"\n'
        '    hyde_provider: str = "anthropic"          # "anthropic" | "openai" | "ollama"\n'
    )
    assert _extract_hyde_provider_comment_set(fixture) == {"anthropic", "openai", "ollama"}


# ---------------------------------------------------------------------------
# Real guards
# ---------------------------------------------------------------------------


def test_valid_providers_matches_registry() -> None:
    """`_VALID_PROVIDERS` must be exactly `set(_PROVIDER_REGISTRY)` — no drift."""
    assert _VALID_PROVIDERS == set(_PROVIDER_REGISTRY)


def test_wizard_prompt_provider_choice_set_matches_registry() -> None:
    """`_prompt_provider`'s valid-choice set must contain exactly the registry keys."""
    source = _WIZARD_PATH.read_text(encoding="utf-8")
    wizard_set = _extract_prompt_provider_choice_set(source)
    assert wizard_set == set(_PROVIDER_REGISTRY), (
        f"wizard._prompt_provider choice set {wizard_set} does not match "
        f"_PROVIDER_REGISTRY {set(_PROVIDER_REGISTRY)} — a provider was added/removed "
        "in one site without the other."
    )


def test_graph_prompt_provider_set_equals_registry() -> None:
    """`_prompt_graph_provider` offers exactly the registered providers (plan 010 S14)."""
    graph_set = _extract_graph_prompt_provider_choice_set(_WIZARD_PATH.read_text(encoding="utf-8"))
    assert graph_set == set(_PROVIDER_REGISTRY), (
        f"wizard._prompt_graph_provider choice set {graph_set} does not match "
        f"_PROVIDER_REGISTRY {set(_PROVIDER_REGISTRY)}"
    )


def test_toml_writer_hyde_provider_comment_matches_registry() -> None:
    """The `hyde_provider` field comment (config_writer.py) must list exactly the registry keys."""
    source = _CONFIG_WRITER_PATH.read_text(encoding="utf-8")
    comment_set = _extract_hyde_provider_comment_set(source)
    assert comment_set == set(_PROVIDER_REGISTRY), (
        f"WizardFeatures.hyde_provider comment set {comment_set} does not match "
        f"_PROVIDER_REGISTRY {set(_PROVIDER_REGISTRY)} — the TOML writer's documented "
        "provider set has drifted from the registry."
    )


def test_provider_extra_is_strict_subset_of_registry() -> None:
    """`_PROVIDER_EXTRA` keys must be a subset of the registry, not equal to it.

    `claude_cli` (binary on PATH) and `llama_cpp` (httpx, core dep) legitimately
    have no pip extra — llama-cpp plan S14.
    """
    extra_keys = set(_PROVIDER_EXTRA.keys())
    registry_keys = set(_PROVIDER_REGISTRY)
    assert extra_keys <= registry_keys, (
        f"_PROVIDER_EXTRA has keys not in the registry: {extra_keys - registry_keys}"
    )
    assert extra_keys != registry_keys, (
        "_PROVIDER_EXTRA should not equal the full registry — some providers "
        "(claude_cli, llama_cpp) legitimately have no pip extra"
    )


def test_graph_package_guard_table_matches_the_installer_extras() -> None:
    """The shared package guard (C12: graph and description) and the wizard's extras installer name the same extra per
    provider."""
    from archon_search.server.provider_dependency import PROVIDER_PACKAGE_EXTRA

    assert PROVIDER_PACKAGE_EXTRA == _PROVIDER_EXTRA


def test_provider_registry_is_source_of_truth() -> None:
    """All derived sites agree with `_PROVIDER_REGISTRY` (subset-only for `_PROVIDER_EXTRA`)."""
    registry_keys = set(_PROVIDER_REGISTRY)
    wizard_source = _WIZARD_PATH.read_text(encoding="utf-8")
    wizard_set = _extract_prompt_provider_choice_set(wizard_source)
    graph_set = _extract_graph_prompt_provider_choice_set(wizard_source)
    comment_set = _extract_hyde_provider_comment_set(_CONFIG_WRITER_PATH.read_text(encoding="utf-8"))

    assert _VALID_PROVIDERS == registry_keys
    assert wizard_set == registry_keys
    assert graph_set == registry_keys
    assert comment_set == registry_keys
    assert set(_PROVIDER_EXTRA.keys()) <= registry_keys
