"""Tests for SearchConfig.reranker_providers field — CoreML split-provider support."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# create_pipeline uses reranker_providers when set
# ---------------------------------------------------------------------------


def _make_pipeline_cfg(tmp_path: Path, **kw) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path)
    cfg.reranker_model = kw.get("reranker_model", "Xenova/ms-marco-MiniLM-L-6-v2")
    cfg.providers = kw.get("providers", ["CoreMLExecutionProvider"])
    cfg.reranker_providers = kw.get("reranker_providers", None)
    cfg.graph.enabled = False
    cfg.multilingual = False
    return cfg


def test_create_pipeline_uses_reranker_providers_when_set(tmp_path: Path) -> None:
    """When reranker_providers=[], create_pipeline passes None to ModelReranker ([] or None = None = CPU)."""
    cfg = _make_pipeline_cfg(tmp_path, reranker_providers=[])

    mock_reranker = MagicMock()
    mock_embedder = MagicMock()
    mock_store = MagicMock()

    with patch("archon_search.pipeline.ModelReranker", return_value=mock_reranker) as reranker_cls, \
         patch("archon_search.pipeline.ModelEmbedder", return_value=mock_embedder), \
         patch("archon_search.pipeline.SearchStore", return_value=mock_store):
        from archon_search.pipeline import create_pipeline
        create_pipeline(cfg)

    reranker_cls.assert_called_once()
    call_kwargs = reranker_cls.call_args
    passed_providers = call_kwargs.kwargs.get("providers")
    assert passed_providers is None, f"Expected None ([] normalised to CPU), got {passed_providers}"


def test_create_pipeline_inherits_providers_when_reranker_providers_none(tmp_path: Path) -> None:
    """When reranker_providers is None, create_pipeline falls back to providers."""
    cfg = _make_pipeline_cfg(tmp_path, reranker_providers=None)

    mock_reranker = MagicMock()
    mock_embedder = MagicMock()
    mock_store = MagicMock()

    with patch("archon_search.pipeline.ModelReranker", return_value=mock_reranker) as reranker_cls, \
         patch("archon_search.pipeline.ModelEmbedder", return_value=mock_embedder), \
         patch("archon_search.pipeline.SearchStore", return_value=mock_store):
        from archon_search.pipeline import create_pipeline
        create_pipeline(cfg)

    reranker_cls.assert_called_once()
    call_kwargs = reranker_cls.call_args
    passed_providers = call_kwargs.kwargs.get("providers")
    assert passed_providers == ["CoreMLExecutionProvider"], (
        f"Expected ['CoreMLExecutionProvider'] (inherited from providers), got {passed_providers}"
    )
