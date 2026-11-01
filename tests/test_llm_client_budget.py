"""BE-4 (collection-descriptions plan, C1, S9, S11): sizing every prompt ahead of the call.

``llama_cpp`` counts exactly through its tokenize callback, ``ollama`` estimates with the token estimate (``TokenCounter.count``) plus
``OLLAMA_ESTIMATE_MARGIN``, cloud is unbounded. The budget is the window minus the answer reserve minus the
template (the longest form, retry reminder included); a window too small for reserve and template gives 0.
"""

from __future__ import annotations

import asyncio

import pytest

from archon_search.constants import ANSWER_RESERVE_TOKENS, ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client.budget import (
    MAX_CACHED_TEMPLATES,
    AsyncTokenCount,
    TemplateCostCache,
    cloud_budget,
    estimated_budget,
    exact_budget,
)
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
)
from archon_search.llm_client.token_counter import TokenCounter
from tests._token_counter_stub import StubTokenizer

_RETRY_REMINDER = "Reminder: the answer must be one line of at most 600 characters."
_RETRY_REMINDER_TOKENS = 12  # StubTokenizer: one token per whitespace-separated word
_TEMPLATE = "Describe this collection in one sentence based on the excerpts below."
_TEMPLATE_TOKENS = 11
_WINDOW = 8192

# The independent oracle for the margin: real tokens may be 6/5 of the token estimate, so the room in estimate tokens is
# 5/6 of the real room. Written as integers here, apart from ``OLLAMA_ESTIMATE_MARGIN``, so the test does not mirror the code.
_MARGIN_ROOM_NUMERATOR = 5
_MARGIN_ROOM_DENOMINATOR = 6
_LARGEST_REAL_OVER_GPT2 = 6 / 5


def _window(size: int, source: ContextWindowSource = ContextWindowSource.CONFIGURED) -> ContextWindowTokens:
    return ContextWindowTokens(size=size, source=source)


def _estimate_with(counter: TokenCounter) -> AsyncTokenCount:
    async def estimate(text: str) -> int:
        return counter.count(text)

    return estimate


def _stub_estimate() -> AsyncTokenCount:
    return _estimate_with(TokenCounter(load_tokenizer=StubTokenizer))


def test_cloud_budget_is_unbounded_and_51k_prompt_sent_whole() -> None:
    # Cloud takes no window and no counter: there is nothing to size with, so no prompt, however long, is trimmed.
    assert cloud_budget() == InputBudget(tokens=UNBOUNDED)


async def test_ollama_budget_applies_20_percent_margin_and_reserve() -> None:
    stub = StubTokenizer()
    counter = TokenCounter(load_tokenizer=lambda: stub)

    budget = await estimated_budget(_window(_WINDOW), _TEMPLATE, _estimate_with(counter))

    gpt2_room = (_WINDOW - ANSWER_RESERVE_TOKENS) * _MARGIN_ROOM_NUMERATOR // _MARGIN_ROOM_DENOMINATOR
    assert budget == InputBudget(tokens=gpt2_room - _TEMPLATE_TOKENS)
    assert stub.calls == [_TEMPLATE]
    # Worst case the real count is 20% over the estimate: template + input + reserve still fit the window.
    assert isinstance(budget.tokens, int)
    assert (_TEMPLATE_TOKENS + budget.tokens) * _LARGEST_REAL_OVER_GPT2 + ANSWER_RESERVE_TOKENS <= _WINDOW


async def test_ollama_budget_of_assumed_window_loses_no_token_to_rounding() -> None:
    window = _window(ASSUMED_CONTEXT_TOKENS, ContextWindowSource.ASSUMED)

    budget = await estimated_budget(window, _TEMPLATE, _stub_estimate())

    # (4096 - 1024) / 1.2 = 2560 exactly, minus the 11-word template.
    assert ASSUMED_CONTEXT_TOKENS == 4096 and ANSWER_RESERVE_TOKENS == 1024
    assert budget == InputBudget(tokens=2549)


async def test_llama_cpp_budget_uses_exact_count() -> None:
    seen: list[str] = []

    async def tokenize_count(text: str) -> int:
        seen.append(text)
        return 100  # the server's exact count, unrelated to the word count

    budget = await exact_budget(_window(_WINDOW, ContextWindowSource.PROBED), _TEMPLATE, tokenize_count)

    assert budget == InputBudget(tokens=_WINDOW - ANSWER_RESERVE_TOKENS - 100)  # no margin
    assert seen == [_TEMPLATE]


async def test_budget_reserves_room_for_retry_reminder() -> None:
    # Pins that sizing counts exactly the text it is given: a caller that passes the template WITH the reminder
    # (``LlmClient.input_budget``) gets a budget smaller by the reminder's tokens, so the retry still fits.
    seen: list[str] = []

    async def tokenize_count(text: str) -> int:
        seen.append(text)
        return len(text.split())

    window = _window(_WINDOW, ContextWindowSource.PROBED)
    with_reminder = f"{_TEMPLATE}\n{_RETRY_REMINDER}"

    plain = await exact_budget(window, _TEMPLATE, tokenize_count)
    longest = await exact_budget(window, with_reminder, tokenize_count)
    estimated_plain = await estimated_budget(window, _TEMPLATE, _stub_estimate())
    estimated_longest = await estimated_budget(window, with_reminder, _stub_estimate())

    assert seen == [_TEMPLATE, with_reminder]
    assert isinstance(plain.tokens, int) and isinstance(longest.tokens, int)
    assert plain.tokens - longest.tokens == _RETRY_REMINDER_TOKENS
    assert isinstance(estimated_plain.tokens, int) and isinstance(estimated_longest.tokens, int)
    # The estimate also counts the three digits of "600" (every digit is a token, the placeholder is one of the words).
    assert estimated_plain.tokens - estimated_longest.tokens == _RETRY_REMINDER_TOKENS + 3


async def test_window_too_small_for_reserve_and_template_gives_zero_budget() -> None:
    async def tokenize_count(text: str) -> int:
        return 5

    reserve_only = _window(ANSWER_RESERVE_TOKENS)  # reserve alone fills it
    template_overflows = _window(ANSWER_RESERVE_TOKENS + 4)  # positive room, smaller than the template
    below_reserve = _window(1)

    for window in (reserve_only, template_overflows, below_reserve):
        assert await estimated_budget(window, _TEMPLATE, _stub_estimate()) == InputBudget(tokens=0)
        assert await exact_budget(window, _TEMPLATE, tokenize_count) == InputBudget(tokens=0)


async def test_budget_boundary_one_token_of_room() -> None:
    async def tokenize_count(text: str) -> int:
        return 5

    assert await exact_budget(_window(ANSWER_RESERVE_TOKENS + 5), _TEMPLATE, tokenize_count) == InputBudget(tokens=0)
    assert await exact_budget(_window(ANSWER_RESERVE_TOKENS + 6), _TEMPLATE, tokenize_count) == InputBudget(tokens=1)


# --- the template cost is a fixed part: counted once ----------------------------------------------------------------


async def test_a_template_is_counted_once_however_often_the_budget_is_asked_for() -> None:
    cache = TemplateCostCache()
    seen: list[str] = []

    async def tokenize_count(text: str) -> int:
        seen.append(text)
        return 100

    window = _window(_WINDOW, ContextWindowSource.PROBED)
    budgets = [await exact_budget(window, _TEMPLATE, cache.memoize_async(tokenize_count)) for _ in range(5)]

    assert budgets == [InputBudget(tokens=_WINDOW - ANSWER_RESERVE_TOKENS - 100)] * 5
    assert seen == [_TEMPLATE]


async def test_the_budget_follows_the_window_while_the_template_count_is_kept() -> None:
    """What is cached is the count of the template, which does not depend on the window: a window that resolves later
    (BE-10) is read live each time, so a budget computed under the assumed window is never served after the answer."""
    cache = TemplateCostCache()
    seen: list[str] = []

    async def tokenize_count(text: str) -> int:
        seen.append(text)
        return 100

    count = cache.memoize_async(tokenize_count)
    assumed = await exact_budget(_window(ASSUMED_CONTEXT_TOKENS, ContextWindowSource.ASSUMED), _TEMPLATE, count)
    probed = await exact_budget(_window(_WINDOW, ContextWindowSource.PROBED), _TEMPLATE, count)

    assert assumed == InputBudget(tokens=ASSUMED_CONTEXT_TOKENS - ANSWER_RESERVE_TOKENS - 100)
    assert probed == InputBudget(tokens=_WINDOW - ANSWER_RESERVE_TOKENS - 100)
    assert seen == [_TEMPLATE]


async def test_each_template_has_its_own_entry() -> None:
    cache = TemplateCostCache()
    seen: list[str] = []

    async def tokenize_count(text: str) -> int:
        seen.append(text)
        return len(text.split())

    count = cache.memoize_async(tokenize_count)
    first = await count(_TEMPLATE)
    second = await count(_RETRY_REMINDER)
    again = [await count(_TEMPLATE), await count(_RETRY_REMINDER)]

    assert (first, second) == (_TEMPLATE_TOKENS, _RETRY_REMINDER_TOKENS)
    assert again == [first, second]
    assert seen == [_TEMPLATE, _RETRY_REMINDER]


async def test_a_failing_count_is_not_remembered() -> None:
    cache = TemplateCostCache()
    attempts: list[str] = []

    async def flaky(text: str) -> int:
        attempts.append(text)
        if len(attempts) == 1:
            raise OSError("tokenize endpoint unreachable")
        return 7

    count = cache.memoize_async(flaky)
    with pytest.raises(OSError, match="unreachable"):
        await count(_TEMPLATE)

    assert await count(_TEMPLATE) == 7
    assert await count(_TEMPLATE) == 7
    assert attempts == [_TEMPLATE, _TEMPLATE]


async def test_clear_makes_every_template_be_counted_again() -> None:
    cache = TemplateCostCache()
    seen: list[str] = []

    async def tokenize_count(text: str) -> int:
        seen.append(text)
        return 1

    count = cache.memoize_async(tokenize_count)
    await count(_TEMPLATE)
    cache.clear()
    await count(_TEMPLATE)

    assert seen == [_TEMPLATE, _TEMPLATE]


async def test_a_count_in_flight_when_the_cache_is_cleared_is_not_stored() -> None:
    """A probe that clears the cache while a count of the old server is awaited must not see that count put back."""
    cache = TemplateCostCache()
    started = asyncio.Event()
    release = asyncio.Event()
    answers = iter([100, 200])

    async def tokenize_count(text: str) -> int:
        answer = next(answers)
        started.set()
        if answer == 100:
            await release.wait()
        return answer

    count = cache.memoize_async(tokenize_count)
    in_flight = asyncio.create_task(count(_TEMPLATE))
    await started.wait()
    cache.clear()
    release.set()

    assert await in_flight == 100  # the caller that asked still gets the answer it waited for
    assert await count(_TEMPLATE) == 200  # but it was not remembered: the new server is asked
    assert await count(_TEMPLATE) == 200


async def test_a_full_cache_forgets_only_the_oldest_template() -> None:
    cache = TemplateCostCache()
    seen: list[str] = []

    async def tokenize_count(text: str) -> int:
        seen.append(text)
        return 1

    count = cache.memoize_async(tokenize_count)
    templates = [f"template number {number}" for number in range(MAX_CACHED_TEMPLATES + 1)]
    for template in templates:
        await count(template)
    seen.clear()

    for template in templates[1:]:
        await count(template)
    assert seen == []  # every template but the oldest is still kept

    await count(templates[0])
    assert seen == [templates[0]]


async def test_remembering_a_template_that_is_already_kept_evicts_nothing() -> None:
    """Two first requests that arrive together both count the template; the second must not push another one out."""
    cache = TemplateCostCache()
    release = asyncio.Event()
    seen: list[str] = []

    async def tokenize_count(text: str) -> int:
        seen.append(text)
        if text == "contended":
            await release.wait()
        return 1

    count = cache.memoize_async(tokenize_count)
    kept = [f"template number {number}" for number in range(MAX_CACHED_TEMPLATES - 1)]
    for template in kept:
        await count(template)
    first = asyncio.create_task(count("contended"))
    second = asyncio.create_task(count("contended"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    seen.clear()

    for template in [*kept, "contended"]:
        await count(template)

    assert seen == []
