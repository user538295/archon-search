"""BE-14 (collection-descriptions plan, C1, S64): the fixed-window rate limiter of the shared LLM client layer.

Semantics ported from the limiter inside the removed Anthropic enrichment adapter (N requests per 60 s window, the window opens when
the limiter is built, a burst is possible at a window boundary, an exhausted limiter raises instead of waiting) and
applied by the ``anthropic`` and ``claude_cli`` wrappers when the graph composition hands them one. The clock is injected,
so no test sleeps.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from archon_search.llm_client import claude_cli_client
from archon_search.llm_client.anthropic_client import AnthropicClient
from archon_search.llm_client.claude_cli_client import ClaudeCliClient
from archon_search.llm_client.protocol import (
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
)
from archon_search.llm_client.rate_limit import (
    RATE_LIMIT_WINDOW_SECONDS,
    FixedWindowRateLimiter,
)
from tests import _fake_claude

_CAPACITY = 3
_PROVIDER = "anthropic"
_PROMPT = "summarise this community"
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is an sh launcher")
_MODEL = "test-model"
_REPLY = "a summary"
_TIMEOUT_SECONDS = 30.0
_MAX_OUTPUT_TOKENS = 1024
_THREADS = 16
_ATTEMPTS_PER_THREAD = 10
_CLOCK_PAUSE_SECONDS = 0.0005


_START_TIME = 1000.0
_ONE_SECOND = 1.0
_JUST_SHORT_SECONDS = 0.001
_REFILL_DELAY_SECONDS = 25.0


class _Clock:
    """A settable monotonic clock."""

    def __init__(self, now: float = _START_TIME) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(clock: _Clock, capacity: int = _CAPACITY) -> FixedWindowRateLimiter:
    return FixedWindowRateLimiter(capacity, clock=clock)


def _request(prompt: str = _PROMPT) -> LlmCallRequest:
    return LlmCallRequest(prompt=prompt, max_output_tokens=_MAX_OUTPUT_TOKENS, timeout_seconds=_TIMEOUT_SECONDS)


# --- the limiter -----------------------------------------------------------------------------------------------------


def test_calls_up_to_the_capacity_pass_and_the_next_raises() -> None:
    limiter = _limiter(_Clock())

    for _ in range(_CAPACITY):
        limiter.acquire(_PROVIDER)

    with pytest.raises(LlmCallFailure) as exhausted:
        limiter.acquire(_PROVIDER)
    assert exhausted.value.provider == _PROVIDER
    assert exhausted.value.reason == f"rate limit exhausted ({_CAPACITY} rpm); retry later"


def test_exhaustion_is_an_ordinary_failure_not_a_timeout() -> None:
    limiter = _limiter(_Clock(), capacity=1)
    limiter.acquire(_PROVIDER)

    with pytest.raises(LlmCallFailure) as exhausted:
        limiter.acquire(_PROVIDER)

    assert not isinstance(exhausted.value, LlmCallTimeout)


def test_exhaustion_does_not_wait_for_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_to_sleep(seconds: float) -> None:
        raise AssertionError(f"an exhausted limiter must not wait ({seconds} s)")

    limiter = _limiter(_Clock(), capacity=1)
    limiter.acquire(_PROVIDER)
    monkeypatch.setattr(time, "sleep", refuse_to_sleep)

    with pytest.raises(LlmCallFailure):
        limiter.acquire(_PROVIDER)


def test_the_window_stays_closed_until_it_has_run_for_the_whole_period() -> None:
    clock = _Clock()
    limiter = _limiter(clock, capacity=1)
    limiter.acquire(_PROVIDER)

    clock.advance(RATE_LIMIT_WINDOW_SECONDS - _JUST_SHORT_SECONDS)

    with pytest.raises(LlmCallFailure):
        limiter.acquire(_PROVIDER)


def test_the_window_refills_to_the_full_capacity_when_the_period_has_elapsed() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    for _ in range(_CAPACITY):
        limiter.acquire(_PROVIDER)

    clock.advance(RATE_LIMIT_WINDOW_SECONDS)

    for _ in range(_CAPACITY):
        limiter.acquire(_PROVIDER)
    with pytest.raises(LlmCallFailure):
        limiter.acquire(_PROVIDER)


def test_the_window_opens_when_the_limiter_is_built_not_at_the_first_call() -> None:
    clock = _Clock()
    limiter = _limiter(clock, capacity=1)
    clock.advance(RATE_LIMIT_WINDOW_SECONDS / 2)
    limiter.acquire(_PROVIDER)  # the first call, half a period after the limiter was built

    clock.advance(RATE_LIMIT_WINDOW_SECONDS / 2 + _ONE_SECOND)  # the period since the build is over, the period since the first call is not

    limiter.acquire(_PROVIDER)  # served: the window that began at the build has ended and was refilled


def test_a_refilled_window_starts_at_the_moment_of_the_refill() -> None:
    clock = _Clock()
    limiter = _limiter(clock, capacity=1)
    limiter.acquire(_PROVIDER)
    clock.advance(RATE_LIMIT_WINDOW_SECONDS + _REFILL_DELAY_SECONDS)  # the refill happens late, at the first call after the boundary
    limiter.acquire(_PROVIDER)

    clock.advance(RATE_LIMIT_WINDOW_SECONDS - _ONE_SECOND)

    with pytest.raises(LlmCallFailure):
        limiter.acquire(_PROVIDER)


def test_a_burst_is_possible_at_a_window_boundary() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    clock.advance(RATE_LIMIT_WINDOW_SECONDS - _ONE_SECOND)
    for _ in range(_CAPACITY):
        limiter.acquire(_PROVIDER)
    clock.advance(_ONE_SECOND)

    for _ in range(_CAPACITY):
        limiter.acquire(_PROVIDER)  # the second window's full capacity, one second after the first window's last call


def _pausing_clock() -> float:
    time.sleep(_CLOCK_PAUSE_SECONDS)  # keeps the threads queued on the limiter's lock instead of finishing one after the other
    return _START_TIME


def test_threads_never_take_more_than_the_capacity() -> None:
    limiter = FixedWindowRateLimiter(_THREADS * _ATTEMPTS_PER_THREAD // 2, clock=_pausing_clock)
    outcomes: list[bool] = []
    outcomes_lock = threading.Lock()
    start = threading.Barrier(_THREADS)

    def worker() -> None:
        start.wait()
        for _ in range(_ATTEMPTS_PER_THREAD):
            try:
                limiter.acquire(_PROVIDER)
                taken = True
            except LlmCallFailure:
                taken = False
            with outcomes_lock:
                outcomes.append(taken)

    threads = [threading.Thread(target=worker) for _ in range(_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == _THREADS * _ATTEMPTS_PER_THREAD
    assert outcomes.count(True) == _THREADS * _ATTEMPTS_PER_THREAD // 2


# --- the anthropic wrapper -------------------------------------------------------------------------------------------


class _FakeSdkClient:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self._error = error
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=_REPLY)])


@pytest.fixture
def anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _anthropic(limiter: FixedWindowRateLimiter | None, sdk: _FakeSdkClient) -> AnthropicClient:
    return AnthropicClient(model=_MODEL, client_factory=lambda: sdk, rate_limiter=limiter)


@pytest.mark.usefixtures("anthropic_key")
async def test_anthropic_wrapper_raises_the_exhausted_failure_without_calling_the_sdk() -> None:
    sdk = _FakeSdkClient()
    client = _anthropic(_limiter(_Clock(), capacity=2), sdk)
    await client.call(_request())
    await client.call(_request())

    with pytest.raises(LlmCallFailure) as exhausted:
        await client.call(_request())

    assert exhausted.value.provider == "anthropic"
    assert "exhausted" in exhausted.value.reason
    assert len(sdk.create_calls) == 2


@pytest.mark.usefixtures("anthropic_key")
async def test_anthropic_wrapper_serves_again_once_the_window_has_elapsed() -> None:
    clock = _Clock()
    client = _anthropic(_limiter(clock, capacity=1), _FakeSdkClient())
    await client.call(_request())
    with pytest.raises(LlmCallFailure):
        await client.call(_request())

    clock.advance(RATE_LIMIT_WINDOW_SECONDS)

    assert (await client.call(_request())).text == _REPLY


@pytest.mark.usefixtures("anthropic_key")
async def test_a_failed_anthropic_call_still_counts_against_the_window() -> None:
    sdk = _FakeSdkClient(error=RuntimeError("provider error"))
    client = _anthropic(_limiter(_Clock(), capacity=1), sdk)
    with pytest.raises(LlmCallFailure, match="unexpected RuntimeError"):
        await client.call(_request())

    with pytest.raises(LlmCallFailure, match="exhausted"):
        await client.call(_request())

    assert len(sdk.create_calls) == 1


async def test_a_missing_key_does_not_consume_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _FakeSdkClient()
    client = _anthropic(_limiter(_Clock(), capacity=1), sdk)
    with pytest.raises(LlmCallFailure, match="ANTHROPIC_API_KEY is not set"):
        await client.call(_request())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    assert (await client.call(_request())).text == _REPLY


@pytest.mark.usefixtures("anthropic_key")
async def test_a_failed_sdk_client_build_does_not_consume_a_slot() -> None:
    sdk = _FakeSdkClient()
    attempts: list[int] = []

    def flaky_factory() -> _FakeSdkClient:
        attempts.append(1)
        if len(attempts) == 1:
            raise ModuleNotFoundError("No module named 'anthropic'", name="anthropic")
        return sdk

    client = AnthropicClient(model=_MODEL, client_factory=flaky_factory, rate_limiter=_limiter(_Clock(), capacity=1))
    with pytest.raises(LlmCallFailure, match="package is not installed"):
        await client.call(_request())

    assert (await client.call(_request())).text == _REPLY


@pytest.mark.usefixtures("anthropic_key")
async def test_anthropic_wrapper_without_a_limiter_is_unthrottled() -> None:
    sdk = _FakeSdkClient()
    client = _anthropic(None, sdk)

    for _ in range(_CAPACITY * 4):
        await client.call(_request())

    assert len(sdk.create_calls) == _CAPACITY * 4


# --- the claude_cli wrapper ------------------------------------------------------------------------------------------


def _claude(limiter: FixedWindowRateLimiter | None, fake: _fake_claude.FakeClaude) -> ClaudeCliClient:
    return ClaudeCliClient(model="", binary=str(fake.path), rate_limiter=limiter)


@pytest.fixture
def fake(tmp_path: Path) -> _fake_claude.FakeClaude:
    return _fake_claude.install(tmp_path, reply=_REPLY)


@_POSIX_ONLY
async def test_claude_cli_wrapper_raises_the_exhausted_failure_without_spawning(fake: _fake_claude.FakeClaude) -> None:
    client = _claude(_limiter(_Clock(), capacity=2), fake)
    await client.call(_request())
    await client.call(_request())

    with pytest.raises(LlmCallFailure) as exhausted:
        await client.call(_request())

    assert exhausted.value.provider == "claude_cli"
    assert "exhausted" in exhausted.value.reason
    assert len(fake.invocations()) == 2


@_POSIX_ONLY
async def test_claude_cli_wrapper_serves_again_once_the_window_has_elapsed(fake: _fake_claude.FakeClaude) -> None:
    clock = _Clock()
    client = _claude(_limiter(clock, capacity=1), fake)
    await client.call(_request())
    with pytest.raises(LlmCallFailure):
        await client.call(_request())

    clock.advance(RATE_LIMIT_WINDOW_SECONDS)

    assert (await client.call(_request())).text == _REPLY


@_POSIX_ONLY
async def test_a_failed_claude_cli_call_still_counts_against_the_window(tmp_path: Path) -> None:
    fake = _fake_claude.install(tmp_path, reply="", exit_codes=(1,))
    client = _claude(_limiter(_Clock(), capacity=1), fake)
    with pytest.raises(LlmCallFailure, match="exit status 1"):
        await client.call(_request())

    with pytest.raises(LlmCallFailure, match="exhausted"):
        await client.call(_request())

    assert len(fake.invocations()) == 1


@_POSIX_ONLY
async def test_a_transport_retry_is_one_slot_not_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_cli_client, "RETRY_BACKOFF_SECONDS", 0.0)
    fake = _fake_claude.install(tmp_path, reply=_REPLY, exit_codes=(1, 0))
    client = ClaudeCliClient(model="", binary=str(fake.path), retry_on_nonzero_exit=True, rate_limiter=_limiter(_Clock(), capacity=1))

    assert (await client.call(_request())).text == _REPLY
    assert len(fake.invocations()) == 2


@_POSIX_ONLY
async def test_a_missing_binary_does_not_consume_a_slot(fake: _fake_claude.FakeClaude) -> None:
    limiter = _limiter(_Clock(), capacity=1)
    missing = ClaudeCliClient(model="", binary=str(fake.path), rate_limiter=limiter)
    missing.binary = None
    with pytest.raises(LlmCallFailure, match="not found on PATH"):
        await missing.call(_request())

    assert (await _claude(limiter, fake).call(_request())).text == _REPLY


@_POSIX_ONLY
async def test_claude_cli_wrapper_without_a_limiter_is_unthrottled(fake: _fake_claude.FakeClaude) -> None:
    client = _claude(None, fake)

    for _ in range(_CAPACITY + 1):
        await client.call(_request())

    assert len(fake.invocations()) == _CAPACITY + 1
