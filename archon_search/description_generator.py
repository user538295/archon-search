"""Description generator for RAG collections (collection descriptions, contract C2).

``DescriptionGenerator.generate`` turns a collection's open chunks into a typed outcome: the model's text
(:class:`DescriptionGenerated`) or a failure value (:class:`DescriptionFailed`). It never raises for a provider or a
store failure, and a failed outcome never replaces an existing description: the caller that writes (the lifecycle through
the shared write operation, C11) writes only a ``DescriptionGenerated``.

The sample is deterministic. The store port lists the open chunk ids sorted by ``chunk_id`` (never in stored row order,
which shifts with compaction and re-ingest); with ``M`` ids and a count ``N`` the picks are the ids at positions
``floor((j + 0.5) * M / N)`` for ``j = 0 .. N-1``, spaced over ALL ``M`` ids. ``N`` starts at
``min(max_sample_chunks, M)``. Under a token budget (a local provider) the excerpts of a draw are counted with the
client's own rule; while the cost exceeds the budget ``N`` becomes ``min(N - 1, floor(N * budget / cost))`` (at least 1)
and the ids are drawn again, re-spaced over all ``M``. ``N`` strictly decreases, so the loop ends, and a small window
yields fewer excerpts from the whole collection, never the head of it, with no tail cut afterwards. If not even one
excerpt fits, the outcome is ``callFailed`` with a detail that names the window. A cloud provider has no budget: ``N``
stays ``min(max_sample_chunks, M)`` and nothing is counted.

A store error while reading is ``sampleReadFailed``; ``noUnrestrictedChunks`` means the read worked and found no open
chunk (no provider call is made then).

Output rules (BE-29, C2). Every answer is cleaned: thinking is removed (closed ``<think>``/``<thinking>``/``<thought>``/
``<reasoning>`` blocks, text up to a closing tag that has no opener, and an unclosed opener at the start) and all white
space collapses to single spaces, so the text is one line. A cleaned answer that is non-empty and at most
``MAX_DESCRIPTION_CHARS`` characters is the description. Anything else gets ONE automatic retry: the same single message
plus ``_RETRY_REMINDER`` (the constant the input budget is sized with, so a retry of a full sample still fits the
window). A provider error, on either attempt, is never retried. An answer that is still too long after the retry is
trimmed to its last full sentence within the limit (``DescriptionGenerated.trimmed``); when it has no sentence end, to
its last word, or to the limit itself when it has no space. When both answers have nothing left after cleaning the
outcome is ``unusableOutput`` with a WARNING that points to the ``think`` setting (a reasoning model's answer was eaten by
its thinking); an empty retry after an over-long first answer trims that first answer instead. Each attempt has the
configured timeout and an answer ceiling: ``ANSWER_RESERVE_TOKENS``, the room the input budget already reserves, plus
``DESCRIPTION_THINKING_HEADROOM_TOKENS`` unless ``think`` is false (a reasoning model's thinking counts against the
ceiling and took 1.9k to 2.4k tokens on a real model). Under a token budget the sample gives up min(headroom, budget // 2)
of it, so prompt plus answer still fit the window; when not even one excerpt then fits (a small window), the sample
keeps the whole budget and the call the plain ceiling, so a small window never fails for the headroom.

Thinking and restricted chunks (BE-30, S54, S56). ``[description].think`` rides on every call as ``ThinkSetting``
(``None`` is ``default``, which sends no thinking control); the wrappers decide what goes on the wire and cloud and
``claude_cli`` ignore it. ``true`` on a model with no thinking mode (``LlmThinkingNotSupported``) logs one WARNING per
process and model and fails the generation like any provider error (no retry without ``think``). Chunks that carry an ACL
are sampled only for ``ollama`` and ``llama_cpp`` and only when ``include_restricted_chunks`` is true; the generator
decides this from its config, so the lifecycle passes nothing.

Logging: every failure logs one WARNING with the collection name and ``DescriptionFailed.detail``, which never holds an
excerpt, a prompt or an answer (the provider's failure text is prompt-masked by the shared client, C1; an unexpected
error is named by its type only). A failed provider call logs once per process (``warn_once``): a provider that is down
fails every collection the same way. Every other failure (a window too small, a store error, no open chunk) is per
collection and logs every time. The one exception is ``think = true`` on a model with no thinking mode: its own WARNING
(model name, no collection) replaces the failure line and appears once per process and model. A sample the budget cut
below the count asked for (``max_sample_chunks`` or the number of open chunks) is not a failure and logs one INFO line per
generation with the collection name, ``N of M`` excerpts (N is the number actually sent), the model's input room (with the
thinking headroom held back from it, if any) and the window: the sizes only.
"""
from __future__ import annotations

import enum
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from archon_search.config import DescriptionConfig
from archon_search.constants import (
    ANSWER_RESERVE_TOKENS,
    DESCRIPTION_THINKING_HEADROOM_TOKENS,
    MAX_DESCRIPTION_CHARS,
    RESTRICTED_CHUNK_PROVIDERS,
)
from archon_search.description_sample import DescriptionSampleStore, StoreReadError
from archon_search.description_write import collapse_to_one_line, trim_to_last_sentence
from archon_search.llm_client.fit import describe_window
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    LlmCallFailure,
    LlmCallRequest,
    LlmClient,
    LlmThinkingNotSupported,
    ThinkSetting,
    TokenCountingClient,
)

logger = logging.getLogger(__name__)

#: Tag names a model wraps its thinking in (regex alternation).
_THINKING_TAGS: Final = "think|thinking|thought|reasoning"
#: A closed thinking block, any of the tag names (case-insensitive, attributes allowed).
_THINKING_BLOCK: Final = re.compile(rf"<({_THINKING_TAGS})\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
#: Everything up to the last closing tag that has no opener: some chat templates open the thinking in the prompt.
_THINKING_ORPHAN_CLOSE: Final = re.compile(rf"\A.*</(?:{_THINKING_TAGS})\s*>", re.IGNORECASE | re.DOTALL)
#: An opening tag at the start that is never closed (the model ran out of answer room while thinking): all is thinking.
_THINKING_UNCLOSED: Final = re.compile(rf"\A\s*<(?:{_THINKING_TAGS})\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL)
#: The failure detail (and WARNING) when the answer has nothing left after cleaning, even after the retry (S21).
_NO_USABLE_OUTPUT_DETAIL: Final = (
    "the model returned no usable text after one retry (empty, or only thinking); it may be a reasoning model: "
    "set `think` to false in [description] (local providers) so it answers directly"
)

#: Joins the excerpts of one prompt; counted as part of the cost of a draw.
_SAMPLE_SEPARATOR: Final = "\n\n---\n\n"
#: ``warn_once`` key: one WARNING per process for every failed provider call of a description generation.
_CALL_WARN_KEY: Final = "description_generation"
#: ``warn_once`` key prefix: one WARNING per process and model for ``think = true`` on a model without a thinking mode (S54).
_THINK_UNSUPPORTED_WARN_KEY: Final = "description_think_unsupported"
#: Document-count change, as a share of the count the description was written at, that makes it stale.
_REGENERATE_CHANGE_RATIO: Final = 0.20

_DESCRIPTION_PROMPT = """\
You are generating a concise description for a document collection named "{name}".

Below are sample excerpts from documents in this collection:

{samples}

Write 2-3 sentences that summarize what this collection contains and its purpose. \
Be specific about the topic and type of content. \
""" + (
    f"Output only the description as one line of plain text of at most {MAX_DESCRIPTION_CHARS} characters"
    " — no preamble, no explanation.\n"
)

#: The reminder of the one automatic retry (BE-29). The input budget is sized for the template WITH it (C1), so a retry
#: of a full sample still fits the window; sizing and the retry share this one constant so they cannot drift.
_RETRY_REMINDER: Final = (
    f"\n\nYour previous answer was empty or longer than {MAX_DESCRIPTION_CHARS} characters. "
    f"Answer again in one line of at most {MAX_DESCRIPTION_CHARS} characters."
)


class DescriptionFailureReason(enum.StrEnum):
    """Why a generation produced no description (the wire words of contract C2)."""

    #: Provider unreachable, key missing, timeout, prompt rejected or window too small: any call failure (not retried).
    CALL_FAILED = "callFailed"
    #: Nothing was left of the answer after cleaning (empty, or only thinking), even after the retry.
    UNUSABLE_OUTPUT = "unusableOutput"
    #: The collection has no open chunk and restricted chunks are not allowed (the read worked).
    NO_UNRESTRICTED_CHUNKS = "noUnrestrictedChunks"
    #: The chunks could not be read (store error); distinct from ``NO_UNRESTRICTED_CHUNKS``.
    SAMPLE_READ_FAILED = "sampleReadFailed"
    #: The description could not be saved, for example the collection lock timed out (set by the lifecycle).
    SAVE_FAILED = "saveFailed"


@dataclass(frozen=True, slots=True)
class DescriptionGenerated:
    """The model produced a description."""

    text: str
    #: True when the text was cut to fit the limit (to the last full sentence, else the last word) instead of kept whole.
    trimmed: bool = False


@dataclass(frozen=True, slots=True)
class DescriptionFailed:
    """No description was produced; an existing one is kept."""

    reason: DescriptionFailureReason
    #: Operator-readable, free of document text.
    detail: str


type DescriptionOutcome = DescriptionGenerated | DescriptionFailed


class _GenerationStopped(Exception):
    """Ends a generation with a failure value; ``generate`` catches it, logs it and returns ``failed``.

    *provider_failure* marks a failure of the provider itself (a failed call, sizing or counting), which is logged once
    per process; every other stop is logged for its collection. *already_logged* is set by the code that raised or
    re-raised the stop after it logged a more specific WARNING of its own, so ``generate`` does not log it again."""

    def __init__(self, reason: DescriptionFailureReason, detail: str, *, provider_failure: bool = False) -> None:
        super().__init__(detail)
        self.failed = DescriptionFailed(reason, detail)
        self.provider_failure = provider_failure
        self.already_logged = False


class _WindowTooSmall(_GenerationStopped):
    """The window leaves too little input room for the prompt, or for one excerpt (a CALL_FAILED stop that names it)."""


def _clean_answer(raw: str) -> str:
    """The model's answer without thinking or control characters, as one line (white space runs become one space)."""
    visible = _THINKING_BLOCK.sub(" ", raw)
    visible = _THINKING_ORPHAN_CLOSE.sub("", visible)
    visible = _THINKING_UNCLOSED.sub("", visible)
    return collapse_to_one_line(visible)


def _should_regenerate(
    doc_count: int,
    chunk_count: int,
    described_at_doc_count: int | None,
) -> bool:
    """Return True if description should be (re)generated for this ingest batch.

    Rules:
    - chunk_count == 0  → never regenerate (no content to describe)
    - described_at_doc_count is None or 0  → always regenerate
    - abs change >= 20%  → regenerate
    """
    if chunk_count == 0:
        return False
    if described_at_doc_count is None or described_at_doc_count == 0:
        return True
    return abs(doc_count - described_at_doc_count) / described_at_doc_count >= _REGENERATE_CHANGE_RATIO


def _evenly_spaced(chunk_ids: list[str], count: int) -> list[str]:
    """The ids at positions ``floor((j + 0.5) * M / count)`` of *chunk_ids* (``M`` of them, ``count <= M``)."""
    total = len(chunk_ids)
    return [chunk_ids[(2 * j + 1) * total // (2 * count)] for j in range(count)]


def _sizing_template(collection: str) -> str:
    """The longest prompt without excerpts that this collection can send: the template with its name and the retry reminder."""
    return _DESCRIPTION_PROMPT.format(name=collection, samples="") + _RETRY_REMINDER


@contextmanager
def _provider_step() -> Iterator[None]:
    """Run a step that talks to the provider (sizing, counting, the call): whatever it raises becomes a stop.

    A ``LlmCallFailure`` carries the client's own prompt-masked reason; any other error is named by its type only.
    Cancellation is a ``BaseException`` and passes through."""
    try:
        yield
    except LlmCallFailure as failure:
        detail = f"{failure.provider}: {failure.reason}"
        raise _GenerationStopped(DescriptionFailureReason.CALL_FAILED, detail, provider_failure=True) from failure
    except Exception as unexpected:
        detail = f"unexpected {type(unexpected).__name__}"
        raise _GenerationStopped(DescriptionFailureReason.CALL_FAILED, detail, provider_failure=True) from unexpected


def _read_failed(failure: StoreReadError) -> _GenerationStopped:
    cause = type(failure.__cause__ or failure).__name__
    return _GenerationStopped(
        DescriptionFailureReason.SAMPLE_READ_FAILED, f"the chunks could not be read from the store ({cause})"
    )


def _no_open_chunk(detail: str) -> _GenerationStopped:
    return _GenerationStopped(DescriptionFailureReason.NO_UNRESTRICTED_CHUNKS, detail)


class DescriptionGenerator:
    """Generates one collection's description on the shared client of ``[description]`` (C2)."""

    def __init__(self, config: DescriptionConfig, client: LlmClient, store: DescriptionSampleStore) -> None:
        self._config = config
        self._client = client
        self._store = store
        self._include_restricted = config.include_restricted_chunks and config.provider in RESTRICTED_CHUNK_PROVIDERS

    async def generate(self, collection: str, namespace: str) -> DescriptionOutcome:
        """Sample *collection*, ask the model (one retry at most, see the output rules) and return the outcome.

        Args:
            collection: the collection to describe.
            namespace: its namespace (the store's ownership rule applies to both reads).

        Restricted chunks (those with an ACL) are sampled only for ``ollama`` and ``llama_cpp`` with
        ``[description].include_restricted_chunks`` on (S56); otherwise only open chunks are.

        Never raises (contract C2): a provider, sizing, counting or store failure, whatever its type, is the returned
        ``DescriptionFailed``; only task cancellation propagates. A store error that is not a ``StoreReadError`` (an
        ownership refusal, a store that is not connected) is ``sampleReadFailed`` whose detail names its type.
        """
        try:
            sample, answer_ceiling = await self._draw_sample(collection, namespace, self._include_restricted)
            generated = await self._ask(collection, sample, answer_ceiling)
        except _GenerationStopped as stopped:
            failed, provider_failure, logged = stopped.failed, stopped.provider_failure, stopped.already_logged
        except Exception as unexpected:  # noqa: BLE001 — a store-side error; provider steps are covered by _provider_step
            failed = DescriptionFailed(DescriptionFailureReason.SAMPLE_READ_FAILED, f"unexpected {type(unexpected).__name__}")
            provider_failure = logged = False
        else:
            return generated
        if not logged:
            self._log_failure(collection, failed.detail, provider_failure=provider_failure)
        return failed

    async def _draw_sample(self, collection: str, namespace: str, include_restricted: bool) -> tuple[list[str], int]:
        """The excerpts and the answer ceiling of the calls: the sample fits the budget left after the thinking headroom."""
        try:
            chunk_ids = await self._store.list_chunk_ids(collection, namespace, include_restricted)
        except StoreReadError as failure:
            raise _read_failed(failure) from failure
        if not chunk_ids:
            raise _no_open_chunk("no unrestricted chunks to describe; set a description by hand")
        with _provider_step():
            sizing = await self._client.input_budget(_sizing_template(collection), think=ThinkSetting.from_config(self._config.think))
            budget = sizing.tokens
        count = min(self._config.max_sample_chunks, len(chunk_ids))
        thinking_possible = self._config.think is not False
        if budget == UNBOUNDED:
            headroom = DESCRIPTION_THINKING_HEADROOM_TOKENS if thinking_possible else 0
            excerpts = await self._read(collection, namespace, _evenly_spaced(chunk_ids, count), include_restricted)
            return excerpts, ANSWER_RESERVE_TOKENS + headroom
        if thinking_possible:
            headroom = min(DESCRIPTION_THINKING_HEADROOM_TOKENS, budget // 2)
            try:
                excerpts = await self._fit(
                    collection, namespace, chunk_ids, include_restricted, budget - headroom, count, headroom
                )
            except _WindowTooSmall:
                pass  # not even one excerpt fits beside the headroom: a small window keeps the sample, not the thinking room
            else:
                return excerpts, ANSWER_RESERVE_TOKENS + headroom
        excerpts = await self._fit(collection, namespace, chunk_ids, include_restricted, budget, count, headroom=0)
        return excerpts, ANSWER_RESERVE_TOKENS

    async def _fit(
        self,
        collection: str,
        namespace: str,
        chunk_ids: list[str],
        include_restricted: bool,
        budget: int,
        count: int,
        headroom: int,
    ) -> list[str]:
        """Draw, count and shrink until the excerpts fit *budget* (the stated loop of the module docstring).

        *headroom* is the thinking room already taken off the model's input room to give *budget* (0 when none was). A
        sample the budget cut below the *count* asked for is logged at INFO (the sizes only, with the number of excerpts
        actually sent): a window can shrink ``max_sample_chunks`` to a handful of excerpts and nothing else would tell the
        operator.
        """
        if budget == 0:
            raise self._window_too_small(budget)
        if not isinstance(self._client, TokenCountingClient):
            raise _GenerationStopped(
                DescriptionFailureReason.CALL_FAILED, "the client sizes its input to a budget but cannot count tokens"
            )
        asked = count
        while True:
            excerpts = await self._read(
                collection, namespace, _evenly_spaced(chunk_ids, count), include_restricted
            )
            with _provider_step():
                cost = await self._client.count_tokens(_SAMPLE_SEPARATOR.join(excerpts))
            if cost <= budget:
                if count < asked:
                    logger.info(
                        "Description sample for collection %r cut to %d of %d excerpts to fit the model's input room of "
                        "%d tokens%s (context window: %s)",
                        collection,
                        len(excerpts),
                        asked,
                        budget + headroom,
                        f", less {headroom} held back for thinking" if headroom else "",
                        describe_window(self._client.context_window()),
                    )
                return excerpts
            if count == 1:
                raise self._window_too_small(budget)
            count = max(1, min(count - 1, count * budget // cost))

    async def _read(
        self, collection: str, namespace: str, picks: list[str], include_restricted: bool
    ) -> list[str]:
        try:
            excerpts = await self._store.read_excerpts(collection, namespace, picks, include_restricted)
        except StoreReadError as failure:
            raise _read_failed(failure) from failure
        if not excerpts:
            raise _no_open_chunk("none of the sampled chunks could be read as unrestricted text")
        return excerpts

    def _window_too_small(self, budget: int) -> _WindowTooSmall:
        room = "the prompt and one excerpt" if budget == 0 else "one excerpt"
        return _WindowTooSmall(
            DescriptionFailureReason.CALL_FAILED,
            f"the model's context window ({describe_window(self._client.context_window())}) leaves {budget} input tokens, "
            f"too little for {room}",
        )

    async def _ask(self, collection: str, sample: list[str], answer_ceiling: int) -> DescriptionGenerated:
        """The model's answer under the output rules: accepted, or retried once and then trimmed or failed."""
        prompt = _DESCRIPTION_PROMPT.format(name=collection, samples=_SAMPLE_SEPARATOR.join(sample))
        first = await self._attempt(prompt, answer_ceiling)
        if first and len(first) <= MAX_DESCRIPTION_CHARS:
            return DescriptionGenerated(text=first)
        second = await self._attempt(prompt + _RETRY_REMINDER, answer_ceiling)
        cleaned = second or first  # an empty retry still leaves an over-long first answer to trim
        if not cleaned:
            raise _GenerationStopped(DescriptionFailureReason.UNUSABLE_OUTPUT, _NO_USABLE_OUTPUT_DETAIL)
        if len(cleaned) <= MAX_DESCRIPTION_CHARS:
            return DescriptionGenerated(text=cleaned)
        return DescriptionGenerated(text=trim_to_last_sentence(cleaned), trimmed=True)

    async def _attempt(self, prompt: str, answer_ceiling: int) -> str:
        """One model call (its own timeout; a provider error stops the generation, it is never retried), cleaned."""
        request = LlmCallRequest(
            prompt=prompt,
            max_output_tokens=answer_ceiling,
            timeout_seconds=self._config.timeout_seconds,
            think=ThinkSetting.from_config(self._config.think),
        )
        try:
            with _provider_step():
                result = await self._client.call(request)
        except _GenerationStopped as stopped:
            if isinstance(stopped.__cause__, LlmThinkingNotSupported):
                self._warn_think_unsupported()
                stopped.already_logged = True
            raise
        return _clean_answer(result.text)

    def _warn_think_unsupported(self) -> None:
        warn_once(
            logger,
            f"{_THINK_UNSUPPORTED_WARN_KEY}:{self._config.model}",
            '[description].think is true but the model %r has no thinking mode; set think to "default" or false',
            self._config.model,
        )

    @staticmethod
    def _log_failure(collection: str, detail: str, *, provider_failure: bool) -> None:
        if provider_failure:
            warn_once(
                logger,
                _CALL_WARN_KEY,
                "Description generation failed for collection %r: %s (later provider failures in this process are not logged)",
                collection,
                detail,
            )
        else:
            logger.warning("Description generation failed for collection %r: %s", collection, detail)
