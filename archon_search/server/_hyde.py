"""HyDE resolution shared by every transport that searches or explains.

REST ``/search`` and ``/explain`` and the MCP ``search``, ``search_with_context`` and
``explain`` tools resolve HyDE the same way: suppressed when a mode that wins over it
is active (:func:`hyde_suppressed`), otherwise generated under the search budget
(S582), embedded with the embedder serving the request — so the hypothesis lands in
the searched collection's vector space — and kept off the request's stage timings.
Each transport keeps its own error envelope and maps
:class:`~archon_search.hyde.HyDEDependencyError` onto it at the edge.
"""
from __future__ import annotations

import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from archon_search.observability import paused_stage_recorder
from archon_search.server._search_budget import SearchBudgetExceeded, run_within_budget

if TYPE_CHECKING:
    from archon_search.config import HyDEConfig
    from archon_search.embedder import Embedder
    from archon_search.hyde import HyDEGenerator

_logger = logging.getLogger(__name__)

HYDE_EXPANSION_FAILED_WARNING = "HyDE expansion failed"


def hyde_suppressed(*, rag_fusion: bool, rag_fusion_enabled: bool, graph_mode: str | None) -> bool:
    """Whether a mode that wins over HyDE is active, so HyDE makes no LLM call and reports no warning.

    * ``rag_fusion=true`` — but only when RAG Fusion can actually run (*rag_fusion_enabled*,
      the config kill-switch, S272).
    * any *graph_mode* — on every surface: most graph paths drop a supplied query vector
      (single-collection ``ppr`` / ``local`` / ``global``, ``naive`` once its expansion
      applies, the fan-out's ``naive``), so a hypothesis would send the query off-host for
      a ``hyde_applied`` that cannot be trusted.
    """
    return (rag_fusion and rag_fusion_enabled) or graph_mode is not None


async def resolve_hyde_within_budget(
    coro: Coroutine[Any, Any, list[float] | None],
    *,
    timeout: float,
) -> list[float] | None:
    """Await a ``HyDEGenerator.generate`` call under the search budget (S582).

    A cold *embedder* leg of HyDE is the caller's to skip (:meth:`RequestHyDE.resolve`,
    S290).  The generation leg is an LLM call whose ceiling is ``[hyde].timeout_seconds``,
    which config validation accepts unclamped and unrelated to this budget — so a
    llama-server that accepts the connection but never answers parks the request for that
    whole duration, past the client's read timeout (``status=0``/``body=None``), exactly
    the S278/S288/S290 out-of-budget-wait family.  Bounding it here caps the wait at the
    server's own budget instead.

    On overrun the call is cancelled and ``None`` is returned: the caller surfaces that as
    its standard HyDE-failure warning and the search runs on the plain query.  Everything
    the coroutine itself raises — notably the ``HyDEDependencyError`` for a missing
    provider package — propagates unchanged.
    """
    try:
        return await run_within_budget(coro, timeout=timeout)
    except SearchBudgetExceeded:
        _logger.warning(
            "HyDE generation exceeded the %.1fs search budget; searching without it",
            timeout,
        )
        return None


@dataclass(frozen=True)
class HyDEOutcome:
    """What HyDE resolution leaves the request with.

    ``vector`` is the ANN lookup vector, or ``None`` to embed the plain query.
    ``expansion_warning`` is :data:`HYDE_EXPANSION_FAILED_WARNING` when HyDE was
    requested, not suppressed, and produced no vector.
    """

    vector: list[float] | None
    applied: bool
    expansion_warning: str | None


_SUPPRESSED = HyDEOutcome(vector=None, applied=False, expansion_warning=None)
_FAILED = HyDEOutcome(vector=None, applied=False, expansion_warning=HYDE_EXPANSION_FAILED_WARNING)


@dataclass(frozen=True)
class RequestHyDE:
    """One request's HyDE inputs; :meth:`resolve` runs them against a serving embedder.

    * *requested* — the request's ``hyde`` flag.
    * *suppressed* — :func:`hyde_suppressed` for the request; :meth:`for_request`
      computes it, so a transport cannot forget the rule.
    * *generator* / *config* — the app's ``HyDEGenerator`` and ``[hyde]`` config; a
      *config* of ``None`` (MCP without a config) counts as disabled.
    * *timeout* — the budget bounding the LLM call (S582).
    """

    query: str
    requested: bool
    suppressed: bool
    generator: HyDEGenerator | None
    config: HyDEConfig | None
    timeout: float

    @classmethod
    def for_request(
        cls,
        query: str,
        *,
        requested: bool,
        rag_fusion: bool,
        rag_fusion_enabled: bool,
        graph_mode: str | None,
        generator: HyDEGenerator | None,
        config: HyDEConfig | None,
        timeout: float,
    ) -> RequestHyDE:
        """Build a request's HyDE inputs, suppressed per :func:`hyde_suppressed`."""
        return cls(
            query=query,
            requested=requested,
            suppressed=hyde_suppressed(
                rag_fusion=rag_fusion, rag_fusion_enabled=rag_fusion_enabled, graph_mode=graph_mode
            ),
            generator=generator,
            config=config,
            timeout=timeout,
        )

    async def resolve(self, embedder: Embedder, *, embedder_warm: bool) -> HyDEOutcome:
        """Resolve HyDE, embedding the hypothesis with *embedder*.

        *embedder* must be the one serving the request: the searched collection's, or
        the global embedder on a fan-out (which excludes collections on another model).

        *embedder_warm* is ``False`` when the request degrades to FTS-only: the caller's
        own bounded warm-up (``warmup_for_search``) found *embedder* cold, or — on
        single-collection REST ``/search`` — the collection's embedder load is still
        pending and *embedder* is only the global placeholder.  HyDE is then skipped with
        the standard warning (S290) rather than waiting on the build.  A caller whose
        retrieval embeds on the same model regardless passes ``True``: the build is
        waited on once, by whichever embeds first.  HyDE's share of that wait is bounded
        by *timeout*; after a HyDE timeout, retrieval on a surface without its own
        budget (``search_with_context``, both ``explain`` surfaces) waits out the rest of
        the build unbounded.

        A request with no generator or a disabled ``[hyde]`` reports the standard warning
        without starting the budget task.

        The generation runs under :func:`paused_stage_recorder`, so the LLM call and the
        hypothesis embed — including any cold build that embed waits on — stay out of
        ``stage_timings_ms``.

        Raises :class:`~archon_search.hyde.HyDEDependencyError` when the HyDE provider
        package is missing; any other exception propagates to the caller's catch-all.
        """
        if not self.requested or self.suppressed:
            return _SUPPRESSED
        if not embedder_warm or self.generator is None or self.config is None or not self.config.enabled:
            return _FAILED
        with paused_stage_recorder():
            vector = await resolve_hyde_within_budget(
                self.generator.generate(self.query, embedder=embedder), timeout=self.timeout
            )
        if vector is None:
            return _FAILED
        return HyDEOutcome(vector=vector, applied=True, expansion_warning=None)
