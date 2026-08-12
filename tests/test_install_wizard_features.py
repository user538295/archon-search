"""Tests for WizardFeatures dataclass (Task 1.1) and prompt functions (Tasks 1.2+)."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

from archon_search.config import LLAMA_CPP_BASE_URL_DEFAULT, OLLAMA_BASE_URL_DEFAULT, load_config
from archon_search.install import (
    WizardFeatures,
    _apply_wizard_features_to_toml,
    _check_claude_cli_present,
    _fetch_llama_cpp_models,
    _fetch_ollama_models,
    _pick_claude_model,
    _pick_llama_cpp_model,
    _pick_ollama_model,
    _prompt_gpu_confirm,
    _prompt_graph_provider,
    _prompt_llama_cpp_model,
    _prompt_model_freetext,
    _prompt_multilingual,
    _prompt_ollama_model,
    _prompt_optional_features,
    _prompt_provider,
)
from archon_search.install.wizard import _LLAMA_CPP_CACHE_LIST_TIMEOUT_SECONDS, _normalise_base_url
from archon_search.platform.types import GpuType
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES


@pytest.fixture(autouse=True)
def _clear_llama_cpp_model_cache():
    """Drop ``_fetch_llama_cpp_models``'s ``functools.cache`` around every test.

    The cache lives on the module, so it outlives any single test and is shared
    by every test in the same xdist worker. Relying on individual tests to call
    ``cache_clear()`` makes isolation a convention that a newly added test
    silently opts out of — one real scan of the developer's or CI runner's
    actual GGUF cache would then leak its result into unrelated tests.
    """
    _fetch_llama_cpp_models.cache_clear()
    yield
    _fetch_llama_cpp_models.cache_clear()


class _FakeResp:
    """Minimal context-manager stand-in for urllib.request.urlopen()."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _tags_response(*names: str) -> _FakeResp:
    """Build a fake /api/tags response body from model names."""
    return _FakeResp(json.dumps({"models": [{"name": n} for n in names]}).encode())


class TestWizardFeaturesDefaults:
    def test_defaults(self) -> None:
        f = WizardFeatures()
        assert f.install_code_extra is False
        assert f.install_multilingual_extra is False
        assert f.disable_reranker is False
        assert f.enable_watch is False
        assert f.enable_telemetry is False
        assert f.eager_load_embedders is False
        # None sentinel = "accepted default" → key omitted from the written TOML (S561).
        # An explicitly-chosen value (CLI flag or interactive answer) is a str and is written.
        assert f.routing_strategy is None
        assert f.log_format is None

    def test_install_multilingual_extra_accepts_value(self) -> None:
        f = WizardFeatures(install_multilingual_extra=True)
        assert f.install_multilingual_extra is True

    def test_custom_values(self) -> None:
        f = WizardFeatures(
            install_code_extra=True,
            disable_reranker=True,
            enable_watch=True,
            enable_telemetry=True,
            eager_load_embedders=True,
            routing_strategy="hybrid",
            log_format="json",
        )
        assert f.install_code_extra is True
        assert f.disable_reranker is True
        assert f.enable_watch is True
        assert f.enable_telemetry is True
        assert f.eager_load_embedders is True
        assert f.routing_strategy == "hybrid"
        assert f.log_format == "json"


class TestWizardFeaturesFE2GraphProviderFields:
    """FE-2: WizardFeatures carries the llama.cpp/graph-provider fields."""

    def test_wizard_features_has_five_new_fields(self) -> None:
        """All new fields exist with correct empty-string defaults."""
        f = WizardFeatures()
        assert f.hyde_llama_cpp_base_url == ""
        assert f.rag_fusion_llama_cpp_base_url == ""
        assert f.graph_provider == ""
        assert f.graph_extraction_model == ""
        assert f.graph_llama_cpp_base_url == ""
        assert f.graph_ollama_base_url == ""

    def test_new_fields_accept_values(self) -> None:
        f = WizardFeatures(
            hyde_llama_cpp_base_url="http://hyde-box:8080",
            rag_fusion_llama_cpp_base_url="http://rag-box:8080",
            graph_provider="llama_cpp",
            graph_extraction_model="qwen2.5-coder",
            graph_llama_cpp_base_url="http://graph-box:8080",
            graph_ollama_base_url="http://graph-box:11434",
        )
        assert f.hyde_llama_cpp_base_url == "http://hyde-box:8080"
        assert f.rag_fusion_llama_cpp_base_url == "http://rag-box:8080"
        assert f.graph_provider == "llama_cpp"
        assert f.graph_extraction_model == "qwen2.5-coder"
        assert f.graph_llama_cpp_base_url == "http://graph-box:8080"
        assert f.graph_ollama_base_url == "http://graph-box:11434"


class TestPromptGraphProvider:
    """FE-2: _prompt_graph_provider() — LLM-backed graph enrichment step (S18, S22, Q8=A)."""

    @staticmethod
    def _ask_choice_from(provider: str):
        def _ask_choice(_prompt: str, valid: set[str], _default: str) -> str:
            assert provider in valid
            return provider
        return _ask_choice

    def test_declined_returns_all_empty(self) -> None:
        """Answering 'n' to the enable question returns all-empty — S27 default."""
        ask_yn = lambda *_a, **_k: False  # noqa: E731
        result = _prompt_graph_provider(ask_yn, lambda *_a: "anthropic", OLLAMA_BASE_URL_DEFAULT)
        assert result == ("", "", "", "")

    def test_anthropic_prompts_for_model_no_base_url(self) -> None:
        """Anthropic still prompts for extraction_model (no built-in default, unlike HyDE)."""
        ask_yn = lambda *_a, **_k: True  # noqa: E731
        with patch("builtins.input", return_value="claude-haiku-4-5"):
            provider, model, ollama_url, llama_url = _prompt_graph_provider(
                ask_yn, self._ask_choice_from("anthropic"), OLLAMA_BASE_URL_DEFAULT
            )
        assert provider == "anthropic"
        assert model == "claude-haiku-4-5"
        assert (ollama_url, llama_url) == ("", "")

    def test_llama_cpp_uses_model_picker_and_returns_base_url(self) -> None:
        """llama_cpp lists the locally cached models and returns the picked model + base URL (S18)."""
        ask_yn = lambda *_a, **_k: True  # noqa: E731
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1", "m2"]):
            with patch("builtins.input", side_effect=["", "2"]):
                provider, model, ollama_url, llama_url = _prompt_graph_provider(
                    ask_yn, self._ask_choice_from("llama_cpp"), OLLAMA_BASE_URL_DEFAULT
                )
        assert provider == "llama_cpp"
        assert model == "m2"
        assert (ollama_url, llama_url) == ("", "")

    def test_llama_cpp_empty_cache_falls_back_to_freetext(self) -> None:
        """An empty local model cache falls back to free-text model entry (S12/S18)."""
        ask_yn = lambda *_a, **_k: True  # noqa: E731
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=[]):
            with patch("builtins.input", side_effect=["", "my-model"]):
                provider, model, ollama_url, llama_url = _prompt_graph_provider(
                    ask_yn, self._ask_choice_from("llama_cpp"), OLLAMA_BASE_URL_DEFAULT
                )
        assert provider == "llama_cpp"
        assert model == "my-model"
        assert (ollama_url, llama_url) == ("", "")

    def test_ollama_uses_the_installed_model_picker(self) -> None:
        """Ollama gets the same numbered picker HyDE/RAG-Fusion have — not free text."""
        ask_yn = lambda *_a, **_k: True  # noqa: E731
        with patch(
            "archon_search.install.wizard._fetch_ollama_models", return_value=["llama3", "qwen3"]
        ):
            with patch("builtins.input", side_effect=["", "2"]):
                provider, model, ollama_url, llama_url = _prompt_graph_provider(
                    ask_yn, self._ask_choice_from("ollama"), OLLAMA_BASE_URL_DEFAULT
                )
        assert provider == "ollama"
        assert model == "qwen3"
        assert (ollama_url, llama_url) == ("", "")

    def test_ollama_custom_base_url_is_returned_for_storage(self) -> None:
        """A custom Ollama address reaches GraphConfig.ollama_base_url instead of being dropped."""
        ask_yn = lambda *_a, **_k: True  # noqa: E731
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3"]) as fetch:
            with patch("builtins.input", side_effect=["graph-box:11434", "1"]):
                provider, model, ollama_url, llama_url = _prompt_graph_provider(
                    ask_yn, self._ask_choice_from("ollama"), OLLAMA_BASE_URL_DEFAULT
                )
        assert provider == "ollama"
        assert model == "llama3"
        assert ollama_url == "http://graph-box:11434"
        assert llama_url == ""
        assert fetch.call_args.args[0] == "http://graph-box:11434"

    def test_ollama_base_url_default_prefills_the_prompt(self) -> None:
        """A re-run keeps the address already in [graph] when the operator presses Enter."""
        ask_yn = lambda *_a, **_k: True  # noqa: E731
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3"]) as fetch:
            with patch("builtins.input", side_effect=["", "1"]):
                _, _, ollama_url, _ = _prompt_graph_provider(
                    ask_yn, self._ask_choice_from("ollama"), "http://saved-box:11434"
                )
        assert ollama_url == "http://saved-box:11434"
        assert fetch.call_args.args[0] == "http://saved-box:11434"

    def test_llama_cpp_base_url_default_prefills_the_prompt(self) -> None:
        """F2: a graph-enrichment re-run must not wipe a previously saved llama-server URL."""
        ask_yn = lambda *_a, **_k: True  # noqa: E731
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]):
            with patch("builtins.input", side_effect=["", "1"]) as mock_input:
                _, _, ollama_url, llama_url = _prompt_graph_provider(
                    ask_yn, self._ask_choice_from("llama_cpp"), OLLAMA_BASE_URL_DEFAULT, "http://saved-box:8080"
                )
        assert "http://saved-box:8080" in mock_input.call_args_list[0].args[0]
        assert llama_url == "http://saved-box:8080"
        assert ollama_url == ""


class TestPromptMultilingual:
    def test_flag_true_skips_prompt(self) -> None:
        """flag_value=True returns True without reading any input."""
        with patch("builtins.input", side_effect=AssertionError("should not call input")):
            result = _prompt_multilingual(non_interactive=False, flag_value=True)
        assert result is True

    def test_non_interactive_returns_false(self) -> None:
        """non_interactive=True, flag_value=None returns False without prompt."""
        with patch("builtins.input", side_effect=AssertionError("should not call input")):
            result = _prompt_multilingual(non_interactive=True, flag_value=None)
        assert result is False

    def test_interactive_yes(self) -> None:
        """Input 'y' returns True (flag_value=None → interactive)."""
        with patch("builtins.input", return_value="y"):
            result = _prompt_multilingual(non_interactive=False, flag_value=None)
        assert result is True

    def test_interactive_no(self) -> None:
        """Empty input returns False (flag_value=None → interactive)."""
        with patch("builtins.input", return_value=""):
            result = _prompt_multilingual(non_interactive=False, flag_value=None)
        assert result is False

    def test_interactive_eof(self) -> None:
        """EOFError returns False without raising (flag_value=None → interactive)."""
        with patch("builtins.input", side_effect=EOFError):
            result = _prompt_multilingual(non_interactive=False, flag_value=None)
        assert result is False

    def test_interactive_yes_uppercase(self) -> None:
        """Input 'YES' (case-insensitive) returns True (flag_value=None → interactive)."""
        with patch("builtins.input", return_value="YES"):
            result = _prompt_multilingual(non_interactive=False, flag_value=None)
        assert result is True

    # ------------------------------------------------------------------
    # Task 2.1 — tri-state (bool | None) tests
    # ------------------------------------------------------------------

    def test_prompt_multilingual_flag_true(self) -> None:
        """flag_value=True returns True without calling input()."""
        with patch("builtins.input", side_effect=AssertionError("should not call input")):
            result = _prompt_multilingual(non_interactive=False, flag_value=True)
        assert result is True

    def test_prompt_multilingual_flag_false(self) -> None:
        """flag_value=False (explicit --no-multilingual) returns False without calling input()."""
        with patch("builtins.input", side_effect=AssertionError("should not call input")):
            result = _prompt_multilingual(non_interactive=False, flag_value=False)
        assert result is False

    def test_prompt_multilingual_flag_none_interactive(self) -> None:
        """flag_value=None in interactive mode asks the user; 'y' → True."""
        with patch("builtins.input", return_value="y"):
            result = _prompt_multilingual(non_interactive=False, flag_value=None)
        assert result is True

    def test_prompt_multilingual_flag_none_non_interactive(self) -> None:
        """flag_value=None in non-interactive mode returns False without calling input()."""
        with patch("builtins.input", side_effect=AssertionError("should not call input")):
            result = _prompt_multilingual(non_interactive=True, flag_value=None)
        assert result is False


class TestPromptOptionalFeatures:
    """Tests for _prompt_optional_features() — Task 1.3."""

    # Profile fixtures
    _profile_with_reranker = ENGLISH_PROFILES["minimal"]   # reranker is not None
    _profile_no_reranker = MULTILINGUAL_PROFILES["minimal"]  # reranker is None

    def test_non_interactive_defaults(self) -> None:
        """non_interactive=True with all None flags returns WizardFeatures with all defaults."""
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            features = _prompt_optional_features(
                non_interactive=True,
                profile=self._profile_with_reranker,
            )
        assert features == WizardFeatures()

    def test_flag_overrides_respected(self) -> None:
        """Non-None flag values are used directly without prompting."""
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            features = _prompt_optional_features(
                non_interactive=True,
                profile=self._profile_with_reranker,
                install_code=True,
                disable_reranker=True,
                enable_watch=True,
                enable_telemetry=True,
                eager_load=True,
                routing_strategy="hybrid",
                log_format="json",
            )
        assert features.install_code_extra is True
        assert features.disable_reranker is True
        assert features.enable_watch is True
        assert features.enable_telemetry is True
        assert features.eager_load_embedders is True
        assert features.routing_strategy == "hybrid"
        assert features.log_format == "json"

    def test_interactive_all_yes(self) -> None:
        """All 'y' inputs (and valid choices) produce all-enabled features."""
        # 7 questions: code(y), reranker(y=keep enabled), watch(y), telemetry(y), eager_load(y),
        # routing_strategy("hybrid"), log_format("json")
        # enable_hyde/enable_rag_fusion pre-answered False to isolate from HyDE prompt changes.
        responses = iter(["y", "y", "y", "y", "y", "hybrid", "json"])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.install_code_extra is True
        assert features.disable_reranker is False
        assert features.enable_watch is True
        assert features.enable_telemetry is True
        assert features.eager_load_embedders is True
        assert features.routing_strategy == "hybrid"
        assert features.log_format == "json"

    def test_reranker_question_skipped_when_no_reranker(self) -> None:
        """When profile.reranker is None, disable_reranker stays False without prompting."""
        # 6 questions (no reranker question): code, watch, telemetry, eager_load,
        # routing_strategy, log_format
        responses = iter(["y", "y", "y", "y", "centroid", "text"])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_no_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.disable_reranker is False

    def test_invalid_routing_strategy_retries(self) -> None:
        """First 'bad' then 'hybrid' → routing_strategy='hybrid'."""
        # questions: code(n), reranker(n), watch(n), telemetry(n), eager(n), routing("bad","hybrid"), log("")
        responses = iter(["n", "n", "n", "n", "n", "bad", "hybrid", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.routing_strategy == "hybrid"

    def test_invalid_routing_strategy_twice_uses_default(self) -> None:
        """Two bad routing values → routing_strategy='centroid' (default)."""
        responses = iter(["n", "n", "n", "n", "n", "bad", "worse", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.routing_strategy == "centroid"

    def test_eof_uses_defaults(self) -> None:
        """EOFError on any question uses defaults for remaining questions; no raise."""
        with patch("builtins.input", side_effect=EOFError):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        # routing_strategy/log_format come back as the concrete prompt defaults (not the
        # None sentinel): this is the interactive path, where _ask_choice always yields a str.
        assert features == WizardFeatures(routing_strategy="centroid", log_format="text")

    def test_invalid_log_format_retries(self) -> None:
        """First 'bad' then 'json' → log_format='json'."""
        # n(code), n(reranker), n(watch), n(telemetry), n(eager), ""(routing), "bad"(log retry 1),
        # "json"(log retry 2)
        responses = iter(["n", "n", "n", "n", "n", "", "bad", "json"])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.log_format == "json"

    def test_invalid_log_format_twice_uses_default(self) -> None:
        """Two bad log_format values → log_format='text' (default)."""
        responses = iter(["n", "n", "n", "n", "n", "", "bad", "worse"])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.log_format == "text"

    def test_partial_flag_override_interactive_rest(self) -> None:
        """Some flags pre-answered; stdin only called for non-overridden questions."""
        # install_code=True, enable_watch=True, enable_hyde=False, enable_rag_fusion=False pre-answered.
        # Remaining interactive questions (with reranker profile):
        #   reranker(y=keep enabled), telemetry(y), eager(n), routing(""), log("")
        responses = iter(["y", "y", "n", "", ""])
        with patch("builtins.input", side_effect=responses) as mock_input:
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                install_code=True,
                enable_watch=True,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.install_code_extra is True
        assert features.enable_watch is True
        assert features.enable_telemetry is True
        assert features.disable_reranker is False
        assert mock_input.call_count == 5  # reranker, telemetry, eager, routing, log

    def test_eof_midway_preserves_prior_answers(self) -> None:
        """EOF after 2 questions answered → first 2 preserved, remaining use defaults."""
        # code=y, reranker=y (keep enabled) → then EOFError for all remaining questions
        responses = ["y", "y"]

        call_count = 0

        def mock_input_fn(prompt: str = "") -> str:  # noqa: ARG001
            nonlocal call_count
            if call_count < len(responses):
                val = responses[call_count]
                call_count += 1
                return val
            call_count += 1
            raise EOFError

        with patch("builtins.input", side_effect=mock_input_fn):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        assert features.install_code_extra is True
        assert features.disable_reranker is False
        # remaining questions use defaults due to EOFError
        assert features.enable_watch is False
        assert features.enable_telemetry is False
        assert features.eager_load_embedders is False
        assert features.routing_strategy == "centroid"
        assert features.log_format == "text"

    def test_ask_yn_empty_input_default_true_keeps_reranker(self) -> None:
        """Pressing Enter at [Y/n] prompt (default=True) keeps the reranker enabled.

        Guards the _ask_yn fix: empty input must return default (True), so
        disable_reranker = not True = False (reranker stays on).
        """
        # code=n, reranker=""(Enter→default=True=keep), watch=n, telemetry=n,
        # eager=n, routing="", log=""
        responses = iter(["n", "", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.disable_reranker is False, (
            "_ask_yn with default=True returned False for empty input — reranker was disabled"
        )

    def test_ask_yn_n_input_default_true_disables_reranker(self) -> None:
        """Answering 'n' at [Y/n] prompt (default=True) disables the reranker.

        The mutation-killing negative case: 'n' must explicitly decline,
        so disable_reranker = not False = True.
        """
        # code=n, reranker="n"(decline keep→disable), watch=n, telemetry=n,
        # eager=n, routing="", log=""
        responses = iter(["n", "n", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.disable_reranker is True, (
            "'n' at [Y/n] prompt must disable the reranker"
        )

    def test_ask_yn_empty_input_default_false_leaves_code_disabled(self) -> None:
        """Pressing Enter at [y/N] prompt (default=False) keeps the feature disabled.

        Guards that the default=True fix did not break default=False: empty input
        must return False for code enrichment.
        """
        # code=""(Enter→default=False=don't install), reranker=n, watch=n,
        # telemetry=n, eager=n, routing="", log=""
        responses = iter(["", "n", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.install_code_extra is False, (
            "Empty input at [y/N] prompt must return False (default)"
        )

    def test_ask_yn_unrecognized_input_default_true_keeps_reranker(self) -> None:
        """Unrecognized non-empty input at [Y/n] prompt (default=True) keeps the reranker.

        Guards the M1 fix: 'yep' is not in {'n', 'no'}, so _ask_yn returns True
        and disable_reranker = not True = False.
        """
        # code=n, reranker="yep"(not 'n'/'no'→True=keep enabled), watch=n,
        # telemetry=n, eager=n, routing="", log=""
        responses = iter(["n", "yep", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.disable_reranker is False, (
            "Unrecognized input 'yep' at [Y/n] prompt must be treated as yes (keep enabled)"
        )

    def test_ask_yn_no_variant_default_true_disables_reranker(self) -> None:
        """'no' (full word) at [Y/n] prompt also disables the reranker."""
        # code=n, reranker="no" (full word, in {"n","no"}→False=decline keep→disable),
        # watch=n, telemetry=n, eager=n, routing="", log=""
        responses = iter(["n", "no", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.disable_reranker is True

    def test_ask_yn_yes_variant_default_false_enables_code(self) -> None:
        """'yes' (full word) at [y/N] prompt enables code enrichment."""
        # code="yes" (full word, in {"y","yes"}→True=install),
        # reranker=n, watch=n, telemetry=n, eager=n, routing="", log=""
        responses = iter(["yes", "n", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.install_code_extra is True

    def test_ask_yn_eof_default_true_keeps_reranker(self) -> None:
        """EOFError at [Y/n] prompt (default=True) keeps the reranker enabled.

        Uses the StopIteration→EOFError pattern from test_wizard_eof_during_model_prompt:
        first call returns "n" for code enrichment; every subsequent call raises EOFError.
        The reranker prompt (second input call) gets EOFError → _ask_yn returns True
        (default) → disable_reranker = not True = False.
        """
        inputs = iter(["n"])

        def mock_input(prompt: str = "") -> str:  # noqa: ARG001
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError

        with patch("builtins.input", side_effect=mock_input):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert features.disable_reranker is False


class TestPromptOptionalFeaturesExplanations:
    """Tests for Task 4.1 — explanation print blocks in _prompt_optional_features."""

    _profile_with_reranker = ENGLISH_PROFILES["minimal"]   # reranker is not None
    _profile_no_reranker = MULTILINGUAL_PROFILES["minimal"]  # reranker is None

    def _run_non_interactive(self, profile, capsys) -> str:
        _prompt_optional_features(non_interactive=True, profile=profile)
        captured = capsys.readouterr()
        return captured.out

    def test_explanation_printed_in_non_interactive_mode(self, capsys) -> None:
        """non-interactive run still prints explanation text for at least 3 prompts."""
        out = self._run_non_interactive(self._profile_with_reranker, capsys)
        assert "Code enrichment" in out
        assert "Filesystem watcher" in out
        assert "Local telemetry" in out

    def test_explanation_printed_in_interactive_mode(self, capsys) -> None:
        """interactive run prints explanation text for at least 3 prompts."""
        responses = iter(["n", "n", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses):
            _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        captured = capsys.readouterr()
        out = captured.out
        assert "Code enrichment" in out
        assert "Reranker" in out
        assert "Filesystem watcher" in out

    def test_no_markdown_in_explanation_output(self, capsys) -> None:
        """Explanation output must not contain raw Markdown markers."""
        out = self._run_non_interactive(self._profile_with_reranker, capsys)
        assert "**" not in out
        assert "``" not in out
        assert "](http" not in out

    def test_reranker_explanation_skipped_when_no_reranker(self, capsys) -> None:
        """Profile with reranker=None: reranker explanation text does NOT appear."""
        out = self._run_non_interactive(self._profile_no_reranker, capsys)
        assert "cross-encoder" not in out.lower()
        assert "second-stage" not in out.lower()

    def test_reranker_explanation_shown_when_reranker_present(self, capsys) -> None:
        """Profile with reranker set: reranker explanation text appears."""
        out = self._run_non_interactive(self._profile_with_reranker, capsys)
        assert "Reranker" in out

    def test_all_7_explanation_blocks_with_reranker(self, capsys) -> None:
        """All 7 feature sections are explained when profile has a reranker."""
        out = self._run_non_interactive(self._profile_with_reranker, capsys)
        for keyword in [
            "Code enrichment",
            "Reranker",
            "Filesystem watcher",
            "Local telemetry",
            "Eager embedder",
            "Routing strategy",
            "Log format",
        ]:
            assert keyword in out, f"Missing explanation block for: {keyword!r}"

    def test_6_explanation_blocks_without_reranker(self, capsys) -> None:
        """Only 6 feature sections explained when profile has no reranker."""
        out = self._run_non_interactive(self._profile_no_reranker, capsys)
        for keyword in [
            "Code enrichment",
            "Filesystem watcher",
            "Local telemetry",
            "Eager embedder",
            "Routing strategy",
            "Log format",
        ]:
            assert keyword in out, f"Missing explanation block for: {keyword!r}"
        assert "cross-encoder" not in out.lower()

    def test_prompt_count_7_with_reranker(self, capsys) -> None:
        """Interactive mode with reranker profile (hyde/rag_fusion pre-answered): exactly 7 input() calls."""
        responses = iter(["n", "n", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses) as mock_input:
            _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert mock_input.call_count == 7

    def test_prompt_count_6_without_reranker(self, capsys) -> None:
        """Interactive mode without reranker profile (hyde/rag_fusion pre-answered): exactly 6 input() calls."""
        responses = iter(["n", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses) as mock_input:
            _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_no_reranker,
                enable_hyde=False,
                enable_rag_fusion=False,
            )
        assert mock_input.call_count == 6

    def test_prompt_count_9_with_reranker_no_api_key_gate(self, capsys) -> None:
        """Interactive mode with reranker profile: 9 input() calls (7 standard + 1 HyDE yn + 1 graph yn).

        After G10 BE-8, HyDE/RAG Fusion prompt fires regardless of ANTHROPIC_API_KEY.
        Answering 'n' to the enable question requires no further provider prompts.
        FE-2 adds a trailing graph-enrichment yn, also declined with "n".
        """
        # 7 standard + 1 HyDE yn (answered "n", so no provider/model prompts) + 1 graph yn ("n")
        responses = iter(["n", "n", "n", "n", "n", "", "", "n", "n"])
        with patch("builtins.input", side_effect=responses) as mock_input:
            _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        assert mock_input.call_count == 9


class TestWizardFeaturesC15NewFields:
    """Tests for C15 Task 1.1 — 9 new WizardFeatures fields."""

    def test_wizard_features_new_fields_default_to_none_or_false(self) -> None:
        """All new C15 fields have correct defaults (None or False)."""
        f = WizardFeatures()
        assert f.host is None
        assert f.port is None
        assert f.db_path is None
        assert f.log_level is None
        assert f.top_k is None
        assert f.telemetry_retention_days is None
        assert f.enable_hyde is False
        assert f.enable_rag_fusion is False

    def test_wizard_features_new_fields_accept_values(self) -> None:
        """All new C15 fields accept non-default values."""
        f = WizardFeatures(
            host="0.0.0.0",
            port=9000,
            db_path="~/custom",
            log_level="DEBUG",
            top_k=20,
            telemetry_retention_days=7,
            enable_hyde=True,
            enable_rag_fusion=True,
        )
        assert f.host == "0.0.0.0"
        assert f.port == 9000
        assert f.db_path == "~/custom"
        assert f.log_level == "DEBUG"
        assert f.top_k == 20
        assert f.telemetry_retention_days == 7
        assert f.enable_hyde is True
        assert f.enable_rag_fusion is True

    def test_wizard_features_existing_fields_unchanged(self) -> None:
        """Existing WizardFeatures fields are unaffected by new additions."""
        f = WizardFeatures(
            install_code_extra=True,
            disable_reranker=True,
            enable_watch=True,
            enable_telemetry=True,
            eager_load_embedders=True,
            routing_strategy="hybrid",
            log_format="json",
        )
        assert f.install_code_extra is True
        assert f.disable_reranker is True
        assert f.enable_watch is True
        assert f.enable_telemetry is True
        assert f.eager_load_embedders is True
        assert f.routing_strategy == "hybrid"
        assert f.log_format == "json"
        # New fields still default
        assert f.host is None
        assert f.port is None


class TestPromptOptionalFeaturesHydeRagFusion:
    """Tests for C15 Task 4.2 — HyDE/RAG Fusion in _prompt_optional_features()."""

    _profile = ENGLISH_PROFILES["minimal"]  # reranker is not None

    def test_hyde_rag_fusion_prompted_regardless_of_api_key(self) -> None:
        """HyDE/RAG Fusion prompt fires without ANTHROPIC_API_KEY (G10 BE-8: no API key gate)."""
        env_without_key = {k: v for k, v in __import__("os").environ.items() if k != "ANTHROPIC_API_KEY"}
        # Answer "n" to enable → no further provider prompts; trailing "n" declines
        # the FE-2 graph-enrichment step, which shares this interactive branch.
        with patch.dict("os.environ", env_without_key, clear=True):
            with patch("builtins.input", side_effect=["n", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )
        assert features.enable_hyde is False
        assert features.enable_rag_fusion is False

    def test_hyde_rag_fusion_prompted_when_api_key_present(self) -> None:
        """Interactive + answer 'y' + 'anthropic' for both → both enabled."""
        # 4 inputs: enable=y, hyde_provider=anthropic, rag_fusion_provider=anthropic
        # (no model prompt for anthropic), then "n" declines the FE-2 graph-enrichment step.
        with patch("builtins.input", side_effect=["y", "anthropic", "anthropic", "n"]):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile,
                install_code=False,
                disable_reranker=False,
                enable_watch=False,
                enable_telemetry=False,
                eager_load=False,
                routing_strategy="centroid",
                log_format="text",
            )
        assert features.enable_hyde is True
        assert features.enable_rag_fusion is True

    def test_hyde_rag_fusion_declined(self) -> None:
        """Interactive + answer 'n' → both remain False; no provider prompt fired."""
        # Trailing "n" declines the FE-2 graph-enrichment step, which shares this branch.
        with patch("builtins.input", side_effect=["n", "n"]):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile,
                install_code=False,
                disable_reranker=False,
                enable_watch=False,
                enable_telemetry=False,
                eager_load=False,
                routing_strategy="centroid",
                log_format="text",
            )
        assert features.enable_hyde is False
        assert features.enable_rag_fusion is False

    def test_hyde_rag_fusion_skipped_non_interactive_even_with_key(self) -> None:
        """non_interactive=True with ANTHROPIC_API_KEY → no prompt, both remain False."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch("builtins.input", side_effect=AssertionError("should not prompt")):
                features = _prompt_optional_features(
                    non_interactive=True,
                    profile=self._profile,
                )
        assert features.enable_hyde is False
        assert features.enable_rag_fusion is False

    def test_enable_hyde_flag_bypasses_prompt(self) -> None:
        """enable_hyde=True flag pre-answers the prompt; no input() call for it."""
        with patch("builtins.input", side_effect=AssertionError("should not prompt for hyde")):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile,
                install_code=False,
                disable_reranker=False,
                enable_watch=False,
                enable_telemetry=False,
                eager_load=False,
                routing_strategy="centroid",
                log_format="text",
                enable_hyde=True,
            )
        assert features.enable_hyde is True

    def test_enable_rag_fusion_flag_bypasses_prompt(self) -> None:
        """enable_rag_fusion=True flag pre-answers the prompt; no input() call for it."""
        with patch("builtins.input", side_effect=AssertionError("should not prompt for rag_fusion")):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile,
                install_code=False,
                disable_reranker=False,
                enable_watch=False,
                enable_telemetry=False,
                eager_load=False,
                routing_strategy="centroid",
                log_format="text",
                enable_rag_fusion=True,
            )
        assert features.enable_rag_fusion is True

    def test_enable_hyde_false_flag_bypasses_prompt(self) -> None:
        """enable_hyde=False flag pre-answers even when ANTHROPIC_API_KEY is set."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch("builtins.input", side_effect=AssertionError("should not prompt")):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                    enable_hyde=False,
                    enable_rag_fusion=False,
                )
        assert features.enable_hyde is False
        assert features.enable_rag_fusion is False


class TestPromptGpuConfirm:
    """Tests for _prompt_gpu_confirm() — Task 1.4."""

    def test_no_gpu_returns_true(self) -> None:
        """GpuType.NONE always returns True without prompting."""
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = _prompt_gpu_confirm(non_interactive=False, gpu=GpuType.NONE)
        assert result is True

    def test_non_interactive_metal_returns_true(self) -> None:
        """non_interactive=True with Metal GPU returns True without prompting."""
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = _prompt_gpu_confirm(non_interactive=True, gpu=GpuType.METAL)
        assert result is True

    def test_interactive_metal_accept(self) -> None:
        """Empty input (default) with Metal returns True."""
        with patch("builtins.input", return_value=""):
            result = _prompt_gpu_confirm(non_interactive=False, gpu=GpuType.METAL)
        assert result is True

    def test_interactive_metal_decline(self) -> None:
        """Input 'n' with Metal returns False."""
        with patch("builtins.input", return_value="n"):
            result = _prompt_gpu_confirm(non_interactive=False, gpu=GpuType.METAL)
        assert result is False

    def test_interactive_cuda_decline(self) -> None:
        """Input 'no' with CUDA returns False."""
        with patch("builtins.input", return_value="no"):
            result = _prompt_gpu_confirm(non_interactive=False, gpu=GpuType.CUDA)
        assert result is False

    def test_interactive_cuda_accept(self) -> None:
        """Empty input (default) with CUDA returns True."""
        with patch("builtins.input", return_value=""):
            result = _prompt_gpu_confirm(non_interactive=False, gpu=GpuType.CUDA)
        assert result is True

    def test_eof_returns_true(self) -> None:
        """EOFError returns True (auto-enable)."""
        with patch("builtins.input", side_effect=EOFError):
            result = _prompt_gpu_confirm(non_interactive=False, gpu=GpuType.METAL)
        assert result is True


class TestWizardFeaturesG10:
    """G10 BE-8 tests — provider-selection in HyDE/RAG Fusion wizard prompts."""

    _profile = ENGLISH_PROFILES["minimal"]  # reranker is not None

    # -------------------------------------------------------------------------
    # WizardFeatures new provider fields
    # -------------------------------------------------------------------------

    def test_wizard_features_g10_provider_fields_defaults(self) -> None:
        """G10 provider fields have correct defaults."""
        f = WizardFeatures()
        assert f.hyde_provider == "anthropic"
        assert f.hyde_model == ""
        assert f.hyde_ollama_base_url == ""
        assert f.rag_fusion_provider == "anthropic"
        assert f.rag_fusion_model == ""
        assert f.rag_fusion_ollama_base_url == ""

    # -------------------------------------------------------------------------
    # _apply_wizard_features_to_toml — provider/model/base_url writing
    # -------------------------------------------------------------------------

    def test_wizard_ollama_writes_provider_and_model_to_toml(self) -> None:
        """Ollama provider + model written under [hyde] when feature enabled."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        doc = tomlkit.parse(_default_toml())
        features = WizardFeatures(
            enable_hyde=True,
            hyde_provider="ollama",
            hyde_model="llama3.2",
            hyde_ollama_base_url="http://localhost:11434",
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["hyde"]["enabled"] is True
        assert doc["hyde"]["provider"] == "ollama"
        assert doc["hyde"]["model"] == "llama3.2"
        assert doc["hyde"]["ollama_base_url"] == "http://localhost:11434"

    def test_wizard_anthropic_provider_not_written_to_toml(self) -> None:
        """Anthropic provider is the default — no provider/model key written."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        doc = tomlkit.parse(_default_toml())
        features = WizardFeatures(
            enable_hyde=True,
            hyde_provider="anthropic",
            hyde_model="",
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["hyde"]["enabled"] is True
        assert "provider" not in doc["hyde"]
        assert "model" not in doc["hyde"]

    def test_wizard_openai_writes_provider_and_model_to_toml(self) -> None:
        """OpenAI provider + model written under [rag_fusion]."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        doc = tomlkit.parse(_default_toml())
        features = WizardFeatures(
            enable_rag_fusion=True,
            rag_fusion_provider="openai",
            rag_fusion_model="gpt-4o",
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["rag_fusion"]["enabled"] is True
        assert doc["rag_fusion"]["provider"] == "openai"
        assert doc["rag_fusion"]["model"] == "gpt-4o"
        assert "ollama_base_url" not in doc["rag_fusion"]

    def test_wizard_ollama_empty_base_url_not_written(self) -> None:
        """Empty ollama_base_url is not written (default handled by config)."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        doc = tomlkit.parse(_default_toml())
        features = WizardFeatures(
            enable_hyde=True,
            hyde_provider="ollama",
            hyde_model="llama3.2",
            hyde_ollama_base_url="",  # empty — not written
        )
        _apply_wizard_features_to_toml(doc, features)
        assert "ollama_base_url" not in doc["hyde"]

    # -------------------------------------------------------------------------
    # _prompt_optional_features — provider-selection step
    # -------------------------------------------------------------------------

    def test_wizard_no_api_key_gate_for_ollama(self) -> None:
        """HyDE/RAG Fusion prompt fires without ANTHROPIC_API_KEY; Ollama selected."""
        env_without_key = {k: v for k, v in __import__("os").environ.items() if k != "ANTHROPIC_API_KEY"}
        # New flow — inputs: enable=y, hyde_provider=ollama, hyde_base_url="",
        #   pick "1" from fetched list; rag_fusion_provider=ollama, base_url="", pick "1".
        with patch.dict("os.environ", env_without_key, clear=True):
            with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]):
                with patch("builtins.input", side_effect=["y", "ollama", "", "1", "ollama", "", "1", "n"]):
                    features = _prompt_optional_features(
                        non_interactive=False,
                        profile=self._profile,
                        install_code=False,
                        disable_reranker=False,
                        enable_watch=False,
                        enable_telemetry=False,
                        eager_load=False,
                        routing_strategy="centroid",
                        log_format="text",
                    )
        assert features.enable_hyde is True
        assert features.hyde_provider == "ollama"
        assert features.hyde_model == "llama3.2"

    def test_wizard_anthropic_path_unchanged(self) -> None:
        """Selecting Anthropic writes enabled=True but no provider/model to TOML."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        # Inputs: enable=y, hyde_provider=anthropic (no model prompt)
        #         rag_fusion_provider=anthropic (no model prompt), then "n" declines graph enrichment
        with patch("builtins.input", side_effect=["y", "anthropic", "anthropic", "n"]):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile,
                install_code=False,
                disable_reranker=False,
                enable_watch=False,
                enable_telemetry=False,
                eager_load=False,
                routing_strategy="centroid",
                log_format="text",
            )
        assert features.enable_hyde is True
        assert features.hyde_provider == "anthropic"

        # Also verify TOML output does not contain provider key
        doc = tomlkit.parse(_default_toml())
        _apply_wizard_features_to_toml(doc, features)
        assert doc["hyde"]["enabled"] is True
        assert "provider" not in doc["hyde"]

    def test_wizard_ollama_default_base_url_on_empty_input(self) -> None:
        """Empty input for Ollama base URL stores empty string (config uses default)."""
        # New flow — inputs: enable=y, provider=ollama, base_url="" (default), pick "1".
        # Same for rag_fusion.
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]):
            with patch("builtins.input", side_effect=["y", "ollama", "", "1", "ollama", "", "1", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )
        assert features.hyde_ollama_base_url == ""
        assert features.rag_fusion_ollama_base_url == ""

    def test_wizard_eof_during_model_prompt_returns_empty_model(self) -> None:
        """EOFError during model name prompt leaves model empty.

        _apply_wizard_features_to_toml writes model="" explicitly for non-Anthropic
        providers, so config.py raises ConfigError at startup when model is blank.
        """
        # "y" = enable, "ollama" = HyDE provider, then EOFError on base-URL prompt and
        # everything after. Empty model list forces the free-text fallback, which also EOFs.
        inputs = iter(["y", "ollama"])

        def mock_input(prompt: str = "") -> str:  # noqa: ARG001
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError

        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=[]):
            with patch("builtins.input", side_effect=mock_input):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )
        assert features.enable_hyde is True
        assert features.hyde_provider == "ollama"
        assert features.hyde_model == ""  # empty on EOFError

    def test_wizard_invalid_provider_falls_back_to_anthropic(self) -> None:
        """Two invalid provider choices fall back to default 'anthropic'."""
        # "y" = enable, "foobar" = invalid (retry), "alsobad" = invalid (fall back to anthropic)
        # Then RAG Fusion provider prompt fires — provide "anthropic"; "n" declines graph enrichment.
        with patch("builtins.input", side_effect=["y", "foobar", "alsobad", "anthropic", "n"]):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile,
                install_code=False,
                disable_reranker=False,
                enable_watch=False,
                enable_telemetry=False,
                eager_load=False,
                routing_strategy="centroid",
                log_format="text",
            )
        assert features.hyde_provider == "anthropic"
        assert features.hyde_model == ""  # anthropic path, no model prompt
        assert features.rag_fusion_provider == "anthropic"

    def test_wizard_picker_invalid_number_then_valid(self) -> None:
        """An out-of-range picker entry re-prompts; a valid number on retry is accepted."""
        # "y"=enable, "ollama"=hyde provider, ""=base url (default), "9"=out of range (retry),
        # "1"=valid → llama3.2; "ollama"=rag provider, ""=base url, "1"=valid → llama3.2.
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2", "mistral"]):
            with patch("builtins.input", side_effect=["y", "ollama", "", "9", "1", "ollama", "", "1", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )
        assert features.hyde_provider == "ollama"
        assert features.hyde_model == "llama3.2"
        assert features.rag_fusion_provider == "ollama"
        assert features.rag_fusion_model == "llama3.2"

    def test_wizard_rag_fusion_openai_model_retry_on_empty_then_valid(self) -> None:
        """RAG Fusion OpenAI free-text model re-prompts on empty; valid accepted on retry."""
        # "y"=enable, "ollama"=hyde provider, ""=hyde base url, "1"=pick llama3.2;
        # "openai"=rag provider, ""=empty model (retry), "gpt-4o"=valid.
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]):
            with patch("builtins.input", side_effect=["y", "ollama", "", "1", "openai", "", "gpt-4o", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )
        assert features.hyde_provider == "ollama"
        assert features.hyde_model == "llama3.2"
        assert features.rag_fusion_provider == "openai"
        assert features.rag_fusion_model == "gpt-4o"

    def test_wizard_non_anthropic_empty_model_writes_empty_to_toml(self) -> None:
        """When model is empty for non-Anthropic provider, empty string is written to TOML.

        This ensures config.py's 'if not model: raise ConfigError' guard fires at
        server startup rather than silently running an Anthropic model against OpenAI/Ollama.
        """
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        doc = tomlkit.parse(_default_toml())
        features = WizardFeatures(
            enable_hyde=True,
            hyde_provider="openai",
            hyde_model="",  # empty — both retries exhausted / EOF
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["hyde"]["provider"] == "openai"
        # Empty model is written explicitly so config.py raises ConfigError at startup
        assert doc["hyde"]["model"] == ""


# =============================================================================
# Ollama model picker (2026-07-15 brief) — unit tests for the new helpers
# =============================================================================


class TestFetchOllamaModels:
    """Unit tests for _fetch_ollama_models() — HTTP fetch with graceful failure."""

    def test_returns_sorted_model_names(self) -> None:
        """A well-formed /api/tags response yields a sorted name list."""
        with patch("archon_search.install.urllib.request.urlopen", return_value=_tags_response("mistral", "llama3.2")):
            models = _fetch_ollama_models("http://localhost:11434")
        assert models == ["llama3.2", "mistral"]

    def test_hits_api_tags_endpoint(self) -> None:
        """The fetch targets {base_url}/api/tags with the trailing slash stripped."""
        with patch("archon_search.install.urllib.request.urlopen", return_value=_tags_response("llama3.2")) as mock_urlopen:
            _fetch_ollama_models("http://box:11434/")
        called_url = mock_urlopen.call_args.args[0]
        assert called_url == "http://box:11434/api/tags"

    def test_connection_error_returns_empty(self) -> None:
        """A URLError (server down) is swallowed and returns []."""
        with patch("archon_search.install.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            assert _fetch_ollama_models("http://localhost:11434") == []

    def test_os_error_returns_empty(self) -> None:
        """A socket/timeout OSError is swallowed and returns []."""
        with patch("archon_search.install.urllib.request.urlopen", side_effect=OSError("timed out")):
            assert _fetch_ollama_models("http://localhost:11434") == []

    def test_truncated_read_does_not_raise(self) -> None:
        """An http.client.IncompleteRead during resp.read() is swallowed (C2-I-1).

        IncompleteRead subclasses HTTPException, not OSError/ValueError, so it would
        escape a narrow except tuple and crash the wizard on a truncated response body.
        """
        import http.client  # noqa: PLC0415

        class _Truncated:
            def __enter__(self) -> _Truncated:
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def read(self) -> bytes:
                raise http.client.IncompleteRead(b"partial")

        with patch("archon_search.install.urllib.request.urlopen", return_value=_Truncated()):
            assert _fetch_ollama_models("http://localhost:11434") == []

    def test_malformed_json_returns_empty(self) -> None:
        """Non-JSON response body is swallowed (JSONDecodeError ⊂ ValueError)."""
        with patch("archon_search.install.urllib.request.urlopen", return_value=_FakeResp(b"not json")):
            assert _fetch_ollama_models("http://localhost:11434") == []

    def test_empty_model_list_returns_empty(self) -> None:
        """Ollama running with zero models installed returns []."""
        with patch("archon_search.install.urllib.request.urlopen", return_value=_tags_response()):
            assert _fetch_ollama_models("http://localhost:11434") == []

    def test_missing_models_key_returns_empty(self) -> None:
        """A response without a 'models' key returns []."""
        with patch("archon_search.install.urllib.request.urlopen", return_value=_FakeResp(b'{"other": 1}')):
            assert _fetch_ollama_models("http://localhost:11434") == []

    def test_skips_entries_without_name(self) -> None:
        """Malformed entries (no 'name') are dropped, not crashed on."""
        body = _FakeResp(json.dumps({"models": [{"name": "llama3.2"}, {"size": 5}, {}]}).encode())
        with patch("archon_search.install.urllib.request.urlopen", return_value=body):
            assert _fetch_ollama_models("http://localhost:11434") == ["llama3.2"]

    def test_non_string_name_does_not_crash(self) -> None:
        """A non-string 'name' is filtered, not raised on — sorted() must not see mixed types.

        Without the isinstance(str) guard, sorted([123, "llama3.2"]) raises TypeError,
        which would escape _fetch_ollama_models and crash the wizard (C1-I-43).
        """
        body = _FakeResp(json.dumps({"models": [{"name": "llama3.2"}, {"name": 123}]}).encode())
        with patch("archon_search.install.urllib.request.urlopen", return_value=body):
            assert _fetch_ollama_models("http://localhost:11434") == ["llama3.2"]

    def test_uses_named_timeout_constant(self) -> None:
        """The fetch passes the module timeout constant to urlopen (C1-I-42)."""
        from archon_search.install import _OLLAMA_FETCH_TIMEOUT_SECONDS  # noqa: PLC0415

        with patch("archon_search.install.urllib.request.urlopen", return_value=_tags_response("m")) as mock_urlopen:
            _fetch_ollama_models("http://localhost:11434")
        assert mock_urlopen.call_args.kwargs["timeout"] == _OLLAMA_FETCH_TIMEOUT_SECONDS


def _scheme_strict_urlopen(response_factory):
    """Build a ``urlopen`` stand-in that behaves like the real one w.r.t. URL schemes.

    ``urllib.request.urlopen("localhost:11434/api/tags")`` does NOT silently assume
    HTTP — it raises ``URLError("unknown url type: localhost")``. A mock that answers
    any URL would make a scheme-normalisation test pass vacuously, so this stand-in
    reproduces that failure mode: scheme-less URLs raise, ``http(s)://`` URLs are served.
    """

    def _open(url, *_args, **_kwargs):
        text = str(url)
        # Real urllib lowercases the scheme before dispatching, so "HTTP://" is served.
        if not text.lower().startswith(("http://", "https://")):
            raise urllib.error.URLError(f"unknown url type: {text.split(':', 1)[0]}")
        return response_factory()

    return _open


class TestFetchOllamaModelsSchemeLessBaseUrl:
    """_fetch_ollama_models() must normalise a base URL that omits the scheme.

    The runtime Ollama client accepts a scheme-less host (``ollama.AsyncClient(
    host="localhost:11434")`` normalises to ``http://localhost:11434``), so an
    operator who types ``localhost:11434`` at the wizard prompt has given a value
    that works at runtime. The wizard, however, concatenates the answer straight
    onto ``/api/tags`` and hands it to ``urllib``, which rejects it with
    ``unknown url type`` — swallowed by the blanket ``except Exception`` into an
    empty list, so the wizard prints the misleading "No models available from
    localhost:11434. Ollama may not be running there…" while Ollama IS running.
    """

    def test_scheme_less_hostname_base_url_is_normalised(self) -> None:
        base = "localhost:11434"
        with patch(
            "archon_search.install.urllib.request.urlopen",
            side_effect=_scheme_strict_urlopen(lambda: _tags_response("mistral", "llama3.2")),
        ) as mock_urlopen:
            models = _fetch_ollama_models(base)

        requested = mock_urlopen.call_args.args[0]
        assert models == ["llama3.2", "mistral"], (
            f"scheme-less base URL {base!r} was requested verbatim as {requested!r}; "
            "urllib raises URLError('unknown url type: localhost') for that, the blanket "
            "except swallows it, and the wizard claims 'No models available' although "
            "Ollama is running"
        )
        assert requested == "http://localhost:11434/api/tags"

    def test_scheme_less_ip_base_url_is_normalised(self) -> None:
        base = "127.0.0.1:11434"
        with patch(
            "archon_search.install.urllib.request.urlopen",
            side_effect=_scheme_strict_urlopen(lambda: _tags_response("llama3.2")),
        ) as mock_urlopen:
            models = _fetch_ollama_models(base)

        requested = mock_urlopen.call_args.args[0]
        assert models == ["llama3.2"], (
            f"scheme-less IP base URL {base!r} was requested verbatim as {requested!r}; "
            "urllib raises URLError('unknown url type: 127.0.0.1') for that, so the "
            "wizard falsely reports Ollama as unreachable"
        )
        assert requested == "http://127.0.0.1:11434/api/tags"

    def test_scheme_less_host_without_port_gets_the_ollama_default_port(self) -> None:
        """A bare hostname must probe :11434 — the port the runtime client infers.

        ``ollama.AsyncClient(host="myollama.internal")`` resolves to
        ``http://myollama.internal:11434``. Prefixing only the scheme would send the
        probe to port 80, so the wizard could confirm a model list from one server
        while the running server talks to another.
        """
        base = "myollama.internal"
        with patch(
            "archon_search.install.urllib.request.urlopen",
            side_effect=_scheme_strict_urlopen(lambda: _tags_response("llama3.2")),
        ) as mock_urlopen:
            models = _fetch_ollama_models(base)

        requested = mock_urlopen.call_args.args[0]
        assert models == ["llama3.2"]
        assert requested == "http://myollama.internal:11434/api/tags", (
            f"bare hostname {base!r} was probed as {requested!r}; the runtime Ollama "
            "client would use port 11434, so the wizard must probe the same server"
        )

    def test_scheme_less_ipv6_host_keeps_its_explicit_port(self) -> None:
        base = "[::1]:11434"
        with patch(
            "archon_search.install.urllib.request.urlopen",
            side_effect=_scheme_strict_urlopen(lambda: _tags_response("llama3.2")),
        ) as mock_urlopen:
            _fetch_ollama_models(base)

        assert mock_urlopen.call_args.args[0] == "http://[::1]:11434/api/tags"

    def test_https_base_url_is_left_untouched(self) -> None:
        """An address that already carries a scheme must never be rewritten."""
        with patch(
            "archon_search.install.urllib.request.urlopen",
            side_effect=_scheme_strict_urlopen(lambda: _tags_response("llama3.2")),
        ) as mock_urlopen:
            models = _fetch_ollama_models("https://ollama.example.com")

        assert models == ["llama3.2"]
        assert mock_urlopen.call_args.args[0] == "https://ollama.example.com/api/tags"


class TestNormaliseBaseUrl:
    """Direct unit tests for _normalise_base_url().

    The helper was previously exercised only through the prompt functions, so a
    malformed result was visible in a test only if it happened to change a
    `stored` value. Its output is both probed by the wizard AND written to
    `[hyde]`/`[rag_fusion]`/`[graph]`, so each input shape is pinned here.
    """

    def test_bare_host_gains_scheme_and_port(self) -> None:
        assert _normalise_base_url("localhost", 11434) == "http://localhost:11434"

    def test_host_with_port_gains_only_the_scheme(self) -> None:
        assert _normalise_base_url("localhost:11434", 11434) == "http://localhost:11434"

    def test_url_with_scheme_is_left_alone_apart_from_trailing_slash(self) -> None:
        assert _normalise_base_url("http://localhost:11434/", 11434) == "http://localhost:11434"

    def test_ipv6_literal_is_not_mistaken_for_a_port(self) -> None:
        """The `]`-aware split must not read the address colons as a port separator."""
        assert _normalise_base_url("[::1]", 11434) == "http://[::1]:11434"

    def test_scheme_less_host_with_path_puts_the_port_on_the_authority(self) -> None:
        """The port belongs on the host, not after the path.

        Appending it to the whole string yields `http://myhost/ollama:11434`,
        which the wizard then probes (reporting a false "no models available")
        and stores verbatim, pointing the runtime provider at an unresolvable
        address.
        """
        assert _normalise_base_url("myhost/ollama", 11434) == "http://myhost:11434/ollama"

    def test_scheme_less_host_with_port_and_path_keeps_both(self) -> None:
        assert _normalise_base_url("myhost:8080/api/v1", 11434) == "http://myhost:8080/api/v1"


class TestPromptOllamaModelStoresNormalisedUrl:
    """A scheme-less spelling of the default address must not pin a custom URL.

    ``_reconcile_ollama_base_url`` deletes ``ollama_base_url`` from the config when
    the wizard reports ``""`` — the "still on the default" signal. Comparing the raw
    answer against ``OLLAMA_BASE_URL_DEFAULT`` means an operator who types
    ``localhost:11434`` probes the default server but gets a redundant custom URL
    written to their config, which then stops tracking the default.
    """

    def test_scheme_less_default_address_collapses_to_empty(self) -> None:
        with patch(
            "archon_search.install.urllib.request.urlopen",
            side_effect=_scheme_strict_urlopen(lambda: _tags_response("llama3.2")),
        ):
            with patch("builtins.input", side_effect=["localhost:11434", "1"]):
                stored, model = _prompt_ollama_model("HyDE", OLLAMA_BASE_URL_DEFAULT)

        assert model == "llama3.2"
        assert stored == "", (
            f"typing the default address without a scheme stored {stored!r}; it resolves "
            "to OLLAMA_BASE_URL_DEFAULT, so the key must be omitted from the config"
        )

    def test_custom_address_is_still_stored(self) -> None:
        with patch(
            "archon_search.install.urllib.request.urlopen",
            side_effect=_scheme_strict_urlopen(lambda: _tags_response("llama3.2")),
        ):
            with patch("builtins.input", side_effect=["box:11434", "1"]):
                stored, _model = _prompt_ollama_model("HyDE", OLLAMA_BASE_URL_DEFAULT)

        assert stored == "http://box:11434"


class TestPickOllamaModel:
    """Unit tests for _pick_ollama_model() — numbered menu selection."""

    def test_valid_selection(self) -> None:
        with patch("builtins.input", return_value="2"):
            assert _pick_ollama_model(["a", "b", "c"]) == "b"

    def test_lists_every_model(self, capsys) -> None:
        """All models are printed as one numbered list — no pagination/truncation (brief §long list)."""
        models = [f"m{i}" for i in range(25)]
        with patch("builtins.input", return_value="25"):
            picked = _pick_ollama_model(models)
        out = capsys.readouterr().out
        assert picked == "m24"
        # Every model must appear at its 1-based index — proves no truncation for >20 lists.
        for i, name in enumerate(models, start=1):
            assert f"{i}. {name}" in out

    def test_out_of_range_then_valid(self) -> None:
        with patch("builtins.input", side_effect=["9", "1"]):
            assert _pick_ollama_model(["a", "b"]) == "a"

    def test_non_numeric_then_valid(self) -> None:
        with patch("builtins.input", side_effect=["abc", "2"]):
            assert _pick_ollama_model(["a", "b"]) == "b"

    def test_two_invalid_returns_empty(self) -> None:
        with patch("builtins.input", side_effect=["0", "99"]):
            assert _pick_ollama_model(["a", "b"]) == ""

    def test_eof_returns_empty(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            assert _pick_ollama_model(["a", "b"]) == ""

    def test_invalid_then_eof_returns_empty(self) -> None:
        """First entry invalid, EOF on the retry → "" (C1-I-44)."""
        with patch("builtins.input", side_effect=["nope", EOFError]):
            assert _pick_ollama_model(["a", "b"]) == ""


class TestPromptModelFreetext:
    """Unit tests for _prompt_model_freetext() — OpenAI/manual model entry."""

    def test_first_attempt_accepted(self) -> None:
        with patch("builtins.input", return_value="gpt-4o"):
            assert _prompt_model_freetext("HyDE") == "gpt-4o"

    def test_empty_then_valid(self) -> None:
        with patch("builtins.input", side_effect=["", "gpt-4o"]):
            assert _prompt_model_freetext("HyDE") == "gpt-4o"

    def test_two_empty_returns_empty(self) -> None:
        with patch("builtins.input", side_effect=["", ""]):
            assert _prompt_model_freetext("HyDE") == ""

    def test_eof_returns_empty(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            assert _prompt_model_freetext("HyDE") == ""


class TestPromptOllamaModel:
    """Unit tests for _prompt_ollama_model() — base-URL prompt + picker/fallback."""

    def test_default_url_and_picker(self) -> None:
        """Empty base-URL input keeps the default; picker returns the chosen model."""
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]) as mock_fetch:
            with patch("builtins.input", side_effect=["", "1"]):
                base_url, model = _prompt_ollama_model("HyDE", OLLAMA_BASE_URL_DEFAULT)
        mock_fetch.assert_called_once_with(OLLAMA_BASE_URL_DEFAULT)
        assert base_url == ""  # resolves to the built-in default → stored empty
        assert model == "llama3.2"

    def test_custom_url_is_stored_and_fetched(self) -> None:
        """A typed custom URL is fetched from and stored (survives config regen)."""
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]) as mock_fetch:
            with patch("builtins.input", side_effect=["http://box:11434", "1"]):
                base_url, model = _prompt_ollama_model("HyDE", OLLAMA_BASE_URL_DEFAULT)
        mock_fetch.assert_called_once_with("http://box:11434")
        assert base_url == "http://box:11434"
        assert model == "llama3.2"

    def test_enter_keeps_saved_custom_default(self) -> None:
        """On re-run, pressing Enter keeps the config-saved custom URL (stored + fetched)."""
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]) as mock_fetch:
            with patch("builtins.input", side_effect=["", "1"]):
                base_url, model = _prompt_ollama_model("HyDE", "http://box:11434")
        mock_fetch.assert_called_once_with("http://box:11434")
        assert base_url == "http://box:11434"
        assert model == "llama3.2"

    def test_unreachable_falls_back_to_freetext(self, capsys) -> None:
        """Empty model list → honest message + free-text fallback."""
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=[]):
            with patch("builtins.input", side_effect=["", "mymodel"]):
                base_url, model = _prompt_ollama_model("HyDE", OLLAMA_BASE_URL_DEFAULT)
        out = capsys.readouterr().out
        assert base_url == ""
        assert model == "mymodel"
        assert "ollama pull" in out
        assert OLLAMA_BASE_URL_DEFAULT in out

    def test_eof_on_base_url_uses_default(self) -> None:
        """EOF on the base-URL prompt resolves to the default and proceeds to fetch."""
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]) as mock_fetch:
            with patch("builtins.input", side_effect=[EOFError, "1"]):
                base_url, model = _prompt_ollama_model("HyDE", OLLAMA_BASE_URL_DEFAULT)
        mock_fetch.assert_called_once_with(OLLAMA_BASE_URL_DEFAULT)
        assert base_url == ""
        assert model == "llama3.2"


class TestOllamaPickerIntegration:
    """Integration tests through _prompt_optional_features() — full prompt flow."""

    _profile = ENGLISH_PROFILES["minimal"]  # reranker is not None

    def _run(self, inputs: list[object], fetch_return, **kwargs) -> WizardFeatures:
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=fetch_return):
            with patch("builtins.input", side_effect=inputs):
                return _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                    **kwargs,
                )

    def test_both_features_pick_independently(self) -> None:
        """HyDE and RAG Fusion each get their own picker; selections are independent."""
        # enable=y; hyde: ollama, base "", pick "1"; rag: ollama, base "", pick "2"; n=decline graph.
        features = self._run(
            ["y", "ollama", "", "1", "ollama", "", "2", "n"],
            fetch_return=["llama3.2", "mistral"],
        )
        assert features.hyde_model == "llama3.2"
        assert features.rag_fusion_model == "mistral"

    def test_unreachable_server_falls_back_for_both(self) -> None:
        """Unreachable Ollama → free-text fallback for both features."""
        features = self._run(
            ["y", "ollama", "", "hmodel", "ollama", "", "rmodel", "n"],
            fetch_return=[],
        )
        assert features.hyde_model == "hmodel"
        assert features.rag_fusion_model == "rmodel"

    def test_custom_base_url_threaded_from_config(self) -> None:
        """A config-saved base URL pre-fills the prompt; Enter keeps it for both features."""
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]) as mock_fetch:
            with patch("builtins.input", side_effect=["y", "ollama", "", "1", "ollama", "", "1", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                    hyde_ollama_base_url_default="http://hyde-box:11434",
                    rag_fusion_ollama_base_url_default="http://rag-box:11434",
                )
        fetched = [c.args[0] for c in mock_fetch.call_args_list]
        assert fetched == ["http://hyde-box:11434", "http://rag-box:11434"]
        assert features.hyde_ollama_base_url == "http://hyde-box:11434"
        assert features.rag_fusion_ollama_base_url == "http://rag-box:11434"


class TestOllamaPickerE2E:
    """End-to-end: wizard prompt → TOML → loaded SearchConfig."""

    _profile = ENGLISH_PROFILES["minimal"]

    def test_picked_model_survives_to_loaded_config(self, tmp_path) -> None:
        """A model picked in the wizard reaches a real SearchConfig via the written TOML."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2", "mistral"]):
            with patch("builtins.input", side_effect=["y", "ollama", "", "2", "ollama", "", "1", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )

        doc = tomlkit.parse(_default_toml())
        _apply_wizard_features_to_toml(doc, features)
        cfg_path = tmp_path / "archon-search.toml"
        cfg_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        cfg = load_config(cfg_path)
        assert cfg.hyde.enabled is True
        assert cfg.hyde.provider == "ollama"
        assert cfg.hyde.model == "mistral"  # index 2 of sorted [llama3.2, mistral]
        assert cfg.rag_fusion.enabled is True
        assert cfg.rag_fusion.provider == "ollama"
        assert cfg.rag_fusion.model == "llama3.2"  # index 1

    def test_custom_url_survives_to_loaded_config(self, tmp_path) -> None:
        """A custom base URL typed in the wizard reaches the loaded SearchConfig."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]):
            with patch("builtins.input", side_effect=["y", "ollama", "http://box:11434", "1", "anthropic", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )

        doc = tomlkit.parse(_default_toml())
        _apply_wizard_features_to_toml(doc, features)
        cfg_path = tmp_path / "archon-search.toml"
        cfg_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        cfg = load_config(cfg_path)
        assert cfg.hyde.provider == "ollama"
        assert cfg.hyde.model == "llama3.2"
        assert cfg.hyde.ollama_base_url == "http://box:11434"

    def test_rag_fusion_custom_url_survives_to_loaded_config(self, tmp_path) -> None:
        """A custom RAG Fusion base URL reaches the loaded config (C1-I-40: covers the rag_fusion write branch)."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        # enable=y; hyde=anthropic (no ollama prompt); rag=ollama, custom URL, pick "1".
        with patch("archon_search.install.wizard._fetch_ollama_models", return_value=["llama3.2"]):
            with patch("builtins.input", side_effect=["y", "anthropic", "ollama", "http://rag:11434", "1", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )

        doc = tomlkit.parse(_default_toml())
        _apply_wizard_features_to_toml(doc, features)
        cfg_path = tmp_path / "archon-search.toml"
        cfg_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        cfg = load_config(cfg_path)
        assert cfg.rag_fusion.provider == "ollama"
        assert cfg.rag_fusion.model == "llama3.2"
        assert cfg.rag_fusion.ollama_base_url == "http://rag:11434"


@pytest.fixture
def isolated_gguf_env(monkeypatch, tmp_path):
    """Point every GGUF cache root at an empty directory.

    Without this the tier-2 directory scan would see whatever the developer's or
    CI runner's real llama.cpp cache happens to hold, making any "returns []"
    assertion environment-dependent. Returns the fake home directory.

    The ``functools.cache`` on ``_fetch_llama_cpp_models`` is cleared by the
    autouse ``_clear_llama_cpp_model_cache`` fixture, which covers every test in
    this module rather than only the ones requesting this one.
    """
    for var in ("LLAMA_CACHE", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _write_gguf(root: Path, *relative_paths: str) -> None:
    """Create empty .gguf files at ``relative_paths`` under ``root``."""
    for rel in relative_paths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")


def _cache_list_run(stdout: str, returncode: int = 0):
    """Build a ``subprocess.run`` stand-in replaying ``stdout``, honouring the text kwargs."""

    def _run(cmd, *_args, **kwargs):
        decoded = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
        return subprocess.CompletedProcess(cmd, returncode, stdout if decoded else stdout.encode(), b"")

    return _run


@pytest.mark.usefixtures("isolated_gguf_env")
class TestFetchLlamaCppModels:
    """Unit tests for _fetch_llama_cpp_models() — `llama cli -cl` probe, graceful failure.

    The model list comes from the local llama.cpp cache, never from a running
    llama-server (see TestLlamaCppModelsComeFromLocalCache for that guard).

    Every ``== []`` assertion here means "both tiers empty": tier 1 is forced
    empty by the stub, and ``isolated_gguf_env`` guarantees the tier-2 directory
    scan finds nothing. Without that fixture these would depend on whatever GGUF
    files the machine running the suite happens to hold.
    """

    _TWO_MODEL_STDOUT = (
        "number of models in cache: 2\n"
        "   1. squ11z1/gpt-oss-nano:Q4_K_M\n"
        "   2. ggml-org/Qwen3-1.7B-GGUF:Q8_0\n"
    )

    def test_parses_numbered_cache_entries(self) -> None:
        """Numbered lines yield model names, in order; the header line is not one of them."""
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=_cache_list_run(self._TWO_MODEL_STDOUT)):
                models = _fetch_llama_cpp_models()
        assert models == ["squ11z1/gpt-oss-nano:Q4_K_M", "ggml-org/Qwen3-1.7B-GGUF:Q8_0"]

    def test_invokes_llama_cache_list_as_argv_without_shell(self) -> None:
        """The probe runs the resolved binary with a fixed argv, a timeout, and check=False."""
        recorded: dict[str, object] = {}

        def _run(cmd, *_args, **kwargs):
            recorded["cmd"] = cmd
            recorded["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, self._TWO_MODEL_STDOUT, "")

        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=_run):
                _fetch_llama_cpp_models()

        assert recorded["cmd"] == ["/opt/homebrew/bin/llama", "cli", "-cl"]
        kwargs = recorded["kwargs"]
        assert kwargs.get("shell") is not True, "the probe must never build a shell command string"
        assert kwargs["timeout"] == _LLAMA_CPP_CACHE_LIST_TIMEOUT_SECONDS
        assert kwargs["check"] is False
        assert kwargs["text"] is True
        assert kwargs["capture_output"] is True

    def test_missing_binary_returns_empty_without_running_anything(self) -> None:
        """No `llama` on PATH must short-circuit before any process is spawned.

        Asserted with a call recorder, not a raising stub: ``subprocess.run`` is
        called inside ``_list_cached_models_via_cli``'s blanket ``except
        Exception``, so an ``AssertionError`` side effect would be swallowed and
        the test would pass whether or not the guard exists.
        """
        calls: list[object] = []

        def _record(cmd, *_args, **_kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("shutil.which", return_value=None):
            with patch("subprocess.run", side_effect=_record):
                assert _fetch_llama_cpp_models() == []

        assert calls == [], f"nothing may be executed when the binary is absent, but ran: {calls}"

    def test_non_zero_exit_returns_empty(self) -> None:
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=_cache_list_run(self._TWO_MODEL_STDOUT, returncode=1)):
                assert _fetch_llama_cpp_models() == []

    def test_timeout_returns_empty(self) -> None:
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="llama", timeout=5)):
                assert _fetch_llama_cpp_models() == []

    def test_os_error_returns_empty(self) -> None:
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=OSError("exec format error")):
                assert _fetch_llama_cpp_models() == []

    def test_unparsable_output_returns_empty(self) -> None:
        """Output with no numbered entries (e.g. a usage banner) yields [], not a crash."""
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=_cache_list_run("usage: llama cli [options]\n")):
                assert _fetch_llama_cpp_models() == []

    def test_empty_output_returns_empty(self) -> None:
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=_cache_list_run("")):
                assert _fetch_llama_cpp_models() == []


@pytest.mark.usefixtures("isolated_gguf_env")
class TestFetchLlamaCppModelsGgufDirFallback:
    """Tier 2 of _fetch_llama_cpp_models() — scanning known GGUF cache directories.

    When ``llama cli -cl`` yields nothing (binary absent, or an empty cache) the
    wizard scans the directories llama.cpp and huggingface_hub download into, so
    an operator who has GGUF files but no ``llama`` binary on PATH still gets a
    picker instead of free-text entry. ``shutil.which`` returns ``None``
    throughout so tier 1 is always the empty branch.
    """

    def test_llama_cache_env_dir_is_scanned(self, monkeypatch, tmp_path) -> None:
        """$LLAMA_CACHE is scanned recursively; entries are paths relative to that root."""
        root = tmp_path / "llama-cache"
        _write_gguf(root, "squ11z1/gpt-oss-nano/Q4_K_M.gguf", "flat.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(root))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["flat.gguf", "squ11z1/gpt-oss-nano/Q4_K_M.gguf"]

    def test_hf_home_hub_subdir_is_scanned(self, monkeypatch, tmp_path) -> None:
        """$HF_HOME is scanned at its `hub/` subdirectory, not at its root."""
        hf_home = tmp_path / "hf"
        _write_gguf(hf_home / "hub", "models--org--m/snapshots/abc/m.Q4_K_M.gguf")
        _write_gguf(hf_home, "outside-hub.gguf")
        monkeypatch.setenv("HF_HOME", str(hf_home))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["models--org--m/snapshots/abc/m.Q4_K_M.gguf"]

    def test_huggingface_default_cache_is_scanned_when_hf_home_unset(self, isolated_gguf_env) -> None:
        """huggingface_hub's default ~/.cache/huggingface/hub is scanned with no env vars set.

        This is the common case for anyone who ran `huggingface-cli download` —
        HF_HOME is normally unset, so scanning only $HF_HOME/hub would miss it.
        """
        _write_gguf(isolated_gguf_env / ".cache" / "huggingface" / "hub", "models--org--m/m.gguf")

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["models--org--m/m.gguf"]

    def test_hf_home_overrides_the_huggingface_default_cache(self, monkeypatch, tmp_path, isolated_gguf_env) -> None:
        """$HF_HOME relocates the huggingface cache — the default is then not scanned."""
        _write_gguf(isolated_gguf_env / ".cache" / "huggingface" / "hub", "default.gguf")
        _write_gguf(tmp_path / "hf" / "hub", "relocated.gguf")
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["relocated.gguf"]

    def test_xdg_cache_home_llama_cpp_subdir_is_scanned(self, monkeypatch, tmp_path) -> None:
        """$XDG_CACHE_HOME is scanned at its `llama.cpp/` subdirectory."""
        xdg = tmp_path / "xdg"
        _write_gguf(xdg / "llama.cpp", "org/model.gguf")
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["org/model.gguf"]

    def test_macos_default_dir_is_scanned_when_no_env_vars_set(self, monkeypatch, isolated_gguf_env) -> None:
        """With no env vars set, macOS falls back to ~/Library/Caches/llama.cpp."""
        _write_gguf(isolated_gguf_env / "Library" / "Caches" / "llama.cpp", "org/mac.gguf")
        _write_gguf(isolated_gguf_env / ".cache" / "llama.cpp", "org/linux.gguf")
        monkeypatch.setattr(sys, "platform", "darwin")

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["org/mac.gguf"]

    def test_linux_default_dir_is_scanned_when_no_env_vars_set(self, monkeypatch, isolated_gguf_env) -> None:
        """With no env vars set, non-macOS platforms fall back to ~/.cache/llama.cpp."""
        _write_gguf(isolated_gguf_env / "Library" / "Caches" / "llama.cpp", "org/mac.gguf")
        _write_gguf(isolated_gguf_env / ".cache" / "llama.cpp", "org/linux.gguf")
        monkeypatch.setattr(sys, "platform", "linux")

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["org/linux.gguf"]

    def test_multiple_dirs_are_unioned_and_sorted(self, monkeypatch, tmp_path) -> None:
        """Results from several roots merge into one globally sorted list.

        ``zzz.gguf`` sits under the highest-priority root and ``aaa.gguf`` under a
        later one, so returning them in root order rather than sorted order fails.
        """
        _write_gguf(tmp_path / "llama-cache", "zzz.gguf")
        _write_gguf(tmp_path / "hf" / "hub", "aaa.gguf")
        _write_gguf(tmp_path / "xdg" / "llama.cpp", "mmm.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(tmp_path / "llama-cache"))
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["aaa.gguf", "mmm.gguf", "zzz.gguf"]

    def test_one_file_reachable_through_two_roots_is_listed_once(self, monkeypatch, tmp_path) -> None:
        """A single file reachable under two roots is listed once, under the first root's name.

        ``$XDG_CACHE_HOME/llama.cpp`` is a symlink to a subdirectory of
        ``$LLAMA_CACHE``, so the same file is reachable as ``nested/m.gguf`` and as
        ``m.gguf``. Only resolving each hit to its real path collapses the two —
        comparing the globbed paths alone offers the same file twice under two names.
        """
        real = tmp_path / "real-cache"
        _write_gguf(real, "nested/m.gguf")
        (tmp_path / "xdg").mkdir()
        (tmp_path / "xdg" / "llama.cpp").symlink_to(real / "nested", target_is_directory=True)
        monkeypatch.setenv("LLAMA_CACHE", str(real))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["nested/m.gguf"]

    def test_earliest_root_decides_the_name_of_a_shared_file(self, monkeypatch, tmp_path) -> None:
        """When a file sits under two nested roots, the higher-priority root names it.

        ``$LLAMA_CACHE`` points deeper than ``$HF_HOME/hub``, so the same file is
        reachable as ``m.gguf`` or as ``nested/m.gguf``; priority order picks the former.
        """
        hub = tmp_path / "hf" / "hub"
        _write_gguf(hub, "nested/m.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(hub / "nested"))
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["m.gguf"]

    def test_distinct_files_sharing_a_name_are_both_listed_with_distinct_labels(self, monkeypatch, tmp_path) -> None:
        """F9: two DIFFERENT files that would render the same relative name must both stay selectable.

        Collapsing them to one label (the old behaviour) made one of the two models
        permanently unreachable from the picker; each colliding label must instead
        be qualified so both remain distinguishable menu entries.
        """
        _write_gguf(tmp_path / "llama-cache", "m.gguf")
        _write_gguf(tmp_path / "hf" / "hub", "m.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(tmp_path / "llama-cache"))
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

        with patch("shutil.which", return_value=None):
            models = _fetch_llama_cpp_models()

        assert len(models) == 2, f"both distinct files must be offered, got {models!r}"
        assert len(set(models)) == 2, "the two colliding labels must be disambiguated, not identical"
        assert all("m.gguf" in m for m in models)

    def test_root_qualifier_is_menu_only_and_never_becomes_the_model_name(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        """The ` [cache-root]` disambiguator must not survive into the stored model name.

        The suffix exists to tell the operator which cache a colliding file came
        from. Returning it verbatim would write
        `m.gguf [/tmp/.../llama-cache]` into `[hyde].model`, which no
        llama-server can resolve.
        """
        _write_gguf(tmp_path / "llama-cache", "m.gguf")
        _write_gguf(tmp_path / "hf" / "hub", "m.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(tmp_path / "llama-cache"))
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

        with patch("shutil.which", return_value=None):
            models = _fetch_llama_cpp_models()
        assert any(" [" in m for m in models), (
            "precondition: this fixture must produce a root-qualified label"
        )

        with patch("builtins.input", side_effect=["1"]):
            chosen = _pick_llama_cpp_model(models)

        assert chosen == "m.gguf", f"the picker returned a menu label, not a model name: {chosen!r}"
        assert " [" not in chosen
        assert models[0] in capsys.readouterr().out, "the menu must still show the qualified label"

    def test_cli_cache_list_wins_over_dir_scan(self, monkeypatch, tmp_path) -> None:
        """Tier 1 short-circuits: a non-empty `llama cli -cl` list suppresses the dir scan."""
        _write_gguf(tmp_path / "llama-cache", "never-listed.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(tmp_path / "llama-cache"))
        stdout = "number of models in cache: 1\n   1. squ11z1/gpt-oss-nano:Q4_K_M\n"

        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=_cache_list_run(stdout)):
                assert _fetch_llama_cpp_models() == ["squ11z1/gpt-oss-nano:Q4_K_M"]

    def test_dir_scan_is_not_even_run_when_the_cli_lists_models(self, monkeypatch, tmp_path) -> None:
        """Tier 1 short-circuits execution, not just the result.

        Evaluating the scan eagerly and discarding it would still return the right
        models while walking every cache root on each wizard run.
        """
        monkeypatch.setenv("LLAMA_CACHE", str(tmp_path))
        stdout = "number of models in cache: 1\n   1. squ11z1/gpt-oss-nano:Q4_K_M\n"

        with patch("archon_search.install.wizard._scan_gguf_cache_dirs") as mock_scan:
            with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
                with patch("subprocess.run", side_effect=_cache_list_run(stdout)):
                    assert _fetch_llama_cpp_models() == ["squ11z1/gpt-oss-nano:Q4_K_M"]

        mock_scan.assert_not_called()

    def test_both_tiers_empty_returns_empty(self, monkeypatch, tmp_path) -> None:
        """No binary and no GGUF file anywhere yields [] — the free-text fallback path."""
        empty = tmp_path / "llama-cache"
        empty.mkdir()
        (empty / "not-a-model.txt").write_text("x", encoding="utf-8")
        monkeypatch.setenv("LLAMA_CACHE", str(empty))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == []

    def test_missing_dirs_are_skipped_without_raising(self, monkeypatch, tmp_path) -> None:
        """A configured root that does not exist yields no entries and no exception."""
        monkeypatch.setenv("LLAMA_CACHE", str(tmp_path / "does-not-exist"))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == []

    def test_directories_named_like_models_are_not_listed(self, monkeypatch, tmp_path) -> None:
        """A directory whose name ends in .gguf is not a selectable model."""
        root = tmp_path / "llama-cache"
        (root / "decoy.gguf").mkdir(parents=True)
        _write_gguf(root, "real.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(root))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["real.gguf"]

    def test_unresolvable_home_yields_roots_found_so_far_instead_of_raising(self, monkeypatch, tmp_path) -> None:
        """F8: Path.home() raising (HOME unset in a container) must not discard already-collected roots.

        `_fetch_llama_cpp_models` documents that it never raises; the caller
        relies on `[]` (only) to reach the free-text prompt — an unresolvable
        home directory must not throw away results already found under
        `$LLAMA_CACHE` or the huggingface cache, both resolved before the
        `Path.home()`-dependent default root is computed.
        """
        _write_gguf(tmp_path / "llama-cache", "env.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(tmp_path / "llama-cache"))

        with patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory")):
            with patch("shutil.which", return_value=None):
                assert _fetch_llama_cpp_models() == ["env.gguf"]

    def test_unwalkable_root_does_not_suppress_a_later_root(self, monkeypatch, tmp_path) -> None:
        """F8: one root failing to glob (e.g. a permission error) must not cancel the remaining roots."""
        bad_root = tmp_path / "bad-root"
        good_root = tmp_path / "hf" / "hub"
        _write_gguf(good_root, "good.gguf")
        monkeypatch.setenv("LLAMA_CACHE", str(bad_root))
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

        real_glob = Path.glob

        def _flaky_glob(self: Path, pattern: str):
            if self == bad_root:
                raise PermissionError("denied")
            return real_glob(self, pattern)

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "glob", _flaky_glob):
                assert _fetch_llama_cpp_models() == ["good.gguf"]

    def test_hf_hub_cache_names_the_hub_directory_outright(self, monkeypatch, tmp_path) -> None:
        """$HF_HUB_CACHE points at the hub dir itself, so no `hub/` suffix is appended.

        It also outranks $HF_HOME, mirroring huggingface_hub's own resolution order.
        """
        _write_gguf(tmp_path / "hub-cache", "direct.gguf")
        _write_gguf(tmp_path / "hf" / "hub", "via-hf-home.gguf")
        monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub-cache"))
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["direct.gguf"]

    def test_legacy_huggingface_hub_cache_is_honoured(self, monkeypatch, tmp_path) -> None:
        """The legacy $HUGGINGFACE_HUB_CACHE spelling still names the hub directory."""
        _write_gguf(tmp_path / "hub-cache", "legacy.gguf")
        monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub-cache"))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["legacy.gguf"]

    def test_xdg_cache_home_relocates_the_huggingface_default(self, monkeypatch, tmp_path, isolated_gguf_env) -> None:
        """With $HF_HOME unset, the huggingface cache follows $XDG_CACHE_HOME.

        huggingface_hub derives HF_HOME as `$XDG_CACHE_HOME/huggingface`, so
        scanning `~/.cache/huggingface/hub` unconditionally would miss it on a
        standard Linux setup that sets XDG_CACHE_HOME.
        """
        _write_gguf(isolated_gguf_env / ".cache" / "huggingface" / "hub", "under-home.gguf")
        _write_gguf(tmp_path / "xdg" / "huggingface" / "hub", "under-xdg.gguf")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["under-xdg.gguf"]

    def test_tilde_is_expanded_in_every_env_var_root(self, monkeypatch, isolated_gguf_env) -> None:
        """Each root env var accepts `~/...` — a literal "~" directory is never scanned."""
        _write_gguf(isolated_gguf_env / "llama", "a.gguf")
        _write_gguf(isolated_gguf_env / "hub", "b.gguf")
        _write_gguf(isolated_gguf_env / "xdg" / "llama.cpp", "c.gguf")
        monkeypatch.setenv("LLAMA_CACHE", "~/llama")
        monkeypatch.setenv("HF_HUB_CACHE", "~/hub")
        monkeypatch.setenv("XDG_CACHE_HOME", "~/xdg")

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == ["a.gguf", "b.gguf", "c.gguf"]

    def test_empty_env_vars_never_scan_a_relative_path(self, monkeypatch, tmp_path) -> None:
        """Every root env var set to "" is treated as unset, not as a relative path.

        `Path("") / "hub"` is the relative path `hub`, so a stray `hub/` or
        `llama.cpp/` directory in the working directory would otherwise be scanned.
        """
        _write_gguf(tmp_path, "cwd.gguf")
        _write_gguf(tmp_path / "hub", "hub.gguf")
        _write_gguf(tmp_path / "llama.cpp", "xdg.gguf")
        monkeypatch.chdir(tmp_path)
        for var in ("LLAMA_CACHE", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
            monkeypatch.setenv(var, "")

        with patch("shutil.which", return_value=None):
            assert _fetch_llama_cpp_models() == []


@pytest.mark.usefixtures("isolated_gguf_env")
class TestLlamaCppModelsComeFromLocalCache:
    """The llama.cpp model list must come from the local cache, not a running llama-server.

    Regression guard: the wizard used to fetch ``{base_url}/v1/models`` over HTTP,
    which surfaces whatever ad-hoc path/model a running llama-server happens to
    have loaded (e.g. a ``/private/tmp/...`` scratchpad path) instead of the
    operator's locally available models. The correct source is the model cache
    that ``llama cli -cl`` (``--cache-list``) reports.

    ``subprocess.run`` and ``shutil.which`` are stubbed so the tests never depend
    on a real ``llama`` binary or on any real model being cached; ``urlopen`` is
    stubbed to fail and its call count is asserted to be zero so an HTTP-backed
    implementation can never satisfy these tests.
    """

    # Verbatim `llama cli -cl` output for a single cached model.
    _CACHE_LIST_STDOUT = "number of models in cache: 1\n   1. squ11z1/gpt-oss-nano:Q4_K_M\n"
    _CACHE_LIST_EMPTY_STDOUT = "number of models in cache: 0\n"

    @staticmethod
    def _fake_run(recorded: list, stdout: str):
        """Return a ``subprocess.run`` stand-in that records argv and replays ``stdout``."""

        def _run(cmd, *_args, **kwargs):
            recorded.append(cmd)
            decoded = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
            return subprocess.CompletedProcess(cmd, 0, stdout if decoded else stdout.encode(), b"")

        return _run

    def _no_http_allowed(self):
        """Patch urlopen so any HTTP attempt is recorded (and fails) rather than served."""
        return patch(
            "archon_search.install.urllib.request.urlopen",
            side_effect=urllib.error.URLError("HTTP must not be used to list llama.cpp models"),
        )

    def test_lists_locally_cached_models_not_llama_server_models(self) -> None:
        """_fetch_llama_cpp_models() returns the models `llama cli -cl` reports, with no HTTP call."""
        recorded: list = []
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=self._fake_run(recorded, self._CACHE_LIST_STDOUT)):
                with self._no_http_allowed() as mock_urlopen:
                    models = _fetch_llama_cpp_models()

        assert mock_urlopen.call_count == 0, (
            "the wizard must list locally cached llama.cpp models via `llama cli -cl`, "
            f"but it made {mock_urlopen.call_count} HTTP call(s) to a running llama-server"
        )
        assert models == ["squ11z1/gpt-oss-nano:Q4_K_M"]
        assert recorded[0] == ["/opt/homebrew/bin/llama", "cli", "-cl"]

    def test_empty_cache_returns_empty_without_http_fallback(self) -> None:
        """Zero cached models yields [] — and must not fall back to querying llama-server."""
        recorded: list = []
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=self._fake_run(recorded, self._CACHE_LIST_EMPTY_STDOUT)):
                with self._no_http_allowed() as mock_urlopen:
                    models = _fetch_llama_cpp_models()

        assert mock_urlopen.call_count == 0, (
            "an empty llama.cpp model cache must not trigger an HTTP fallback to llama-server, "
            f"but {mock_urlopen.call_count} HTTP call(s) were made"
        )
        assert models == []
        assert recorded, "expected the wizard to invoke the llama CLI"

    def test_prompt_offers_locally_cached_models(self) -> None:
        """The wizard prompt picks from the local cache list (base URL '' + selection '1')."""
        recorded: list = []
        with patch("shutil.which", return_value="/opt/homebrew/bin/llama"):
            with patch("subprocess.run", side_effect=self._fake_run(recorded, self._CACHE_LIST_STDOUT)):
                with self._no_http_allowed() as mock_urlopen:
                    with patch("builtins.input", side_effect=["", "1"]):
                        _base_url, model = _prompt_llama_cpp_model("HyDE", LLAMA_CPP_BASE_URL_DEFAULT)

        assert mock_urlopen.call_count == 0, (
            "the wizard prompt must offer locally cached llama.cpp models, "
            f"but it made {mock_urlopen.call_count} HTTP call(s) to a running llama-server"
        )
        assert model == "squ11z1/gpt-oss-nano:Q4_K_M"
        assert recorded, "expected the wizard to invoke the llama CLI"


class TestPickLlamaCppModel:
    """Unit tests for _pick_llama_cpp_model() — numbered menu selection."""

    def test_valid_selection(self) -> None:
        with patch("builtins.input", return_value="2"):
            assert _pick_llama_cpp_model(["a", "b", "c"]) == "b"

    def test_out_of_range_then_valid(self) -> None:
        with patch("builtins.input", side_effect=["9", "1"]):
            assert _pick_llama_cpp_model(["a", "b"]) == "a"

    def test_non_numeric_then_valid(self) -> None:
        with patch("builtins.input", side_effect=["abc", "2"]):
            assert _pick_llama_cpp_model(["a", "b"]) == "b"

    def test_two_invalid_returns_empty(self) -> None:
        with patch("builtins.input", side_effect=["0", "99"]):
            assert _pick_llama_cpp_model(["a", "b"]) == ""

    def test_eof_returns_empty(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            assert _pick_llama_cpp_model(["a", "b"]) == ""


class TestPromptLlamaCppModel:
    """Unit tests for _prompt_llama_cpp_model() — base-URL prompt + picker/fallback.

    The base URL is still prompted (``[hyde]``/``[rag_fusion]``/``[graph]`` need
    the server address at runtime) but it no longer decides the model list — that
    is read from the local llama.cpp cache, so the probe takes no URL.
    """

    def test_default_url_and_picker(self) -> None:
        """Empty base-URL input keeps the default; picker returns the chosen model."""
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]) as mock_fetch:
            with patch("builtins.input", side_effect=["", "1"]):
                base_url, model = _prompt_llama_cpp_model("HyDE", LLAMA_CPP_BASE_URL_DEFAULT)
        mock_fetch.assert_called_once_with()
        assert base_url == ""  # resolves to the built-in default → stored empty
        assert model == "m1"

    def test_custom_url_is_stored_but_does_not_drive_the_model_list(self) -> None:
        """A custom base URL is stored for the config; the cache probe is URL-independent."""
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]) as mock_fetch:
            with patch("builtins.input", side_effect=["http://box:8080", "1"]):
                base_url, model = _prompt_llama_cpp_model("HyDE", LLAMA_CPP_BASE_URL_DEFAULT)
        mock_fetch.assert_called_once_with()
        assert base_url == "http://box:8080"
        assert model == "m1"

    def test_empty_cache_falls_back_to_freetext(self, capsys) -> None:
        """Empty model list → honest local-cache message + free-text fallback (S12)."""
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=[]):
            with patch("builtins.input", side_effect=["", "mymodel"]):
                base_url, model = _prompt_llama_cpp_model("HyDE", LLAMA_CPP_BASE_URL_DEFAULT)
        out = capsys.readouterr().out
        assert base_url == ""
        assert model == "mymodel"
        assert "No locally cached llama.cpp models found." in out
        assert "llama download" in out, "the message must say how to populate the cache"
        assert LLAMA_CPP_BASE_URL_DEFAULT not in out, (
            "the fallback message must not blame an unreachable llama-server — the model "
            "list comes from the local cache, not from the server at the base URL"
        )

    def test_eof_on_base_url_uses_default(self) -> None:
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]) as mock_fetch:
            with patch("builtins.input", side_effect=[EOFError, "1"]):
                base_url, model = _prompt_llama_cpp_model("HyDE", LLAMA_CPP_BASE_URL_DEFAULT)
        mock_fetch.assert_called_once_with()
        assert base_url == ""
        assert model == "m1"

    def test_saved_default_is_prefilled_and_preserved_on_enter(self) -> None:
        """F2: a re-run must not wipe a previously saved custom base URL.

        Pressing Enter (empty input) must keep the SAVED address, not fall back
        to the built-in default — the old signature had no way to express this.
        """
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]):
            with patch("builtins.input", side_effect=["", "1"]) as mock_input:
                base_url, model = _prompt_llama_cpp_model("graph enrichment", "http://saved-box:8080")
        assert "http://saved-box:8080" in mock_input.call_args_list[0].args[0], (
            "the base-URL prompt text must show the saved default"
        )
        assert base_url == "http://saved-box:8080"
        assert model == "m1"

    def test_scheme_less_saved_default_is_normalised_before_comparison(self) -> None:
        """F3a: a bare host:port default is normalised the same way _prompt_ollama_model does."""
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]):
            with patch("builtins.input", side_effect=["", "1"]):
                base_url, _model = _prompt_llama_cpp_model("HyDE", "localhost:8080")
        assert base_url == ""  # normalises to the built-in default → stored empty

    def test_trailing_slash_on_default_url_is_stripped(self) -> None:
        """F3b: a trailing slash must not pin a redundant custom URL for the default address."""
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]):
            with patch("builtins.input", side_effect=["", "1"]):
                base_url, _model = _prompt_llama_cpp_model("HyDE", f"{LLAMA_CPP_BASE_URL_DEFAULT}/")
        assert base_url == ""


class TestPromptProviderLlamaCpp:
    """Unit tests for the llama_cpp branch of _prompt_provider() (FE-1)."""

    def test_includes_llama_cpp_in_choices_and_prompt(self) -> None:
        """The choice set offered to ask_choice(), and the prompt text, both include llama_cpp (S4)."""
        captured: dict[str, object] = {}

        def fake_ask_choice(prompt_text: str, valid: set[str], default: str) -> str:
            captured["prompt_text"] = prompt_text
            captured["valid"] = valid
            return "llama_cpp"

        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]):
            with patch("builtins.input", side_effect=["", "1"]):
                provider, model, ollama_url, llama_cpp_url = _prompt_provider(
                    "HyDE", fake_ask_choice, OLLAMA_BASE_URL_DEFAULT
                )

        assert "llama_cpp" in captured["valid"]
        assert "llama_cpp" in captured["prompt_text"]
        assert provider == "llama_cpp"
        assert model == "m1"
        assert ollama_url == ""
        assert llama_cpp_url == ""

    def test_llama_cpp_custom_base_url_returned(self) -> None:
        """A custom llama-server base URL is threaded back as the 4th return value."""
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]):
            with patch("builtins.input", side_effect=["http://box:8080", "1"]):
                provider, model, ollama_url, llama_cpp_url = _prompt_provider(
                    "HyDE", lambda *_a, **_k: "llama_cpp", OLLAMA_BASE_URL_DEFAULT
                )
        assert provider == "llama_cpp"
        assert model == "m1"
        assert ollama_url == ""
        assert llama_cpp_url == "http://box:8080"

    def test_llama_cpp_base_url_default_prefills_the_prompt(self) -> None:
        """F2: a re-run must not wipe a previously saved custom llama-server URL."""
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]):
            with patch("builtins.input", side_effect=["", "1"]) as mock_input:
                provider, model, ollama_url, llama_cpp_url = _prompt_provider(
                    "HyDE", lambda *_a, **_k: "llama_cpp", OLLAMA_BASE_URL_DEFAULT, "http://saved-box:8080"
                )
        assert "http://saved-box:8080" in mock_input.call_args_list[0].args[0]
        assert provider == "llama_cpp"
        assert model == "m1"
        assert ollama_url == ""
        assert llama_cpp_url == "http://saved-box:8080"

    def test_other_providers_return_empty_llama_cpp_url(self) -> None:
        """Non-llama_cpp providers keep the 4th tuple slot empty (no behavior change)."""
        with patch("builtins.input", return_value="gpt-4o-mini"):
            provider, model, ollama_url, llama_cpp_url = _prompt_provider(
                "HyDE", lambda *_a, **_k: "openai", OLLAMA_BASE_URL_DEFAULT
            )
        assert provider == "openai"
        assert model == "gpt-4o-mini"
        assert ollama_url == ""
        assert llama_cpp_url == ""


class TestLlamaCppPickerIntegration:
    """Integration tests through _prompt_optional_features() — full prompt flow (S4, S12)."""

    _profile = ENGLISH_PROFILES["minimal"]  # reranker is not None

    def _run(self, inputs: list[object], fetch_return, **kwargs) -> WizardFeatures:
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=fetch_return):
            with patch("builtins.input", side_effect=inputs):
                return _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                    **kwargs,
                )

    def test_both_features_pick_independently(self) -> None:
        """HyDE and RAG Fusion each get their own llama_cpp picker; selections are independent (S4)."""
        # enable=y; hyde: llama_cpp, base "", pick "1"; rag: llama_cpp, base "", pick "2"; n=decline graph.
        features = self._run(
            ["y", "llama_cpp", "", "1", "llama_cpp", "", "2", "n"],
            fetch_return=["m1", "m2"],
        )
        assert features.hyde_provider == "llama_cpp"
        assert features.hyde_model == "m1"
        assert features.rag_fusion_provider == "llama_cpp"
        assert features.rag_fusion_model == "m2"

    def test_unreachable_server_falls_back_for_both(self) -> None:
        """Unreachable llama-server → free-text fallback for both features (S12)."""
        features = self._run(
            ["y", "llama_cpp", "", "hmodel", "llama_cpp", "", "rmodel", "n"],
            fetch_return=[],
        )
        assert features.hyde_model == "hmodel"
        assert features.rag_fusion_model == "rmodel"

    def test_saved_llama_cpp_base_url_defaults_are_threaded_through(self) -> None:
        """F2: end-to-end — a saved llama-server URL survives a re-run with Enter presses."""
        features = self._run(
            ["y", "llama_cpp", "", "1", "llama_cpp", "", "1", "n"],
            fetch_return=["m1"],
            hyde_llama_cpp_base_url_default="http://hyde-box:8080",
            rag_fusion_llama_cpp_base_url_default="http://rag-box:8080",
        )
        assert features.hyde_llama_cpp_base_url == "http://hyde-box:8080"
        assert features.rag_fusion_llama_cpp_base_url == "http://rag-box:8080"


class TestOllamaBaseUrlReconciliation:
    """M1 (C1-I-20): the re-run merge path must not leak a stale custom base URL.

    _write_profile_config parses the *existing* config on re-run and mutates it in
    place, so a plain conditional write would keep a previously-saved custom URL
    when the operator reverts to the default — silently pointing the AI feature at
    a dead address, the exact quiet-failure class the brief eliminates.
    """

    @staticmethod
    def _saved_section(feature: str, saved_url: str) -> tomlkit.TOMLDocument:
        return tomlkit.parse(
            f'[{feature}]\nenabled = true\nprovider = "ollama"\nmodel = "old"\n'
            f'ollama_base_url = "{saved_url}"\n'
        )

    def test_hyde_revert_to_default_clears_stale_url(self) -> None:
        """Empty stored base URL (revert to default) removes the stale saved key."""
        doc = self._saved_section("hyde", "http://box:11434")
        features = WizardFeatures(
            enable_hyde=True, hyde_provider="ollama", hyde_model="llama3.2", hyde_ollama_base_url=""
        )
        _apply_wizard_features_to_toml(doc, features)
        assert "ollama_base_url" not in doc["hyde"]
        assert doc["hyde"]["model"] == "llama3.2"

    def test_hyde_new_custom_url_replaces_saved(self) -> None:
        """A newly typed custom URL replaces the previously-saved one."""
        doc = self._saved_section("hyde", "http://box:11434")
        features = WizardFeatures(
            enable_hyde=True,
            hyde_provider="ollama",
            hyde_model="llama3.2",
            hyde_ollama_base_url="http://new:11434",
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["hyde"]["ollama_base_url"] == "http://new:11434"

    def test_rag_fusion_revert_to_default_clears_stale_url(self) -> None:
        doc = self._saved_section("rag_fusion", "http://box:11434")
        features = WizardFeatures(
            enable_rag_fusion=True,
            rag_fusion_provider="ollama",
            rag_fusion_model="llama3.2",
            rag_fusion_ollama_base_url="",
        )
        _apply_wizard_features_to_toml(doc, features)
        assert "ollama_base_url" not in doc["rag_fusion"]


class TestPickClaudeModel:
    """Unit tests for _pick_claude_model() — curated alias picker + free-text."""

    def test_pick_by_number(self) -> None:
        with patch("builtins.input", return_value="2"):
            assert _pick_claude_model("HyDE") == "sonnet"

    def test_pick_by_alias_name(self) -> None:
        with patch("builtins.input", return_value="opus"):
            assert _pick_claude_model("HyDE") == "opus"

    def test_pick_free_text_full_id(self) -> None:
        with patch("builtins.input", return_value="claude-haiku-4-5-20251001"):
            assert _pick_claude_model("HyDE") == "claude-haiku-4-5-20251001"

    def test_blank_returns_empty(self) -> None:
        with patch("builtins.input", return_value=""):
            assert _pick_claude_model("HyDE") == ""

    def test_eof_returns_empty(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            assert _pick_claude_model("HyDE") == ""

    def test_out_of_range_number_treated_as_free_text(self) -> None:
        # "9" is not a valid index → treated as a (nonsense) full model ID.
        with patch("builtins.input", return_value="9"):
            assert _pick_claude_model("HyDE") == "9"


class TestCheckClaudeCliPresent:
    """Unit tests for _check_claude_cli_present() — warn-not-block PATH check."""

    def test_present_returns_true_no_warning(self, capsys) -> None:
        with patch("archon_search.install.shutil.which", return_value="/usr/bin/claude"):
            assert _check_claude_cli_present() is True
        assert "WARNING" not in capsys.readouterr().out

    def test_absent_returns_false_and_warns(self, capsys) -> None:
        with patch("archon_search.install.shutil.which", return_value=None):
            assert _check_claude_cli_present() is False
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "claude.ai/code" in out


class TestWizardClaudeCLI:
    """claude_cli provider-selection in the HyDE/RAG Fusion wizard."""

    _profile = ENGLISH_PROFILES["minimal"]

    # --- _apply_wizard_features_to_toml ---

    def test_claude_cli_with_model_writes_provider_and_model(self) -> None:
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        doc = tomlkit.parse(_default_toml())
        features = WizardFeatures(
            enable_hyde=True, hyde_provider="claude_cli", hyde_model="sonnet"
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["hyde"]["provider"] == "claude_cli"
        assert doc["hyde"]["model"] == "sonnet"

    def test_claude_cli_blank_model_omits_model_key(self) -> None:
        """Blank model must NOT be written — an empty model trips config's guard."""
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        doc = tomlkit.parse(_default_toml())
        features = WizardFeatures(
            enable_rag_fusion=True, rag_fusion_provider="claude_cli", rag_fusion_model=""
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["rag_fusion"]["provider"] == "claude_cli"
        # No model key written → config falls back to its default (not "").
        assert "model" not in doc["rag_fusion"]

    # --- _prompt_optional_features flow ---

    def test_wizard_claude_cli_selected(self) -> None:
        """Selecting claude_cli picks a model alias for both features."""
        # enable=y; hyde=claude_cli, model "1" (haiku); rag=claude_cli, model "sonnet".
        with patch("archon_search.install.shutil.which", return_value="/usr/bin/claude"):
            with patch("builtins.input", side_effect=["y", "claude_cli", "1", "claude_cli", "sonnet", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )
        assert features.hyde_provider == "claude_cli"
        assert features.hyde_model == "haiku"
        assert features.rag_fusion_provider == "claude_cli"
        assert features.rag_fusion_model == "sonnet"

    def test_wizard_claude_cli_missing_warns_but_proceeds(self, capsys) -> None:
        """claude not on PATH → warning printed, config still gathered."""
        with patch("archon_search.install.shutil.which", return_value=None):
            with patch("builtins.input", side_effect=["y", "claude_cli", "", "anthropic", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )
        assert features.hyde_provider == "claude_cli"
        assert features.hyde_model == ""  # left blank → use Claude Code default
        assert "WARNING" in capsys.readouterr().out

    # --- E2E: wizard → TOML → loaded SearchConfig ---

    def test_claude_cli_survives_to_loaded_config(self, tmp_path) -> None:
        from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

        with patch("archon_search.install.shutil.which", return_value="/usr/bin/claude"):
            with patch("builtins.input", side_effect=["y", "claude_cli", "opus", "claude_cli", "", "n"]):
                features = _prompt_optional_features(
                    non_interactive=False,
                    profile=self._profile,
                    install_code=False,
                    disable_reranker=False,
                    enable_watch=False,
                    enable_telemetry=False,
                    eager_load=False,
                    routing_strategy="centroid",
                    log_format="text",
                )

        doc = tomlkit.parse(_default_toml())
        _apply_wizard_features_to_toml(doc, features)
        cfg_path = tmp_path / "archon-search.toml"
        cfg_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        # Must load without ConfigError even though RAG Fusion model was left blank.
        cfg = load_config(cfg_path)
        assert cfg.hyde.provider == "claude_cli"
        assert cfg.hyde.model == "opus"
        assert cfg.rag_fusion.provider == "claude_cli"
        # Blank model → config keeps its default (DEFAULT_FAST_MODEL), no crash.
        from archon_search.constants import DEFAULT_FAST_MODEL  # noqa: PLC0415
        assert cfg.rag_fusion.model == DEFAULT_FAST_MODEL
