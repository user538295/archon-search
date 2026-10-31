"""BE-23 / LCD BE-15 — one-shot startup probe for [graph].extraction_model.

K3 (2026-08-19-035 team plan) found reasoning models economically unsupportable via the
enrichment path: the shipped `extraction_timeout_seconds` default (30.0s) is the real
disqualifier — a reasoning model needs 111s-286s (measured), far past it — and detection inside
the summarisation path is inert because `community_builder.py`'s blanket fallback swallows it.
The probe bounds one fixed, tiny summarisation call on the shared graph client by
`min(extraction_timeout_seconds, economic ceiling)`: a timeout (the shared `LlmCallTimeout`) is
the primary "reasoning model" signal, any other failure is a distinctly-worded misconfiguration
warning, and a call that completes within budget passes regardless of content.

LCD BE-15: for `claude_cli` the probe only checks that the `claude` program exists (no model
call), and only when the graph feature is configured to use it.

Negative-test scenario (K3's notes): a non-reasoning model producing `finish_reason="length"`
with empty content on a genuinely oversized *production* prompt must not be misdetected by this
probe — the probe never touches production chunk content; it runs its own small, fixed
round-trip in isolation at startup, under its own configured timeout.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from archon_search.config import GraphConfig, SearchConfig, load_config
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindow,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallResult,
    LlmCallTimeout,
)
from archon_search.model_validation import (
    EXTRACTION_MODEL_PROBE_CLAUDE_MISSING_WARNING,
    EXTRACTION_MODEL_PROBE_ERROR_WARNING,
    EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING,
    ModelValidationResult,
    _probe_extraction_model,
    failed_result,
    validate_models_async,
)

_FAST_TEST_TIMEOUT_SECONDS = 0.05
_CLAUDE_BINARY = "archon_search.model_validation.resolve_claude_binary"


def _cfg(**graph_kwargs: object) -> SearchConfig:
    return SearchConfig(graph=GraphConfig(**graph_kwargs))


class _Client:
    """A graph-client stand-in: every ``call`` is recorded, then ``behaviour`` decides the answer (or raises)."""

    def __init__(self, behaviour: Callable[[LlmCallRequest], Awaitable[str]] | None = None) -> None:
        self.requests: list[LlmCallRequest] = []
        self._behaviour = behaviour

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        self.requests.append(request)
        text = "A concise community summary." if self._behaviour is None else await self._behaviour(request)
        return LlmCallResult(text=text)

    def context_window(self) -> ContextWindow:
        return UNBOUNDED

    async def input_budget(self, template_text: str) -> InputBudget:
        return InputBudget(UNBOUNDED)


def _raising(failure: Exception) -> Callable[[LlmCallRequest], Awaitable[str]]:
    async def _behaviour(_request: LlmCallRequest) -> str:
        raise failure

    return _behaviour


def _factory(client: object) -> Callable[[], object]:
    return lambda: client


def _unused_factory() -> Callable[[], object]:
    def _fail() -> object:
        raise AssertionError("the probe must not build a client here")

    return _fail


async def test_probe_skipped_when_graph_disabled() -> None:
    cfg = _cfg(enabled=False, provider="ollama", extraction_model="deepseek-r1")
    assert await _probe_extraction_model(cfg, graph_client_factory=_unused_factory()) == []


async def test_probe_skipped_when_provider_unset() -> None:
    cfg = _cfg(enabled=True, provider=None, extraction_model="deepseek-r1")
    assert await _probe_extraction_model(cfg, graph_client_factory=lambda: None) == []


async def test_probe_skipped_when_extraction_model_unset() -> None:
    cfg = _cfg(enabled=True, provider="ollama", extraction_model=None)
    assert await _probe_extraction_model(cfg, graph_client_factory=_unused_factory()) == []


def test_claude_cli_graph_probe_checks_presence_only_and_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S64: with graph on `claude_cli` the probe only looks for the `claude` program: present -> no warning, absent
    -> the install warning, and in neither case is a client built or a model called. Any other graph setup never
    even looks for the program."""
    resolver = MagicMock(return_value="/usr/local/bin/claude")
    monkeypatch.setattr(_CLAUDE_BINARY, resolver)
    configured = _cfg(enabled=True, provider="claude_cli", extraction_model="claude-haiku-4-5")

    assert asyncio.run(_probe_extraction_model(configured, graph_client_factory=_unused_factory())) == []
    resolver.assert_called_once_with()

    resolver.reset_mock(return_value=True)
    resolver.return_value = None
    warnings = asyncio.run(_probe_extraction_model(configured, graph_client_factory=_unused_factory()))
    assert warnings == [EXTRACTION_MODEL_PROBE_CLAUDE_MISSING_WARNING]
    resolver.assert_called_once_with()

    resolver.reset_mock()
    for not_configured in (
        _cfg(enabled=True, provider="anthropic", extraction_model="m"),
        _cfg(enabled=True, provider=None, extraction_model="m"),
        _cfg(enabled=False, provider="claude_cli", extraction_model="m"),
    ):
        asyncio.run(_probe_extraction_model(not_configured, graph_client_factory=lambda: None))
    resolver.assert_not_called()


def test_claude_cli_graph_without_a_model_in_the_file_still_gets_the_presence_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S71: the wizard omits a blank claude_cli model; the loaded config must still open the probe's gate."""
    toml = tmp_path / "archon-search.toml"
    toml.write_text('[graph]\nenabled = true\nprovider = "claude_cli"\n', encoding="utf-8")
    monkeypatch.setattr(_CLAUDE_BINARY, lambda: None)
    config = load_config(toml)
    warnings = asyncio.run(_probe_extraction_model(config, graph_client_factory=_unused_factory()))
    assert warnings == [EXTRACTION_MODEL_PROBE_CLAUDE_MISSING_WARNING]


async def test_claude_cli_missing_warning_is_a_fixed_text_naming_the_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_CLAUDE_BINARY, lambda: None)
    warnings = await _probe_extraction_model(_cfg(enabled=True, provider="claude_cli", extraction_model="m"))
    assert warnings == [EXTRACTION_MODEL_PROBE_CLAUDE_MISSING_WARNING]
    assert "claude" in warnings[0] and "PATH" in warnings[0]


class _ClosableClient(_Client):
    """A client with an ``aclose`` (as the cloud wrappers have), recording the closes; ``aclose_failure`` makes it raise."""

    def __init__(self, aclose_failure: Exception | None = None) -> None:
        super().__init__()
        self.closed = 0
        self._aclose_failure = aclose_failure

    async def aclose(self) -> None:
        self.closed += 1
        if self._aclose_failure is not None:
            raise self._aclose_failure


async def test_probe_closes_a_client_that_has_aclose_even_when_the_call_fails() -> None:
    """The cloud wrappers hold a pooled SDK client — the probe must close whatever client it builds rather than leak it,
    on a pass and on a failure alike. ``_Client`` has no ``aclose`` (a plain class, not an auto-creating mock), so this
    is a real assertion."""
    cfg = _cfg(enabled=True, provider="anthropic", extraction_model="claude-haiku-4-5")

    passing = _ClosableClient()
    assert await _probe_extraction_model(cfg, graph_client_factory=_factory(passing)) == []
    assert passing.closed == 1

    failing = _ClosableClient()
    failing._behaviour = _raising(LlmCallFailure("anthropic", "boom"))
    assert await _probe_extraction_model(cfg, graph_client_factory=_factory(failing)) == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert failing.closed == 1


async def test_probe_survives_an_aclose_that_raises_and_skips_a_client_without_one() -> None:
    cfg = _cfg(enabled=True, provider="anthropic", extraction_model="claude-haiku-4-5")

    broken = _ClosableClient(aclose_failure=RuntimeError("close failed"))
    assert await _probe_extraction_model(cfg, graph_client_factory=_factory(broken)) == []
    assert broken.closed == 1

    plain = _Client()
    assert not hasattr(plain, "aclose")
    assert await _probe_extraction_model(cfg, graph_client_factory=_factory(plain)) == []


async def test_probe_skipped_for_llama_cpp_when_already_known_unreachable() -> None:
    """When _probe_llama_cpp already found the server down, re-probing here would just
    produce a second, confusing warning for the same root cause — reuse the signal instead."""
    cfg = _cfg(enabled=True, provider="llama_cpp", extraction_model="model-x")
    assert await _probe_extraction_model(cfg, llama_cpp_ok=False, graph_client_factory=_unused_factory()) == []


async def test_probe_still_runs_for_llama_cpp_when_reachable() -> None:
    cfg = _cfg(enabled=True, provider="llama_cpp", extraction_model="model-x")
    client = _Client()
    assert await _probe_extraction_model(cfg, llama_cpp_ok=True, graph_client_factory=_factory(client)) == []
    assert len(client.requests) == 1


async def test_probe_without_a_client_factory_does_not_probe() -> None:
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b")
    assert await _probe_extraction_model(cfg) == []


async def test_probe_timeout_failure_produces_reasoning_model_warning() -> None:
    """K3's actual finding: a reasoning model returns content only after 111-286s, far past
    extraction_timeout_seconds. The shared client reports that as ``LlmCallTimeout``; the probe warns distinctly."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")
    client = _Client(_raising(LlmCallTimeout("ollama", "no answer within 0.05s")))
    warnings = await _probe_extraction_model(cfg, graph_client_factory=_factory(client))
    assert warnings == [EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]


async def test_probe_other_call_failure_is_a_generic_error_not_reasoning_model() -> None:
    """An unreachable server, a bad model name or an exhausted limiter (``LlmCallFailure`` that is not a timeout) is
    an infrastructure/config problem, not evidence of a slow reasoning model."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")
    client = _Client(_raising(LlmCallFailure("ollama", "cannot connect to the server")))
    warnings = await _probe_extraction_model(cfg, graph_client_factory=_factory(client))
    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]


async def test_probe_no_warning_when_model_responds_within_budget() -> None:
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b")
    assert await _probe_extraction_model(cfg, graph_client_factory=_factory(_Client())) == []


async def test_probe_no_warning_on_genuinely_empty_result() -> None:
    """An empty answer (no failure, no timeout) is a legitimate answer, not a disqualifier —
    only a slow or broken call disqualifies."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b")

    async def _empty(_request: LlmCallRequest) -> str:
        return ""

    assert await _probe_extraction_model(cfg, graph_client_factory=_factory(_Client(_empty))) == []


async def test_probe_call_is_bounded_by_the_tighter_of_operator_timeout_and_economic_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dual-bound math (min of the operator's own timeout and the fixed economic ceiling), proven on the timeout
    the request carries to the client. An operator raising extraction_timeout_seconds past the ceiling can never
    defeat detection."""
    test_ceiling = 0.2
    monkeypatch.setattr(
        "archon_search.model_validation._EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS",
        test_ceiling,
    )

    looser_operator = _cfg(enabled=True, provider="ollama", extraction_model="m", extraction_timeout_seconds=test_ceiling * 10)
    tighter_operator = _cfg(enabled=True, provider="ollama", extraction_model="m", extraction_timeout_seconds=test_ceiling / 4)
    seen: list[float] = []
    for cfg in (looser_operator, tighter_operator):
        client = _Client()
        assert await _probe_extraction_model(cfg, graph_client_factory=_factory(client)) == []
        seen.append(client.requests[0].timeout_seconds)

    assert seen == [test_ceiling, test_ceiling / 4]


async def test_probe_warns_distinctly_on_unexpected_failure_never_raises() -> None:
    """Anything the client raises that is not a call failure still reads as plain misconfiguration, never raises, and
    is not the timeout warning (the message that stakes the reasoning-model claim)."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="typo-model")
    client = _Client(_raising(ConnectionError("connection refused")))
    warnings = await _probe_extraction_model(cfg, graph_client_factory=_factory(client))
    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert warnings != [EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]


async def test_probe_warning_does_not_leak_exception_internals() -> None:
    """CLAUDE.md: never put str(exc) in a wire-facing field. provider_warnings is surfaced
    verbatim on GET /status, so the warning text must be the sanitized constant only."""
    cfg = _cfg(enabled=True, provider="openai", extraction_model="gpt-4o-mini")
    client = _Client(_raising(LlmCallFailure("openai", "secret-internal-detail sk-verysecretapikey123")))
    warnings = await _probe_extraction_model(cfg, graph_client_factory=_factory(client))
    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert "secret-internal-detail" not in warnings[0]
    assert "sk-verysecretapikey123" not in warnings[0]


async def test_probe_uses_a_small_fixed_prompt_not_production_chunk_content() -> None:
    """Negative-test scenario from K3: the probe runs its OWN small, fixed round-trip — never anything shaped like a
    genuinely oversized production prompt — through the same summarisation template production sends, with the
    configured answer ceiling."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b", extraction_token_budget=77)
    client = _Client()

    assert await _probe_extraction_model(cfg, graph_client_factory=_factory(client)) == []

    (request,) = client.requests
    assert "knowledge-graph summariser" in request.prompt
    assert "Alpha uses Beta." in request.prompt and "Alpha, Beta" in request.prompt
    assert len(request.prompt) < 1000, "probe must not send a production-sized prompt"
    assert request.max_output_tokens == 77


async def test_probe_survives_factory_raising() -> None:
    """A client-construction failure (malformed config, missing required field) converts to the same sanitized
    error warning as any other probe failure, never propagates and crashes validate_models_async's caller."""
    cfg = _cfg(enabled=True, provider="anthropic", extraction_model="claude-haiku-4-5")

    def _raising_factory() -> object:
        raise RuntimeError("malformed config: secret-internal-detail")

    warnings = await _probe_extraction_model(cfg, graph_client_factory=_raising_factory)
    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert "secret-internal-detail" not in warnings[0]


async def test_validate_models_async_survives_factory_raising() -> None:
    """End-to-end: a client-construction failure must not crash validate_models_async — it must
    still return a normal result with embedder/reranker outcomes intact, not fall back to failed_result()."""
    cfg = _cfg(enabled=True, provider="anthropic", extraction_model="claude-haiku-4-5")

    def _raising_factory() -> object:
        raise RuntimeError("boom")

    result = await validate_models_async(cfg, timeout_seconds=5, graph_client_factory=_raising_factory)
    assert isinstance(result, ModelValidationResult)
    assert EXTRACTION_MODEL_PROBE_ERROR_WARNING in result.provider_warnings
    assert result.embedder_ok is True  # validation still ran normally


async def test_validate_models_async_surfaces_extraction_probe_warning() -> None:
    """End-to-end wiring: a timed-out graph call's probe warning must reach
    validate_models_async().provider_warnings, not just the isolated probe function."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")
    cfg.graph.extraction_timeout_seconds = _FAST_TEST_TIMEOUT_SECONDS
    client = _Client(_raising(LlmCallTimeout("ollama", "no answer")))

    result = await validate_models_async(cfg, timeout_seconds=5, graph_client_factory=_factory(client))
    assert isinstance(result, ModelValidationResult)
    assert EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING in result.provider_warnings


async def test_failed_result_skips_extraction_probe_rather_than_reprobing_blind() -> None:
    """failed_result() has no already-known llama_cpp_ok signal on the crash-fallback path (it
    never received one, and validate_models_async's own result — including llama_cpp_ok — was
    never committed since it crashed before returning). Re-probing with llama_cpp_ok=None would
    defeat the "skip when llama_cpp already known down" gate and risk a second billed API call
    during error handling — so failed_result() must skip the extraction-model probe entirely
    rather than call it. The reason string must still surface."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")

    result = await failed_result("model validation task crashed", cfg)

    assert EXTRACTION_MODEL_PROBE_ERROR_WARNING not in result.provider_warnings
    assert EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING not in result.provider_warnings
    assert "model validation task crashed" in result.provider_warnings
