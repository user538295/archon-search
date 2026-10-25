"""tests/test_prose_extraction_backend.py — unit tests for ProseExtractionBackend (Task BE-8).

gliner/torch are mocked via sys.modules injection — imports live inside load() per the task
spec, so patch.dict(sys.modules, ...) is required rather than patching an attribute.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import subprocess
import sys
import threading
import time
import traceback
import types
import warnings
import weakref
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock, patch

import pytest

from archon_search import torch_runtime
from archon_search.paths import GRAPH_NER_MODEL_REVISION, get_graph_models_dir
from archon_search.prose_extraction_backend import (
    ProseExtractionBackend,
    ProseExtractionLoadTimeoutError,
    _LatchedLoadFailure,
)
from tests._graph_engine_stub import fake_torch_and_gliner, seed_graph_model_snapshot
from tests._load_single_flight_probe import (
    LoadSingleFlightProbe,
    gated_failing_from_pretrained,
)


@pytest.fixture(autouse=True)
def _graph_model_snapshot_is_cached() -> None:
    """The loader downloads the checkpoint before handing gliner a directory; a seeded snapshot is a cache hit."""
    seed_graph_model_snapshot()


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
# test_resolve_device_* — [graph].device vocabulary (auto|cpu|mps|cuda)
# ---------------------------------------------------------------------------

def test_resolve_device_cpu_by_default() -> None:
    backend = ProseExtractionBackend()
    assert backend._resolve_device() == "cpu"


def test_resolve_device_uses_torch_device_names() -> None:
    cuda_backend = ProseExtractionBackend(device="cuda")
    with patch.dict(sys.modules, {"torch": _fake_torch_with_availability(cuda_available=True)}):
        assert cuda_backend._resolve_device() == "cuda"

    mps_backend = ProseExtractionBackend(device="mps")
    with patch.dict(sys.modules, {"torch": _fake_torch_with_availability(mps_available=True)}):
        assert mps_backend._resolve_device() == "mps"


def test_resolve_device_auto_picks_the_available_accelerator() -> None:
    backend = ProseExtractionBackend(device="auto")
    with patch.dict(sys.modules, {"torch": _fake_torch_with_availability(mps_available=True)}):
        assert backend._resolve_device() == "mps"
    with patch.dict(sys.modules, {"torch": _fake_torch_with_availability()}):
        assert backend._resolve_device() == "cpu"


def test_resolve_device_steps_down_to_cpu_when_device_unavailable(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable accelerator steps down to CPU with a WARNING logged, never raises —
    a stale/misconfigured device must not latch a permanent failure."""
    monkeypatch.setattr(torch_runtime, "_warned_settings", set())
    cuda_backend = ProseExtractionBackend(device="cuda")
    with (
        patch.dict(sys.modules, {"torch": _fake_torch_with_availability(cuda_available=False)}),
        caplog.at_level(logging.WARNING, logger="archon_search.torch_runtime"),
    ):
        assert cuda_backend._resolve_device() == "cpu"
    assert [r.getMessage() for r in caplog.records] == ["device 'cuda' is not available on this host; using cpu"]

    caplog.clear()
    mps_backend = ProseExtractionBackend(device="mps")
    with (
        patch.dict(sys.modules, {"torch": _fake_torch_with_availability(mps_available=False)}),
        caplog.at_level(logging.WARNING, logger="archon_search.torch_runtime"),
    ):
        assert mps_backend._resolve_device() == "cpu"
    assert [r.getMessage() for r in caplog.records] == ["device 'mps' is not available on this host; using cpu"]


def test_graph_extractor_hands_the_graph_device_to_its_backend() -> None:
    """[graph].device — never [database].device — reaches the engine."""
    from archon_search import graph_extractor
    from archon_search.config import GraphConfig

    with patch.object(graph_extractor, "ProseExtractionBackend") as backend_cls:
        graph_extractor.GraphExtractor(GraphConfig(device="mps"))

    backend_cls.assert_called_once_with(device="mps")


# ---------------------------------------------------------------------------
# test_unloadable_artifact_latches_once_per_process
# ---------------------------------------------------------------------------

# The loader-side frame every latched re-raise's traceback must still reach.
_LOADER_FRAME_NAME = "_load_sync"
# The frame only a latched re-raise (never the loader's own raise) passes through.
_LATCHED_RERAISE_FRAME_NAME = _LatchedLoadFailure.reraise.__name__
_LATCHED_RELOAD_CALLS = 5
_CONCURRENT_LOAD_CALLERS = 4


@pytest.mark.asyncio
async def test_unloadable_artifact_latches_once_per_process() -> None:
    """A latched failure is re-raised as the IDENTICAL exception object on every
    later call — the contract ``GraphExtractor`` keys its once-per-process
    load-failure WARNING on (S612)."""
    from_pretrained = MagicMock(side_effect=RuntimeError("boom"))
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with pytest.raises(RuntimeError) as first:
            await backend.load()
        latched: list[BaseException] = []
        for _ in range(_LATCHED_RELOAD_CALLS):
            with pytest.raises(RuntimeError) as caught:
                await backend.load()
            latched.append(caught.value)

    assert from_pretrained.call_count == 1
    assert all(exc is first.value for exc in latched), (
        "a latched load() must re-raise the first failure's exact instance"
    )


def _frame_names(exc: BaseException) -> list[str]:
    return [frame.name for frame in traceback.extract_tb(exc.__traceback__)]


@pytest.mark.asyncio
async def test_repeated_failed_loads_do_not_retain_earlier_callers_frames() -> None:
    """S612: every later load() re-raises the latched failure. Each caller must
    receive the same-depth traceback that still reaches the original failing
    loader frame — re-raising the one cached instance as-is prepends the
    caller's frames to its ``__traceback__`` on every call, so the traceback
    logged via ``exc_info`` grows per prose document and pins every earlier
    caller's frames (and their locals) for the life of the process; dropping
    the traceback instead would lose the diagnosis."""
    from_pretrained = MagicMock(side_effect=RuntimeError("boom"))
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with pytest.raises(RuntimeError):
            await backend.load()  # the genuine (latching) load attempt
        latched_frame_names: list[list[str]] = []
        for _ in range(_LATCHED_RELOAD_CALLS):
            with pytest.raises(RuntimeError) as caught:
                await backend.load()
            latched_frame_names.append(_frame_names(caught.value))

    assert from_pretrained.call_count == 1
    depths = [len(names) for names in latched_frame_names]
    assert all(depth == depths[0] for depth in depths), (
        f"traceback depth grows per latched call: {depths}"
    )
    for names in latched_frame_names:
        assert _LOADER_FRAME_NAME in names, f"original loader frame lost: {names}"


@pytest.mark.asyncio
async def test_concurrent_callers_racing_a_failing_first_load_share_one_attempt() -> None:
    """S612: concurrent callers racing the FIRST, failing load — one loader,
    the rest genuine waiters (proven blocked on the in-flight load before it
    fails) that loop back and re-raise the latch — all raise the same failure
    from a single load attempt, each with a traceback that still reaches the
    original loader frame."""
    release = threading.Event()
    from_pretrained = gated_failing_from_pretrained(release, lambda: RuntimeError("boom"))
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()
    probe = LoadSingleFlightProbe()

    async def _load_and_capture_frames() -> list[str]:
        # Capture at catch time: every caller shares ONE exception instance,
        # whose `__traceback__` the next caller's re-raise replaces.
        with pytest.raises(RuntimeError) as caught:
            await backend.load()
        return _frame_names(caught.value)

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}), probe.tracking():
        callers = [
            asyncio.create_task(_load_and_capture_frames())
            for _ in range(_CONCURRENT_LOAD_CALLERS)
        ]
        try:
            await probe.until_blocked(waiters=_CONCURRENT_LOAD_CALLERS - 1)
        finally:
            release.set()  # let the one in-flight load fail
            # Never leave a task pending if until_blocked() raised.
            await asyncio.gather(*callers, return_exceptions=True)
        frame_names_per_caller = await asyncio.gather(*callers)

    assert from_pretrained.call_count == 1
    assert probe.loader_dispatches == 1
    assert probe.waiter_registrations == _CONCURRENT_LOAD_CALLERS - 1
    for names in frame_names_per_caller:
        assert _LOADER_FRAME_NAME in names, f"original loader frame lost: {names}"
    # Waiters re-raise the latch; their tracebacks must not grow per re-raise.
    waiter_depths = [
        len(names) for names in frame_names_per_caller if _LATCHED_RERAISE_FRAME_NAME in names
    ]
    assert len(waiter_depths) == _CONCURRENT_LOAD_CALLERS - 1
    assert len(set(waiter_depths)) == 1, f"latched re-raise traceback grows: {waiter_depths}"


class _DevicePlacementFailingModel:
    """A constructed model whose ``.to(device)`` fails — e.g. CUDA OOM."""

    def to(self, _device: str) -> NoReturn:
        raise RuntimeError("CUDA out of memory")


@pytest.mark.asyncio
async def test_latched_load_failure_does_not_pin_the_constructed_model() -> None:
    """S612: a load that constructs the model but fails at ``.to(device)`` latches
    the failure for the process lifetime. The latched traceback must not keep the
    already-built (~1.2 GB) model alive through ``_load_sync``'s frame locals,
    yet must still format with the original ``_load_sync`` frame for diagnosis."""
    model_refs: list[weakref.ref[_DevicePlacementFailingModel]] = []

    def _from_pretrained(*_args: object, **_kwargs: object) -> _DevicePlacementFailingModel:
        model = _DevicePlacementFailingModel()
        model_refs.append(weakref.ref(model))
        return model

    torch_mod, gliner_mod = fake_torch_and_gliner(MagicMock(side_effect=_from_pretrained))
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with pytest.raises(RuntimeError):
            await backend.load()
        with pytest.raises(RuntimeError) as latched:
            await backend.load()

    gc.collect()
    (model_ref,) = model_refs
    assert model_ref() is None, "the latched failure pins the failed load's model"
    assert _LOADER_FRAME_NAME in _frame_names(latched.value)
    assert _LOADER_FRAME_NAME in "".join(traceback.format_exception(latched.value))


@pytest.mark.asyncio
async def test_load_failure_logs_warning_before_latching(caplog) -> None:
    """A failed load attempt must be logged at WARNING before the exception latches."""
    from_pretrained = MagicMock(side_effect=RuntimeError("boom"))
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with (
        patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}),
        caplog.at_level(logging.WARNING, logger="archon_search.prose_extraction_backend"),
        pytest.raises(RuntimeError),
    ):
        await backend.load()

    assert any("boom" in r.message for r in caplog.records)


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
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        with caplog.at_level(logging.WARNING, logger="archon_search.prose_extraction_backend"), pytest.raises(TimeoutError):
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
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)

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
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        await backend.load()

    _, kwargs = from_pretrained.call_args
    assert kwargs["cache_dir"] == str(get_graph_models_dir())


# ---------------------------------------------------------------------------
# Third-party deprecation warnings raised by the model load
# ---------------------------------------------------------------------------

_TORCH_JIT_SCRIPT_MESSAGE = (
    "`torch.jit.script` is deprecated. Please switch to `torch.compile` or `torch.export`."
)


def _install_importable_fake_gliner(root: Path, import_time_code: str) -> None:
    """Write a ``gliner`` package under ``root`` whose import runs ``import_time_code``.

    A real file, not a ``sys.modules`` entry: the warning under test is raised while
    ``gliner`` is being imported, so the import machinery has to execute the module.
    """
    package = root / "gliner"
    package.mkdir()
    (package / "__init__.py").write_text(
        "import warnings\n"
        f"{import_time_code}\n"
        "class _Model:\n"
        "    def to(self, device):\n"
        "        return self\n"
        "class GLiNER:\n"
        "    @staticmethod\n"
        "    def from_pretrained(*args, **kwargs):\n"
        "        return _Model()\n"
    )


@pytest.mark.asyncio
async def test_load_hands_gliner_the_local_snapshot_and_forbids_any_fetch() -> None:
    """gliner loads the pinned snapshot directory with ``local_files_only``: it is given no repo id or revision it
    could resolve against the hub, so it can fetch nothing (no whole-repo download, no unpinned backbone)."""
    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        await backend.load()

    (model_id,), kwargs = from_pretrained.call_args
    assert Path(model_id).is_dir()
    assert Path(model_id).parent.name == "snapshots"
    assert Path(model_id).name == GRAPH_NER_MODEL_REVISION
    assert kwargs["local_files_only"] is True
    assert "revision" not in kwargs
    assert "resume_download" not in kwargs
    assert backend.is_loaded is True


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize(
    "category",
    [
        pytest.param("DeprecationWarning", id="torch-2.13"),
        pytest.param("FutureWarning", id="torch-2.14"),
    ],
)
async def test_load_silences_the_torch_jit_script_deprecation_raised_by_the_gliner_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, category: str
) -> None:
    """Importing gliner imports transformers' DeBERTa-v2 modeling module, whose
    `@torch.jit.script` decorators warn at import time — an upstream deprecation neither
    archon-search nor its users can act on."""
    _install_importable_fake_gliner(
        tmp_path, f"warnings.warn({_TORCH_JIT_SCRIPT_MESSAGE!r}, {category})"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    torch_mod, _ = fake_torch_and_gliner(MagicMock())
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod}):
        sys.modules.pop("gliner", None)
        await backend.load()
        assert sys.modules["gliner"].__file__.startswith(str(tmp_path))

    assert backend.is_loaded is True


@pytest.mark.asyncio
async def test_load_does_not_leak_the_jit_script_silencing_past_the_gliner_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ignore filter must be undone once gliner is imported, not left process-wide.
    Inspects ``warnings.filters`` directly: ``pytest.warns`` prepends an 'always' filter,
    which would mask a leaked 'ignore' entry and let a permanent filter pass."""
    _install_importable_fake_gliner(
        tmp_path, f"warnings.warn({_TORCH_JIT_SCRIPT_MESSAGE!r}, FutureWarning)"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    torch_mod, _ = fake_torch_and_gliner(MagicMock())
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod}):
        sys.modules.pop("gliner", None)
        await backend.load()

    leaked = [
        entry
        for entry in warnings.filters
        if entry[1] is not None and entry[1].match(_TORCH_JIT_SCRIPT_MESSAGE)
    ]
    assert leaked == []


@pytest.mark.asyncio
async def test_load_keeps_unrelated_warnings_raised_by_the_gliner_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The jit-script silencing is scoped to that one message: any other warning the
    gliner import raises still reaches the caller."""
    _install_importable_fake_gliner(
        tmp_path, "warnings.warn('an unrelated gliner deprecation', FutureWarning)"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    torch_mod, _ = fake_torch_and_gliner(MagicMock())
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod}):
        sys.modules.pop("gliner", None)
        with pytest.warns(FutureWarning, match="an unrelated gliner deprecation"):
            await backend.load()


@pytest.mark.asyncio
async def test_loading_the_graph_model_does_not_change_torch_thread_count() -> None:
    """The graph model shares the process with the embedder and reranker: a process-global
    ``torch.set_num_threads(1)`` pin made PyTorch CPU embedding 3x slower."""
    torch = pytest.importorskip("torch")
    _, gliner_mod = fake_torch_and_gliner(MagicMock(return_value=_model_with_to()))
    backend = ProseExtractionBackend()
    threads_before = (torch.get_num_threads(), torch.get_num_interop_threads())

    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        await backend.load()

    assert backend.is_loaded is True
    assert (torch.get_num_threads(), torch.get_num_interop_threads()) == threads_before


# ---------------------------------------------------------------------------
# test_concurrent_extractions_load_the_model_once (integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_extractions_load_the_model_once() -> None:
    """Proves the asyncio.Lock single-flight, not just the inner threading.Lock
    double-check: asserts asyncio.to_thread was dispatched exactly once across N
    concurrent callers, not merely that the model constructor was called once."""
    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    to_thread_calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _tracking_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append(threading_name())
        return await real_to_thread(func, *args, **kwargs)

    def threading_name() -> str:
        import threading

        return threading.current_thread().name

    with (
        patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}),
        patch("archon_search.prose_extraction_backend.asyncio.to_thread", _tracking_to_thread),
    ):
        await asyncio.gather(*(backend.load() for _ in range(8)))

    assert from_pretrained.call_count == 1
    assert len(to_thread_calls) == 1, (
        f"expected exactly one asyncio.to_thread dispatch across 8 concurrent "
        f"callers, got {len(to_thread_calls)}"
    )


# ---------------------------------------------------------------------------
# test_loader_cancellation_frees_a_concurrent_waiter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loader_cancellation_frees_a_concurrent_waiter(
    monkeypatch,
) -> None:
    """Cancelling the LOADER while a WAITER is genuinely blocked on the same
    ``_load_event`` must unblock the waiter (not wedge it forever), clear
    ``_load_event``, and let the waiter become the next loader itself.

    This is NOT a regression test for a reachable wedge: the loader's
    ``finally`` block clears ``_load_event`` and calls ``event.set()`` as a
    lock-free, non-await synchronous pair (see the comment in ``load()``'s
    ``finally`` block) — a defensive simplification, not a fix for an
    actively-reachable bug, since the ``_load_lock`` critical section it once
    also acquired here contains no await point and so could never actually
    be contended across a suspension point. What this test DOES prove: a
    cancelled loader's ``finally`` still runs to completion and correctly
    wakes/frees a waiting waiter, which then becomes the next loader and
    succeeds.
    """
    monkeypatch.setattr(
        "archon_search.prose_extraction_backend._LOAD_WAIT_TIMEOUT_SECONDS", 5.0
    )

    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    dispatch_started = asyncio.Event()
    never_release = asyncio.Event()

    async def _blocking_to_thread(func, /, *args, **kwargs):
        dispatch_started.set()
        await never_release.wait()  # cancellation must interrupt this await
        return func(*args, **kwargs)

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}), patch(
        "archon_search.prose_extraction_backend.asyncio.to_thread",
        _blocking_to_thread,
    ):
        loader_task = asyncio.create_task(backend.load())
        await dispatch_started.wait()  # loader genuinely in flight

        waiter_task = asyncio.create_task(backend.load())
        await asyncio.sleep(0)  # let the waiter register on the event

        loader_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loader_task

        # Let the waiter's own re-dispatch (it becomes the next loader)
        # complete immediately instead of blocking forever on the same
        # mock — it must be free to become the new loader.
        never_release.set()

        # Not wedged: the waiter must have woken up (event.set() ran) and
        # _load_event cleared, letting it become the next loader and
        # succeed — not hang for the full wait timeout.
        await asyncio.wait_for(waiter_task, timeout=5)

    assert backend.is_loaded is True
    assert backend._load_event is None
    assert from_pretrained.call_count == 1


# ---------------------------------------------------------------------------
# test_wait_timeout_degrades_and_leaves_loader_bookkeeping_untouched (BE-12, S46)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_timeout_degrades_and_leaves_loader_bookkeeping_untouched(
    monkeypatch,
) -> None:
    """A genuine two-coroutine race: task A is the real LOADER (blocked mid
    ``_load_sync`` via a controllable event, not a hand-assigned
    ``_load_event`` with no loader in flight), task B lands concurrently as
    a WAITER and times out on a short bounded wait.

    Asserts: (1) B raises ``ProseExtractionLoadTimeoutError`` without
    touching A's bookkeeping; (2) once A's load completes, ``is_loaded`` is
    True and a THIRD, subsequent ``load()`` call returns cleanly using the
    now-loaded model — proving the loader was genuinely unaffected by B's
    timeout (also exercises the finding-A fix: the loader's own ``finally``
    still runs its lock-free clear + ``event.set()`` normally here).
    """
    import archon_search.prose_extraction_backend as pe_mod

    monkeypatch.setattr(pe_mod, "_LOAD_WAIT_TIMEOUT_SECONDS", 0.01)

    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    dispatch_started = asyncio.Event()
    release_loader = asyncio.Event()

    async def _blocking_to_thread(func, /, *args, **kwargs):
        dispatch_started.set()
        await release_loader.wait()  # holds the loader in flight for B to time out on
        return func(*args, **kwargs)

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}), patch(
        "archon_search.prose_extraction_backend.asyncio.to_thread",
        _blocking_to_thread,
    ):
        loader_task = asyncio.create_task(backend.load())
        await dispatch_started.wait()  # A is genuinely the in-flight loader
        in_flight_event = backend._load_event
        assert in_flight_event is not None

        with pytest.raises(ProseExtractionLoadTimeoutError):
            await backend.load()  # B: waits, times out

        # B must not have mutated any of A's (the loader's) own state.
        assert backend._model is None
        assert backend._latched_load_failure is None
        assert backend._load_event is in_flight_event
        assert not in_flight_event.is_set()

        # Let A finish its load.
        release_loader.set()
        await loader_task

    assert backend.is_loaded is True
    assert backend._load_event is None
    assert from_pretrained.call_count == 1

    # A third, subsequent call must return cleanly using the loaded model —
    # no re-dispatch, no lingering effect from B's timeout.
    await backend.load()
    assert from_pretrained.call_count == 1


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
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# load_count — C1 `EngineCapability.loadCount` (Task T-8, S26/S48)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_count_counts_only_real_model_constructions() -> None:
    """`load_count` starts at 0, reaches 1 on a real construction, and stays 1 across
    repeated `load()` calls — the real-artifact lane asserts `== 1` to prove the load path
    executed, so a counter that also ticked on a no-op re-`load()` would not discriminate."""
    from_pretrained = MagicMock(return_value=_model_with_to())
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    assert backend.load_count == 0

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        await backend.load()
        assert backend.load_count == 1
        await backend.load()

    assert backend.load_count == 1
    assert from_pretrained.call_count == 1


@pytest.mark.asyncio
async def test_load_count_stays_zero_when_construction_fails() -> None:
    """A failed load must NOT increment `load_count`. The lane's non-vacuity assert reads
    `== 1` as "the model really was built"; counting attempts instead would let a degraded
    run — the exact silent no-op the assert exists to catch — report a successful load."""
    from_pretrained = MagicMock(side_effect=RuntimeError("boom"))
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}), pytest.raises(RuntimeError):
        await backend.load()

    assert backend.load_count == 0
    assert backend.is_loaded is False


@pytest.mark.asyncio
async def test_load_count_stays_zero_when_device_placement_fails() -> None:
    """A `from_pretrained` that succeeds but whose `.to(device)` call raises must also
    NOT increment `load_count` — the increment sits after `.to()` specifically so a
    device-placement failure (e.g. a CUDA OOM) can never leave `load_count == 1` while
    `is_loaded` is False, which would contradict the property's own "the model really
    was built" contract."""
    model = _model_with_to()
    model.to.side_effect = RuntimeError("device placement failed")
    from_pretrained = MagicMock(return_value=model)
    torch_mod, gliner_mod = fake_torch_and_gliner(from_pretrained)
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}), pytest.raises(RuntimeError):
        await backend.load()

    assert backend.load_count == 0
    assert backend.is_loaded is False
