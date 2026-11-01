"""BE-2 (collection-descriptions plan, C1, S13): the shared LLM client interface, its constants,
the warn-once helper and ``provider_key_available``.

The interface is consumer-owned (Use Cases); every wrapper (Interface Adapters) implements it and
raises on failure. These tests pin the contract that the callers and the wrappers agree on.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import logging
import pickle
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import patch

import pytest

from archon_search.constants import ASSUMED_CONTEXT_TOKENS, OLLAMA_ESTIMATE_MARGIN
from archon_search.llm_client import once_per_process as once_per_process_module
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindow,
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallResult,
    LlmCallTimeout,
    LlmClient,
    ThinkSetting,
)
from archon_search.llm_client.provider_key import provider_key_available

_WARN_KEY = "test-caller"
_WARN_MESSAGE = "provider call failed"
_WARN_LOGGER_NAME = "archon_search.test_warn_once"
_FAILING_CALLS = 25
_RACING_THREADS = 16
_THREAD_JOIN_SECONDS = 30.0
_SLOW_MEMBERSHIP_SECONDS = 0.05
_IMPORT_PROBE_TIMEOUT_SECONDS = 60.0

_REQUEST_MAX_OUTPUT_TOKENS = 200
_REQUEST_TIMEOUT_SECONDS = 10.0

# The contract's literal numbers, pinned once here so a drift in ``constants.py`` fails a test.
_CONTRACT_ASSUMED_CONTEXT_TOKENS = 4096
_CONTRACT_ESTIMATE_MARGIN = 0.20

# Dirty the process-wide warn-once state while this module is collected, before any test runs. The
# autouse ``_reset_warn_once`` fixture must clear it, so ``test_warn_once_state_starts_empty_for_every_test``
# fails (on every worker that collects this file) if that fixture is removed.
once_per_process_module._warned_keys.add("dirtied-at-collection")


def _request(**overrides: object) -> LlmCallRequest:
    fields: dict[str, object] = {
        "prompt": "p",
        "max_output_tokens": _REQUEST_MAX_OUTPUT_TOKENS,
        "timeout_seconds": _REQUEST_TIMEOUT_SECONDS,
    }
    fields.update(overrides)
    return LlmCallRequest(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Warn-once helper (S13)
# ---------------------------------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def test_warn_once_logs_single_warning_per_process_key(caplog: pytest.LogCaptureFixture) -> None:
    """Many failing calls under one key log one WARNING; another key still gets its own."""
    logger = logging.getLogger(_WARN_LOGGER_NAME)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        logged = [warn_once(logger, _WARN_KEY, _WARN_MESSAGE) for _ in range(_FAILING_CALLS)]
        assert logged == [True] + [False] * (_FAILING_CALLS - 1)
        assert len(_warnings(caplog)) == 1

        assert warn_once(logger, "another-caller", _WARN_MESSAGE) is True
    assert len(_warnings(caplog)) == 2


def test_warn_once_state_starts_empty_for_every_test(caplog: pytest.LogCaptureFixture) -> None:
    """The suite-wide fixture resets the state: it holds no key at test start, so the key logs here."""
    logger = logging.getLogger(_WARN_LOGGER_NAME)
    assert once_per_process_module._warned_keys == set()
    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert warn_once(logger, _WARN_KEY, _WARN_MESSAGE) is True
    assert len(_warnings(caplog)) == 1
    assert once_per_process_module._warned_keys == {_WARN_KEY}


def test_warn_once_formats_lazy_arguments(caplog: pytest.LogCaptureFixture) -> None:
    """The message takes logging-style arguments, like ``logger.warning``."""
    logger = logging.getLogger(_WARN_LOGGER_NAME)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        warn_once(logger, _WARN_KEY, "provider %s failed: %s", "ollama", "timeout")
    assert [record.getMessage() for record in _warnings(caplog)] == ["provider ollama failed: timeout"]


def test_warn_once_record_points_at_the_caller_not_the_helper(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger(_WARN_LOGGER_NAME)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        warn_once(logger, _WARN_KEY, _WARN_MESSAGE)
    (record,) = _warnings(caplog)
    assert record.funcName == "test_warn_once_record_points_at_the_caller_not_the_helper"


class _SlowMembershipSet(set[str]):
    """A set whose ``in`` test lingers after answering, so an unguarded check-then-add race surfaces."""

    def __contains__(self, item: object) -> bool:
        answer = super().__contains__(item)
        time.sleep(_SLOW_MEMBERSHIP_SECONDS)
        return answer


def test_warn_once_logs_exactly_once_when_threads_race(caplog: pytest.LogCaptureFixture) -> None:
    """Concurrent failing calls (worker threads, request tasks) still log one WARNING."""
    logger = logging.getLogger(_WARN_LOGGER_NAME)
    barrier = threading.Barrier(_RACING_THREADS)
    results: list[bool] = []

    def _fail() -> None:
        barrier.wait()
        results.append(warn_once(logger, _WARN_KEY, _WARN_MESSAGE))

    threads = [threading.Thread(target=_fail) for _ in range(_RACING_THREADS)]
    with (
        patch.object(once_per_process_module, "_warned_keys", _SlowMembershipSet()),
        caplog.at_level(logging.WARNING, logger=logger.name),
    ):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_THREAD_JOIN_SECONDS)

    assert len(results) == _RACING_THREADS
    assert results.count(True) == 1
    assert len(_warnings(caplog)) == 1


# ---------------------------------------------------------------------------
# Failure type (C1: a timeout is told apart from every other failure)
# ---------------------------------------------------------------------------


def test_failure_type_distinguishes_timeout_from_other() -> None:
    """The probe tells a timeout from any other provider failure by type; a timeout is still a failure."""
    timeout = LlmCallTimeout(provider="ollama", reason="no answer within 30s")
    other = LlmCallFailure(provider="ollama", reason="connection refused")

    assert isinstance(timeout, LlmCallFailure)
    assert isinstance(timeout, LlmCallTimeout)
    assert not isinstance(other, LlmCallTimeout)


def test_failure_carries_provider_and_reason_in_its_text() -> None:
    failure = LlmCallFailure(provider="llama_cpp", reason="HTTP 400")

    assert failure.provider == "llama_cpp"
    assert failure.reason == "HTTP 400"
    assert str(failure) == "llama_cpp: HTTP 400"


@pytest.mark.parametrize("failure_type", [LlmCallFailure, LlmCallTimeout])
def test_failure_survives_copy_and_pickle(failure_type: type[LlmCallFailure]) -> None:
    """A failure crossing a process or thread boundary keeps its type, provider and reason."""
    original = failure_type(provider="openai", reason="HTTP 500")

    for clone in (copy.copy(original), pickle.loads(pickle.dumps(original))):
        assert type(clone) is failure_type
        assert (clone.provider, clone.reason) == ("openai", "HTTP 500")


# ---------------------------------------------------------------------------
# provider_key_available
# ---------------------------------------------------------------------------


@pytest.fixture
def no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return monkeypatch


@pytest.mark.parametrize("provider", ["ollama", "claude_cli", "llama_cpp"])
def test_provider_key_available_returns_bool(provider: str, no_provider_keys: pytest.MonkeyPatch) -> None:
    """A keyless provider answers a real ``True`` with every key absent."""
    assert provider_key_available(provider) is True


@pytest.mark.parametrize(("provider", "env_var"), [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")])
def test_provider_key_available_follows_the_providers_own_key(
    provider: str, env_var: str, no_provider_keys: pytest.MonkeyPatch
) -> None:
    """A keyed provider answers a real ``bool`` (never ``str | None``) that follows its own variable only."""
    assert provider_key_available(provider) is False

    no_provider_keys.setenv(env_var, "")
    assert provider_key_available(provider) is False

    no_provider_keys.setenv(env_var, "key-value")
    assert provider_key_available(provider) is True


def test_provider_key_available_ignores_the_other_providers_key(no_provider_keys: pytest.MonkeyPatch) -> None:
    no_provider_keys.setenv("OPENAI_API_KEY", "key-value")
    assert provider_key_available("anthropic") is False

    no_provider_keys.delenv("OPENAI_API_KEY")
    no_provider_keys.setenv("ANTHROPIC_API_KEY", "key-value")
    assert provider_key_available("openai") is False


def test_provider_key_available_defaults_an_unknown_provider_to_the_anthropic_key(
    no_provider_keys: pytest.MonkeyPatch,
) -> None:
    """The documented default: anything that is not keyless or ``openai`` needs ``ANTHROPIC_API_KEY``."""
    assert provider_key_available("unknown") is False
    no_provider_keys.setenv("ANTHROPIC_API_KEY", "key-value")
    assert provider_key_available("unknown") is True


# ---------------------------------------------------------------------------
# C1 constants
# ---------------------------------------------------------------------------


def test_c1_constants_match_the_contract() -> None:
    assert ASSUMED_CONTEXT_TOKENS == _CONTRACT_ASSUMED_CONTEXT_TOKENS
    assert OLLAMA_ESTIMATE_MARGIN == pytest.approx(_CONTRACT_ESTIMATE_MARGIN)


# ---------------------------------------------------------------------------
# Interface shape
# ---------------------------------------------------------------------------


def test_request_carries_think_and_defaults_it_to_no_thinking_control() -> None:
    assert _request().think is ThinkSetting.DEFAULT
    assert _request(think=ThinkSetting.OFF).think is ThinkSetting.OFF
    assert {member.name for member in ThinkSetting} == {"DEFAULT", "ON", "OFF"}


def test_request_is_an_immutable_single_message() -> None:
    request = _request()

    with pytest.raises(dataclasses.FrozenInstanceError):
        request.prompt = "q"  # type: ignore[misc]
    assert {field.name for field in dataclasses.fields(LlmCallRequest)} == {
        "prompt",
        "max_output_tokens",
        "timeout_seconds",
        "think",
    }
    assert LlmCallResult(text="answer").text == "answer"


def test_context_window_source_values_are_the_wire_words() -> None:
    assert {source.value for source in ContextWindowSource} == {"configured", "probed", "assumed"}
    window = ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)
    assert (window.size, window.source) == (ASSUMED_CONTEXT_TOKENS, ContextWindowSource.ASSUMED)
    assert UNBOUNDED == "unbounded"


class _StubClient:
    """A structural implementation: no inheritance from the consumer-owned interface."""

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        return LlmCallResult(text=request.prompt)

    def context_window(self) -> ContextWindow:
        return UNBOUNDED

    async def input_budget(
        self, template_text: str, *, max_output_tokens: int, think: ThinkSetting = ThinkSetting.DEFAULT
    ) -> InputBudget:
        return InputBudget(tokens=UNBOUNDED)


def test_input_budget_takes_the_thinking_setting_of_the_calls_it_sizes_for() -> None:
    """The chat template's framing can depend on it (llama.cpp ``enable_thinking``); the default is what a call sends when it sets nothing."""
    parameter = inspect.signature(LlmClient.input_budget).parameters["think"]

    assert parameter.default is ThinkSetting.DEFAULT


def test_input_budget_requires_the_answer_ceiling_of_the_calls_it_sizes_for() -> None:
    """No default: a caller that forgets its ceiling fails loudly instead of silently holding back a global reserve."""
    parameter = inspect.signature(LlmClient.input_budget).parameters["max_output_tokens"]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_interface_is_structural_and_runtime_checkable() -> None:
    assert isinstance(_StubClient(), LlmClient)
    assert not isinstance(object(), LlmClient)


# ---------------------------------------------------------------------------
# Lazy import: the package is stdlib-only, so importing it costs no SDK at CLI start-up
# ---------------------------------------------------------------------------


def test_client_package_import_loads_no_provider_sdk() -> None:
    script = textwrap.dedent(
        """
        import sys
        import archon_search.llm_client.protocol
        import archon_search.llm_client.provider_key
        import archon_search.llm_client.once_per_process
        import archon_search.llm_client.budget
        import archon_search.llm_client.rate_limit
        heavy = ("anthropic", "openai", "ollama", "httpx", "claude_agent_sdk", "torch", "transformers")
        loaded = sorted(name for name in heavy if name in sys.modules)
        print(",".join(loaded))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=_IMPORT_PROBE_TIMEOUT_SECONDS,
    )

    assert result.stdout.strip() == ""
