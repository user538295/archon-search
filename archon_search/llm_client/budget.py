"""Input budgets: how many tokens a caller may send, sized ahead of the call (contract C1, scenarios S9, S11).

Budget = window - the caller's own answer ceiling (the ``max_output_tokens`` its calls send) - template, where the
template is the LONGEST form the caller will send (retry reminder included), so a retry of a full sample still fits and
never needs a second sizing. Prompt tokens plus the call's ``max_output_tokens`` therefore stay within the window: for
``llama_cpp`` because the budget also holds back ``LLAMA_CPP_JOIN_SLACK_TOKENS`` for the tokens that span the join of
template and query (counted apart; measured 2 on Qwen2.5), for ``ollama`` within ``OLLAMA_ESTIMATE_MARGIN`` after
``OLLAMA_CHAT_FRAMING_TOKENS`` of chat framing. The ceiling is an input of every sizing,
never a constant: a HyDE call asks for 200 tokens and holds back 200, a graph summary holds back its own budget.
A window too small to hold ceiling and template gives a budget of 0 (never negative), which a caller reads as
"nothing fits" and reports with the window it holds. There is no check after the call.

* ``llama_cpp``: :func:`exact_budget`; the count is the server's own, in the server's tokens, and it counts the template
  as the server's chat template frames it for the thinking setting of the calls (``input_budget(..., think=...)``).
* ``ollama``: :func:`estimated_budget`; the count is the token estimate, ``TokenCounter.count`` (gpt2 tokens with every
  digit, every non-BMP character and the special-token literal counted pessimistically; see ``token_counter``), and
  callers measure their input in that unit too. The real tokenizer may count up to ``OLLAMA_ESTIMATE_MARGIN`` more, so
  the budget takes the chat template's own tokens (``OLLAMA_CHAT_FRAMING_TOKENS``: role markers, a default system
  prompt, 8 to 33 on the models measured; real tokens) and the answer ceiling off the window, then divides the room by
  ``1 + margin``: an input at the budget still fits in real tokens.
* cloud: :func:`cloud_budget`; no limit, nothing is counted or trimmed.
* :class:`TemplateCostCache`: a client memoizes the count of each template it sizes (counts only, bounded; ``llama_cpp``
  keeps one cache per thinking setting, since its template framing depends on the switch). Budgets are computed from the
  live window on every call, so a window resolved after start-up is used at once.

A failing count (an unreachable tokenize endpoint, a tokenizer that cannot be downloaded) propagates; the wrapper
that calls these functions turns it into ``LlmCallFailure``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from fractions import Fraction
from typing import Final

from archon_search.constants import (
    LLAMA_CPP_JOIN_SLACK_TOKENS,
    OLLAMA_CHAT_FRAMING_TOKENS,
    OLLAMA_ESTIMATE_MARGIN,
)
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindowTokens,
    InputBudget,
)

#: Token count of a text: exact from a server (``llama_cpp`` tokenize endpoint) or the token estimate of a counter run
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


async def exact_budget(
    window: ContextWindowTokens, template_text: str, count_tokens: AsyncTokenCount, *, max_output_tokens: int
) -> InputBudget:
    """Budget for a provider that counts exactly (``llama_cpp``): window - *max_output_tokens* - template - join slack.

    The template and the query are counted apart but sent joined, and a token may span the join (``LLAMA_CPP_JOIN_SLACK_TOKENS``),
    so the slack keeps prompt tokens + *max_output_tokens* within the window for a query of exactly the budget.
    """
    template_tokens = await count_tokens(template_text)
    return InputBudget(tokens=max(0, window.size - max_output_tokens - template_tokens - LLAMA_CPP_JOIN_SLACK_TOKENS))


async def estimated_budget(
    window: ContextWindowTokens, template_text: str, count_estimate: AsyncTokenCount, *, max_output_tokens: int
) -> InputBudget:
    """Budget in estimate tokens (``TokenCounter.count``) for a provider whose tokenizer is unknown (``ollama``).

    *count_estimate* is async so that a client can keep the blocking count (its first use may download the tokenizer)
    in a worker thread and still skip that thread when the answer is cached (:class:`TemplateCostCache`).
    The room left by the caller's answer ceiling (*max_output_tokens*) and by the chat framing Ollama adds around the
    message (``OLLAMA_CHAT_FRAMING_TOKENS``, real tokens) is divided by ``1 + margin`` before the template is taken off.
    """
    template_tokens = await count_estimate(template_text)
    room_in_estimate_tokens = (window.size - max_output_tokens - OLLAMA_CHAT_FRAMING_TOKENS) // (1 + _ESTIMATE_MARGIN)
    return InputBudget(tokens=max(0, room_in_estimate_tokens - template_tokens))
