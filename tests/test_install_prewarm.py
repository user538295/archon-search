"""Tests for _prewarm_timeout and _prewarm_models in archon_search/install.py (Task C0-2.3)."""
from __future__ import annotations

import sys
import time
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import InstallError, _prewarm_models, _prewarm_timeout
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES


def _make_fastembed_mock(te_mock: MagicMock, tce_mock: MagicMock) -> MagicMock:
    """Return a fake fastembed module with TextEmbedding and TextCrossEncoder."""
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = te_mock  # type: ignore[attr-defined]
    mod.TextCrossEncoder = tce_mock  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# _prewarm_timeout
# ---------------------------------------------------------------------------

def test_prewarm_timeout_minimal():
    """147 MB profile → timeout between 300 and 1800 (inclusive)."""
    profile = ENGLISH_PROFILES["minimal"]  # download_mb=147
    result = _prewarm_timeout(profile)
    assert 300 <= result <= 1800


def test_prewarm_timeout_max():
    """2300 MB profile → capped at 1800."""
    profile = ENGLISH_PROFILES["max"]  # download_mb=2300
    result = _prewarm_timeout(profile)
    assert result == 1800


# ---------------------------------------------------------------------------
# _prewarm_models — happy paths
# ---------------------------------------------------------------------------

def test_prewarm_calls_text_embedding_lazy():
    """TextEmbedding must be called with lazy_load=True."""
    profile = ENGLISH_PROFILES["minimal"]
    mock_te = MagicMock()
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    with patch.dict(sys.modules, {"fastembed": fe_mod}):
        _prewarm_models(profile, timeout=300)
    mock_te.assert_called_once_with(profile.embedder, lazy_load=True)


def test_prewarm_calls_cross_encoder_when_reranker_set():
    """TextCrossEncoder must be called with reranker model when profile.reranker is set."""
    profile = ENGLISH_PROFILES["minimal"]  # reranker is set
    assert profile.reranker is not None
    mock_te = MagicMock()
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    with patch.dict(sys.modules, {"fastembed": fe_mod}):
        _prewarm_models(profile, timeout=300)
    mock_tce.assert_called_once_with(profile.reranker, lazy_load=True)


def test_prewarm_skips_cross_encoder_when_reranker_none():
    """TextCrossEncoder must NOT be called when profile.reranker is None."""
    profile = MULTILINGUAL_PROFILES["minimal"]  # reranker=None
    assert profile.reranker is None
    mock_te = MagicMock()
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    with patch.dict(sys.modules, {"fastembed": fe_mod}):
        _prewarm_models(profile, timeout=300)
    mock_tce.assert_not_called()


# ---------------------------------------------------------------------------
# _prewarm_models — error paths
# ---------------------------------------------------------------------------

def test_prewarm_raises_install_error_on_download_failure():
    """If TextEmbedding raises, InstallError must be raised with model name in message."""
    profile = ENGLISH_PROFILES["minimal"]
    mock_te = MagicMock(side_effect=RuntimeError("network error"))
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    with patch.dict(sys.modules, {"fastembed": fe_mod}):
        with pytest.raises(InstallError) as exc_info:
            _prewarm_models(profile, timeout=300)
    assert profile.embedder in str(exc_info.value)


def test_prewarm_raises_install_error_on_cross_encoder_failure():
    """If TextCrossEncoder raises, InstallError must be raised with reranker model name."""
    profile = ENGLISH_PROFILES["minimal"]
    mock_te = MagicMock()
    mock_tce = MagicMock(side_effect=RuntimeError("download failed"))
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    with patch.dict(sys.modules, {"fastembed": fe_mod}):
        with pytest.raises(InstallError) as exc_info:
            _prewarm_models(profile, timeout=300)
    assert profile.reranker in str(exc_info.value)


# ---------------------------------------------------------------------------
# _prewarm_models — timeout behavior
# ---------------------------------------------------------------------------

def test_prewarm_timeout_fires_and_warns(caplog):
    """Short timeout fires during TextEmbedding call; warns and skips TextCrossEncoder."""
    import logging

    profile = ENGLISH_PROFILES["minimal"]

    def slow_embedding(*args, **kwargs):
        time.sleep(0.1)  # sleep past the 0.01s timeout
        return MagicMock()

    mock_te = MagicMock(side_effect=slow_embedding)
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)

    with caplog.at_level(logging.WARNING, logger="archon_search.install"):
        with patch.dict(sys.modules, {"fastembed": fe_mod}):
            _prewarm_models(profile, timeout=0.01)  # type: ignore[arg-type]

    # Function returned without raising
    mock_tce.assert_not_called()
    assert any("timed out" in record.message for record in caplog.records)


def test_prewarm_cancels_timer_on_success():
    """timer.cancel() must be called after a successful download."""
    profile = ENGLISH_PROFILES["minimal"]

    mock_timer_instance = MagicMock()
    mock_timer_class = MagicMock(return_value=mock_timer_instance)
    mock_te = MagicMock()
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)

    with patch("archon_search.install.threading.Timer", mock_timer_class), \
         patch.dict(sys.modules, {"fastembed": fe_mod}):
        _prewarm_models(profile, timeout=300)

    mock_timer_instance.cancel.assert_called_once()
