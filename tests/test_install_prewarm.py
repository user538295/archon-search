"""Tests for _prewarm_timeout and _prewarm_models in archon_search/install/prewarm.py (Task C0-2.3, S671)."""
from __future__ import annotations

import logging
import sys
import types
import warnings
import weakref
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest

from archon_search.config import SearchConfig, resolve_reranker_providers
from archon_search.embedder import ModelEmbedder
from archon_search.install import (
    InstallError,
    _prewarm_graph_model,
    _prewarm_models,
    _prewarm_timeout,
)
from archon_search.paths import get_models_dir
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES, InstallProfile
from archon_search.reranker import ModelReranker

_COREML = "CoreMLExecutionProvider"
_TIMEOUT_S = 300.0
# The onnxruntime error S671 hit: the files downloaded, but no session could be built.
_LOAD_ERROR = (
    "[ONNXRuntimeError] : 1 : FAIL : External data path validation failed: "
    "External data path escapes model directory."
)
_CLOCK_TARGET = "archon_search.install.prewarm.time"


class _FakeModel:
    """A loaded fastembed model: ``embed``/``rerank`` are generators, as in fastembed,
    so *inference_error* surfaces only when the result is iterated — as a fresh
    instance each time, so no test-held exception carries a failed model's traceback."""

    def __init__(self, inference_error: Exception | None) -> None:
        self._inference_error = inference_error

    def _raise_inference_error(self) -> None:
        if self._inference_error is not None:
            raise type(self._inference_error)(*self._inference_error.args)

    def embed(self, documents: list[str]) -> Iterator[np.ndarray]:
        for _ in documents:
            self._raise_inference_error()
            yield np.zeros(1, dtype=np.float32)

    def rerank(self, query: str, documents: list[str]) -> Iterator[float]:
        for _ in documents:
            self._raise_inference_error()
            yield 0.0


def _fastembed_cls(
    *,
    download_error: Exception | None = None,
    session_error: Exception | None = None,
    inference_error: Exception | None = None,
    on_build: Callable[[_FakeModel], None] | None = None,
) -> MagicMock:
    """A fake fastembed model class with fastembed 0.9.0's load semantics.

    Construction resolves and downloads the model (*download_error*). With
    ``lazy_load=True`` that is all it does; otherwise it also builds the ONNX session
    (*session_error*). *on_build* sees every instance constructed.
    """

    def _construct(*_args: Any, lazy_load: bool = False, **_kwargs: Any) -> _FakeModel:
        if download_error is not None:
            raise download_error
        if not lazy_load and session_error is not None:
            raise session_error
        model = _FakeModel(inference_error)
        if on_build is not None:
            on_build(model)
        return model

    return MagicMock(side_effect=_construct)


def _fastembed_modules(te_mock: MagicMock, tce_mock: MagicMock) -> dict[str, types.ModuleType]:
    """sys.modules entries for the two import paths pre-warm and the runtime classes use:
    ``from fastembed import TextEmbedding`` and
    ``from fastembed.rerank.cross_encoder import TextCrossEncoder``."""
    fe_mod = types.ModuleType("fastembed")
    fe_mod.TextEmbedding = te_mock  # type: ignore[attr-defined]
    rerank_mod = types.ModuleType("fastembed.rerank")
    cross_encoder_mod = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder_mod.TextCrossEncoder = tce_mock  # type: ignore[attr-defined]
    return {
        "fastembed": fe_mod,
        "fastembed.rerank": rerank_mod,
        "fastembed.rerank.cross_encoder": cross_encoder_mod,
    }


def _cfg_for(profile: InstallProfile, **overrides: Any) -> SearchConfig:
    """The [database] model keys the wizard writes for *profile* (config_writer.py)."""
    fields: dict[str, Any] = {
        "embedding_model": profile.embedder,
        "reranker_model": profile.reranker if profile.reranker is not None else "",
    }
    return SearchConfig(**{**fields, **overrides})


class _FakeClock:
    """Stands in for prewarm's ``time`` module: ``monotonic()`` reads ``now``, which a
    test moves past the deadline from inside a fake model's construction."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def pass_deadline(self, _model: object = None) -> None:
        self.now += _TIMEOUT_S


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
# _prewarm_models — download, then load exactly as the service loads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("providers", [[], [_COREML]], ids=["cpu", "coreml"])
def test_prewarm_downloads_then_loads_embedder_exactly_as_the_runtime_does(providers):
    """Phase 1 downloads (``lazy_load=True``: no session). Phase 2 is the eager
    construction ModelEmbedder.encode performs, with the runtime's kwargs, and one
    inference (S671)."""
    profile = ENGLISH_PROFILES["minimal"]
    cfg = _cfg_for(profile, providers=providers, reranker_model="")
    runtime_te, prewarm_te = _fastembed_cls(), _fastembed_cls()

    with patch.dict(sys.modules, _fastembed_modules(runtime_te, _fastembed_cls())):
        ModelEmbedder(cfg.embedding_model, providers=cfg.providers).encode(["warmup"])
    with patch.dict(sys.modules, _fastembed_modules(prewarm_te, _fastembed_cls())):
        _prewarm_models(cfg, _TIMEOUT_S)

    assert runtime_te.call_args.kwargs["providers"] == (providers or None)
    assert prewarm_te.call_args_list == [
        call(cfg.embedding_model, cache_dir=str(get_models_dir()), lazy_load=True),
        runtime_te.call_args,
    ]


@pytest.mark.parametrize(
    ("reranker_providers", "expected"),
    [([], None), (None, [_COREML])],
    ids=["split-coreml-reranker-on-cpu", "reranker-on-coreml"],
)
def test_prewarm_downloads_then_loads_reranker_exactly_as_the_runtime_does(
    reranker_providers, expected
):
    """The reranker loads under resolve_reranker_providers — CPU on the split CoreML
    config, [database].providers otherwise — as ModelReranker.predict builds it."""
    profile = ENGLISH_PROFILES["minimal"]
    cfg = _cfg_for(profile, providers=[_COREML], reranker_providers=reranker_providers)
    runtime_tce, prewarm_tce = _fastembed_cls(), _fastembed_cls()

    with patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), runtime_tce)):
        ModelReranker(cfg.reranker_model, providers=resolve_reranker_providers(cfg)).predict(
            [("warmup", "warmup")]
        )
    with patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), prewarm_tce)):
        _prewarm_models(cfg, _TIMEOUT_S)

    assert runtime_tce.call_args.kwargs["providers"] == expected
    assert prewarm_tce.call_args_list == [
        call(cfg.reranker_model, cache_dir=str(get_models_dir()), lazy_load=True),
        runtime_tce.call_args,
    ]


def test_prewarm_skips_reranker_disabled_in_config():
    """--no-reranker (or a profile without one) writes reranker_model = "": the service
    never loads one, so pre-warm must not download or load the profile's reranker either."""
    profile = ENGLISH_PROFILES["minimal"]
    assert profile.reranker is not None
    mock_tce = _fastembed_cls()
    with patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), mock_tce)):
        _prewarm_models(_cfg_for(profile, reranker_model=""), _TIMEOUT_S)
    mock_tce.assert_not_called()


def test_prewarm_releases_each_model_before_the_next_one_loads():
    """Every model is freed before the next one is built — the reranker's and the graph
    model's included — and nothing survives the return, so peak memory is one model
    (the max profile's embedder is ~2.2 GB)."""
    profile = ENGLISH_PROFILES["minimal"]
    built: list[weakref.ref[_FakeModel]] = []
    alive_at_each_build: list[int] = []

    def _track(model: _FakeModel) -> None:
        alive_at_each_build.append(sum(ref() is not None for ref in built))
        built.append(weakref.ref(model))

    def _graph_prewarm(_providers: list[str] | None) -> None:
        alive_at_each_build.append(sum(ref() is not None for ref in built))

    with patch("archon_search.install.prewarm._prewarm_graph_model", side_effect=_graph_prewarm), \
         patch.dict(
             sys.modules,
             _fastembed_modules(_fastembed_cls(on_build=_track), _fastembed_cls(on_build=_track)),
         ):
        _prewarm_models(_cfg_for(profile), _TIMEOUT_S, install_graph_extra=True)

    # embedder download, embedder load, reranker download, reranker load, graph model
    assert alive_at_each_build == [0, 0, 0, 0, 0]
    assert all(ref() is None for ref in built)


# ---------------------------------------------------------------------------
# _prewarm_models — fastembed mean-pooling UserWarning suppression (Bug B)
# ---------------------------------------------------------------------------

def _warning_embedder(message: str) -> MagicMock:
    """A TextEmbedding class whose constructor emits *message* as a UserWarning."""
    inner = _fastembed_cls()

    def _construct(*args: Any, **kwargs: Any) -> _FakeModel:
        warnings.warn(message, UserWarning, stacklevel=2)
        return inner(*args, **kwargs)

    return MagicMock(side_effect=_construct)


def test_prewarm_suppresses_mean_pooling_userwarning():
    """The mean-pooling UserWarning fired by TextEmbedding.__init__ must not surface."""
    profile = MULTILINGUAL_PROFILES["minimal"]
    mock_te = _warning_embedder("This model does not have CLS token; switching to mean pooling.")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with patch.dict(sys.modules, _fastembed_modules(mock_te, _fastembed_cls())):
            _prewarm_models(_cfg_for(profile), _TIMEOUT_S)

    assert mock_te.call_count == 2
    mean_pooling = [w for w in caught if "mean pooling" in str(w.message)]
    assert mean_pooling == [], "mean-pooling UserWarning leaked to wizard output"


def test_prewarm_does_not_suppress_unrelated_userwarning():
    """The filter is narrow: an unrelated UserWarning must still surface."""
    profile = MULTILINGUAL_PROFILES["minimal"]
    mock_te = _warning_embedder("some other actionable warning")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with patch.dict(sys.modules, _fastembed_modules(mock_te, _fastembed_cls())):
            _prewarm_models(_cfg_for(profile), _TIMEOUT_S)

    assert any("some other actionable warning" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# _prewarm_models — error paths: download vs load, embedder fatal, reranker not
# ---------------------------------------------------------------------------

def test_prewarm_embedder_download_failure_raises_naming_model_and_cause():
    """A failed resolve/download is fatal, says which phase failed, and stops before
    the reranker is touched."""
    profile = ENGLISH_PROFILES["minimal"]
    mock_tce = _fastembed_cls()
    mock_te = _fastembed_cls(download_error=RuntimeError("network error"))
    with patch.dict(sys.modules, _fastembed_modules(mock_te, mock_tce)):
        with pytest.raises(InstallError) as exc_info:
            _prewarm_models(_cfg_for(profile), _TIMEOUT_S)
    message = str(exc_info.value)
    assert f"Could not resolve or download the embedder model {profile.embedder!r}" in message
    assert "network error" in message
    mock_tce.assert_not_called()


@pytest.mark.parametrize(
    "load_failure",
    [{"session_error": RuntimeError(_LOAD_ERROR)}, {"inference_error": RuntimeError(_LOAD_ERROR)}],
    ids=["session-build", "first-inference"],
)
def test_prewarm_embedder_load_failure_raises_naming_model_and_cause(load_failure):
    """S671: the files download but onnxruntime cannot build a session (or run it).
    That must fail the install with the model and the cause, not surface at the first
    ingest — and the reranker is never touched."""
    profile = ENGLISH_PROFILES["minimal"]
    mock_tce = _fastembed_cls()
    with patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(**load_failure), mock_tce)):
        with pytest.raises(InstallError) as exc_info:
            _prewarm_models(_cfg_for(profile), _TIMEOUT_S)
    message = str(exc_info.value)
    assert f"The embedder model {profile.embedder!r} was downloaded but cannot be loaded" in message
    assert _LOAD_ERROR in message
    mock_tce.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [{"download_error": MemoryError()}, {"session_error": MemoryError()}],
    ids=["download", "load"],
)
def test_prewarm_failure_with_empty_exception_text_names_the_exception_type(failure):
    """An exception without a message (MemoryError()) still leaves a cause in the
    message: its type name, not an empty string after the colon."""
    profile = ENGLISH_PROFILES["minimal"]
    with patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(**failure), _fastembed_cls())):
        with pytest.raises(InstallError) as exc_info:
            _prewarm_models(_cfg_for(profile), _TIMEOUT_S)
    message = str(exc_info.value)
    assert profile.embedder in message
    assert message.endswith(": MemoryError"), message


def _reranker_warnings(caplog: pytest.LogCaptureFixture, mock_tce: MagicMock) -> list[str]:
    profile = ENGLISH_PROFILES["minimal"]
    with caplog.at_level(logging.WARNING, logger="archon_search.install"):
        with patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), mock_tce)):
            _prewarm_models(_cfg_for(profile), _TIMEOUT_S)  # must NOT raise
    return [r.getMessage() for r in caplog.records]


def test_prewarm_reranker_download_failure_logs_warning_not_raises(caplog):
    """S202: a reranker failure is non-fatal — the install continues and says what breaks."""
    reranker = ENGLISH_PROFILES["minimal"].reranker
    messages = _reranker_warnings(
        caplog, _fastembed_cls(download_error=RuntimeError("download failed"))
    )
    assert any(
        f"Could not resolve or download the reranker model {reranker!r}" in m
        and "download failed" in m
        and "/search skips reranking" in m
        for m in messages
    ), messages


@pytest.mark.parametrize(
    "load_failure",
    [{"session_error": RuntimeError(_LOAD_ERROR)}, {"inference_error": RuntimeError(_LOAD_ERROR)}],
    ids=["session-build", "first-inference"],
)
def test_prewarm_reranker_load_failure_logs_warning_naming_model_and_cause(caplog, load_failure):
    """A reranker that downloads but cannot load warns with the model and the cause,
    and the install continues (S202: e.g. CoreML rejecting the cross-encoder)."""
    reranker = ENGLISH_PROFILES["minimal"].reranker
    messages = _reranker_warnings(caplog, _fastembed_cls(**load_failure))
    assert any(
        f"The reranker model {reranker!r} was downloaded but cannot be loaded" in m
        and _LOAD_ERROR in m
        and "/explain with rerank fails until the model loads" in m
        for m in messages
    ), messages


def test_prewarm_reranker_failure_frees_its_model_and_still_prewarms_the_graph_model(caplog):
    """A failed reranker is non-fatal: the graph pre-warm still runs, and the reranker's
    model is released before it — the logged warning keeps no reference to it."""
    profile = ENGLISH_PROFILES["minimal"]
    built: list[weakref.ref[_FakeModel]] = []
    alive_at_graph_prewarm: list[int] = []

    def _graph_prewarm(_providers: list[str] | None) -> str:
        alive_at_graph_prewarm.append(sum(ref() is not None for ref in built))
        return "cpu"

    mock_tce = _fastembed_cls(
        inference_error=RuntimeError(_LOAD_ERROR), on_build=lambda m: built.append(weakref.ref(m))
    )
    with caplog.at_level(logging.WARNING, logger="archon_search.install"), \
         patch("archon_search.install.prewarm._prewarm_graph_model", side_effect=_graph_prewarm), \
         patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), mock_tce)):
        device = _prewarm_models(_cfg_for(profile), _TIMEOUT_S, install_graph_extra=True)

    assert device == "cpu"
    assert alive_at_graph_prewarm == [0]
    assert any("cannot be loaded" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _prewarm_models — deadline, checked between models
# ---------------------------------------------------------------------------

def _timeout_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "timed out after" in r.getMessage()]


def _expected_timeout_warning(skipped: str, pronoun: str) -> str:
    return (
        f"Model pre-warm timed out after {_TIMEOUT_S}s with the embedder ready — skipping the"
        f" {skipped}; the service downloads {pronoun} when it starts or first uses {pronoun}."
    )


@pytest.mark.parametrize(
    ("reranker_model", "install_graph_extra", "expected_warning"),
    [
        (None, True, _expected_timeout_warning("reranker and graph NER models", "them")),
        (None, False, _expected_timeout_warning("reranker model", "it")),
        ("", True, _expected_timeout_warning("graph NER model", "it")),
        ("", False, None),
    ],
    ids=["reranker-and-graph", "reranker-only", "graph-only", "nothing-pending"],
)
def test_prewarm_past_deadline_after_embedder_skips_and_names_the_remaining_models(
    caplog, reranker_model, install_graph_extra, expected_warning
):
    """A deadline passed while the embedder loads skips the models not yet started and
    names exactly those — no warning at all when none were pending."""
    profile = ENGLISH_PROFILES["minimal"]
    clock = _FakeClock()
    mock_te, mock_tce = _fastembed_cls(on_build=clock.pass_deadline), _fastembed_cls()
    graph_mock = MagicMock()
    cfg = _cfg_for(profile) if reranker_model is None else _cfg_for(profile, reranker_model=reranker_model)

    with caplog.at_level(logging.WARNING, logger="archon_search.install"), \
         patch(_CLOCK_TARGET, clock), \
         patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
         patch.dict(sys.modules, _fastembed_modules(mock_te, mock_tce)):
        device = _prewarm_models(cfg, _TIMEOUT_S, install_graph_extra=install_graph_extra)

    assert device is None
    assert mock_te.call_count == 2  # downloaded and loaded
    mock_tce.assert_not_called()
    graph_mock.assert_not_called()
    warnings_logged = _timeout_warnings(caplog)
    assert warnings_logged == ([] if expected_warning is None else [expected_warning])


def test_prewarm_past_deadline_after_reranker_skips_the_graph_model(caplog):
    """A deadline passed while the reranker loads still lets the reranker finish (a
    running load is never interrupted) and skips only the graph model, by name."""
    profile = ENGLISH_PROFILES["minimal"]
    clock = _FakeClock()
    mock_tce = _fastembed_cls(on_build=clock.pass_deadline)
    graph_mock = MagicMock()

    with caplog.at_level(logging.WARNING, logger="archon_search.install"), \
         patch(_CLOCK_TARGET, clock), \
         patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
         patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), mock_tce)):
        device = _prewarm_models(_cfg_for(profile), _TIMEOUT_S, install_graph_extra=True)

    assert device is None
    assert mock_tce.call_count == 2  # downloaded and loaded
    graph_mock.assert_not_called()
    assert _timeout_warnings(caplog) == [_expected_timeout_warning("graph NER model", "it")]


def test_prewarm_past_deadline_after_reranker_without_graph_finishes_silently(caplog):
    """A deadline passed while the last requested model (the reranker) loads skips
    nothing, so nothing warns."""
    profile = ENGLISH_PROFILES["minimal"]
    clock = _FakeClock()
    mock_tce = _fastembed_cls(on_build=clock.pass_deadline)
    graph_mock = MagicMock()

    with caplog.at_level(logging.WARNING, logger="archon_search.install"), \
         patch(_CLOCK_TARGET, clock), \
         patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
         patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), mock_tce)):
        device = _prewarm_models(_cfg_for(profile), _TIMEOUT_S, install_graph_extra=False)

    assert device is None
    assert mock_tce.call_count == 2  # downloaded and loaded
    graph_mock.assert_not_called()
    assert _timeout_warnings(caplog) == []


def test_prewarm_deadline_never_passed_runs_every_model(caplog):
    """Within the deadline every requested model is pre-warmed and nothing warns."""
    profile = ENGLISH_PROFILES["minimal"]
    mock_te, mock_tce = _fastembed_cls(), _fastembed_cls()
    graph_mock = MagicMock(return_value="cpu")

    with caplog.at_level(logging.WARNING, logger="archon_search.install"), \
         patch(_CLOCK_TARGET, _FakeClock()), \
         patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
         patch.dict(sys.modules, _fastembed_modules(mock_te, mock_tce)):
        device = _prewarm_models(_cfg_for(profile), _TIMEOUT_S, install_graph_extra=True)

    assert device == "cpu"
    assert (mock_te.call_count, mock_tce.call_count) == (2, 2)
    graph_mock.assert_called_once_with(None)
    assert _timeout_warnings(caplog) == []


# ---------------------------------------------------------------------------
# _prewarm_models — cache_dir matches runtime ModelEmbedder/ModelReranker (BE-26)
# ---------------------------------------------------------------------------

def test_archon_search_data_dir_relocates_embedder_and_reranker_caches(tmp_path, monkeypatch):
    """Redirecting ARCHON_SEARCH_DATA_DIR relocates get_models_dir() (and thus every cache_dir
    derived from it — see test_embedder.py / test_reranker.py / the prewarm tests above)."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

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
            device = _prewarm_graph_model()  # must not raise

    # Stage 2 rides this return: a failed load must yield None, not a device.
    assert device is None
    assert any("first use" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# _prewarm_graph_model wiring into _prewarm_models (Task BE-8, finding 2:
# _prewarm_graph_model was previously dead code, never called by anything)
# ---------------------------------------------------------------------------


def test_prewarm_models_invokes_graph_prewarm_when_requested():
    """install_graph_extra=True must actually dispatch _prewarm_graph_model as
    part of the same pre-warm call chain used by preload_models."""
    profile = ENGLISH_PROFILES["minimal"]
    graph_mock = MagicMock()

    with patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
         patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), _fastembed_cls())):
        _prewarm_models(_cfg_for(profile), _TIMEOUT_S, install_graph_extra=True)

    graph_mock.assert_called_once_with(None)


def test_prewarm_models_skips_graph_prewarm_when_not_requested():
    """install_graph_extra=False (the default) must never dispatch the graph pre-warm."""
    profile = ENGLISH_PROFILES["minimal"]
    graph_mock = MagicMock()

    with patch("archon_search.install.prewarm._prewarm_graph_model", graph_mock), \
         patch.dict(sys.modules, _fastembed_modules(_fastembed_cls(), _fastembed_cls())):
        _prewarm_models(_cfg_for(profile), _TIMEOUT_S)

    graph_mock.assert_not_called()
