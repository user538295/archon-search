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
        with patch("archon_search.server.app._import_fasttext", return_value=mock_fasttext):
            with patch("archon_search.server.app._MULTILINGUAL_MODEL_PATH", tmp_path / "missing" / "lid.176.ftz"):
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
            with patch("archon_search.server.app._MULTILINGUAL_MODEL_PATH", model_path):
                _check_multilingual_deps(cfg)  # no exception
