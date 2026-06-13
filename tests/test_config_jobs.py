"""Tests for Task 1.2 — [jobs] config section in SearchConfig."""

from __future__ import annotations

import textwrap

import pytest

from archon_search.config import ConfigError, JobsConfig, SearchConfig, _apply_toml
import tomlkit


class TestJobsConfigDefaults:
    def test_jobs_config_defaults(self) -> None:
        """SearchConfig() has jobs.max_concurrent_bulk == 1 and jobs.checkpoint_interval == 100."""
        config = SearchConfig()
        assert config.jobs.max_concurrent_bulk == 1
        assert config.jobs.checkpoint_interval == 100

    def test_jobs_config_dataclass_direct(self) -> None:
        """JobsConfig() defaults are 1 and 100."""
        jc = JobsConfig()
        assert jc.max_concurrent_bulk == 1
        assert jc.checkpoint_interval == 100


class TestJobsConfigFromToml:
    def test_jobs_config_from_toml(self) -> None:
        """TOML with [jobs] section is parsed correctly."""
        toml_text = textwrap.dedent("""\
            [jobs]
            max_concurrent_bulk = 3
            checkpoint_interval = 50
        """)
        doc = tomlkit.parse(toml_text)
        config = SearchConfig()
        _apply_toml(config, doc)
        assert config.jobs.max_concurrent_bulk == 3
        assert config.jobs.checkpoint_interval == 50

    def test_jobs_config_missing_section_uses_defaults(self) -> None:
        """TOML without [jobs] section uses defaults."""
        toml_text = "[server]\nport = 8765\n"
        doc = tomlkit.parse(toml_text)
        config = SearchConfig()
        _apply_toml(config, doc)
        assert config.jobs.max_concurrent_bulk == 1
        assert config.jobs.checkpoint_interval == 100

    def test_jobs_config_partial_section_uses_defaults_for_missing_keys(self) -> None:
        """TOML with only one key in [jobs] uses default for the other."""
        toml_text = "[jobs]\nmax_concurrent_bulk = 5\n"
        doc = tomlkit.parse(toml_text)
        config = SearchConfig()
        _apply_toml(config, doc)
        assert config.jobs.max_concurrent_bulk == 5
        assert config.jobs.checkpoint_interval == 100


class TestJobsConfigValidation:
    def test_jobs_config_invalid_zero_max_concurrent(self) -> None:
        """max_concurrent_bulk = 0 raises ConfigError."""
        toml_text = "[jobs]\nmax_concurrent_bulk = 0\n"
        doc = tomlkit.parse(toml_text)
        config = SearchConfig()
        with pytest.raises(ConfigError):
            _apply_toml(config, doc)

    def test_jobs_config_invalid_negative_max_concurrent(self) -> None:
        """max_concurrent_bulk = -1 raises ConfigError."""
        toml_text = "[jobs]\nmax_concurrent_bulk = -1\n"
        doc = tomlkit.parse(toml_text)
        config = SearchConfig()
        with pytest.raises(ConfigError):
            _apply_toml(config, doc)

    def test_jobs_config_invalid_zero_checkpoint_interval(self) -> None:
        """checkpoint_interval = 0 raises ConfigError."""
        toml_text = "[jobs]\ncheckpoint_interval = 0\n"
        doc = tomlkit.parse(toml_text)
        config = SearchConfig()
        with pytest.raises(ConfigError):
            _apply_toml(config, doc)

    def test_jobs_config_invalid_negative(self) -> None:
        """checkpoint_interval = -1 raises ConfigError."""
        toml_text = "[jobs]\ncheckpoint_interval = -1\n"
        doc = tomlkit.parse(toml_text)
        config = SearchConfig()
        with pytest.raises(ConfigError):
            _apply_toml(config, doc)

    def test_jobs_config_invalid_non_int_max_concurrent(self) -> None:
        """max_concurrent_bulk = 'two' raises ConfigError."""
        toml_text = '[jobs]\nmax_concurrent_bulk = "two"\n'
        doc = tomlkit.parse(toml_text)
        config = SearchConfig()
        with pytest.raises(ConfigError):
            _apply_toml(config, doc)
