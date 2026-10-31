"""Tests for ``archon_search.description_generator`` (collection descriptions BE-22, contract C2).

The generator draws a deterministic sample of open chunks through the store port, fits it to the client's input budget,
makes one call on the shared LLM client and returns a typed outcome. It never raises for a provider or store failure.
"""
from __future__ import annotations

import ast
import asyncio
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from archon_search.config import DescriptionConfig
from archon_search.constants import (
    ANSWER_RESERVE_TOKENS,
    DEFAULT_FAST_MODEL,
    DEFAULT_MODEL,
)
from archon_search.description_generator import (
    DescriptionFailed,
    DescriptionFailureReason,
    DescriptionGenerated,
    DescriptionGenerator,
    _should_regenerate,
)
from archon_search.description_sample import StoreReadError
from archon_search.llm_client.protocol import (
    ContextWindow,
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
)
from tests._hyde_stubs import ScriptedLlmClient

_COLLECTION = "handbook"
_NAMESPACE = "default"
_CHUNK_COUNT = 1000
_SEPARATOR = "\n\n---\n\n"
_DESCRIPTION_TEXT = "A handbook of company policies."
_SMALL_WINDOW_BUDGET_TOKENS = 300
_LARGE_WINDOW_BUDGET_TOKENS = 200_000
_SMALL_WINDOW_SIZE = 4096
_LARGE_WINDOW_SIZE = 204_800


def _chunk_id(index: int) -> str:
    return f"chunk-{index:05d}"


def _excerpt(chunk_id: str) -> str:
    return f"excerpt of {chunk_id}"


class FakeSampleStore:
    """The two port reads over ``chunk_count`` open chunks (ids already sorted, as the real port returns them)."""

    def __init__(
        self,
        chunk_count: int = _CHUNK_COUNT,
        *,
        list_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self._ids = [_chunk_id(i) for i in range(chunk_count)]
        self._list_error = list_error
        self._read_error = read_error
        self.list_calls: list[tuple[str, str, bool]] = []
        self.read_calls: list[tuple[str, str, list[str], bool]] = []

    async def list_chunk_ids(self, collection: str, namespace: str, include_restricted: bool = False) -> list[str]:
        self.list_calls.append((collection, namespace, include_restricted))
        if self._list_error is not None:
            raise self._list_error
        return list(self._ids)

    async def read_excerpts(
        self, collection: str, namespace: str, chunk_ids: list[str], include_restricted: bool = False
    ) -> list[str]:
        self.read_calls.append((collection, namespace, list(chunk_ids), include_restricted))
        if self._read_error is not None:
            raise self._read_error
        return [_excerpt(chunk_id) for chunk_id in chunk_ids]


class CountingClient(ScriptedLlmClient):
    """A client with a token window: one token per character, a fixed input budget, a recorded sizing template."""

    def __init__(self, *, budget_tokens: int, window: ContextWindow, **scripted: Any) -> None:
        super().__init__(**scripted)
        self._budget_tokens = budget_tokens
        self._window = window
        self.sized_templates: list[str] = []

    def context_window(self) -> ContextWindow:
        return self._window

    async def input_budget(self, template_text: str) -> InputBudget:
        self.sized_templates.append(template_text)
        return InputBudget(self._budget_tokens)

    async def count_tokens(self, text: str) -> int:
        return len(text)


def _small_window_client(**scripted: Any) -> CountingClient:
    window = ContextWindowTokens(size=_SMALL_WINDOW_SIZE, source=ContextWindowSource.CONFIGURED)
    return CountingClient(budget_tokens=_SMALL_WINDOW_BUDGET_TOKENS, window=window, reply=_DESCRIPTION_TEXT, **scripted)


def _large_window_client(**scripted: Any) -> CountingClient:
    window = ContextWindowTokens(size=_LARGE_WINDOW_SIZE, source=ContextWindowSource.CONFIGURED)
    return CountingClient(budget_tokens=_LARGE_WINDOW_BUDGET_TOKENS, window=window, reply=_DESCRIPTION_TEXT, **scripted)


def _generator(client: Any, store: FakeSampleStore, **config: Any) -> DescriptionGenerator:
    return DescriptionGenerator(DescriptionConfig(**config), client, store)


def _read_ids(store: FakeSampleStore) -> list[str]:
    assert len(store.read_calls) >= 1
    return store.read_calls[-1][2]


def _expected_picks(total: int, count: int) -> list[str]:
    """The contract's picks: the ids at positions floor((j + 0.5) * total / count), in exact arithmetic."""
    return [_chunk_id(int((Fraction(j) + Fraction(1, 2)) * total / count)) for j in range(count)]


class TestConstants:
    def test_default_models(self) -> None:
        """The model ids ``[description].model`` and the other features' models default to."""
        assert DEFAULT_FAST_MODEL == "claude-haiku-4-5-20251001"
        assert DEFAULT_MODEL == "claude-sonnet-4-6"


class TestSampling:
    @pytest.mark.asyncio
    async def test_sample_evenly_spaced_over_all_open_chunks_and_identical_each_run(self) -> None:
        """The picks sit at floor((j + 0.5) * M / N) over ALL M ids, not at the head, and repeat on every run."""
        picks_per_run: list[list[str]] = []
        for _ in range(2):
            store = FakeSampleStore()
            outcome = await _generator(ScriptedLlmClient(reply=_DESCRIPTION_TEXT), store).generate(_COLLECTION, _NAMESPACE)
            assert isinstance(outcome, DescriptionGenerated)
            picks_per_run.append(_read_ids(store))

        assert picks_per_run[0] == picks_per_run[1]
        assert picks_per_run[0] == _expected_picks(_CHUNK_COUNT, 100)
        assert picks_per_run[0][0] != _chunk_id(0)
        assert picks_per_run[0][-1] > _chunk_id(_CHUNK_COUNT - 2 * (_CHUNK_COUNT // 100))

    @pytest.mark.asyncio
    async def test_fewer_open_chunks_than_the_cap_are_all_sampled(self) -> None:
        store = FakeSampleStore(chunk_count=7)

        await _generator(ScriptedLlmClient(reply=_DESCRIPTION_TEXT), store).generate(_COLLECTION, _NAMESPACE)

        assert _read_ids(store) == [_chunk_id(i) for i in range(7)]

    @pytest.mark.asyncio
    async def test_small_window_fits_large_window_larger_and_both_capped_at_100(self) -> None:
        """S10: the small window's sample is smaller than the large one's, neither passes ``max_sample_chunks``, and a
        small window still samples the whole collection (spaced over all ids), never its head."""
        small_store, large_store = FakeSampleStore(), FakeSampleStore()
        small_client, large_client = _small_window_client(), _large_window_client()

        small = await _generator(small_client, small_store).generate(_COLLECTION, _NAMESPACE)
        large = await _generator(large_client, large_store).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(small, DescriptionGenerated) and isinstance(large, DescriptionGenerated)
        small_ids, large_ids = _read_ids(small_store), _read_ids(large_store)
        assert 0 < len(small_ids) < len(large_ids) == 100
        assert small_ids == _expected_picks(_CHUNK_COUNT, len(small_ids))
        assert small_ids[-1] > _chunk_id(_CHUNK_COUNT // 2)

    @pytest.mark.asyncio
    async def test_shrink_loop_sends_the_largest_sample_the_loop_finds_within_the_budget(self) -> None:
        """With 22-character excerpts and a 300-token budget the loop goes 100 -> 10 (min(99, 100 * 300 // 2893)) in two
        reads, and the prompt holds exactly the ten excerpts of the last read."""
        store = FakeSampleStore()
        client = _small_window_client()

        await _generator(client, store).generate(_COLLECTION, _NAMESPACE)

        assert [len(call[2]) for call in store.read_calls] == [100, 10]
        sent_ids = _read_ids(store)
        assert sent_ids == _expected_picks(_CHUNK_COUNT, 10)
        sent_excerpts = [_excerpt(chunk_id) for chunk_id in sent_ids]
        assert len(_SEPARATOR.join(sent_excerpts)) <= _SMALL_WINDOW_BUDGET_TOKENS
        assert _SEPARATOR.join(sent_excerpts) in client.requests[0].prompt
        assert client.requests[0].prompt.count("excerpt of ") == 10

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("total", "cap"), [(1003, 100), (250, 100), (101, 100), (999, 7), (100, 100)])
    async def test_picks_follow_the_contract_formula_when_the_count_does_not_divide_the_total(
        self, total: int, cap: int
    ) -> None:
        store = FakeSampleStore(chunk_count=total)

        await _generator(ScriptedLlmClient(reply=_DESCRIPTION_TEXT), store, max_sample_chunks=cap).generate(
            _COLLECTION, _NAMESPACE
        )

        assert _read_ids(store) == _expected_picks(total, min(cap, total))

    @pytest.mark.asyncio
    async def test_shrink_loop_goes_round_again_when_the_first_redraw_is_still_too_big(self) -> None:
        """Ids from 500 up hold 100-character excerpts, the rest 5: the draws go 100 -> 5 (cost 338 > 300) -> 4 (231)."""

        def text_of(chunk_id: str) -> str:
            return "x" * (100 if int(chunk_id.removeprefix("chunk-")) >= _CHUNK_COUNT // 2 else 5)

        class _TwoTierStore(FakeSampleStore):
            async def read_excerpts(
                self, collection: str, namespace: str, chunk_ids: list[str], include_restricted: bool = False
            ) -> list[str]:
                self.read_calls.append((collection, namespace, list(chunk_ids), include_restricted))
                return [text_of(chunk_id) for chunk_id in chunk_ids]

        store = _TwoTierStore()
        client = _small_window_client()

        outcome = await _generator(client, store).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionGenerated)
        assert [len(call[2]) for call in store.read_calls] == [100, 5, 4]
        sent = _SEPARATOR.join(text_of(chunk_id) for chunk_id in _read_ids(store))
        assert len(sent) <= _SMALL_WINDOW_BUDGET_TOKENS
        assert sent in client.requests[0].prompt

    @pytest.mark.asyncio
    async def test_max_sample_chunks_from_config_is_the_cap(self) -> None:
        store = FakeSampleStore()

        await _generator(ScriptedLlmClient(reply=_DESCRIPTION_TEXT), store, max_sample_chunks=5).generate(
            _COLLECTION, _NAMESPACE
        )

        assert _read_ids(store) == _expected_picks(_CHUNK_COUNT, 5)

    @pytest.mark.asyncio
    async def test_cloud_client_gets_the_whole_capped_sample_without_counting(self) -> None:
        """An unbounded client is never counted: N stays min(K, M) and one read serves the call."""
        store = FakeSampleStore()

        await _generator(ScriptedLlmClient(reply=_DESCRIPTION_TEXT), store).generate(_COLLECTION, _NAMESPACE)

        assert len(store.read_calls) == 1
        assert len(_read_ids(store)) == 100

    @pytest.mark.asyncio
    @pytest.mark.parametrize("include_restricted", [False, True])
    async def test_both_reads_use_the_same_restricted_flag(self, include_restricted: bool) -> None:
        store = FakeSampleStore()

        await _generator(ScriptedLlmClient(reply=_DESCRIPTION_TEXT), store).generate(
            _COLLECTION, _NAMESPACE, include_restricted=include_restricted
        )

        assert store.list_calls == [(_COLLECTION, _NAMESPACE, include_restricted)]
        assert [call[3] for call in store.read_calls] == [include_restricted]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("include_restricted", [False, True])
    async def test_every_read_of_the_shrink_loop_keeps_the_restricted_flag(self, include_restricted: bool) -> None:
        store = FakeSampleStore()

        await _generator(_small_window_client(), store).generate(
            _COLLECTION, _NAMESPACE, include_restricted=include_restricted
        )

        assert len(store.read_calls) > 1
        assert {call[3] for call in store.read_calls} == {include_restricted}


class TestWindowTooSmall:
    @pytest.mark.asyncio
    async def test_budget_of_zero_fails_naming_the_window_without_a_call(self) -> None:
        window = ContextWindowTokens(size=_SMALL_WINDOW_SIZE, source=ContextWindowSource.CONFIGURED)
        client = CountingClient(budget_tokens=0, window=window)
        store = FakeSampleStore()

        outcome = await _generator(client, store).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.CALL_FAILED
        assert f"({_SMALL_WINDOW_SIZE} tokens, configured) leaves 0 input tokens" in outcome.detail
        assert outcome.detail.endswith("too little for the prompt and one excerpt")
        assert client.requests == []

    @pytest.mark.asyncio
    async def test_not_even_one_excerpt_fits_fails_naming_the_window(self) -> None:
        window = ContextWindowTokens(size=_SMALL_WINDOW_SIZE, source=ContextWindowSource.CONFIGURED)
        client = CountingClient(budget_tokens=len(_excerpt(_chunk_id(0))) - 1, window=window)
        store = FakeSampleStore()

        outcome = await _generator(client, store).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.CALL_FAILED
        assert f"({_SMALL_WINDOW_SIZE} tokens, configured) leaves 21 input tokens" in outcome.detail
        assert outcome.detail.endswith("too little for one excerpt"), "the prompt itself fits; one excerpt does not"
        assert client.requests == []
        assert len(_read_ids(store)) == 1

    @pytest.mark.asyncio
    async def test_exactly_one_excerpt_fitting_is_enough(self) -> None:
        window = ContextWindowTokens(size=_SMALL_WINDOW_SIZE, source=ContextWindowSource.CONFIGURED)
        client = CountingClient(budget_tokens=len(_excerpt(_chunk_id(0))), window=window, reply=_DESCRIPTION_TEXT)
        store = FakeSampleStore()

        outcome = await _generator(client, store).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionGenerated)
        assert len(_read_ids(store)) == 1

    @pytest.mark.asyncio
    async def test_window_failure_detail_holds_no_excerpt_text(self) -> None:
        window = ContextWindowTokens(size=_SMALL_WINDOW_SIZE, source=ContextWindowSource.CONFIGURED)
        client = CountingClient(budget_tokens=1, window=window)

        outcome = await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert "excerpt of" not in outcome.detail

    @pytest.mark.asyncio
    async def test_bounded_budget_on_a_client_that_cannot_count_is_a_call_failure(self) -> None:
        """A budget in tokens needs the client's own counter; without it the sample cannot be fitted."""

        class _NoCounter(ScriptedLlmClient):
            async def input_budget(self, template_text: str) -> InputBudget:
                return InputBudget(_SMALL_WINDOW_BUDGET_TOKENS)

        client = _NoCounter()

        outcome = await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.CALL_FAILED
        assert client.requests == []


class TestSizingTemplate:
    @pytest.mark.asyncio
    async def test_budget_is_sized_for_the_template_with_the_retry_reminder(self) -> None:
        """BE-4: the template handed to ``input_budget`` is the longest form (retry reminder included) and carries the name."""
        client = _large_window_client()

        await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)
        await _generator(client, FakeSampleStore()).generate("other-collection", _NAMESPACE)

        first, second = client.sized_templates
        assert first.startswith('You are generating a concise description for a document collection named "handbook".')
        assert 'named "other-collection".' in second
        assert first.endswith("Answer again in one line of at most 600 characters."), "the retry reminder ends the template"
        assert "excerpt of" not in first, "the template carries no excerpts"


class TestCall:
    @pytest.mark.asyncio
    async def test_one_call_with_the_unchanged_prompt_and_configured_timeout(self) -> None:
        store = FakeSampleStore(chunk_count=2)
        client = ScriptedLlmClient(reply=_DESCRIPTION_TEXT)

        outcome = await _generator(client, store, timeout_seconds=12.5).generate(_COLLECTION, _NAMESPACE)

        assert outcome == DescriptionGenerated(text=_DESCRIPTION_TEXT, trimmed=False)
        [request] = client.requests
        assert isinstance(request, LlmCallRequest)
        assert request.timeout_seconds == 12.5
        assert request.max_output_tokens == ANSWER_RESERVE_TOKENS
        assert request.prompt == (
            'You are generating a concise description for a document collection named "handbook".\n\n'
            "Below are sample excerpts from documents in this collection:\n\n"
            f"{_excerpt(_chunk_id(0))}{_SEPARATOR}{_excerpt(_chunk_id(1))}\n\n"
            "Write 2-3 sentences that summarize what this collection contains and its purpose. "
            "Be specific about the topic and type of content. "
            "Output only the description — no preamble, no explanation.\n"
        )

    @pytest.mark.asyncio
    async def test_answer_is_returned_stripped(self) -> None:
        client = ScriptedLlmClient(reply=f"  {_DESCRIPTION_TEXT}\n")

        outcome = await _generator(client, FakeSampleStore(chunk_count=1)).generate(_COLLECTION, _NAMESPACE)

        assert outcome == DescriptionGenerated(text=_DESCRIPTION_TEXT, trimmed=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [LlmCallFailure("anthropic", "no API key"), LlmCallTimeout("ollama", "no answer")])
    async def test_provider_failure_is_a_call_failed_value_not_an_exception(self, failure: LlmCallFailure) -> None:
        """S20: an unreachable provider or missing key is ``failed`` and never raises, so no description is replaced."""
        client = ScriptedLlmClient(failure=failure)

        outcome = await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.CALL_FAILED
        assert failure.reason in outcome.detail
        assert len(client.requests) == 1

    @pytest.mark.asyncio
    async def test_provider_failure_is_not_retried(self) -> None:
        client = ScriptedLlmClient(failure=LlmCallFailure("openai", "HTTP 500"))

        await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)

        assert len(client.requests) == 1

    @pytest.mark.asyncio
    async def test_failure_while_sizing_is_a_call_failed_value(self) -> None:
        class _BrokenSizing(ScriptedLlmClient):
            async def input_budget(self, template_text: str) -> InputBudget:
                raise LlmCallFailure("ollama", "cannot size the prompt")

        client = _BrokenSizing()

        outcome = await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.CALL_FAILED
        assert "cannot size the prompt" in outcome.detail
        assert client.requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply", ["", "   \n"])
    async def test_blank_answer_is_unusable_output(self, reply: str) -> None:
        outcome = await _generator(ScriptedLlmClient(reply=reply), FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.UNUSABLE_OUTPUT

    @pytest.mark.asyncio
    async def test_call_failure_logs_one_warning_per_process_without_excerpts(self, caplog: pytest.LogCaptureFixture) -> None:
        client = ScriptedLlmClient(failure=LlmCallFailure("anthropic", "no API key"))
        generator = _generator(client, FakeSampleStore())

        with caplog.at_level("WARNING", logger="archon_search.description_generator"):
            await generator.generate(_COLLECTION, _NAMESPACE)
            await generator.generate(_COLLECTION, _NAMESPACE)

        records = [r for r in caplog.records if r.name == "archon_search.description_generator"]
        assert len(records) == 1
        assert "no API key" in caplog.text
        assert "excerpt of" not in caplog.text

    @pytest.mark.asyncio
    async def test_count_failure_while_fitting_is_a_call_failed_value(self) -> None:
        class _BrokenCounter(CountingClient):
            async def count_tokens(self, text: str) -> int:
                raise LlmCallFailure("llama_cpp", "tokenize endpoint unreachable")

        window = ContextWindowTokens(size=_SMALL_WINDOW_SIZE, source=ContextWindowSource.PROBED)
        client = _BrokenCounter(budget_tokens=_SMALL_WINDOW_BUDGET_TOKENS, window=window)

        outcome = await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.CALL_FAILED
        assert "tokenize endpoint unreachable" in outcome.detail
        assert client.requests == []

    @pytest.mark.asyncio
    async def test_failing_second_read_while_fitting_is_sample_read_failed(self) -> None:
        class _SecondReadFails(FakeSampleStore):
            async def read_excerpts(
                self, collection: str, namespace: str, chunk_ids: list[str], include_restricted: bool = False
            ) -> list[str]:
                if self.read_calls:
                    raise _store_read_error()
                return await super().read_excerpts(collection, namespace, chunk_ids, include_restricted)

        client = _small_window_client()

        outcome = await _generator(client, _SecondReadFails()).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.SAMPLE_READ_FAILED
        assert client.requests == []


class TestNeverRaises:
    """C2: generation never raises to its caller; an unexpected error is a failed outcome that names only its type."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("broken", ["input_budget", "count_tokens"])
    async def test_unexpected_sizing_or_counting_error_is_a_call_failed_value_logged_once(
        self, broken: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _Broken(CountingClient):
            async def input_budget(self, template_text: str) -> InputBudget:
                if broken == "input_budget":
                    raise TypeError("secret tokenizer path")
                return await super().input_budget(template_text)

            async def count_tokens(self, text: str) -> int:
                raise TypeError("secret tokenizer path")

        window = ContextWindowTokens(size=_SMALL_WINDOW_SIZE, source=ContextWindowSource.PROBED)
        client = _Broken(budget_tokens=_SMALL_WINDOW_BUDGET_TOKENS, window=window)

        with caplog.at_level("WARNING", logger="archon_search.description_generator"):
            first = await _generator(client, FakeSampleStore()).generate("first", _NAMESPACE)
            await _generator(client, FakeSampleStore()).generate("second", _NAMESPACE)

        assert isinstance(first, DescriptionFailed)
        assert first.reason is DescriptionFailureReason.CALL_FAILED
        assert "TypeError" in first.detail and "secret" not in first.detail
        records = [r for r in caplog.records if r.name == "archon_search.description_generator"]
        assert len(records) == 1, "a broken provider is one WARNING per process, like any provider failure"
        assert client.requests == []

    @pytest.mark.asyncio
    async def test_unexpected_client_error_is_a_call_failed_value(self, caplog: pytest.LogCaptureFixture) -> None:
        client = ScriptedLlmClient(failure=ValueError("excerpt of chunk-00000 leaked into the message"))

        with caplog.at_level("WARNING", logger="archon_search.description_generator"):
            outcome = await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)
            await _generator(client, FakeSampleStore()).generate("second", _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.CALL_FAILED
        assert "ValueError" in outcome.detail
        assert "leaked" not in outcome.detail and "leaked" not in caplog.text
        assert len([r for r in caplog.records if r.name == "archon_search.description_generator"]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failing_call", ["list_error", "read_error"])
    async def test_unexpected_store_error_is_a_sample_read_failed_value(
        self, failing_call: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = FakeSampleStore(**{failing_call: RuntimeError("store is not connected: secret path")})
        client = ScriptedLlmClient(reply=_DESCRIPTION_TEXT)

        with caplog.at_level("WARNING", logger="archon_search.description_generator"):
            outcome = await _generator(client, store).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.SAMPLE_READ_FAILED
        assert "RuntimeError" in outcome.detail
        assert "secret path" not in outcome.detail and "secret path" not in caplog.text
        assert client.requests == []

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self) -> None:
        client = ScriptedLlmClient(failure=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await _generator(client, FakeSampleStore()).generate(_COLLECTION, _NAMESPACE)


class TestWindowFailureLogging:
    """A too-small window is per collection and per window, not a provider outage: it is never silenced by the
    once-per-process provider WARNING."""

    @pytest.mark.asyncio
    async def test_window_failure_is_logged_for_every_collection_even_after_a_provider_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        window = ContextWindowTokens(size=_SMALL_WINDOW_SIZE, source=ContextWindowSource.ASSUMED)
        too_small = CountingClient(budget_tokens=0, window=window)
        provider_down = ScriptedLlmClient(failure=LlmCallFailure("anthropic", "no API key"))

        with caplog.at_level("WARNING", logger="archon_search.description_generator"):
            await _generator(provider_down, FakeSampleStore()).generate("first", _NAMESPACE)
            await _generator(too_small, FakeSampleStore()).generate("second", _NAMESPACE)
            await _generator(too_small, FakeSampleStore()).generate("third", _NAMESPACE)

        messages = [r.getMessage() for r in caplog.records if r.name == "archon_search.description_generator"]
        assert len(messages) == 3
        assert "'second'" in messages[1] and "'third'" in messages[2]
        assert str(_SMALL_WINDOW_SIZE) in messages[1] and "assumed" in messages[1]


class TestNoOpenChunk:
    @pytest.mark.asyncio
    async def test_no_open_chunk_fails_without_provider_call(self, caplog: pytest.LogCaptureFixture) -> None:
        """S57: the read worked and found nothing open: ``noUnrestrictedChunks``, no call, and a WARNING that says so."""
        store = FakeSampleStore(chunk_count=0)
        client = ScriptedLlmClient(reply=_DESCRIPTION_TEXT)

        with caplog.at_level("WARNING", logger="archon_search.description_generator"):
            outcome = await _generator(client, store).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.NO_UNRESTRICTED_CHUNKS
        assert client.requests == []
        assert store.read_calls == []
        assert "no unrestricted chunks to describe" in caplog.text

    @pytest.mark.asyncio
    async def test_every_picked_chunk_vanishing_before_the_read_is_no_unrestricted_chunks(self) -> None:
        """Ids that turned restricted or were deleted between the listing and the read leave nothing to describe."""

        class _EmptyReads(FakeSampleStore):
            async def read_excerpts(self, *args: Any, **kwargs: Any) -> list[str]:
                return []

        client = ScriptedLlmClient(reply=_DESCRIPTION_TEXT)

        outcome = await _generator(client, _EmptyReads(chunk_count=4)).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.NO_UNRESTRICTED_CHUNKS
        assert client.requests == []


class TestSampleReadError:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("failing_call", ["list_error", "read_error"])
    async def test_sample_read_error_is_sample_read_failed_not_no_open_chunks(
        self, failing_call: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """S82: a store error has its own reason and its own WARNING (collection name and cause type, no text)."""
        store = FakeSampleStore(**{failing_call: _store_read_error()})
        client = ScriptedLlmClient(reply=_DESCRIPTION_TEXT)

        with caplog.at_level("WARNING", logger="archon_search.description_generator"):
            outcome = await _generator(client, store).generate(_COLLECTION, _NAMESPACE)

        assert isinstance(outcome, DescriptionFailed)
        assert outcome.reason is DescriptionFailureReason.SAMPLE_READ_FAILED
        assert client.requests == []
        assert _COLLECTION in caplog.text
        assert "OSError" in caplog.text
        assert "disk secret path" not in caplog.text


def _store_read_error() -> StoreReadError:
    error = StoreReadError(_COLLECTION)
    error.__cause__ = OSError("disk secret path")
    return error


class TestRegenerationRule:
    @pytest.mark.parametrize(
        ("doc_count", "described_at", "expected"),
        [
            (120, 100, True),  # grew by exactly 20%
            (80, 100, True),  # shrank by exactly 20%
            (200, 100, True),
            (50, 100, True),
            (119, 100, False),  # grew by less than 20%
            (81, 100, False),  # shrank by less than 20%
            (100, 100, False),
        ],
    )
    def test_regeneration_rule_fires_at_20_percent_either_direction(
        self, doc_count: int, described_at: int, expected: bool
    ) -> None:
        assert _should_regenerate(doc_count, chunk_count=10, described_at_doc_count=described_at) is expected

    @pytest.mark.parametrize("described_at", [None, 0])
    def test_never_described_always_regenerates(self, described_at: int | None) -> None:
        assert _should_regenerate(5, chunk_count=10, described_at_doc_count=described_at) is True

    @pytest.mark.parametrize("described_at", [None, 0, 100])
    def test_collection_without_chunks_never_regenerates(self, described_at: int | None) -> None:
        assert _should_regenerate(500, chunk_count=0, described_at_doc_count=described_at) is False


class TestModuleSource:
    _SOURCE = Path(__file__).parent.parent / "archon_search" / "description_generator.py"
    _SAMPLE_SOURCE = Path(__file__).parent.parent / "archon_search" / "description_sample.py"

    @staticmethod
    def _imports(source: Path) -> set[str]:
        """Every imported module (``a.b``) and imported name (``a.b.c``) of *source*, parsed, never imported."""
        names: set[str] = set()
        for node in ast.walk(ast.parse(source.read_text("utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        return names

    @pytest.mark.parametrize("forbidden", ["claude_agent_sdk", "random", "os"])
    def test_removed_sdk_machinery_is_not_imported(self, forbidden: str) -> None:
        top_level = {name.split(".")[0] for name in self._imports(self._SOURCE)}
        assert forbidden not in top_level

    @pytest.mark.parametrize("forbidden", ["claude_agent_sdk", "CLAUDECODE", "_ENV_LOCK", "os.environ", "__import__"])
    def test_removed_sdk_machinery_is_not_mentioned(self, forbidden: str) -> None:
        assert forbidden not in self._SOURCE.read_text("utf-8")

    def test_generator_depends_inward_not_on_the_store(self) -> None:
        """Use Cases never import the concrete store or the database: the port and its error live in description_sample."""
        top_level = {name.split(".")[0] for name in self._imports(self._SOURCE)}
        assert "lancedb" not in top_level
        assert "archon_search.store" not in self._imports(self._SOURCE)

    def test_sample_port_module_is_stdlib_only(self) -> None:
        """The store imports the port module, so it must cost nothing and import nothing of the project."""
        top_level = {name.split(".")[0] for name in self._imports(self._SAMPLE_SOURCE)}
        assert top_level <= {"__future__", "typing"}

    def test_no_archon_imports(self) -> None:
        """description_generator must not import from archon.*."""
        source = self._SOURCE.read_text("utf-8")
        assert "from archon." not in source
        assert "import archon." not in source
        assert "import archon\n" not in source
        assert "from archon import" not in source
