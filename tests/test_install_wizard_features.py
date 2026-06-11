"""Tests for WizardFeatures dataclass (Task 1.1) and prompt functions (Tasks 1.2+)."""
from __future__ import annotations
from unittest.mock import patch

from archon_search.install import WizardFeatures, _prompt_multilingual


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
