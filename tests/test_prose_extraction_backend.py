"""tests/test_prose_extraction_backend.py — unit tests for ProseExtractionBackend (Task BE-8).

gliner/torch are mocked via sys.modules injection — imports live inside load() per the task
spec, so patch.dict(sys.modules, ...) is required rather than patching an attribute.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
import types
from unittest.mock import MagicMock, patch

import pytest

from archon_search.paths import get_graph_models_dir
from archon_search.prose_extraction_backend import (
    _TORCH_INTER_OP_THREADS,
    _TORCH_INTRA_OP_THREADS,
    ProseExtractionBackend,
)


def _fake_torch_and_gliner(from_pretrained: MagicMock) -> tuple[types.ModuleType, types.ModuleType]:
    """Build fake `torch` and `gliner` modules for sys.modules injection."""
    torch_mod = types.ModuleType("torch")
    torch_mod.set_num_threads = MagicMock()  # type: ignore[attr-defined]
    torch_mod.set_num_interop_threads = MagicMock()  # type: ignore[attr-defined]

    gliner_mod = types.ModuleType("gliner")
    gliner_cls = MagicMock()
    gliner_cls.from_pretrained = from_pretrained
    gliner_mod.GLiNER = gliner_cls  # type: ignore[attr-defined]
    return torch_mod, gliner_mod


def _fake_torch_with_availability(
    cuda_available: bool = False, mps_available: bool = False
) -> types.ModuleType:
    """Build a fake `torch` module exposing only the availability checks
    `_resolve_device` needs — no `set_num_threads`/GLiNER wiring."""
    torch_mod = types.ModuleType("torch")
    torch_mod.cuda = types.SimpleNamespace(  # type: ignore[attr-defined]
        is_available=MagicMock(return_value=cuda_available)
    )
    torch_mod.backends = types.SimpleNamespace(  # type: ignore[attr-defined]
        mps=types.SimpleNamespace(is_available=MagicMock(return_value=mps_available))
    )
    return torch_mod


def _model_with_to() -> MagicMock:
    model = MagicMock()
    model.to.return_value = model
    return model


# ---------------------------------------------------------------------------
# test_resolve_device_* — real [graph].providers ONNX vocabulary (finding 4)
# ---------------------------------------------------------------------------

def test_resolve_device_cpu_when_no_providers() -> None:
    backend = ProseExtractionBackend()
    assert backend._resolve_device() == "cpu"


def test_resolve_device_uses_onnx_provider_vocabulary() -> None:
    """[graph].providers values are ONNX provider strings (archon-search.toml.example's
    [database].providers vocabulary), not literal 'cuda'/'mps'."""
    cuda_backend = ProseExtractionBackend(providers=["CUDAExecutionProvider"])
    with patch.dict(sys.modules, {"torch": _fake_torch_with_availability(cuda_available=True)}):
        assert cuda_backend._resolve_device() == "cuda"

    mps_backend = ProseExtractionBackend(providers=["CoreMLExecutionProvider"])
    with patch.dict(sys.modules, {"torch": _fake_torch_with_availability(mps_available=True)}):
        assert mps_backend._resolve_device() == "mps"


def test_resolve_device_steps_down_to_cpu_when_device_unavailable(caplog) -> None:
    """An unavailable device steps down to CPU with a WARNING logged, never raises —
    a stale/misconfigured providers list must not latch a permanent failure."""
    cuda_backend = ProseExtractionBackend(providers=["CUDAExecutionProvider"])
    with patch.dict(sys.modules, {"torch": _fake_torch_with_availability(cuda_available=False)}):
        with caplog.at_level(logging.WARNING, logger="archon_search.prose_extraction_backend"):
            assert cuda_backend._resolve_device() == "cpu"
    assert any("CUDA" in r.message for r in caplog.records)

    caplog.clear()
    mps_backend = ProseExtractionBackend(providers=["CoreMLExecutionProvider"])
    with patch.dict(sys.modules, {"torch": _fake_torch_with_availability(mps_available=False)}):
        with caplog.at_level(logging.WARNING, logger="archon_search.prose_extraction_backend"):
            assert mps_backend._resolve_device() == "cpu"
    assert any("CoreML" in r.message or "MPS" in r.message for r in caplog.records)


def test_providers_never_falls_back_to_embedder_or_reranker_config() -> None:
    """_resolve_device must only ever consult self._providers — never the embedder's
    or reranker's own provider config (archon_search.config.resolve_reranker_providers)."""
    mock_resolve = MagicMock(side_effect=AssertionError("must not be called"))
    with patch("archon_search.config.resolve_reranker_providers", mock_resolve):
        backend = ProseExtractionBackend()
        assert backend._resolve_device() == "cpu"
        assert backend._providers is None
    mock_resolve.assert_not_called()


# ---------------------------------------------------------------------------
# test_unloadable_artifact_latches_once_per_process
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unloadable_artifact_latches_once_per_process() -> None:
    from_pretrained = MagicMock(side_effect=RuntimeError("boom"))
    torch_mod, gliner_mod = _fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with pytest.raises(RuntimeError):
            await backend.load()
        with pytest.raises(RuntimeError):
            await backend.load()

    assert from_pretrained.call_count == 1


@pytest.mark.asyncio
async def test_load_failure_logs_warning_before_latching(caplog) -> None:
    """A failed load attempt must be logged at WARNING before the exception latches."""
    from_pretrained = MagicMock(side_effect=RuntimeError("boom"))
    torch_mod, gliner_mod = _fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with caplog.at_level(logging.WARNING, logger="archon_search.prose_extraction_backend"):
            with pytest.raises(RuntimeError):
                await backend.load()

    assert any("boom" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# test_interop_thread_runtime_error_does_not_latch (finding 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_interop_thread_runtime_error_does_not_latch() -> None:
    """torch.set_num_interop_threads/set_num_threads may only be called once per
    process — a RuntimeError from either must not poison the load-exception latch."""
    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = _fake_torch_and_gliner(from_pretrained)
    torch_mod.set_num_interop_threads.side_effect = RuntimeError(
        "Error: cannot set number of interop threads after parallel work has started"
    )
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        await backend.load()  # must not raise

    assert backend.is_loaded is True
    from_pretrained.assert_called_once()


# ---------------------------------------------------------------------------
# test_load_times_out (finding 6)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_times_out_and_latches(monkeypatch, caplog) -> None:
    """A stalled GLiNER.from_pretrained() must not hang load() forever — it times
    out, logs, and latches (a second call raises without retrying)."""
    import archon_search.prose_extraction_backend as pe_mod

    monkeypatch.setattr(pe_mod, "_LOAD_TIMEOUT_SECONDS", 0.05)

    def _slow_from_pretrained(*args, **kwargs):
        time.sleep(0.3)
        return _model_with_to()

    from_pretrained = MagicMock(side_effect=_slow_from_pretrained)
    torch_mod, gliner_mod = _fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with caplog.at_level(logging.WARNING, logger="archon_search.prose_extraction_backend"):
            with pytest.raises(TimeoutError):
                await backend.load()
        with pytest.raises(TimeoutError):
            await backend.load()  # latched — no retry

    assert any("Failed to load graph NER model" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# test_cancellation_during_load_reraises_without_latching (finding 7)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancellation_during_load_reraises_without_latching() -> None:
    """Cancel a task while it is genuinely awaiting the dispatched load work (not a
    synchronous mock raise before any dispatch) — must not latch, and a subsequent
    call must actually retry (re-dispatch, not just re-raise the right type)."""
    backend = ProseExtractionBackend()

    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = _fake_torch_and_gliner(from_pretrained)

    dispatch_started = asyncio.Event()
    never_release = asyncio.Event()

    async def _blocking_to_thread(func, /, *args, **kwargs):
        dispatch_started.set()
        await never_release.wait()  # cancellation must interrupt this await
        return func(*args, **kwargs)

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with patch("archon_search.prose_extraction_backend.asyncio.to_thread", _blocking_to_thread):
            task = asyncio.create_task(backend.load())
            await dispatch_started.wait()  # genuinely inside the dispatched work
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Not latched, and the cancelled dispatch never reached the real load work.
        assert from_pretrained.call_count == 0
        assert backend.is_loaded is False

        # A subsequent (non-cancelled) call retries — genuine re-dispatch, not just
        # the right exception type.
        await backend.load()

    assert from_pretrained.call_count == 1
    assert backend.is_loaded is True


# ---------------------------------------------------------------------------
# test_load_passes_cache_dir_from_get_graph_models_dir
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_passes_cache_dir_from_get_graph_models_dir() -> None:
    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = _fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        await backend.load()

    _, kwargs = from_pretrained.call_args
    assert kwargs["cache_dir"] == str(get_graph_models_dir())


@pytest.mark.asyncio
async def test_load_pins_torch_thread_counts() -> None:
    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = _fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        await backend.load()

    torch_mod.set_num_threads.assert_called_once_with(_TORCH_INTRA_OP_THREADS)
    torch_mod.set_num_interop_threads.assert_called_once_with(_TORCH_INTER_OP_THREADS)


# ---------------------------------------------------------------------------
# test_concurrent_extractions_load_the_model_once (integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_extractions_load_the_model_once() -> None:
    """Proves the asyncio.Lock single-flight, not just the inner threading.Lock
    double-check: asserts asyncio.to_thread was dispatched exactly once across N
    concurrent callers, not merely that the model constructor was called once."""
    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = _fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    to_thread_calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _tracking_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append(threading_name())
        return await real_to_thread(func, *args, **kwargs)

    def threading_name() -> str:
        import threading

        return threading.current_thread().name

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with patch("archon_search.prose_extraction_backend.asyncio.to_thread", _tracking_to_thread):
            await asyncio.gather(*(backend.load() for _ in range(8)))

    assert from_pretrained.call_count == 1
    assert len(to_thread_calls) == 1, (
        f"expected exactly one asyncio.to_thread dispatch across 8 concurrent "
        f"callers, got {len(to_thread_calls)}"
    )


# ---------------------------------------------------------------------------
# test_module_import_does_not_pull_in_ml_libraries (finding 7, last bullet)
# ---------------------------------------------------------------------------

def test_module_import_does_not_pull_in_ml_libraries() -> None:
    """Importing the module must not eagerly import gliner/torch/transformers —
    those live behind load()'s function-local imports. Runs in a subprocess for a
    clean sys.modules, since this file's own tests inject fakes via patch.dict."""
    script = (
        "import sys\n"
        "import archon_search.prose_extraction_backend\n"
        "assert 'gliner' not in sys.modules, 'gliner'\n"
        "assert 'torch' not in sys.modules, 'torch'\n"
        "assert 'transformers' not in sys.modules, 'transformers'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
