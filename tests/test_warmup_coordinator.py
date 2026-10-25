"""Unit tests for ``WarmupCoordinator``, the single-flight warm-up shared by Embedder and Reranker."""
from __future__ import annotations

import asyncio
import errno
import gc
import logging

import pytest

from archon_search import warmup_coordinator
from archon_search.warmup_coordinator import WarmupCoordinator, WarmupTimeout

_SHORT_WAIT_SECONDS = 0.01
#: Loop iterations that let a released build fail and its done-callbacks run.
_SETTLE_ITERATIONS = 10


class _ProbeTimeout(WarmupTimeout):
    """Stands in for the per-model timeout classes."""


class _Build:
    """A controllable build: counts runs, parks on a gate, can fail or turn the target warm."""

    def __init__(self) -> None:
        self.runs = 0
        self.warm = False
        self.gate: asyncio.Event | None = None
        self.failures: list[BaseException] = []

    async def __call__(self) -> None:
        self.runs += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.failures:
            raise self.failures.pop(0)
        self.warm = True


def _coordinator(build: _Build) -> WarmupCoordinator:
    return WarmupCoordinator(
        subject="probe",
        is_warm=lambda: build.warm,
        build=build,
        timeout_error=_ProbeTimeout,
    )


async def test_warm_target_never_starts_a_build() -> None:
    build = _Build()
    build.warm = True

    await _coordinator(build).warmup()

    assert build.runs == 0


async def test_cold_warmup_builds_once_and_is_then_a_noop() -> None:
    build = _Build()
    coordinator = _coordinator(build)

    await coordinator.warmup()
    await coordinator.warmup()

    assert build.runs == 1


async def test_concurrent_callers_join_one_build() -> None:
    build = _Build()
    build.gate = asyncio.Event()
    coordinator = _coordinator(build)

    waiters = [asyncio.create_task(coordinator.warmup()) for _ in range(8)]
    await asyncio.sleep(0)
    build.gate.set()
    await asyncio.gather(*waiters)

    assert build.runs == 1


async def test_caller_cancellation_does_not_cancel_the_build() -> None:
    build = _Build()
    build.gate = asyncio.Event()
    coordinator = _coordinator(build)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(coordinator.warmup(), timeout=_SHORT_WAIT_SECONDS)
    assert build.runs == 1
    assert build.warm is False

    joiner = asyncio.create_task(coordinator.warmup())
    await asyncio.sleep(0)
    build.gate.set()
    await joiner

    assert build.runs == 1, "the later caller started a second build instead of joining"
    assert build.warm is True


async def test_failure_is_not_latched() -> None:
    build = _Build()
    build.failures.append(RuntimeError("transient"))
    coordinator = _coordinator(build)

    with pytest.raises(RuntimeError, match="transient"):
        await coordinator.warmup()
    assert build.warm is False

    await coordinator.warmup()

    assert build.runs == 2
    assert build.warm is True


async def test_build_failing_after_every_waiter_left_logs_no_unretrieved_exception() -> None:
    build = _Build()
    build.gate = asyncio.Event()
    build.failures.append(RuntimeError("nobody is listening"))
    reported: list[dict[str, object]] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: reported.append(context))
    coordinator = _coordinator(build)

    waiter = asyncio.create_task(coordinator.warmup())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    build.gate.set()
    for _ in range(_SETTLE_ITERATIONS):
        await asyncio.sleep(0)
    gc.collect()

    assert build.runs == 1
    assert reported == []


async def test_build_failing_after_every_waiter_left_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    build = _Build()
    build.gate = asyncio.Event()
    build.failures.append(RuntimeError("nobody is listening"))
    coordinator = _coordinator(build)

    with caplog.at_level(logging.WARNING, logger="archon_search.warmup_coordinator"):
        waiter = asyncio.create_task(coordinator.warmup())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        build.gate.set()
        for _ in range(_SETTLE_ITERATIONS):
            await asyncio.sleep(0)

    record = next(r for r in caplog.records if "probe" in r.getMessage())
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


async def test_build_failure_a_waiter_received_is_not_also_logged_by_the_coordinator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    build = _Build()
    build.failures.append(RuntimeError("transient"))
    coordinator = _coordinator(build)

    with caplog.at_level(logging.WARNING, logger="archon_search.warmup_coordinator"):
        with pytest.raises(RuntimeError, match="transient"):
            await coordinator.warmup()
        for _ in range(_SETTLE_ITERATIONS):
            await asyncio.sleep(0)

    assert caplog.records == []


async def test_timeout_raises_the_configured_exception_and_cancels_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(warmup_coordinator, "WARMUP_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS)
    build = _Build()
    build.gate = asyncio.Event()
    coordinator = _coordinator(build)

    with pytest.raises(_ProbeTimeout, match=rf"probe warm-up exceeded {_SHORT_WAIT_SECONDS}s") as caught:
        await coordinator.warmup()

    assert isinstance(caught.value.__cause__, TimeoutError)
    assert build.warm is False


def test_timeout_class_is_not_the_builtin_timeout() -> None:
    """Callers use ``TimeoutError`` for "I stopped waiting"; the ceiling must stay distinguishable."""
    assert issubclass(WarmupTimeout, Exception)
    assert not issubclass(WarmupTimeout, TimeoutError)
    assert not issubclass(WarmupTimeout, OSError)


async def test_socket_etimedout_inside_the_build_is_not_mistaken_for_the_ceiling() -> None:
    build = _Build()
    build.failures.append(TimeoutError(errno.ETIMEDOUT, "Connection timed out"))
    coordinator = _coordinator(build)

    with pytest.raises(TimeoutError) as caught:
        await coordinator.warmup()

    assert not isinstance(caught.value, WarmupTimeout)
    assert caught.value.errno == errno.ETIMEDOUT


def test_event_loop_change_cancels_the_stale_build_and_starts_a_fresh_one() -> None:
    build = _Build()
    coordinator = _coordinator(build)
    stale_loop = asyncio.new_event_loop()
    try:
        build.gate = asyncio.Event()
        with pytest.raises(TimeoutError):
            stale_loop.run_until_complete(asyncio.wait_for(coordinator.warmup(), timeout=_SHORT_WAIT_SECONDS))
        stale_task = coordinator._task
        assert stale_task is not None
        assert not stale_task.done()

        build.gate = None
        asyncio.run(coordinator.warmup())

        assert build.runs == 2, "a build bound to the old loop was reused"
        assert build.warm is True
        stale_loop.run_until_complete(asyncio.sleep(0))
        assert stale_task.cancelled()
    finally:
        stale_loop.close()


def test_event_loop_change_after_a_finished_build_just_rebinds() -> None:
    build = _Build()
    coordinator = _coordinator(build)
    asyncio.run(coordinator.warmup())
    build.warm = False

    asyncio.run(coordinator.warmup())

    assert build.runs == 2
