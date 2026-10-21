"""Tests for ``archon_search.server._hyde.RequestHyDE`` — the one HyDE path every
search/explain transport shares (REST /search, /explain; MCP search, search_with_context,
explain)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import HyDEConfig
from archon_search.hyde import HyDEDependencyError
from archon_search.observability import _stage_recorder, bind_stage_recorder
from archon_search.server._hyde import (
    HYDE_EXPANSION_FAILED_WARNING,
    HyDEOutcome,
    RequestHyDE,
    hyde_suppressed,
    resolve_hyde_within_budget,
)
from archon_search.server._search_budget import SEARCH_TIMEOUT_SECONDS
from tests._hyde_stubs import stub_hyde_generator

_VECTOR = [0.1, 0.2, 0.3]
_SHORT_BUDGET_SECONDS = 0.05
_SILENT_PROVIDER_SECONDS = 30.0
_BOUNDED_CALL_CEILING_SECONDS = 5.0


def _generator(vector: list[float] | None = _VECTOR) -> MagicMock:
    return stub_hyde_generator(return_value=vector)


def _request_hyde(**overrides: object) -> RequestHyDE:
    fields: dict[str, object] = {
        "query": "query",
        "requested": True,
        "suppressed": False,
        "generator": _generator(),
        "config": HyDEConfig(enabled=True),
        "timeout": SEARCH_TIMEOUT_SECONDS,
    }
    fields.update(overrides)
    return RequestHyDE(**fields)  # type: ignore[arg-type]


async def _resolve(*, embedder: object | None = None, embedder_warm: bool = True, **overrides: object) -> HyDEOutcome:
    return await _request_hyde(**overrides).resolve(
        embedder if embedder is not None else MagicMock(), embedder_warm=embedder_warm  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_embeds_the_hypothesis_with_the_embedder_it_is_given() -> None:
    embedder = MagicMock()
    generator = _generator()

    outcome = await _resolve(embedder=embedder, generator=generator)

    assert outcome == HyDEOutcome(vector=_VECTOR, applied=True, expansion_warning=None)
    assert generator.generate.await_args.kwargs["embedder"] is embedder


@pytest.mark.asyncio
async def test_suppression_skips_the_llm_call_without_a_warning() -> None:
    """RAG Fusion or graph_mode active: no LLM call (no query off-host), no warning."""
    generator = _generator()

    outcome = await _resolve(generator=generator, suppressed=True)

    assert outcome == HyDEOutcome(vector=None, applied=False, expansion_warning=None)
    generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_expansion_reports_the_standard_warning() -> None:
    outcome = await _resolve(generator=_generator(vector=None))

    assert outcome == HyDEOutcome(vector=None, applied=False, expansion_warning=HYDE_EXPANSION_FAILED_WARNING)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expansion_warning"),
    [
        ({"requested": False}, None),
        ({"suppressed": True}, None),
        ({"embedder_warm": False}, HYDE_EXPANSION_FAILED_WARNING),
        ({"generator": None}, HYDE_EXPANSION_FAILED_WARNING),
        ({"config": HyDEConfig(enabled=False)}, HYDE_EXPANSION_FAILED_WARNING),
        ({"config": None}, HYDE_EXPANSION_FAILED_WARNING),
    ],
    ids=["not-requested", "suppressed", "cold-embedder", "no-generator", "disabled-config", "no-config"],
)
async def test_an_early_outcome_starts_no_budget_task(overrides: dict, expansion_warning: str | None) -> None:
    """C2-I-5/C3-A-1: no task / wait_for unless HyDE can actually run.

    hyde=false (the default request) and suppression report nothing; a requested HyDE that
    cannot run — cold model, no generator, ``[hyde]`` disabled or absent — reports the
    standard warning.
    """
    budget = AsyncMock()
    generator = _generator()

    with patch("archon_search.server._hyde.resolve_hyde_within_budget", new=budget):
        outcome = await _resolve(**{"generator": generator, **overrides})

    assert outcome == HyDEOutcome(vector=None, applied=False, expansion_warning=expansion_warning)
    budget.assert_not_called()
    generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_silent_provider_is_bounded_by_the_budget() -> None:
    """S582: the LLM leg is capped by *timeout*, not the unclamped [hyde].timeout_seconds."""

    async def _never_answers(*_args: object, **_kwargs: object) -> list[float]:
        await asyncio.sleep(_SILENT_PROVIDER_SECONDS)
        raise AssertionError("the HyDE call was not cancelled by the search budget")

    generator = stub_hyde_generator(side_effect=_never_answers)
    loop = asyncio.get_running_loop()
    started = loop.time()

    outcome = await _resolve(generator=generator, timeout=_SHORT_BUDGET_SECONDS)

    assert loop.time() - started < _BOUNDED_CALL_CEILING_SECONDS
    assert outcome == HyDEOutcome(vector=None, applied=False, expansion_warning=HYDE_EXPANSION_FAILED_WARNING)


@pytest.mark.asyncio
async def test_the_dependency_error_propagates() -> None:
    generator = stub_hyde_generator(side_effect=HyDEDependencyError("Install archon-search[hyde] to use HyDE"))

    with pytest.raises(HyDEDependencyError):
        await _resolve(generator=generator)


@pytest.mark.asyncio
async def test_hyde_runs_off_the_stage_recorder() -> None:
    """F6: the LLM call and the hypothesis embed stay out of ``stage_timings_ms``."""
    seen: list[object] = []

    async def _generate(*_args: object, **_kwargs: object) -> list[float]:
        seen.append(_stage_recorder.get())
        return _VECTOR

    generator = stub_hyde_generator(side_effect=_generate)

    with bind_stage_recorder() as recorder:
        outcome = await _resolve(generator=generator)
        assert _stage_recorder.get() is recorder

    assert outcome.applied is True
    assert seen == [None]


@pytest.mark.parametrize(
    ("rag_fusion", "rag_fusion_enabled", "graph_mode", "suppressed"),
    [
        (False, False, None, False),
        (True, False, None, False),
        (False, True, None, False),
        (True, True, None, True),
        (False, False, "naive", True),
        (True, False, "ppr", True),
    ],
)
def test_hyde_suppressed(rag_fusion: bool, rag_fusion_enabled: bool, graph_mode: str | None, suppressed: bool) -> None:
    """RAG Fusion wins only when it can run (S272); any graph_mode always wins (C2-A-1).

    ``RequestHyDE.for_request`` — what every transport builds — applies the same rule.
    """
    assert (
        hyde_suppressed(rag_fusion=rag_fusion, rag_fusion_enabled=rag_fusion_enabled, graph_mode=graph_mode)
        is suppressed
    )
    generator = _generator()
    config = HyDEConfig(enabled=True)
    built = RequestHyDE.for_request(
        "query",
        requested=True,
        rag_fusion=rag_fusion,
        rag_fusion_enabled=rag_fusion_enabled,
        graph_mode=graph_mode,
        generator=generator,
        config=config,
        timeout=_SHORT_BUDGET_SECONDS,
    )
    assert built == RequestHyDE(
        query="query",
        requested=True,
        suppressed=suppressed,
        generator=generator,
        config=config,
        timeout=_SHORT_BUDGET_SECONDS,
    )


# ---------------------------------------------------------------------------
# resolve_hyde_within_budget — S582
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_hyde_within_budget_returns_the_generated_vector() -> None:
    """A HyDE call that finishes in time is passed through untouched."""

    async def _generated() -> list[float]:
        return _VECTOR

    assert await resolve_hyde_within_budget(_generated(), timeout=_BOUNDED_CALL_CEILING_SECONDS) == _VECTOR


@pytest.mark.asyncio
async def test_resolve_hyde_within_budget_cancels_and_degrades_on_overrun(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unresponsive LLM server is cancelled, not waited out (S582)."""
    observed: list[str] = []

    async def _silent_provider() -> list[float]:
        try:
            await asyncio.sleep(_SILENT_PROVIDER_SECONDS)
        except asyncio.CancelledError:
            observed.append("cancelled")
            raise
        return _VECTOR  # pragma: no cover - unreachable, the sleep is cancelled

    with caplog.at_level("WARNING", logger="archon_search.server._hyde"):
        result = await resolve_hyde_within_budget(_silent_provider(), timeout=_SHORT_BUDGET_SECONDS)

    assert result is None
    assert observed == ["cancelled"]
    assert "HyDE generation exceeded" in caplog.text


@pytest.mark.asyncio
async def test_resolve_hyde_within_budget_propagates_the_dependency_error() -> None:
    """The missing-provider-package error still reaches each transport's 422 mapping."""

    async def _missing_package() -> list[float]:
        raise HyDEDependencyError("Install archon-search[hyde] to use HyDE")

    with pytest.raises(HyDEDependencyError):
        await resolve_hyde_within_budget(_missing_package(), timeout=_BOUNDED_CALL_CEILING_SECONDS)


@pytest.mark.asyncio
async def test_resolve_hyde_inner_builtin_timeouterror_is_not_a_budget_overrun() -> None:
    """A ``TimeoutError`` raised *by* the HyDE call keeps its own mapping.

    Swallowing it into ``None`` would hide a provider-side socket error behind the silent
    HyDE-failure degrade.
    """

    async def _raises() -> list[float]:
        raise TimeoutError("ETIMEDOUT from the provider socket")

    with pytest.raises(TimeoutError) as excinfo:
        await resolve_hyde_within_budget(_raises(), timeout=_BOUNDED_CALL_CEILING_SECONDS)
    assert str(excinfo.value) == "ETIMEDOUT from the provider socket"
