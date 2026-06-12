"""Tests for archon_search.server.app startup guards."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.config import SearchConfig
from archon_search.server.app import _check_multilingual_deps


# ---------------------------------------------------------------------------
# _check_multilingual_deps
# ---------------------------------------------------------------------------


def _make_config(*, multilingual: bool) -> SearchConfig:
    cfg = SearchConfig()
    cfg.multilingual = multilingual
    return cfg


class TestCheckMultilingualDeps:
    def test_check_multilingual_deps_disabled(self):
        """When multilingual=False, _import_fasttext is never called and function returns normally."""
        cfg = _make_config(multilingual=False)
        with patch("archon_search.server.app._import_fasttext") as mock_import:
            _check_multilingual_deps(cfg)
        mock_import.assert_not_called()

    def test_check_multilingual_deps_package_missing(self, tmp_path):
        """When multilingual=True and fasttext-wheel is not installed, RuntimeError with 'fasttext-wheel' in message."""
        cfg = _make_config(multilingual=True)
        model_path = tmp_path / "models" / "lid.176.ftz"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.touch()

        # Simulate fasttext not installed by making import raise ImportError
        with patch("archon_search.server.app._import_fasttext", side_effect=ImportError("no module named fasttext")):
            with pytest.raises(RuntimeError) as exc_info:
                _check_multilingual_deps(cfg)
        assert "fasttext-wheel" in str(exc_info.value)

    def test_check_multilingual_deps_model_missing(self, tmp_path):
        """When multilingual=True, import succeeds but model file is absent: RuntimeError with 'lid.176.ftz'."""
        cfg = _make_config(multilingual=True)

        mock_fasttext = MagicMock()
        missing_path = tmp_path / "missing" / "lid.176.ftz"
        with patch("archon_search.server.app._import_fasttext", return_value=mock_fasttext):
            with patch(
                "archon_search.server.app._multilingual_model_path",
                return_value=missing_path,
            ):
                with pytest.raises(RuntimeError) as exc_info:
                    _check_multilingual_deps(cfg)
        assert "lid.176.ftz" in str(exc_info.value)

    def test_check_multilingual_deps_all_present(self, tmp_path):
        """When multilingual=True, import succeeds, and model file exists: returns without error."""
        cfg = _make_config(multilingual=True)
        model_path = tmp_path / "models" / "lid.176.ftz"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.touch()

        mock_fasttext = MagicMock()
        with patch("archon_search.server.app._import_fasttext", return_value=mock_fasttext):
            with patch(
                "archon_search.server.app._multilingual_model_path",
                return_value=model_path,
            ):
                _check_multilingual_deps(cfg)  # no exception


# ---------------------------------------------------------------------------
# create_app production path — LanguageDetector wired when multilingual=True
# ---------------------------------------------------------------------------


class TestCreateAppLanguageDetectorWiring:
    def test_create_app_wires_language_detector_when_multilingual(self, tmp_path):
        """create_app() passes a LanguageDetector to SearchPipeline when multilingual=True."""
        from archon_search.jobs import JobStore
        from archon_search.server.app import create_app
        from archon_search.language_detector import LanguageDetector

        cfg = SearchConfig()
        cfg.multilingual = True
        cfg.db_path = str(tmp_path / "test.db")

        job_store = JobStore()

        with (
            patch("archon_search.server.app._check_multilingual_deps"),
            patch("archon_search.server.app._import_fasttext"),
            patch.object(LanguageDetector, "__init__", return_value=None),
            patch("archon_search.server.app.ModelEmbedder"),
            patch("archon_search.server.app.SearchStore"),
            patch("archon_search.server.app.IndexingStateStore"),
        ):
            app = create_app(cfg, job_store)

        pipeline = app.state.pipeline
        assert pipeline._language_detector is not None
        assert isinstance(pipeline._language_detector, LanguageDetector)
        assert pipeline._language_detection_confidence_threshold == cfg.language_detection_confidence_threshold

    def test_create_app_no_language_detector_when_not_multilingual(self, tmp_path):
        """create_app() does NOT pass a LanguageDetector to SearchPipeline when multilingual=False."""
        from archon_search.jobs import JobStore
        from archon_search.server.app import create_app
        from archon_search.language_detector import LanguageDetector

        cfg = SearchConfig()
        cfg.multilingual = False
        cfg.db_path = str(tmp_path / "test.db")

        job_store = JobStore()

        with (
            patch("archon_search.server.app._check_multilingual_deps"),
            patch.object(LanguageDetector, "__init__", side_effect=AssertionError("should not be called")),
            patch("archon_search.server.app.ModelEmbedder"),
            patch("archon_search.server.app.SearchStore"),
            patch("archon_search.server.app.IndexingStateStore"),
        ):
            app = create_app(cfg, job_store)

        pipeline = app.state.pipeline
        assert pipeline._language_detector is None
