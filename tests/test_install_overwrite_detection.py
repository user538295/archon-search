"""Tests for _detect_config_hand_edits — Task C14-5.4."""
from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from archon_search.install import WizardFeatures, _write_profile_config
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES, get_profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_wizard_config(config_path: Path, profile_name: str, multilingual: bool, features: WizardFeatures | None = None) -> None:
    """Write a config file exactly as the wizard would."""
    profile = get_profile(profile_name, multilingual)
    _write_profile_config(config_path, profile, profile_name, multilingual, features=features)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDetectConfigHandEdits:
    def test_detect_no_edits_returns_false(self, tmp_path: Path) -> None:
        """Config written by wizard with balanced profile + defaults → no hand edits detected."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is False

    def test_detect_changed_chunk_size_returns_true(self, tmp_path: Path) -> None:
        """chunk_size modified from profile default → hand edit detected."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        # Manually edit chunk_size
        doc = tomlkit.parse(config_path.read_text())
        doc["database"]["chunk_size"] = 999
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_changed_embedding_model_returns_true(self, tmp_path: Path) -> None:
        """embedding_model changed from profile default → hand edit detected."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False)

        doc = tomlkit.parse(config_path.read_text())
        doc["database"]["embedding_model"] = "some/custom-model"
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "minimal", False)
        assert result is True

    def test_detect_absent_telemetry_not_hand_edit(self, tmp_path: Path) -> None:
        """[telemetry].enabled absent in config → not a hand edit (static default in effect)."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        # Confirm telemetry section is absent
        doc = tomlkit.parse(config_path.read_text())
        assert "telemetry" not in doc or "enabled" not in doc.get("telemetry", {})

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is False

    def test_detect_present_telemetry_true_is_hand_edit(self, tmp_path: Path) -> None:
        """[telemetry].enabled = true present, but WizardFeatures default is False → hand edit."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        if "telemetry" not in doc:
            doc.add("telemetry", tomlkit.table())
        doc["telemetry"]["enabled"] = True
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_unknown_profile_always_warns(self, tmp_path: Path) -> None:
        """prev_profile_name not a recognized profile → returns True (always warn)."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text("[database]\nembedding_model = \"some/model\"\n")

        result = _detect_config_hand_edits(config_path, "unknown", False)
        assert result is True

    def test_detect_profile_switch_no_false_positive(self, tmp_path: Path) -> None:
        """Config has balanced values; detection run with balanced as prev_profile → returns False.

        Switching profiles is not a hand-edit. Detection compares against the
        PREVIOUS stored profile (balanced here), so its own values match → False.
        """
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        # Detection against the same profile (balanced) should find no edits
        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is False

    def test_detect_minimal_profile_no_edits(self, tmp_path: Path) -> None:
        """Config written with minimal profile + defaults → no hand edits."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False)

        result = _detect_config_hand_edits(config_path, "minimal", False)
        assert result is False

    def test_detect_max_profile_no_edits(self, tmp_path: Path) -> None:
        """Config written with max profile + defaults → no hand edits."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "max", False)

        result = _detect_config_hand_edits(config_path, "max", False)
        assert result is False

    def test_detect_multilingual_profile_no_edits(self, tmp_path: Path) -> None:
        """Multilingual balanced config with no edits → False."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", True)

        result = _detect_config_hand_edits(config_path, "balanced", True)
        assert result is False

    def test_detect_changed_reranker_model_returns_true(self, tmp_path: Path) -> None:
        """reranker_model changed from profile default → hand edit detected."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        doc["database"]["reranker_model"] = "some/custom-reranker"
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_present_watch_true_is_hand_edit(self, tmp_path: Path) -> None:
        """[collections].watch = true present, WizardFeatures default is False → hand edit."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        if "collections" not in doc:
            doc.add("collections", tomlkit.table())
        doc["collections"]["watch"] = True
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_present_routing_hybrid_is_hand_edit(self, tmp_path: Path) -> None:
        """[routing].routing_strategy = 'hybrid' → hand edit (default is 'centroid')."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        if "routing" not in doc:
            doc.add("routing", tomlkit.table())
        doc["routing"]["routing_strategy"] = "hybrid"
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_present_log_format_json_is_hand_edit(self, tmp_path: Path) -> None:
        """[logging].format = 'json' → hand edit (default is 'text')."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        if "logging" not in doc:
            doc.add("logging", tomlkit.table())
        doc["logging"]["format"] = "json"
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_present_routing_default_is_not_hand_edit(self, tmp_path: Path) -> None:
        """S561: `--routing-strategy centroid` writes the key; a present-but-default
        value must NOT be reported as a hand edit (else every re-run falsely warns)."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        if "routing" not in doc:
            doc.add("routing", tomlkit.table())
        doc["routing"]["routing_strategy"] = "centroid"
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is False

    def test_detect_present_log_format_default_is_not_hand_edit(self, tmp_path: Path) -> None:
        """S561: `--log-format text` writes the key; a present-but-default value must
        NOT be reported as a hand edit (else every re-run falsely warns)."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        if "logging" not in doc:
            doc.add("logging", tomlkit.table())
        doc["logging"]["format"] = "text"
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is False

    def test_detect_present_eager_load_true_is_hand_edit(self, tmp_path: Path) -> None:
        """[database].eager_load_embedders = true → hand edit (default is False)."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        doc["database"]["eager_load_embedders"] = True
        config_path.write_text(tomlkit.dumps(doc))

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_missing_database_section_returns_true(self, tmp_path: Path) -> None:
        """Config with no [database] section → hand edit detected (all wizard keys absent/None)."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text("[server]\nhost = \"127.0.0.1\"\nport = 8765\n")

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_wizard_non_default_feature_triggers_warning(self, tmp_path: Path) -> None:
        """Config written by wizard WITH non-default features triggers detection.

        _detect_config_hand_edits compares against WizardFeatures() static defaults.
        A config with non-default wizard features (e.g. telemetry enabled) appears as
        a hand-edit. This is the documented accepted trade-off: wizard-selected
        non-default features trigger the overwrite warning on re-run.
        """
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        features = WizardFeatures(enable_telemetry=True)
        _write_wizard_config(config_path, "balanced", False, features=features)

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True
