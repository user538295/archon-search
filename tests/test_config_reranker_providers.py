"""Tests for SearchConfig.reranker_providers field — CoreML split-provider support."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from archon_search.config import SearchConfig


def test_reranker_providers_default_is_none() -> None:
    cfg = SearchConfig()
    assert cfg.reranker_providers is None


def test_reranker_providers_parsed_from_toml(tmp_path: Path) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text("[database]\nreranker_providers = []\n", encoding="utf-8")
    old = os.environ.get("ARCHON_SEARCH_CONFIG")
    os.environ["ARCHON_SEARCH_CONFIG"] = str(toml_path)
    try:
        from archon_search.config import load_config

        cfg = load_config()
        assert cfg.reranker_providers == []
    finally:
        if old is None:
            del os.environ["ARCHON_SEARCH_CONFIG"]
        else:
            os.environ["ARCHON_SEARCH_CONFIG"] = old


def test_reranker_providers_absent_means_inherit(tmp_path: Path) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        '[database]\nproviders = ["CoreMLExecutionProvider"]\n', encoding="utf-8"
    )
    old = os.environ.get("ARCHON_SEARCH_CONFIG")
    os.environ["ARCHON_SEARCH_CONFIG"] = str(toml_path)
    try:
        from archon_search.config import load_config

        cfg = load_config()
        assert cfg.reranker_providers is None
        assert cfg.providers == ["CoreMLExecutionProvider"]
    finally:
        if old is None:
            del os.environ["ARCHON_SEARCH_CONFIG"]
        else:
            os.environ["ARCHON_SEARCH_CONFIG"] = old


def test_reranker_providers_non_empty_list(tmp_path: Path) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        '[database]\nreranker_providers = ["CPUExecutionProvider"]\n', encoding="utf-8"
    )
    old = os.environ.get("ARCHON_SEARCH_CONFIG")
    os.environ["ARCHON_SEARCH_CONFIG"] = str(toml_path)
    try:
        from archon_search.config import load_config

        cfg = load_config()
        assert cfg.reranker_providers == ["CPUExecutionProvider"]
    finally:
        if old is None:
            del os.environ["ARCHON_SEARCH_CONFIG"]
        else:
            os.environ["ARCHON_SEARCH_CONFIG"] = old
