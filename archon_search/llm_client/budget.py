"""Input budgets: how many tokens a caller may send, sized ahead of the call (contract C1, scenarios S9, S11).

Budget = window - ``ANSWER_RESERVE_TOKENS`` - template, where the template is the LONGEST form the caller will
send (retry reminder included), so a retry of a full sample still fits and never needs a second sizing.
A window too small to hold reserve and template gives a budget of 0 (never negative), which a caller reads as
"nothing fits" and reports with the window it holds. There is no check after the call.

* ``llama_cpp``: :func:`exact_budget`; the count is the server's own, in the server's tokens.
* ``ollama``: :func:`estimated_budget`; the count is ``TokenCounter.count`` (gpt2 tokens, every digit and every
  non-BMP character counted pessimistically; see ``token_counter``), and callers measure their input in that unit
  too. The real tokenizer may count up to ``OLLAMA_ESTIMATE_MARGIN`` more, so the budget divides the room by
  ``1 + margin``: an input at the budget still fits in real tokens.
* cloud: :func:`cloud_budget`; no limit, nothing is counted or trimmed.

A failing count (an unreachable tokenize endpoint, a tokenizer that cannot be downloaded) propagates; the wrapper
that calls these functions turns it into ``LlmCallFailure``. ``llama_cpp`` counts the template as the server's chat template frames it; for ``ollama`` the chat template's own
tokens (role markers, a default system prompt: 9 to 33 on the models measured) are not counted, and the margin must
cover them.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from fractions import Fraction
from typing import Final

from archon_search.constants import ANSWER_RESERVE_TOKENS, OLLAMA_ESTIMATE_MARGIN
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindowTokens,
    InputBudget,
)

#: Exact token count of a text, as a wrapper obtains it from its server (``llama_cpp`` tokenize endpoint).
AsyncTokenCount = Callable[[str], Awaitable[int]]

#: Blocking token estimate of a text (``TokenCounter.count``: gpt2 plus the digit and astral-plane surcharges).
TokenCount = Callable[[str], int]

# ``Fraction`` of the decimal text keeps 0.20 as exactly 1/5, so the floor division below is exact.
_ESTIMATE_MARGIN = Fraction(str(OLLAMA_ESTIMATE_MARGIN))

#: Most templates one :class:`TemplateCostCache` keeps. The sizing templates of HyDE, RAG Fusion and graph summaries are a
#: handful of constant strings; a description template carries the collection name, so a server with many collections
#: would otherwise grow the cache for ever. A full cache is emptied to make room.
MAX_CACHED_TEMPLATES: Final = 64


class TemplateCostCache:
    """The token count of each prompt template a client sized, kept so a template is counted once per process.

    The template is the fixed part of every prompt (HyDE and RAG Fusion size a query against it on every search), and
    its count depends on the model's tokenizer and chat template, never on the context window. So the cache holds
    counts, not budgets: ``exact_budget`` and ``estimated_budget`` subtract the cached count from the window they are
    given on every call, and a window that is resolved after start-up (BE-10) is never served a budget computed under the
    assumed one. A client empties the cache (:meth:`clear`) when its server may have changed (a successful window probe).
    A count that raised is not remembered.

    Two first requests that arrive together may each count the template once; both get the same answer.
    """

    def __init__(self) -> None:
        self._costs: dict[str, int] = {}
        self._lock = threading.Lock()  # the Ollama estimate runs in worker threads

    def __len__(self) -> int:
        return len(self._costs)

    def memoize(self, count: TokenCount) -> TokenCount:
        """*count* with each answer remembered by text (for a blocking estimate)."""

        def counted(text: str) -> int:
            cost = self._costs.get(text)
            if cost is None:
                cost = count(text)
                self._remember(text, cost)
            return cost

        return counted

    def memoize_async(self, count: AsyncTokenCount) -> AsyncTokenCount:
        """*count* with each answer remembered by text (for a count that asks a server)."""

        async def counted(text: str) -> int:
            cost = self._costs.get(text)
            if cost is None:
                cost = await count(text)
                self._remember(text, cost)
            return cost

        return counted

    def clear(self) -> None:
        """Forget every template: the next sizing of each counts it again."""
        with self._lock:
            self._costs.clear()

    def _remember(self, text: str, cost: int) -> None:
        with self._lock:
            if text not in self._costs and len(self._costs) >= MAX_CACHED_TEMPLATES:
                self._costs.clear()
            self._costs[text] = cost


def cloud_budget() -> InputBudget:
    """Cloud providers enforce no limit the client sizes to: the prompt goes whole."""
    return InputBudget(tokens=UNBOUNDED)


async def exact_budget(window: ContextWindowTokens, template_text: str, count_tokens: AsyncTokenCount) -> InputBudget:
    """Budget for a provider that counts exactly (``llama_cpp``)."""
    template_tokens = await count_tokens(template_text)
    return InputBudget(tokens=max(0, window.size - ANSWER_RESERVE_TOKENS - template_tokens))


async def estimated_budget(window: ContextWindowTokens, template_text: str, count_gpt2_tokens: TokenCount) -> InputBudget:
    """Budget in estimate tokens (``TokenCounter.count``) for a provider whose tokenizer is unknown (``ollama``).

    The count runs in a thread: the counter's first use may download its tokenizer.
    """
    template_tokens = await asyncio.to_thread(count_gpt2_tokens, template_text)
    room_in_gpt2_tokens = (window.size - ANSWER_RESERVE_TOKENS) // (1 + _ESTIMATE_MARGIN)
    return InputBudget(tokens=max(0, room_in_gpt2_tokens - template_tokens))
