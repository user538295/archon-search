"""Fitting a caller's variable text to an input budget (contract C1, scenarios S9, S11).

:func:`fit_text_to_budget` keeps the head of a text that the caller's own count says fits, cut before a space (a whole
word) or, where there is none in reach, on a character-cluster boundary (never inside a surrogate pair, a combining or
joiner sequence, a skin tone or a flag), and
:func:`fit_query_to_window` is the whole sizing step of a feature that puts one query into a prompt template
(HyDE, RAG Fusion): ask the client for the budget of the template, then fit the query to it.

* Cloud: the budget is unbounded, so the text is returned as it came: nothing is counted or cut (S9).
* ``llama_cpp``: the count is the server's own (``LlamaCppClient.count_tokens``).
* ``ollama``: the count is the token estimate (``OllamaClient.count_tokens``); the 20% margin and the chat framing allowance already sit in the budget.

A text is fitted with a bounded number of counts (``MAX_FIT_COUNTS``, each one a request to the server for
``llama_cpp``) and the text returned is one the count has itself seen fit, never an extrapolation. The one exception
is the ceiling bypass of :func:`fit_query_to_window`: a query whose request-free ``token_ceiling`` is within the budget
is returned without any count. The first estimate scales the cut by the measured tokens per character, the next ones
close in on the budget from both sides.
The head is also never longer than ``MAX_CHARS_PER_TOKEN`` characters per budget token: the server's request-size
limit lets megabytes through, and counting them only to cut them would stall the search (no real text spends 16
characters per token on average).

The failure of a count (an unreachable tokenize endpoint, a tokenizer that cannot be downloaded) propagates as the
client raised it. The detail of :class:`NoRoomForInput` names the window and never holds the text.
"""

from __future__ import annotations

import unicodedata
from typing import Final

from archon_search.llm_client.budget import AsyncTokenCount
from archon_search.llm_client.protocol import (
    ContextWindow,
    ContextWindowTokens,
    LlmCallFailure,
    LlmClient,
    TokenBoundingClient,
    Unbounded,
)

#: The two methods of ``TokenBoundingClient`` a sized query needs, named by the failure of a client that lacks one.
_COUNTING_METHODS: Final = ("count_tokens", "token_ceiling")
#: Most counts :func:`fit_text_to_budget` asks for, the first one (the whole head) included.
MAX_FIT_COUNTS: Final = 10
#: Characters per budget token beyond which the rest of a text is cut without being counted.
MAX_CHARS_PER_TOKEN: Final = 16
#: A cut text is aimed at this share of the budget (percent) ...
_TARGET_FILL_PERCENT: Final = 97
#: ... and one that uses at least this share is good enough: no further count is spent on the last few tokens.
_ACCEPTED_FILL_PERCENT: Final = 90
_PERCENT: Final = 100
#: A word end is looked for this many characters back from the cut; farther back, a word is not what is being cut.
_WORD_LOOKBACK_CHARS: Final = 64
#: A grapheme cluster end is looked for this many characters back from the cut.
_CLUSTER_LOOKBACK_CHARS: Final = 32
#: A next cut stays this fraction (1/8) of the open interval away from its two ends, so the interval always shrinks.
_BRACKET_MARGIN_DIVISOR: Final = 8

_JOINERS: Final = ("\u200c", "\u200d")  # zero-width non-joiner and joiner
#: The emoji tag characters (U+E0020 to U+E007F) that extend a subdivision flag.
_TAG_CHARACTERS: Final = ("\U000e0020", "\U000e007f")
#: A flag is two regional indicators: how far back a run of them is counted to tell whether a cut would split one.
_FLAG_RUN_LOOKBACK_CHARS: Final = 64
_HIGH_SURROGATES: Final = ("\ud800", "\udbff")
_LOW_SURROGATES: Final = ("\udc00", "\udfff")
_EMOJI_MODIFIERS: Final = ("\U0001f3fb", "\U0001f3ff")
_REGIONAL_INDICATORS: Final = ("\U0001f1e6", "\U0001f1ff")


class NoRoomForInput(Exception):
    """The window holds the template and the call's answer ceiling but too little (or nothing) of the text.

    Not a provider failure: nothing went wrong with a call, the feature just cannot be sized into this window.
    ``detail`` names the window and the room left and never holds the text.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def describe_window(window: ContextWindow) -> str:
    """The window as a message names it: its size and where the size came from, or ``unbounded``."""
    if isinstance(window, ContextWindowTokens):
        return f"{window.size} tokens, {window.source.value}"
    return str(window)


async def fit_query_to_window(
    client: LlmClient, template_text: str, query: str, *, provider: str, max_output_tokens: int
) -> str:
    """The part of *query* that fits *client*'s window next to *template_text*: the query itself when it all fits.

    Args:
        client: the feature's client; its ``input_budget`` and ``count_tokens`` size the query.
        template_text: the feature's prompt template with an empty query (the longest fixed part).
        query: the user's query.
        provider: the feature's provider, named by the failure of a client that has a budget but cannot count.
        max_output_tokens: the answer ceiling of the call the query is for (the one it will send): the window holds exactly
            this much back for the answer.

    Raises:
        NoRoomForInput: the window leaves no room for the template and the answer ceiling, or for even one word of the query.
        LlmCallFailure: the budget or a count failed, or the client has a budget but lacks ``count_tokens`` or ``token_ceiling``.
    """
    budget = (await client.input_budget(template_text, max_output_tokens=max_output_tokens)).tokens
    if not isinstance(budget, int):
        return query
    if budget > 0:
        if not isinstance(client, TokenBoundingClient):
            # `isinstance` fails on a method `getattr` still finds (supplied through `__getattr__`): then name both.
            missing = [method for method in _COUNTING_METHODS if not callable(getattr(client, method, None))] or _COUNTING_METHODS
            raise LlmCallFailure(provider, f"the client sizes its input to a budget but has no {' and '.join(missing)}")
        if len(query) <= budget and client.token_ceiling(query) <= budget:
            # Fast path: a token covers at least one byte, so a text whose request-free ceiling (its UTF-8 length plus the
            # client's own surcharge, see ``TokenBoundingClient.token_ceiling``) is within the budget cannot exceed it. A
            # text with more characters than the budget has more bytes too, so its ceiling is not even computed.
            return query
        fitted = await fit_text_to_budget(query, budget, client.count_tokens)
        if fitted or not query:
            return fitted
    raise NoRoomForInput(
        f"the model's context window ({describe_window(client.context_window())}) leaves {budget} input tokens after the "
        f"prompt and the {max_output_tokens}-token answer, too little for the query"
    )


async def fit_text_to_budget(text: str, budget: int | Unbounded, count: AsyncTokenCount) -> str:
    """The longest head of *text* (within the bounded number of counts) that *count* puts within *budget* tokens.

    Returns *text* itself when the budget is unbounded or the text fits, ``""`` when no head fits (a budget of 0, or
    none the count puts within it). A head may end inside the first word: a text with no space in reach (a URL, CJK) is
    cut on a character-cluster boundary, so a budget of a few tokens can leave a fragment of a word.
    """
    if not isinstance(budget, int) or not text:
        return text
    if budget <= 0:
        return ""
    reach = min(len(text), budget * MAX_CHARS_PER_TOKEN)
    target = budget * _TARGET_FILL_PERCENT // _PERCENT
    best = ""
    ceiling = reach + 1  # no head of this length or more is known to fit
    previous: tuple[int, int] | None = None  # (length, cost) of the count before this one
    position = reach
    for _ in range(MAX_FIT_COUNTS):
        candidate = _head(text, position)
        if len(candidate) <= len(best):
            break
        cost = await count(candidate)
        if cost <= budget:
            best = candidate
            if position >= reach or cost * _PERCENT >= budget * _ACCEPTED_FILL_PERCENT:
                break
        else:
            ceiling = len(candidate)
        estimate = _estimate_length(len(candidate), cost, previous, target)
        previous = (len(candidate), cost)
        margin = (ceiling - len(best)) // _BRACKET_MARGIN_DIVISOR
        position = min(max(estimate, len(best) + margin), ceiling - 1 - margin)
    return best


def _estimate_length(length: int, cost: int, previous: tuple[int, int] | None, target: int) -> int:
    """The head length expected to cost *target* tokens, from the last two counts.

    The line through the two counts when they rise together (a cost that is not proportional to the length, such as a
    dense head or a quadratic one, is followed instead of assumed away), else the cost per character of this count.
    """
    if previous is not None:
        previous_length, previous_cost = previous
        if (length - previous_length) * (cost - previous_cost) > 0:
            return length + (target - cost) * (length - previous_length) // (cost - previous_cost)
    return length * target // max(cost, 1)


def _head(text: str, position: int) -> str:
    """The head of *text* ending at or before *position* on a word or cluster boundary, trailing space removed."""
    if position >= len(text):
        return text
    return text[: _cut_index(text, position)].rstrip()


def _cut_index(text: str, position: int) -> int:
    """The largest index at or before *position* at which *text* may be cut: before a space, else on a cluster boundary."""
    for index in range(position, max(0, position - _WORD_LOOKBACK_CHARS), -1):
        if text[index].isspace():
            return index
    for index in range(position, max(0, position - _CLUSTER_LOOKBACK_CHARS), -1):
        if _is_cluster_boundary(text, index):
            return index
    return position


def _is_cluster_boundary(text: str, index: int) -> bool:
    """Whether *text* may be cut between ``text[index - 1]`` and ``text[index]`` without splitting what a reader sees as one character.

    Covers the clusters real queries hold: a surrogate pair stored as two code points, a combining mark or variation
    selector with its base, a zero-width joiner or non-joiner sequence (a family emoji, a Persian word), an emoji skin
    tone, an emoji tag sequence (a subdivision flag) and a flag (two regional indicators). Hangul jamo, viramas and the
    other rare prepend and extend rules are not modelled.
    """
    before, after = text[index - 1], text[index]
    if _HIGH_SURROGATES[0] <= before <= _HIGH_SURROGATES[1] and _LOW_SURROGATES[0] <= after <= _LOW_SURROGATES[1]:
        return False
    if before in _JOINERS or after in _JOINERS or unicodedata.category(after).startswith("M"):
        return False
    if _EMOJI_MODIFIERS[0] <= after <= _EMOJI_MODIFIERS[1] or _TAG_CHARACTERS[0] <= after <= _TAG_CHARACTERS[1]:
        return False
    if _is_regional_indicator(after) and _is_regional_indicator(before):
        return _regional_indicators_before(text, index) % 2 == 0
    return True


def _is_regional_indicator(character: str) -> bool:
    return _REGIONAL_INDICATORS[0] <= character <= _REGIONAL_INDICATORS[1]


def _regional_indicators_before(text: str, index: int) -> int:
    """How many regional indicators run up to *index* (looking back at most ``_FLAG_RUN_LOOKBACK_CHARS``): an odd number leaves a flag half open.

    A run longer than the bound is not a text anyone writes; the scan stops there, so a crafted query of nothing but
    regional indicators costs a bounded scan per cut instead of one over its whole length.
    """
    count = 0
    while count < min(index, _FLAG_RUN_LOOKBACK_CHARS) and _is_regional_indicator(text[index - 1 - count]):
        count += 1
    return count
