"""BE-4 (collection-descriptions plan, C1, S9, S11): sizing every prompt ahead of the call.

``llama_cpp`` counts exactly through its tokenize callback, ``ollama`` estimates with the gpt2 counter plus
``OLLAMA_ESTIMATE_MARGIN``, cloud is unbounded. The budget is the window minus the answer reserve minus the
template (the longest form, retry reminder included); a window too small for reserve and template gives 0.
"""

from __future__ import annotations

import threading

from archon_search.constants import ANSWER_RESERVE_TOKENS, ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client.budget import cloud_budget, estimated_budget, exact_budget
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

# The independent oracle for the margin: real tokens may be 6/5 of the gpt2 count, so the gpt2 room is 5/6 of the
# real room. Written as integers here, apart from ``OLLAMA_ESTIMATE_MARGIN``, so the test does not mirror the code.
_MARGIN_ROOM_NUMERATOR = 5
_MARGIN_ROOM_DENOMINATOR = 6
_LARGEST_REAL_OVER_GPT2 = 6 / 5


def _window(size: int, source: ContextWindowSource = ContextWindowSource.CONFIGURED) -> ContextWindowTokens:
    return ContextWindowTokens(size=size, source=source)


def _stub_counter() -> TokenCounter:
    return TokenCounter(load_tokenizer=StubTokenizer)


def test_cloud_budget_is_unbounded_and_51k_prompt_sent_whole() -> None:
    # Cloud takes no window and no counter: there is nothing to size with, so no prompt, however long, is trimmed.
    assert cloud_budget() == InputBudget(tokens=UNBOUNDED)


async def test_ollama_budget_applies_20_percent_margin_and_reserve() -> None:
    stub = StubTokenizer()
    counter = TokenCounter(load_tokenizer=lambda: stub)

    budget = await estimated_budget(_window(_WINDOW), _TEMPLATE, counter.count)

    gpt2_room = (_WINDOW - ANSWER_RESERVE_TOKENS) * _MARGIN_ROOM_NUMERATOR // _MARGIN_ROOM_DENOMINATOR
    assert budget == InputBudget(tokens=gpt2_room - _TEMPLATE_TOKENS)
    assert stub.calls == [_TEMPLATE]
    # Worst case the real count is 20% over the estimate: template + input + reserve still fit the window.
    assert isinstance(budget.tokens, int)
    assert (_TEMPLATE_TOKENS + budget.tokens) * _LARGEST_REAL_OVER_GPT2 + ANSWER_RESERVE_TOKENS <= _WINDOW


async def test_ollama_budget_of_assumed_window_loses_no_token_to_rounding() -> None:
    window = _window(ASSUMED_CONTEXT_TOKENS, ContextWindowSource.ASSUMED)

    budget = await estimated_budget(window, _TEMPLATE, _stub_counter().count)

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
    estimated_plain = await estimated_budget(window, _TEMPLATE, _stub_counter().count)
    estimated_longest = await estimated_budget(window, with_reminder, _stub_counter().count)

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
        assert await estimated_budget(window, _TEMPLATE, _stub_counter().count) == InputBudget(tokens=0)
        assert await exact_budget(window, _TEMPLATE, tokenize_count) == InputBudget(tokens=0)


async def test_budget_boundary_one_token_of_room() -> None:
    async def tokenize_count(text: str) -> int:
        return 5

    assert await exact_budget(_window(ANSWER_RESERVE_TOKENS + 5), _TEMPLATE, tokenize_count) == InputBudget(tokens=0)
    assert await exact_budget(_window(ANSWER_RESERVE_TOKENS + 6), _TEMPLATE, tokenize_count) == InputBudget(tokens=1)


async def test_ollama_estimate_runs_off_the_event_loop_thread() -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []

    def load() -> StubTokenizer:
        threads.append(threading.get_ident())  # the first count loads (and may download) the tokenizer
        return StubTokenizer()

    await estimated_budget(_window(_WINDOW), _TEMPLATE, TokenCounter(load_tokenizer=load).count)

    assert threads and threads[0] != loop_thread
