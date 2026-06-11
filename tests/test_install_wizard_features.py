"""Tests for WizardFeatures dataclass (Task 1.1) and prompt functions (Tasks 1.2+)."""
from __future__ import annotations
from unittest.mock import patch

from archon_search.install import WizardFeatures, _prompt_gpu_confirm, _prompt_multilingual, _prompt_optional_features
from archon_search.platform.types import GpuType
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
        """Interactive mode with reranker profile: exactly 7 input() calls."""
        responses = iter(["n", "n", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses) as mock_input:
            _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_with_reranker,
            )
        assert mock_input.call_count == 7

    def test_prompt_count_6_without_reranker(self, capsys) -> None:
        """Interactive mode without reranker profile: exactly 6 input() calls."""
        responses = iter(["n", "n", "n", "n", "", ""])
        with patch("builtins.input", side_effect=responses) as mock_input:
            _prompt_optional_features(
                non_interactive=False,
                profile=self._profile_no_reranker,
            )
        assert mock_input.call_count == 6


class TestWizardFeaturesC15NewFields:
    """Tests for C15 Task 1.1 — 9 new WizardFeatures fields."""

    def test_wizard_features_new_fields_default_to_none_or_false(self) -> None:
        """All 9 new C15 fields have correct defaults (None or False)."""
        f = WizardFeatures()
        assert f.host is None
        assert f.port is None
        assert f.db_path is None
        assert f.log_level is None
        assert f.log_to_stderr is False
        assert f.top_k is None
        assert f.telemetry_retention_days is None
        assert f.enable_hyde is False
        assert f.enable_rag_fusion is False

    def test_wizard_features_new_fields_accept_values(self) -> None:
        """All 9 new C15 fields accept non-default values."""
        f = WizardFeatures(
            host="0.0.0.0",
            port=9000,
            db_path="~/custom",
            log_level="DEBUG",
            log_to_stderr=True,
            top_k=20,
            telemetry_retention_days=7,
            enable_hyde=True,
            enable_rag_fusion=True,
        )
        assert f.host == "0.0.0.0"
        assert f.port == 9000
        assert f.db_path == "~/custom"
        assert f.log_level == "DEBUG"
        assert f.log_to_stderr is True
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
