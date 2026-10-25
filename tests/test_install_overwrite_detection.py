"""Tests for _detect_config_hand_edits — Task C14-5.4."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from archon_search.install import WizardFeatures, _write_profile_config
from archon_search.install.config_writer import (
    _record_wizard_values,
    _wizard_record_path,
)
from archon_search.profiles import get_profile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_wizard_config(config_path: Path, profile_name: str, multilingual: bool, features: WizardFeatures | None = None) -> None:
    """Write a config file exactly as the wizard would."""
    profile = get_profile(profile_name, multilingual)
    _write_profile_config(config_path, profile, profile_name, multilingual, features=features)


def _set_key(config_path: Path, section: str, key: str, value: Any) -> None:
    """Set ``[section].key = value`` the way a user editing the file would."""
    doc = tomlkit.parse(config_path.read_text())
    if section not in doc:
        doc.add(section, tomlkit.table())
    doc[section][key] = value
    config_path.write_text(tomlkit.dumps(doc))


def _delete_key(config_path: Path, section: str, key: str) -> None:
    doc = tomlkit.parse(config_path.read_text())
    del doc[section][key]
    config_path.write_text(tomlkit.dumps(doc))


# Wizard answers that write a non-default value into every kind of re-run-managed key.
_NON_DEFAULT_FEATURES = WizardFeatures(
    enable_watch=True,
    enable_telemetry=True,
    telemetry_retention_days=7,
    eager_load_embedders=True,
    routing_strategy="hybrid",
    log_format="json",
    disable_reranker=True,
    enable_hyde=True,
    enable_rag_fusion=True,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDetectConfigHandEdits:
    """No wizard record next to the config (written before records existed): baseline fallback."""

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
        """[telemetry].enabled absent in config → not a hand edit (config default in effect)."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)

        # Confirm telemetry section is absent
        doc = tomlkit.parse(config_path.read_text())
        assert "telemetry" not in doc or "enabled" not in doc.get("telemetry", {})

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is False

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

    def test_detect_wizard_written_explicit_false_is_not_hand_edit(self, tmp_path: Path) -> None:
        """S553/S556/S557: `--no-eager-load --no-watch --no-telemetry` write `false`; those
        present-but-default keys must NOT be reported as a hand edit on re-run."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        features = WizardFeatures(eager_load_embedders=False, enable_watch=False, enable_telemetry=False)
        _write_wizard_config(config_path, "balanced", False, features=features)
        doc = tomlkit.parse(config_path.read_text())
        assert doc["database"]["eager_load_embedders"] is False
        assert doc["collections"]["watch"] is False
        assert doc["telemetry"]["enabled"] is False

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is False

    @pytest.mark.parametrize(
        ("section", "key"),
        [("database", "eager_load_embedders"), ("collections", "watch"), ("telemetry", "enabled")],
    )
    def test_detect_present_bool_true_is_hand_edit(self, tmp_path: Path, section: str, key: str) -> None:
        """A present `true` for each wizard bool key differs from the SearchConfig default → hand edit."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)
        doc = tomlkit.parse(config_path.read_text())
        if section not in doc:
            doc.add(section, tomlkit.table())
        doc[section][key] = True
        config_path.write_text(tomlkit.dumps(doc))

        assert _detect_config_hand_edits(config_path, "balanced", False) is True

    @pytest.mark.parametrize(
        ("section", "key", "value"),
        [
            pytest.param("hyde", "enabled", True, id="hyde-enabled"),
            pytest.param("rag_fusion", "enabled", True, id="rag-fusion-enabled"),
            pytest.param("telemetry", "retention_days", 7, id="telemetry-retention-days"),
        ],
    )
    def test_detect_non_default_query_expansion_or_retention_is_hand_edit(
        self, tmp_path: Path, section: str, key: str, value: int | bool
    ) -> None:
        """A no-flag re-run removes [hyde]/[rag_fusion] `enabled` and `[telemetry].retention_days`,
        so a present non-default value there is a hand edit the re-run must warn about."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)
        _set_key(config_path, section, key, value)

        assert _detect_config_hand_edits(config_path, "balanced", False) is True

    def test_detect_present_default_retention_days_is_not_hand_edit(self, tmp_path: Path) -> None:
        """A present `[telemetry].retention_days` equal to the SearchConfig default is not a hand edit."""
        from archon_search.config import SearchConfig
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)
        _set_key(config_path, "telemetry", "retention_days", SearchConfig().telemetry.retention_days)

        assert _detect_config_hand_edits(config_path, "balanced", False) is False

    def test_detect_excluded_key_is_not_hand_edit(self, tmp_path: Path) -> None:
        """`[server].host` is a deployment key a re-run only writes when `--host` names it,
        so a non-default value on an otherwise profile-matching config is not a hand edit."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)
        _set_key(config_path, "server", "host", "0.0.0.0")

        assert _detect_config_hand_edits(config_path, "balanced", False) is False

    def test_detect_changed_device_alone_is_not_hand_edit(self, tmp_path: Path) -> None:
        """`[database].device` is rewritten by Step 9 from the GPU answer on every re-run,
        so a changed value on an otherwise profile-matching config is not a hand edit."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)
        _set_key(config_path, "database", "device", "mps")

        assert _detect_config_hand_edits(config_path, "balanced", False) is False

    def test_detect_non_table_section_does_not_crash(self, tmp_path: Path) -> None:
        """A hand-edited `hyde = "oops"` (not a table) holds no managed key: no crash, no hand edit."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "balanced", False)
        config_path.write_text('hyde = "oops"\n' + config_path.read_text())

        assert _detect_config_hand_edits(config_path, "balanced", False) is False

    def test_detect_missing_database_section_returns_true(self, tmp_path: Path) -> None:
        """Config with no [database] section → hand edit detected (all wizard keys absent/None)."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text("[server]\nhost = \"127.0.0.1\"\nport = 8765\n")

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True

    def test_detect_wizard_non_default_feature_triggers_warning(self, tmp_path: Path) -> None:
        """Without a wizard record, a non-default wizard-written feature is indistinguishable
        from a hand edit: the baseline only knows SearchConfig() defaults, so it warns."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        features = WizardFeatures(enable_telemetry=True)
        _write_wizard_config(config_path, "balanced", False, features=features)

        result = _detect_config_hand_edits(config_path, "balanced", False)
        assert result is True


class TestWizardRecord:
    """The record `_record_wizard_values` leaves next to the config makes detection exact:
    a re-run flags only managed keys that changed since the wizard last wrote the config."""

    def test_record_holds_managed_values_present_on_disk(self, tmp_path: Path) -> None:
        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False, features=WizardFeatures(enable_watch=True, port=9000))

        _record_wizard_values(config_path)

        profile = get_profile("minimal", False)
        assert json.loads(_wizard_record_path(config_path).read_text()) == {
            "collections.watch": True,
            "database.chunk_size": profile.chunk_size,
            "database.embedding_model": profile.embedder,
            "database.multilingual": False,
            "database.profile": "minimal",
            "database.reranker_model": profile.reranker,
        }

    def test_record_removed_when_config_missing(self, tmp_path: Path) -> None:
        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False)
        _record_wizard_values(config_path)
        config_path.unlink()

        _record_wizard_values(config_path)

        assert not _wizard_record_path(config_path).exists()

    def test_record_write_failure_warns_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An OSError writing the record must not turn a finished install into a traceback."""
        from archon_search.install import config_writer

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False)

        def _fail_write(path: Path, data: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(config_writer, "atomic_write_bytes", _fail_write)

        _record_wizard_values(config_path)

        err = capsys.readouterr().err
        assert str(_wizard_record_path(config_path)) in err
        assert "disk full" in err
        assert "hand edit" in err
        assert not _wizard_record_path(config_path).exists()

    def test_unparseable_config_at_record_time_warns_instead_of_raising(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A config that no longer parses must not turn a finished install into a traceback."""
        config_path = tmp_path / "archon-search.toml"
        config_path.write_text("[database\n")

        _record_wizard_values(config_path)

        err = capsys.readouterr().err
        assert str(_wizard_record_path(config_path)) in err
        assert "hand edit" in err
        assert not _wizard_record_path(config_path).exists()

    def test_stale_record_temp_from_killed_run_does_not_block_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A `.tmp` left by a hard-killed run must not make every later record write fail."""
        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False)
        record_path = _wizard_record_path(config_path)
        record_path.with_suffix(record_path.suffix + ".tmp").write_text("partial")

        _record_wizard_values(config_path)

        assert json.loads(record_path.read_text())["database.profile"] == "minimal"
        assert capsys.readouterr().err == ""

    def test_non_json_managed_value_is_recorded_and_reads_as_hand_edit(self, tmp_path: Path) -> None:
        """A hand-edited TOML datetime in a managed key is recorded as text, never raising."""
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False)
        _set_key(config_path, "routing", "routing_strategy", datetime(2026, 1, 1, tzinfo=UTC))

        _record_wizard_values(config_path)

        record = json.loads(_wizard_record_path(config_path).read_text())
        assert record["routing.routing_strategy"] == "2026-01-01 00:00:00+00:00"
        assert _detect_config_hand_edits(config_path, "minimal", False) is True

    def test_record_of_non_table_section_skips_it(self, tmp_path: Path) -> None:
        """A hand-edited `hyde = "oops"` (not a table) contributes no managed value."""
        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False)
        config_path.write_text('hyde = "oops"\n' + config_path.read_text())

        _record_wizard_values(config_path)

        assert not any(key.startswith("hyde.") for key in json.loads(_wizard_record_path(config_path).read_text()))

    def test_wizard_written_non_default_values_are_not_hand_edits(self, tmp_path: Path) -> None:
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False, features=_NON_DEFAULT_FEATURES)
        _record_wizard_values(config_path)

        assert _detect_config_hand_edits(config_path, "minimal", False) is False

    @pytest.mark.parametrize(
        ("section", "key", "value"),
        [
            pytest.param("collections", "watch", False, id="changed-back-to-default"),
            pytest.param("database", "chunk_size", 999, id="changed-profile-key"),
            pytest.param("hyde", "enabled", True, id="added-hyde-enabled"),
            pytest.param("telemetry", "retention_days", 30, id="added-retention-days"),
        ],
    )
    def test_changed_or_added_managed_key_is_hand_edit(
        self, tmp_path: Path, section: str, key: str, value: Any
    ) -> None:
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False, features=WizardFeatures(enable_watch=True))
        _record_wizard_values(config_path)
        _set_key(config_path, section, key, value)

        assert _detect_config_hand_edits(config_path, "minimal", False) is True

    def test_removed_managed_key_is_hand_edit(self, tmp_path: Path) -> None:
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False, features=WizardFeatures(enable_watch=True))
        _record_wizard_values(config_path)
        _delete_key(config_path, "collections", "watch")

        assert _detect_config_hand_edits(config_path, "minimal", False) is True

    @pytest.mark.parametrize(
        ("section", "key", "value"),
        [
            pytest.param("server", "port", 9100, id="server-port"),
            pytest.param("routing", "routing_shortlist_size", 7, id="routing-shortlist-size"),
        ],
    )
    def test_non_managed_key_change_is_not_hand_edit(
        self, tmp_path: Path, section: str, key: str, value: int
    ) -> None:
        from archon_search.install import _detect_config_hand_edits

        config_path = tmp_path / "archon-search.toml"
        _write_wizard_config(config_path, "minimal", False)
        _record_wizard_values(config_path)
        _set_key(config_path, section, key, value)

        assert _detect_config_hand_edits(config_path, "minimal", False) is False

    @pytest.mark.parametrize(
        "record_text",
        [pytest.param("{not json", id="unparseable"), pytest.param("[]", id="not-an-object")],
    )
    def test_invalid_record_falls_back_to_baseline(self, tmp_path: Path, record_text: str) -> None:
        """An unreadable record is ignored: the baseline flags the non-default watch = true
        and passes the untouched default config."""
        from archon_search.install import _detect_config_hand_edits

        flagged = tmp_path / "flagged.toml"
        _write_wizard_config(flagged, "minimal", False, features=WizardFeatures(enable_watch=True))
        _wizard_record_path(flagged).write_text(record_text)
        clean = tmp_path / "clean.toml"
        _write_wizard_config(clean, "minimal", False)
        _wizard_record_path(clean).write_text(record_text)

        assert _detect_config_hand_edits(flagged, "minimal", False) is True
        assert _detect_config_hand_edits(clean, "minimal", False) is False
