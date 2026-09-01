"""Tests for _prewarm_timeout and _prewarm_models in archon_search/install.py (Task C0-2.3)."""
from __future__ import annotations

import sys
import time
import threading
import types
import warnings
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import (
    InstallError,
    _prewarm_graph_model,
    _prewarm_models,
    _prewarm_timeout,
)
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
    from archon_search.paths import get_models_dir
    mock_te.assert_called_once_with(profile.embedder, lazy_load=True, cache_dir=str(get_models_dir()))


def test_prewarm_calls_cross_encoder_when_reranker_set():
    """TextCrossEncoder must be called with reranker model when profile.reranker is set."""
    profile = ENGLISH_PROFILES["minimal"]  # reranker is set
    assert profile.reranker is not None
    mock_te = MagicMock()
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    with patch.dict(sys.modules, {"fastembed": fe_mod}):
        _prewarm_models(profile, timeout=300)
    from archon_search.paths import get_models_dir
    mock_tce.assert_called_once_with(profile.reranker, lazy_load=True, cache_dir=str(get_models_dir()))


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
# _prewarm_models — fastembed mean-pooling UserWarning suppression (Bug B)
# ---------------------------------------------------------------------------

def test_prewarm_suppresses_mean_pooling_userwarning():
    """The mean-pooling UserWarning fired by TextEmbedding.__init__ must not surface."""
    profile = MULTILINGUAL_PROFILES["minimal"]

    def warn_mean_pooling(*args, **kwargs):
        warnings.warn(
            "This model does not have CLS token; switching to mean pooling.",
            UserWarning,
            stacklevel=2,
        )
        return MagicMock()

    mock_te = MagicMock(side_effect=warn_mean_pooling)
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with patch.dict(sys.modules, {"fastembed": fe_mod}):
            _prewarm_models(profile, timeout=300)

    mean_pooling = [w for w in caught if "mean pooling" in str(w.message)]
    assert mean_pooling == [], "mean-pooling UserWarning leaked to wizard output"


def test_prewarm_does_not_suppress_unrelated_userwarning():
    """The filter is narrow: an unrelated UserWarning must still surface."""
    profile = MULTILINGUAL_PROFILES["minimal"]

    def warn_other(*args, **kwargs):
        warnings.warn("some other actionable warning", UserWarning, stacklevel=2)
        return MagicMock()

    mock_te = MagicMock(side_effect=warn_other)
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with patch.dict(sys.modules, {"fastembed": fe_mod}):
            _prewarm_models(profile, timeout=300)

    assert any("some other actionable warning" in str(w.message) for w in caught)


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


def test_prewarm_reranker_failure_logs_warning_not_raises(caplog):
    """When TextCrossEncoder raises, _prewarm_models must warn and continue — NOT raise.

    S202: CoreML reranker probe failure must be non-fatal so the wizard does not exit 1
    on hardware where the CoreML ONNX backend rejects the reranker model.
    """
    import logging

    profile = ENGLISH_PROFILES["minimal"]
    assert profile.reranker is not None
    mock_te = MagicMock()
    mock_tce = MagicMock(side_effect=RuntimeError("CoreML reranker probe failed: [ONNXRuntimeError]"))
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)

    with caplog.at_level(logging.WARNING, logger="archon_search.install"):
        with patch.dict(sys.modules, {"fastembed": fe_mod}):
            _prewarm_models(profile, timeout=300)  # must NOT raise

    assert any(profile.reranker in r.message for r in caplog.records), (
        f"expected warning mentioning {profile.reranker!r} in {[r.message for r in caplog.records]}"
    )


def test_prewarm_raises_install_error_on_cross_encoder_failure(caplog):
    """Reranker prewarm failure logs a warning and does NOT raise (S202 non-fatal fix)."""
    import logging

    profile = ENGLISH_PROFILES["minimal"]
    mock_te = MagicMock()
    mock_tce = MagicMock(side_effect=RuntimeError("download failed"))
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    with caplog.at_level(logging.WARNING, logger="archon_search.install"):
        with patch.dict(sys.modules, {"fastembed": fe_mod}):
            _prewarm_models(profile, timeout=300)  # must NOT raise
    assert any(profile.reranker in r.message for r in caplog.records)


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


# ---------------------------------------------------------------------------
# _prewarm_models — cache_dir matches runtime ModelEmbedder/ModelReranker (BE-26)
# ---------------------------------------------------------------------------

def test_archon_search_data_dir_relocates_embedder_and_reranker_caches(tmp_path, monkeypatch):
    """Redirecting ARCHON_SEARCH_DATA_DIR relocates get_models_dir() (and thus every cache_dir
    derived from it — see test_embedder.py / test_reranker.py / the prewarm tests above)."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    from archon_search.paths import get_models_dir

    assert str(get_models_dir()) == str(tmp_path / "models")


# ---------------------------------------------------------------------------
# _prewarm_graph_model — non-fatal, mirrors the reranker branch (Task BE-8)
# ---------------------------------------------------------------------------

def test_prewarm_graph_model_calls_from_pretrained_with_cache_dir():
    """GLiNER.from_pretrained is called with the same cache_dir as the runtime lazy-load."""
    from archon_search.paths import (
        GRAPH_NER_MODEL_NAME,
        GRAPH_NER_MODEL_REVISION,
        get_graph_models_dir,
    )

    mock_gliner_cls = MagicMock()
    gliner_mod = types.ModuleType("gliner")
    gliner_mod.GLiNER = mock_gliner_cls  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        _prewarm_graph_model()

    mock_gliner_cls.from_pretrained.assert_called_once_with(
        GRAPH_NER_MODEL_NAME,
        revision=GRAPH_NER_MODEL_REVISION,
        cache_dir=str(get_graph_models_dir()),
    )


def test_prewarm_entry_is_non_fatal_and_logs_first_use_will_download(caplog):
    """A failed graph model pre-warm must not raise — it logs and lets first use download it."""
    mock_gliner_cls = MagicMock()
    mock_gliner_cls.from_pretrained.side_effect = RuntimeError("network hiccup")
    gliner_mod = types.ModuleType("gliner")
    gliner_mod.GLiNER = mock_gliner_cls  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        with caplog.at_level("WARNING"):
            _prewarm_graph_model()  # must not raise

    assert any("first use" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# _prewarm_graph_model wiring into _prewarm_models (Task BE-8, finding 2:
# _prewarm_graph_model was previously dead code, never called by anything)
# ---------------------------------------------------------------------------


def test_prewarm_models_invokes_graph_prewarm_when_requested():
    """install_graph_extra=True must actually dispatch _prewarm_graph_model as
    part of the same pre-warm call chain used by preload_models."""
    profile = ENGLISH_PROFILES["minimal"]
    mock_te = MagicMock()
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    graph_mock = MagicMock()

    with patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
         patch.dict(sys.modules, {"fastembed": fe_mod}):
        _prewarm_models(profile, timeout=300, install_graph_extra=True)

    graph_mock.assert_called_once_with()


def test_prewarm_models_skips_graph_prewarm_when_not_requested():
    """install_graph_extra=False (the default) must never dispatch the graph pre-warm."""
    profile = ENGLISH_PROFILES["minimal"]
    mock_te = MagicMock()
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    graph_mock = MagicMock()

    with patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
         patch.dict(sys.modules, {"fastembed": fe_mod}):
        _prewarm_models(profile, timeout=300)

    graph_mock.assert_not_called()


def test_prewarm_models_skips_graph_prewarm_after_timeout(caplog):
    """If the embedder/reranker pre-warm already timed out, the graph pre-warm must
    be skipped (not silently dispatched after the wizard's timeout budget is spent)."""
    import logging

    profile = ENGLISH_PROFILES["minimal"]

    def slow_embedding(*args, **kwargs):
        time.sleep(0.1)  # sleep past the 0.01s timeout
        return MagicMock()

    mock_te = MagicMock(side_effect=slow_embedding)
    mock_tce = MagicMock()
    fe_mod = _make_fastembed_mock(mock_te, mock_tce)
    graph_mock = MagicMock()

    with caplog.at_level(logging.WARNING, logger="archon_search.install"):
        with patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
             patch.dict(sys.modules, {"fastembed": fe_mod}):
            _prewarm_models(profile, timeout=0.01, install_graph_extra=True)  # type: ignore[arg-type]

    graph_mock.assert_not_called()
    assert any("timed out" in r.message for r in caplog.records)


def test_preload_models_passes_install_graph_extra_through_to_prewarm():
    """RealInstaller.preload_models must forward install_graph_extra to _prewarm_models —
    the actual call chain the wizard's Step 14 pre-warm goes through."""
    from unittest.mock import patch as _patch

    from archon_search.install.installer import RealInstaller

    profile = ENGLISH_PROFILES["minimal"]
    installer = RealInstaller.__new__(RealInstaller)  # bypass __init__ — no FS/service needed

    with _patch("archon_search.install.installer._prewarm_models") as prewarm_mock, \
         _patch.object(RealInstaller, "_fe1_reprobe", MagicMock()):
        installer.preload_models(profile, None, False, install_graph_extra=True)

    prewarm_mock.assert_called_once_with(profile, install_graph_extra=True)
