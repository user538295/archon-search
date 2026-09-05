"""BE-23 — one-shot startup probe for [graph].extraction_model (acts on K3's outcome).

K3 (2026-08-19-035 team plan) found reasoning models economically unsupportable via the
enrichment path: the shipped `extraction_timeout_seconds` default (30.0s) is the real
disqualifier — a reasoning model needs 111s-286s (measured), far past it — and detection inside
the enrichment clients is inert because `graph_extractor.py`'s blanket `except Exception`
swallows it. Since BE-17 narrowed the protocol to community summarisation only, the probe bounds
a call to `summarize_community` (the one surviving enrichment path) with `asyncio.wait_for(...,
timeout=extraction_timeout_seconds)`: a timeout is the primary "reasoning model" signal, any
other exception is a distinctly-worded misconfiguration warning, and a call that completes
within budget passes regardless of content.

Negative-test scenario (K3's notes): a non-reasoning model producing `finish_reason="length"`
with empty content on a genuinely oversized *production* prompt must not be misdetected by this
probe — the probe never touches production chunk content; it runs its own small, fixed
round-trip in isolation at startup, under its own configured timeout.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from archon_search.config import GraphConfig, SearchConfig
from archon_search.model_validation import (
    _EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS,
    _EXTRACTION_MODEL_PROBE_ERROR_WARNING,
    _EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING,
    ModelValidationResult,
    _probe_extraction_model,
    failed_result,
    validate_models_async,
)

# Named per the "no magic numbers" finding — the simulated "reasoning model" delay used by
# several tests below; must stay comfortably above the tiny extraction_timeout_seconds those
# tests configure so the timeout branch is exercised deterministically.
_SIMULATED_SLOW_MODEL_DELAY_SECONDS = 10
# The extraction_timeout_seconds configured for the "probe times out" tests: small enough to
# keep the test fast, well under _SIMULATED_SLOW_MODEL_DELAY_SECONDS.
_FAST_TEST_TIMEOUT_SECONDS = 0.05


def _cfg(**graph_kwargs: object) -> SearchConfig:
    return SearchConfig(graph=GraphConfig(**graph_kwargs))


class _AclosingClientDouble:
    """A plain (non-Mock) client double that does NOT auto-create ``aclose``.

    A bare ``AsyncMock`` auto-vivifies any attribute you ask for, so a test using one can never
    fail if the production code stops calling ``aclose()`` — this double only has the attributes
    explicitly given to it, so ``hasattr(client, "aclose")`` and the actual call are both real
    assertions.
    """

    def __init__(self, *, summarize_community: AsyncMock, aclose: AsyncMock) -> None:
        self.summarize_community = summarize_community
        self.aclose = aclose


def _patch_factory(monkeypatch: pytest.MonkeyPatch, fake_client: AsyncMock) -> None:
    monkeypatch.setattr(
        "archon_search.enrichment.factory.EnrichmentClientFactory.build",
        lambda _config: fake_client,
    )


async def test_probe_skipped_when_graph_disabled() -> None:
    cfg = _cfg(enabled=False, provider="ollama", extraction_model="deepseek-r1")
    assert await _probe_extraction_model(cfg) == []


async def test_probe_skipped_when_provider_unset() -> None:
    cfg = _cfg(enabled=True, provider=None, extraction_model="deepseek-r1")
    assert await _probe_extraction_model(cfg) == []


async def test_probe_skipped_when_extraction_model_unset() -> None:
    cfg = _cfg(enabled=True, provider="ollama", extraction_model=None)
    assert await _probe_extraction_model(cfg) == []


async def test_probe_skipped_for_claude_cli_deferred_provider() -> None:
    """EnrichmentClientFactory.build returns None for claude_cli — probe must not crash on it."""
    cfg = _cfg(enabled=True, provider="claude_cli", extraction_model="claude-cli-model")
    assert await _probe_extraction_model(cfg) == []


async def test_probe_skipped_for_llama_cpp_when_already_known_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _probe_llama_cpp already found the server down, re-probing here would just
    produce a second, confusing warning for the same root cause — reuse the signal instead."""
    cfg = _cfg(enabled=True, provider="llama_cpp", extraction_model="model-x")
    fake_client = AsyncMock()
    _patch_factory(monkeypatch, fake_client)
    assert await _probe_extraction_model(cfg, llama_cpp_ok=False) == []
    fake_client.summarize_community.assert_not_called()


async def test_probe_still_runs_for_llama_cpp_when_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(enabled=True, provider="llama_cpp", extraction_model="model-x")
    fake_client = AsyncMock()
    fake_client.summarize_community.return_value = "A concise community summary."
    _patch_factory(monkeypatch, fake_client)
    assert await _probe_extraction_model(cfg, llama_cpp_ok=True) == []
    fake_client.summarize_community.assert_awaited_once()


async def test_probe_times_out_on_reasoning_model_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K3's actual finding: a reasoning model returns content, but only after 111-286s, far
    past extraction_timeout_seconds (30.0s default). The probe must bound the call at that
    configured value and warn distinctly for a timeout."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")

    async def _slow_summarize_community(*_args: object, **_kwargs: object) -> str | None:
        await asyncio.sleep(_SIMULATED_SLOW_MODEL_DELAY_SECONDS)
        return None

    fake_client = AsyncMock()
    fake_client.summarize_community.side_effect = _slow_summarize_community
    _patch_factory(monkeypatch, fake_client)

    # extraction_timeout_seconds well under the simulated "reasoning" delay, and small enough
    # that this test stays fast.
    cfg.graph.extraction_timeout_seconds = _FAST_TEST_TIMEOUT_SECONDS
    warnings = await _probe_extraction_model(cfg)
    assert warnings == [_EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]


async def test_probe_read_timeout_produces_reasoning_model_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx.ReadTimeout: the server accepted the connection and is still generating — this IS
    evidence of a slow/reasoning model, unlike ConnectTimeout/PoolTimeout below."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")
    fake_client = AsyncMock()
    fake_client.summarize_community.side_effect = httpx.ReadTimeout("read timed out")
    _patch_factory(monkeypatch, fake_client)
    warnings = await _probe_extraction_model(cfg)
    assert warnings == [_EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]


async def test_probe_connect_timeout_produces_generic_error_warning_not_reasoning_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx.ConnectTimeout is a sibling of httpx.TimeoutException but means the server was
    unreachable — an infrastructure/config problem, not evidence of a slow reasoning model. It
    must NOT be classified as the timeout/reasoning-model warning."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")
    fake_client = AsyncMock()
    fake_client.summarize_community.side_effect = httpx.ConnectTimeout("connect timed out")
    _patch_factory(monkeypatch, fake_client)
    warnings = await _probe_extraction_model(cfg)
    assert warnings == [_EXTRACTION_MODEL_PROBE_ERROR_WARNING]


async def test_probe_no_warning_when_model_responds_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A working non-reasoning model responds within extraction_timeout_seconds — no warning,
    with real, non-empty summary content returned."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b")
    fake_client = AsyncMock()
    fake_client.summarize_community.return_value = "A concise community summary."
    _patch_factory(monkeypatch, fake_client)
    assert await _probe_extraction_model(cfg) == []


async def test_probe_no_warning_on_genuinely_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty result (no exception, no timeout) is a legitimate answer, not a disqualifier —
    only a slow or broken call disqualifies."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b")
    fake_client = AsyncMock()
    fake_client.summarize_community.return_value = None
    _patch_factory(monkeypatch, fake_client)
    assert await _probe_extraction_model(cfg) == []


async def test_probe_no_warning_when_slow_but_within_economic_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K3's actual negative-test requirement: a model that is slow but still finishes within
    budget must NOT be flagged, and the dual-bound math (min of the operator's own timeout and
    the fixed economic ceiling) must be correct. Proven deterministically by spying on
    ``asyncio.wait_for`` and asserting the ``timeout`` it was actually invoked with, rather than
    racing a real ``asyncio.sleep`` against a patched bound — the latter needs only a few tens of
    ms of scheduler jitter (e.g. under pytest-xdist or a loaded CI runner) to flake."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b")
    test_ceiling = 0.2
    cfg.graph.extraction_timeout_seconds = test_ceiling * 10  # operator's timeout is the looser bound
    monkeypatch.setattr(
        "archon_search.model_validation._EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS",
        test_ceiling,
    )

    fake_client = AsyncMock()
    fake_client.summarize_community.return_value = "A concise community summary."
    _patch_factory(monkeypatch, fake_client)

    real_wait_for = asyncio.wait_for
    captured_timeouts: list[float] = []

    async def _spying_wait_for(awaitable: object, timeout: float) -> object:
        captured_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr("archon_search.model_validation.asyncio.wait_for", _spying_wait_for)

    assert await _probe_extraction_model(cfg) == []
    assert captured_timeouts == [test_ceiling], (
        "probe must bound the call at min(extraction_timeout_seconds, economic_ceiling)"
    )


async def test_probe_operator_raised_timeout_does_not_defeat_economic_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core MAJOR finding this fix addresses: an operator raising extraction_timeout_seconds
    past the economic ceiling (exactly what K3's own experiment needed to do) must still get a
    warning once the call exceeds the fixed economic ceiling. The ceiling is patched to a small
    value so the test stays fast — the logic under test (operator's timeout ignored once it
    exceeds the ceiling) is identical regardless of the ceiling's magnitude."""
    test_ceiling = 0.05
    monkeypatch.setattr(
        "archon_search.model_validation._EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS",
        test_ceiling,
    )
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")
    cfg.graph.extraction_timeout_seconds = test_ceiling * 100  # operator raised it way past ceiling

    async def _slow_summarize_community(*_args: object, **_kwargs: object) -> str | None:
        await asyncio.sleep(test_ceiling + 1)
        return None

    fake_client = AsyncMock()
    fake_client.summarize_community.side_effect = _slow_summarize_community
    _patch_factory(monkeypatch, fake_client)

    warnings = await _probe_extraction_model(cfg)
    assert warnings == [_EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]


async def test_probe_warns_distinctly_on_transport_failure_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection error / bad model name / auth failure is plain misconfiguration, not a
    reasoning-model economics problem — must warn with the distinct error (not timeout) message,
    and not raise. The error message legitimately mentions "reasoning models" as one possible
    parameter-incompatibility cause (finding: OpenAI's max_tokens 400 rejection) without ever
    asserting with certainty that this specific failure IS one — it must not be the timeout
    warning, which is the message that stakes that claim."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="typo-model")
    fake_client = AsyncMock()
    fake_client.summarize_community.side_effect = ConnectionError("connection refused")
    _patch_factory(monkeypatch, fake_client)
    warnings = await _probe_extraction_model(cfg)
    assert warnings == [_EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert warnings != [_EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]


async def test_probe_warning_does_not_leak_exception_internals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLAUDE.md: never put str(exc) in a wire-facing field. provider_warnings is surfaced
    verbatim on GET /status, so the warning text must be the sanitized constant only."""
    cfg = _cfg(enabled=True, provider="openai", extraction_model="gpt-4o-mini")
    fake_client = AsyncMock()
    fake_client.summarize_community.side_effect = RuntimeError(
        "secret-internal-detail sk-verysecretapikey123"
    )
    _patch_factory(monkeypatch, fake_client)
    warnings = await _probe_extraction_model(cfg)
    assert warnings == [_EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert "secret-internal-detail" not in warnings[0]
    assert "sk-verysecretapikey123" not in warnings[0]


async def test_probe_closes_client_that_exposes_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AsyncAnthropic-backed client holds a persistent connection pool — the probe must
    close whatever client it builds rather than leaking it.

    Uses ``_AclosingClientDouble`` rather than a bare ``AsyncMock`` — an ``AsyncMock``
    auto-vivifies ``aclose`` even if the probe never calls ``hasattr``/``aclose`` at all, so a
    plain mock can never fail this test if the production code regresses."""
    cfg = _cfg(enabled=True, provider="anthropic", extraction_model="claude-haiku-4-5")
    aclose_mock = AsyncMock()
    fake_client = _AclosingClientDouble(
        summarize_community=AsyncMock(return_value="A concise community summary."),
        aclose=aclose_mock,
    )
    _patch_factory(monkeypatch, fake_client)
    await _probe_extraction_model(cfg)
    aclose_mock.assert_awaited_once()


async def test_probe_does_not_close_client_without_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ollama/llama_cpp/openai clients expose no aclose() — hasattr(client, "aclose") must be
    False for a client double that genuinely lacks the attribute (unlike AsyncMock, which would
    auto-create it), and the probe must not raise trying to call it."""

    class _NoAcloseClientDouble:
        def __init__(self, summarize_community: AsyncMock) -> None:
            self.summarize_community = summarize_community

    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b")
    fake_client = _NoAcloseClientDouble(
        summarize_community=AsyncMock(return_value="A concise community summary.")
    )
    assert not hasattr(fake_client, "aclose")
    _patch_factory(monkeypatch, fake_client)
    assert await _probe_extraction_model(cfg) == []


async def test_probe_uses_a_small_fixed_prompt_not_production_chunk_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative-test scenario from K3: the probe runs its OWN small, fixed round-trip — never
    anything shaped like a genuinely oversized production chunk/candidate-pair prompt. Proven
    behaviorally: mock the client so that, if it were handed a large/production-shaped prompt,
    it would return finish_reason="length"/empty content (simulated here as raising, since the
    real clients raise ValueError on a JSON parse failure of truncated content) — but the probe
    completes fast and does NOT warn, because its own call is representative of "the model
    responds usably within its own configured timeout", not of a would-be-oversized production
    prompt reaching this probe's decision at all."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="llama3.1:8b")
    captured_calls: list[tuple[list[str], list[str]]] = []

    async def _summarize_community(
        chunk_texts: list[str], entity_names: list[str]
    ) -> str | None:
        # Recorded, not asserted, here: an AssertionError raised inside a mock's side_effect is
        # swallowed by the probe's own blanket `except Exception`, turning a test bug into an
        # opaque mismatch failure instead of a clear AssertionError. Assert after the call.
        captured_calls.append((chunk_texts, entity_names))
        return None

    fake_client = AsyncMock()
    fake_client.summarize_community.side_effect = _summarize_community
    _patch_factory(monkeypatch, fake_client)

    warnings = await _probe_extraction_model(cfg)

    assert warnings == []
    fake_client.summarize_community.assert_awaited_once()
    (chunk_texts, entity_names) = captured_calls[0]
    # A genuinely oversized production prompt would trigger finish_reason="length" and raise on
    # parse — assert the probe never sends anything resembling that shape.
    assert len(chunk_texts) == 1 and len(chunk_texts[0]) < 200, (
        "probe must not send production-sized chunk content"
    )
    assert len(entity_names) <= 2, "probe must not send a production-sized entity list"


async def test_probe_survives_factory_build_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """EnrichmentClientFactory.build() must be called from inside the probe's own try/except —
    a constructor failure (malformed config, missing required field) must convert to the same
    sanitized error warning as any other probe failure, never propagate and crash
    validate_models_async's caller."""
    cfg = _cfg(enabled=True, provider="anthropic", extraction_model="claude-haiku-4-5")

    def _raising_build(_config: object) -> None:
        raise RuntimeError("malformed config: secret-internal-detail")

    monkeypatch.setattr(
        "archon_search.enrichment.factory.EnrichmentClientFactory.build", _raising_build
    )
    warnings = await _probe_extraction_model(cfg)
    assert warnings == [_EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert "secret-internal-detail" not in warnings[0]


async def test_validate_models_async_survives_factory_build_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a factory.build() failure must not crash validate_models_async — it must
    still return a normal result with embedder/reranker outcomes intact, not fall back to
    failed_result()."""
    cfg = _cfg(enabled=True, provider="anthropic", extraction_model="claude-haiku-4-5")

    def _raising_build(_config: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "archon_search.enrichment.factory.EnrichmentClientFactory.build", _raising_build
    )
    result = await validate_models_async(cfg, timeout_seconds=5, embedder_is_warm=True)
    assert isinstance(result, ModelValidationResult)
    assert _EXTRACTION_MODEL_PROBE_ERROR_WARNING in result.provider_warnings
    assert result.embedder_ok is True  # embedder_is_warm=True — validation still ran normally


async def test_validate_models_async_surfaces_extraction_probe_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end wiring: a slow/failing extraction client's probe warning must reach
    validate_models_async().provider_warnings, not just the isolated probe function."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")
    cfg.graph.extraction_timeout_seconds = 0.05

    async def _slow_summarize_community(*_args: object, **_kwargs: object) -> str | None:
        await asyncio.sleep(10)
        return None

    fake_client = AsyncMock()
    fake_client.summarize_community.side_effect = _slow_summarize_community
    _patch_factory(monkeypatch, fake_client)

    result = await validate_models_async(cfg, timeout_seconds=5, embedder_is_warm=True)
    assert isinstance(result, ModelValidationResult)
    assert _EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING in result.provider_warnings


async def test_failed_result_skips_extraction_probe_rather_than_reprobing_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """failed_result() has no already-known llama_cpp_ok signal on the crash-fallback path (it
    never received one, and validate_models_async's own result — including llama_cpp_ok — was
    never committed since it crashed before returning). Re-probing with llama_cpp_ok=None would
    defeat the "skip when llama_cpp already known down" gate and risk a second billed API call
    during error handling — so failed_result() must skip the extraction-model probe entirely
    rather than call it. The reason string must still surface."""
    cfg = _cfg(enabled=True, provider="ollama", extraction_model="deepseek-r1")
    fake_client = AsyncMock()
    _patch_factory(monkeypatch, fake_client)

    result = await failed_result("model validation task crashed", cfg)

    fake_client.summarize_community.assert_not_called()
    assert _EXTRACTION_MODEL_PROBE_ERROR_WARNING not in result.provider_warnings
    assert _EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING not in result.provider_warnings
    assert "model validation task crashed" in result.provider_warnings
