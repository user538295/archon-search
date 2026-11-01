"""Fitting a text to an input budget (``llm_client.fit``): the head of the text, cut at a word or grapheme boundary.

The count is the caller's: ``llama_cpp`` counts through its server, ``ollama`` through the gpt2 estimate. The helper
asks for a bounded number of counts and returns a text it has itself seen fit; an unbounded budget (cloud) returns
the text untouched without counting.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

import pytest

from archon_search.llm_client import fit as fit_module
from archon_search.llm_client.fit import (
    NoRoomForInput,
    describe_window,
    fit_query_to_window,
    fit_text_to_budget,
)
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
)
from archon_search.llm_client.token_counter import TokenCounter
from tests._hyde_stubs import BudgetedLlmClient, ScriptedLlmClient
from tests._query_sizing_helpers import ACCEPTED_FILL_PERCENT, per_character_cost, words
from tests._token_counter_stub import StubTokenizer

_ZWJ = "\u200d"
_ZWNJ = "\u200c"
_EMOJI_MODIFIER_FIRST = "\U0001f3fb"
_EMOJI_MODIFIER_LAST = "\U0001f3ff"
_TAG_FIRST = "\U000e0020"
_TAG_LAST = "\U000e007f"
# The regional indicators, two of which make a flag.
_FLAG_LETTERS = "".join(chr(code) for code in range(0x1F1E6, 0x1F1FF + 1))
_HIGH_SURROGATE = "\ud83d"
_LOW_SURROGATE = "\ude00"
# The helper's documented limits (Architecture 210), restated here apart from the code under test.
_MOST_COUNTS = 10
_MOST_COUNTS_FOR_A_LINEAR_TEXT = 3
_LOOKAHEAD_CHARS_PER_TOKEN = 16

TokenCost = Callable[[str], int]


def _words_cost(text: str) -> int:
    return len(text.split())


class _Counter:
    """Counts with ``token_cost`` and records every text it was asked about."""

    def __init__(self, token_cost: TokenCost = _words_cost) -> None:
        self._token_cost = token_cost
        self.texts: list[str] = []

    async def __call__(self, text: str) -> int:
        self.texts.append(text)
        return self._token_cost(text)


# --- unbounded, empty, whole -------------------------------------------------------------------------------------


async def test_an_unbounded_budget_returns_the_text_untouched_without_counting() -> None:
    counter = _Counter()
    text = words(60_000)  # roughly the 51k-token prompt of S9

    assert await fit_text_to_budget(text, UNBOUNDED, counter) is text
    assert counter.texts == []


async def test_an_empty_text_is_returned_without_counting() -> None:
    counter = _Counter()

    assert await fit_text_to_budget("", 100, counter) == ""
    assert counter.texts == []


async def test_a_text_that_fits_is_returned_untouched_after_one_count() -> None:
    counter = _Counter()
    text = words(100)

    assert await fit_text_to_budget(text, 100, counter) is text
    assert counter.texts == [text]


async def test_a_budget_of_zero_fits_nothing_and_counts_nothing() -> None:
    counter = _Counter()

    assert await fit_text_to_budget("some words here", 0, counter) == ""
    assert counter.texts == []


# --- trimming -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", range(20, 60))  # a cut that lands on a space by luck would not survive every budget
async def test_english_is_cut_to_a_prefix_ending_on_a_whole_word_within_the_budget(budget: int) -> None:
    counter = _Counter()
    text = words(5000)

    fitted = await fit_text_to_budget(text, budget, counter)

    assert text.startswith(fitted)
    assert text[len(fitted)] == " ", "the cut falls before a space, so the last word is whole"
    assert fitted == fitted.rstrip()
    assert budget * ACCEPTED_FILL_PERCENT // 100 <= len(fitted.split()) <= budget


async def test_cjk_text_without_spaces_is_cut_on_a_character_within_the_budget() -> None:
    counter = _Counter(per_character_cost)
    text = "検索拡張生成" * 2000
    budget = 300

    fitted = await fit_text_to_budget(text, budget, counter)

    assert text.startswith(fitted)
    assert budget * ACCEPTED_FILL_PERCENT // 100 <= len(fitted) <= budget


async def test_a_digit_query_fits_by_the_digit_aware_estimate() -> None:
    estimate = TokenCounter(load_tokenizer=StubTokenizer).count
    counter = _Counter(estimate)
    text = "1234567890" * 3000  # one digit run: every digit counts one token
    budget = 400

    fitted = await fit_text_to_budget(text, budget, counter)

    assert text.startswith(fitted)
    assert budget * ACCEPTED_FILL_PERCENT // 100 <= estimate(fitted) <= budget


def _digits_plus_words(text: str) -> int:
    return sum(1 for character in text if character.isdigit()) + len(text.split())


async def test_a_text_whose_dense_head_costs_more_than_its_average_is_still_fitted() -> None:
    # 400 digits (one token each) then a long run of cheap words: the average density misleads the first estimate.
    counter = _Counter(_digits_plus_words)
    text = "7" * 400 + " " + words(20_000)
    budget = 450

    fitted = await fit_text_to_budget(text, budget, counter)

    assert fitted
    assert text.startswith(fitted)
    assert budget * ACCEPTED_FILL_PERCENT // 100 <= _digits_plus_words(fitted) <= budget


async def test_nothing_fitting_gives_an_empty_text_after_a_bounded_number_of_counts() -> None:
    counter = _Counter(lambda text: 10_000)  # every text costs more than the budget

    assert await fit_text_to_budget(words(500), 50, counter) == ""
    assert len(counter.texts) <= _MOST_COUNTS


@pytest.mark.parametrize(
    ("text", "token_cost"),
    [
        (words(100_000), _words_cost),
        ("検索" * 100_000, per_character_cost),
        ("a" * 200_000, lambda text: len(text) // 3 + 1),
        (words(50_000), lambda text: len(text.split()) ** 2 // 50 + 1),  # super-linear: the estimate keeps missing
    ],
    ids=["english", "cjk", "one-long-word", "super-linear"],
)
async def test_the_number_of_counts_is_bounded_and_the_result_is_counted_to_fit_the_budget_well(
    text: str, token_cost: TokenCost
) -> None:
    counter = _Counter(token_cost)
    budget = 500

    fitted = await fit_text_to_budget(text, budget, counter)

    assert len(counter.texts) <= _MOST_COUNTS
    assert fitted
    assert budget * ACCEPTED_FILL_PERCENT // 100 <= token_cost(fitted) <= budget
    assert fitted in counter.texts, "the result is a text the count has seen, not an extrapolation"


@pytest.mark.parametrize(
    ("text", "token_cost"),
    [(words(100_000), _words_cost), ("検索" * 100_000, per_character_cost), ("a" * 200_000, lambda text: len(text) // 3 + 1)],
    ids=["english", "cjk", "one-long-word"],
)
async def test_a_text_with_a_steady_cost_per_character_needs_few_counts(text: str, token_cost: TokenCost) -> None:
    """Each count is a request to the server for ``llama_cpp``: the first estimate lands the cut within a count or two."""
    counter = _Counter(token_cost)

    await fit_text_to_budget(text, 500, counter)

    assert len(counter.texts) <= _MOST_COUNTS_FOR_A_LINEAR_TEXT


async def test_a_text_far_longer_than_the_budget_is_not_counted_in_full() -> None:
    counter = _Counter()
    budget = 10
    text = words(2_000_000)  # megabytes: counting it whole would stall the search

    fitted = await fit_text_to_budget(text, budget, counter)

    assert max(len(counted) for counted in counter.texts) <= budget * _LOOKAHEAD_CHARS_PER_TOKEN
    assert text.startswith(fitted)
    assert len(fitted.split()) <= budget


async def test_a_failing_count_propagates() -> None:
    async def failing(text: str) -> int:
        raise LlmCallFailure("llama_cpp", "cannot connect to the llama-server")

    with pytest.raises(LlmCallFailure, match="cannot connect"):
        await fit_text_to_budget(words(1000), 10, failing)


# --- boundaries ---------------------------------------------------------------------------------------------------


def _is_mid_cluster(text: str, fitted: str) -> bool:
    """Whether the cut between *fitted* and the rest of *text* separates a grapheme cluster or a surrogate pair."""
    if len(fitted) >= len(text):
        return False
    before, after = fitted[-1], text[len(fitted)]
    if before in (_ZWJ, _ZWNJ) or after in (_ZWJ, _ZWNJ) or unicodedata.category(after).startswith("M"):
        return True
    if "\ud800" <= before <= "\udbff" and "\udc00" <= after <= "\udfff":
        return True
    if _EMOJI_MODIFIER_FIRST <= after <= _EMOJI_MODIFIER_LAST:  # a skin tone belongs to the emoji before it
        return True
    if _TAG_FIRST <= after <= _TAG_LAST:  # a tag belongs to the flag emoji before it
        return True
    trailing_flag_halves = len(fitted) - len(fitted.rstrip(_FLAG_LETTERS))
    return after in _FLAG_LETTERS and trailing_flag_halves % 2 == 1


@pytest.mark.parametrize(
    "unit",
    [
        "e\u0301",  # a letter and its combining accent
        "\U0001f468" + _ZWJ + "\U0001f469" + _ZWJ + "\U0001f467",  # a ZWJ family
        "\u0645\u06cc" + _ZWNJ + "\u062e\u0648\u0627\u0647\u0645",  # a Persian word with a zero-width non-joiner
        "\U0001f44d\U0001f3fd",  # an emoji and its skin tone
        "\U0001f1ed\U0001f1fa",  # a flag: two regional indicators
        "\U0001f3f4" + "".join(chr(0xE0000 + ord(letter)) for letter in "gbeng") + "\U000e007f",  # the flag of England: a tag sequence
        _HIGH_SURROGATE + _LOW_SURROGATE,  # a pair stored as two code points (lone halves of one character)
    ],
    ids=["combining", "zwj-family", "zwnj-word", "skin-tone", "flag", "tag-flag", "split-surrogate-pair"],
)
@pytest.mark.parametrize("budget", range(40, 50))  # every phase of the cut position against the unit length
async def test_a_cut_never_separates_a_grapheme_cluster_or_a_surrogate_pair(unit: str, budget: int) -> None:
    # One token per code point: the cut position moves one character per budget step, so every offset into a unit is tried.
    counter = _Counter(len)
    text = unit * 200

    fitted = await fit_text_to_budget(text, budget, counter)

    assert fitted
    assert text.startswith(fitted)
    assert not _is_mid_cluster(text, fitted)


@pytest.mark.parametrize("budget", range(50, 54))
async def test_a_head_never_ends_in_a_carriage_return_that_belongs_to_a_crlf(budget: int) -> None:
    counter = _Counter(len)
    text = "ab\r\n" * 300

    fitted = await fit_text_to_budget(text, budget, counter)

    assert not fitted.endswith("\r")


async def test_a_word_cut_looks_back_only_a_bounded_distance() -> None:
    # A short word, then one 5000-character word: the only space is far behind the cut, beyond the word lookback, so
    # the cut must be a character boundary near the budget and not the early space.
    counter = _Counter(len)
    text = "a " + "x" * 5000

    fitted = await fit_text_to_budget(text, 1000, counter)

    assert len(fitted) >= 1000 * ACCEPTED_FILL_PERCENT // 100


async def test_a_text_of_nothing_but_flag_letters_is_cut_with_a_scan_bounded_by_the_flag_run_limit() -> None:
    """A crafted query of regional indicators must not cost a scan of its whole length at every cut."""
    text = "\U0001f1fa" * 200_000
    budget = 5000

    fitted = await fit_text_to_budget(text, budget, _Counter(len))

    assert text.startswith(fitted)
    assert 0 < len(fitted) <= budget
    assert fit_module._regional_indicators_before(text, len(text) // 2) <= fit_module._FLAG_RUN_LOOKBACK_CHARS


# --- the query step -----------------------------------------------------------------------------------------------


async def test_an_unbounded_client_gets_the_query_whole() -> None:
    client = ScriptedLlmClient()  # no count_tokens at all: a cloud client never counts
    query = words(60_000)

    assert await fit_query_to_window(client, "template", query, provider="openai") is query


async def test_the_query_is_sized_against_the_clients_budget_for_the_template() -> None:
    client = BudgetedLlmClient(window_tokens=4096, budget=40)

    fitted = await fit_query_to_window(client, "the template", words(500), provider="ollama")

    assert client.templates == ["the template"]
    assert len(fitted.split()) <= 40


async def test_an_empty_query_with_room_is_returned_as_it_is() -> None:
    client = BudgetedLlmClient(window_tokens=4096, budget=40)

    assert await fit_query_to_window(client, "the template", "", provider="ollama") == ""


async def test_a_zero_budget_has_no_room_for_the_query_and_names_the_window_not_the_query() -> None:
    client = BudgetedLlmClient(window_tokens=1100, budget=0)

    with pytest.raises(NoRoomForInput) as raised:
        await fit_query_to_window(client, "the template", "very-unique-secret-query", provider="ollama")

    assert "1100 tokens" in raised.value.detail
    assert "very-unique-secret-query" not in raised.value.detail
    assert client.counted == []


async def test_a_zero_budget_is_no_room_even_for_a_client_that_cannot_count() -> None:
    class CannotCount(ScriptedLlmClient):
        async def input_budget(self, template_text: str) -> InputBudget:
            return InputBudget(0)

    with pytest.raises(NoRoomForInput):
        await fit_query_to_window(CannotCount(), "the template", "a query", provider="llama_cpp")


async def test_a_budget_too_small_for_even_one_word_has_no_room() -> None:
    client = BudgetedLlmClient(window_tokens=1100, budget=4, tokens_per_word=5)  # not one word fits a budget of 4

    with pytest.raises(NoRoomForInput):
        await fit_query_to_window(client, "the template", words(50), provider="ollama")


async def test_a_client_with_a_budget_that_cannot_count_is_a_failure() -> None:
    class CannotCount(ScriptedLlmClient):
        async def input_budget(self, template_text: str) -> InputBudget:
            return InputBudget(100)

    with pytest.raises(LlmCallFailure, match="cannot count tokens") as raised:
        await fit_query_to_window(CannotCount(), "the template", "a query", provider="llama_cpp")

    assert raised.value.provider == "llama_cpp"


# --- the byte-bound fast path -------------------------------------------------------------------------------------


async def test_a_query_whose_ceiling_fits_the_budget_is_returned_untouched_without_any_count() -> None:
    """A token covers at least one byte, so a text of at most ``budget`` bytes cannot cost more than ``budget`` tokens."""
    client = BudgetedLlmClient(window_tokens=4096, budget=200)
    query = words(10)

    assert await fit_query_to_window(client, "the template", query, provider="llama_cpp") is query
    assert client.counted == []
    assert client.templates == ["the template"]


async def test_an_empty_query_needs_no_count() -> None:
    client = BudgetedLlmClient(window_tokens=4096, budget=40)

    assert await fit_query_to_window(client, "the template", "", provider="llama_cpp") == ""
    assert client.counted == []


async def test_a_query_exactly_at_the_ceiling_goes_untouched_and_one_byte_more_is_counted() -> None:
    at_the_ceiling = "x" * 40
    client = BudgetedLlmClient(window_tokens=4096, budget=40)

    assert await fit_query_to_window(client, "the template", at_the_ceiling, provider="llama_cpp") is at_the_ceiling
    assert client.counted == []

    over_by_one_byte = "x" * 41  # one long word: the count says one token, the byte bound cannot know that
    assert await fit_query_to_window(client, "the template", over_by_one_byte, provider="llama_cpp") == over_by_one_byte
    assert client.counted == [over_by_one_byte]


async def test_a_query_over_the_ceiling_is_counted_and_cut_as_before() -> None:
    client = BudgetedLlmClient(window_tokens=4096, budget=40)

    fitted = await fit_query_to_window(client, "the template", words(500), provider="llama_cpp")

    assert client.counted, "only a text longer than the budget in bytes goes through the counting loop"
    assert len(fitted.split()) <= 40


async def test_the_ceiling_counts_bytes_not_characters() -> None:
    """Thirty CJK characters are within a budget of 50 as characters but ninety bytes: the byte bound does not apply."""
    client = BudgetedLlmClient(window_tokens=4096, budget=50)
    query = "検" * 30

    assert await fit_query_to_window(client, "the template", query, provider="llama_cpp") == query
    assert client.counted == [query], "counted once, and it fits (the stub counts the unspaced run as one word)"


async def test_a_query_longer_than_the_budget_in_characters_is_not_encoded_to_find_its_ceiling() -> None:
    """Characters never outnumber bytes, so a text with more characters than the budget has too many bytes too: the
    ceiling (an O(n) pass over a possibly megabyte-long query) is not computed for it."""
    ceilings: list[str] = []

    class Spying(BudgetedLlmClient):
        def token_ceiling(self, text: str) -> int:
            ceilings.append(text)
            return super().token_ceiling(text)

    client = Spying(window_tokens=4096, budget=40)

    await fit_query_to_window(client, "the template", words(500), provider="llama_cpp")

    assert ceilings == []


async def test_a_ceiling_above_the_budget_is_not_a_verdict_the_count_decides() -> None:
    """Eight one-letter words are fifteen bytes (above a budget of 10) but eight tokens: the count lets the whole text through."""
    client = BudgetedLlmClient(window_tokens=4096, budget=10)
    query = "a b c d e f g h"

    assert await fit_query_to_window(client, "the template", query, provider="llama_cpp") == query
    assert client.counted == [query]


@pytest.mark.parametrize("text", ["plain words in a short query", "検索拡張生成", "🙂 emoji 👍🏽", "1 2 3 4", ""])
async def test_the_fast_path_returns_what_the_counting_path_would(text: str) -> None:
    """With the worst-case count (a token per byte), a text the ceiling lets through is also one the count says fits whole."""
    budget = len(text.encode("utf-8")) + 1
    counter = _Counter(lambda candidate: len(candidate.encode("utf-8")))

    assert await fit_text_to_budget(text, budget, counter) == text


def test_describe_window_names_size_and_source_or_unbounded() -> None:
    assert describe_window(ContextWindowTokens(size=4096, source=ContextWindowSource.ASSUMED)) == "4096 tokens, assumed"
    assert describe_window(ContextWindowTokens(size=8192, source=ContextWindowSource.PROBED)) == "8192 tokens, probed"
    assert describe_window(UNBOUNDED) == "unbounded"
