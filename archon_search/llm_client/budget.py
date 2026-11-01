"""Input budgets: how many tokens a caller may send, sized ahead of the call (contract C1, scenarios S9, S11).

Budget = window - ``ANSWER_RESERVE_TOKENS`` - template, where the template is the LONGEST form the caller will
send (retry reminder included), so a retry of a full sample still fits and never needs a second sizing.
A window too small to hold reserve and template gives a budget of 0 (never negative), which a caller reads as
"nothing fits" and reports with the window it holds. There is no check after the call.

* ``llama_cpp``: :func:`exact_budget`; the count is the server's own, in the server's tokens, and it counts the template
  as the server's chat template frames it.
* ``ollama``: :func:`estimated_budget`; the count is ``TokenCounter.count`` (gpt2 tokens, every digit and every
  non-BMP character counted pessimistically; see ``token_counter``), and callers measure their input in that unit
  too. The real tokenizer may count up to ``OLLAMA_ESTIMATE_MARGIN`` more, so the budget divides the room by
  ``1 + margin``: an input at the budget still fits in real tokens. The chat template's own tokens (role markers, a
  default system prompt: 9 to 33 on the models measured) are not counted, and the margin must cover them.
* cloud: :func:`cloud_budget`; no limit, nothing is counted or trimmed.
* :class:`TemplateCostCache`: a client memoizes the count of each template it sizes (counts only, bounded). Budgets are
  computed from the live window on every call, so a window resolved after start-up is used at once.

A failing count (an unreachable tokenize endpoint, a tokenizer that cannot be downloaded) propagates; the wrapper
that calls these functions turns it into ``LlmCallFailure``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from fractions import Fraction
from typing import Final

from archon_search.constants import ANSWER_RESERVE_TOKENS, OLLAMA_ESTIMATE_MARGIN
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindowTokens,
    InputBudget,
)

#: Token count of a text: exact from a server (``llama_cpp`` tokenize endpoint) or the gpt2 estimate of a counter run
#: in a worker thread (``ollama``).
AsyncTokenCount = Callable[[str], Awaitable[int]]

# ``Fraction`` of the decimal text keeps 0.20 as exactly 1/5, so the floor division below is exact.
_ESTIMATE_MARGIN = Fraction(str(OLLAMA_ESTIMATE_MARGIN))

#: Most templates one :class:`TemplateCostCache` keeps. The sizing templates of HyDE, RAG Fusion and graph summaries are a
#: handful of constant strings; a description template carries the collection name, so a server with many collections
#: would otherwise grow the cache for ever. A full cache drops its oldest template to make room.
MAX_CACHED_TEMPLATES: Final = 64


class TemplateCostCache:
    """The token count of each prompt template a client sized, kept until the server may have changed or the cache is full.

    The template is the fixed part of every prompt (HyDE and RAG Fusion size a query against it on every search), and
    its count depends on the model's tokenizer and chat template, never on the context window. So the cache holds
    template counts only, never a budget and never the window: ``exact_budget`` and ``estimated_budget`` subtract the
    cached count from the window they are given on every call, and a window that is resolved after start-up (BE-10) is
    never served a budget computed under the assumed one. A count that raised is not remembered.

    The cache is for ONE event loop: it has no lock, and only the async path (:meth:`memoize_async`) fills it. When the
    cache holds :data:`MAX_CACHED_TEMPLATES` templates the OLDEST one is dropped (first in, first out) for a new one.
    Two first requests of one template that arrive together each count it and get the same answer.

    A client empties the cache with :meth:`clear` when its server may have changed. The only such moment today is a
    successful ``/props`` probe (``LlamaCppClient.probe_context_window``), which runs once at boot; a llama-server that
    is restarted with another model keeps the old template count until this process restarts. A count that was in
    flight when :meth:`clear` ran is returned to the caller that asked but never stored.
    """

    def __init__(self) -> None:
        self._costs: dict[str, int] = {}
        self._epoch = 0  # bumped by clear(); an answer asked for in an older epoch comes from the server clear() replaced

    def memoize_async(self, count: AsyncTokenCount) -> AsyncTokenCount:
        """*count* with each answer remembered by text."""

        async def counted(text: str) -> int:
            cost = self._costs.get(text)
            if cost is None:
                epoch = self._epoch
                cost = await count(text)
                self._remember(text, cost, epoch)
            return cost

        return counted

    def clear(self) -> None:
        """Forget every template: the next sizing of each counts it again."""
        self._costs.clear()
        self._epoch += 1

    def _remember(self, text: str, cost: int, epoch: int) -> None:
        if epoch != self._epoch:
            return
        if text not in self._costs and len(self._costs) >= MAX_CACHED_TEMPLATES:
            del self._costs[next(iter(self._costs))]
        self._costs[text] = cost


def cloud_budget() -> InputBudget:
    """Cloud providers enforce no limit the client sizes to: the prompt goes whole."""
    return InputBudget(tokens=UNBOUNDED)


async def exact_budget(window: ContextWindowTokens, template_text: str, count_tokens: AsyncTokenCount) -> InputBudget:
    """Budget for a provider that counts exactly (``llama_cpp``)."""
    template_tokens = await count_tokens(template_text)
    return InputBudget(tokens=max(0, window.size - ANSWER_RESERVE_TOKENS - template_tokens))


async def estimated_budget(window: ContextWindowTokens, template_text: str, count_gpt2_tokens: AsyncTokenCount) -> InputBudget:
    """Budget in estimate tokens (``TokenCounter.count``) for a provider whose tokenizer is unknown (``ollama``).

    *count_gpt2_tokens* is async so that a client can keep the blocking count (its first use may download the tokenizer)
    in a worker thread and still skip that thread when the answer is cached (:class:`TemplateCostCache`).
    """
    template_tokens = await count_gpt2_tokens(template_text)
    room_in_gpt2_tokens = (window.size - ANSWER_RESERVE_TOKENS) // (1 + _ESTIMATE_MARGIN)
    return InputBudget(tokens=max(0, room_in_gpt2_tokens - template_tokens))
