"""Tests for WizardFeatures dataclass (Task 1.1) and prompt functions (Tasks 1.2+)."""
from __future__ import annotations
from unittest.mock import patch

from archon_search.install import WizardFeatures, _prompt_multilingual, _prompt_optional_features
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES


class TestWizardFeaturesDefaults:
    def test_defaults(self) -> None:
        f = WizardFeatures()
        assert f.install_code_extra is False
        assert f.disable_reranker is False
        assert f.enable_watch is False
        assert f.enable_telemetry is False
        assert f.eager_load_embedders is False
        assert f.routing_strategy == "centroid"
        assert f.log_format == "text"

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


class TestPromptMultilingual:
    def test_flag_true_skips_prompt(self) -> None:
        """flag_value=True returns True without reading any input."""
        with patch("builtins.input", side_effect=AssertionError("should not call input")):
            result = _prompt_multilingual(non_interactive=False, flag_value=True)
        assert result is True

    def test_non_interactive_returns_false(self) -> None:
        """non_interactive=True, flag_value=False returns False without prompt."""
        with patch("builtins.input", side_effect=AssertionError("should not call input")):
            result = _prompt_multilingual(non_interactive=True, flag_value=False)
        assert result is False

    def test_interactive_yes(self) -> None:
        """Input 'y' returns True."""
        with patch("builtins.input", return_value="y"):
            result = _prompt_multilingual(non_interactive=False, flag_value=False)
        assert result is True

    def test_interactive_no(self) -> None:
        """Empty input returns False."""
        with patch("builtins.input", return_value=""):
            result = _prompt_multilingual(non_interactive=False, flag_value=False)
        assert result is False

    def test_interactive_eof(self) -> None:
        """EOFError returns False without raising."""
        with patch("builtins.input", side_effect=EOFError):
            result = _prompt_multilingual(non_interactive=False, flag_value=False)
        assert result is False

    def test_interactive_yes_uppercase(self) -> None:
        """Input 'YES' (case-insensitive) returns True."""
        with patch("builtins.input", return_value="YES"):
            result = _prompt_multilingual(non_interactive=False, flag_value=False)
        assert result is True


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
        # 7 questions: code(y), reranker(y), watch(y), telemetry(y), eager_load(y),
        # routing_strategy("hybrid"), log_format("json")
        responses = iter(["y", "y", "y", "y", "y", "hybrid", "json"])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        assert features.install_code_extra is True
        assert features.disable_reranker is True
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
            )
        assert features.routing_strategy == "hybrid"

    def test_invalid_routing_strategy_twice_uses_default(self) -> None:
        """Two bad routing values → routing_strategy='centroid' (default)."""
        responses = iter(["n", "n", "n", "n", "n", "bad", "worse", ""])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        assert features.routing_strategy == "centroid"

    def test_eof_uses_defaults(self) -> None:
        """EOFError on any question uses defaults for remaining questions; no raise."""
        with patch("builtins.input", side_effect=EOFError):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        assert features == WizardFeatures()

    def test_invalid_log_format_retries(self) -> None:
        """First 'bad' then 'json' → log_format='json'."""
        responses = iter(["n", "n", "n", "n", "n", "", "bad", "json"])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        assert features.log_format == "json"

    def test_invalid_log_format_twice_uses_default(self) -> None:
        """Two bad log_format values → log_format='text' (default)."""
        responses = iter(["n", "n", "n", "n", "n", "", "bad", "worse"])
        with patch("builtins.input", side_effect=responses):
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        assert features.log_format == "text"

    def test_partial_flag_override_interactive_rest(self) -> None:
        """Some flags pre-answered; stdin only called for non-overridden questions."""
        # install_code=True and enable_watch=True are pre-answered (non-None).
        # Remaining interactive questions (with reranker profile):
        #   reranker(n), telemetry(y), eager(n), routing(""), log("")
        responses = iter(["n", "y", "n", "", ""])
        with patch("builtins.input", side_effect=responses) as mock_input:
            features = _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
                install_code=True,
                enable_watch=True,
            )
        assert features.install_code_extra is True
        assert features.enable_watch is True
        assert features.enable_telemetry is True
        assert features.disable_reranker is False
        assert mock_input.call_count == 5  # reranker, telemetry, eager, routing, log

    def test_eof_midway_preserves_prior_answers(self) -> None:
        """EOF after 2 questions answered → first 2 preserved, remaining use defaults."""
        # code=y, reranker=y → then EOFError for all remaining questions
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
        assert features.disable_reranker is True
        # remaining questions use defaults due to EOFError
        assert features.enable_watch is False
        assert features.enable_telemetry is False
        assert features.eager_load_embedders is False
        assert features.routing_strategy == "centroid"
        assert features.log_format == "text"
