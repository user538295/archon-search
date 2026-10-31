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
chunk (no provider call is made then). The outcome carries the model's answer stripped of surrounding whitespace; the
output rules (cleaning, the retry, the length limit) are applied on top of it.

Logging: every failure logs one WARNING with the collection name and ``DescriptionFailed.detail``, which never holds an
excerpt, a prompt or an answer (the provider's failure text is prompt-masked by the shared client, C1; an unexpected
error is named by its type only). A failed provider call logs once per process (``warn_once``): a provider that is down
fails every collection the same way. Every other failure (a window too small, a store error, no open chunk) is per
collection and logs every time.
"""
from __future__ import annotations

import enum
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from archon_search.config import DescriptionConfig
from archon_search.constants import ANSWER_RESERVE_TOKENS, MAX_DESCRIPTION_CHARS
from archon_search.description_sample import DescriptionSampleStore, StoreReadError
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindowTokens,
    LlmCallFailure,
    LlmCallRequest,
    LlmClient,
    TokenCountingClient,
)

logger = logging.getLogger(__name__)

#: Joins the excerpts of one prompt; counted as part of the cost of a draw.
_SAMPLE_SEPARATOR: Final = "\n\n---\n\n"
#: ``warn_once`` key: one WARNING per process for every failed provider call of a description generation.
_CALL_WARN_KEY: Final = "description_generation"
#: Document-count change, as a share of the count the description was written at, that makes it stale.
_REGENERATE_CHANGE_RATIO: Final = 0.20

_DESCRIPTION_PROMPT = """\
You are generating a concise description for a document collection named "{name}".

Below are sample excerpts from documents in this collection:

{samples}

Write 2-3 sentences that summarize what this collection contains and its purpose. \
Be specific about the topic and type of content. \
Output only the description — no preamble, no explanation.
"""

#: The reminder of the one automatic retry (BE-29). Until that retry exists it only sizes the prompt: the input budget is
#: sized for the template WITH it (C1), so a retry of a full sample will still fit the window.
_RETRY_REMINDER: Final = (
    f"\n\nYour previous answer was longer than {MAX_DESCRIPTION_CHARS} characters. "
    f"Answer again in one line of at most {MAX_DESCRIPTION_CHARS} characters."
)


class DescriptionFailureReason(enum.StrEnum):
    """Why a generation produced no description (the wire words of contract C2)."""

    #: Provider unreachable, key missing, timeout, prompt rejected or window too small: any call failure (not retried).
    CALL_FAILED = "callFailed"
    #: The answer was empty.
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
    #: True when the text was trimmed to the last full sentence after the retry.
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
    per process; every other stop is logged for its collection."""

    def __init__(self, reason: DescriptionFailureReason, detail: str, *, provider_failure: bool = False) -> None:
        super().__init__(detail)
        self.failed = DescriptionFailed(reason, detail)
        self.provider_failure = provider_failure


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

    async def generate(
        self, collection: str, namespace: str, *, include_restricted: bool = False
    ) -> DescriptionOutcome:
        """Sample *collection*, call the model once and return the outcome.

        Args:
            collection: the collection to describe.
            namespace: its namespace (the store's ownership rule applies to both reads).
            include_restricted: sample chunks that carry an ACL too (the operator's switch for a local provider).

        Never raises (contract C2): a provider, sizing, counting or store failure, whatever its type, is the returned
        ``DescriptionFailed``; only task cancellation propagates. A store error that is not a ``StoreReadError`` (an
        ownership refusal, a store that is not connected) is ``sampleReadFailed`` whose detail names its type.
        """
        try:
            sample = await self._draw_sample(collection, namespace, include_restricted)
            text = await self._ask(collection, sample)
        except _GenerationStopped as stopped:
            failed, provider_failure = stopped.failed, stopped.provider_failure
        except Exception as unexpected:  # noqa: BLE001 — a store-side error; provider steps are covered by _provider_step
            failed = DescriptionFailed(DescriptionFailureReason.SAMPLE_READ_FAILED, f"unexpected {type(unexpected).__name__}")
            provider_failure = False
        else:
            return DescriptionGenerated(text=text)
        self._log_failure(collection, failed.detail, provider_failure=provider_failure)
        return failed

    async def _draw_sample(self, collection: str, namespace: str, include_restricted: bool) -> list[str]:
        try:
            chunk_ids = await self._store.list_chunk_ids(collection, namespace, include_restricted)
        except StoreReadError as failure:
            raise _read_failed(failure) from failure
        if not chunk_ids:
            raise _no_open_chunk("no unrestricted chunks to describe; set a description by hand")
        with _provider_step():
            budget = (await self._client.input_budget(_sizing_template(collection))).tokens
        count = min(self._config.max_sample_chunks, len(chunk_ids))
        if budget == UNBOUNDED:
            return await self._read(collection, namespace, _evenly_spaced(chunk_ids, count), include_restricted)
        return await self._fit(collection, namespace, chunk_ids, include_restricted, budget, count)

    async def _fit(
        self, collection: str, namespace: str, chunk_ids: list[str], include_restricted: bool, budget: int, count: int
    ) -> list[str]:
        """Draw, count and shrink until the excerpts fit *budget* (the stated loop of the module docstring)."""
        if budget == 0:
            raise self._window_too_small(budget)
        if not isinstance(self._client, TokenCountingClient):
            raise _GenerationStopped(
                DescriptionFailureReason.CALL_FAILED, "the client sizes its input to a budget but cannot count tokens"
            )
        while True:
            excerpts = await self._read(
                collection, namespace, _evenly_spaced(chunk_ids, count), include_restricted
            )
            with _provider_step():
                cost = await self._client.count_tokens(_SAMPLE_SEPARATOR.join(excerpts))
            if cost <= budget:
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

    def _window_too_small(self, budget: int) -> _GenerationStopped:
        window = self._client.context_window()
        named = f"{window.size} tokens, {window.source.value}" if isinstance(window, ContextWindowTokens) else str(window)
        room = "the prompt and one excerpt" if budget == 0 else "one excerpt"
        return _GenerationStopped(
            DescriptionFailureReason.CALL_FAILED,
            f"the model's context window ({named}) leaves {budget} input tokens, too little for {room}",
        )

    async def _ask(self, collection: str, sample: list[str]) -> str:
        request = LlmCallRequest(
            prompt=_DESCRIPTION_PROMPT.format(name=collection, samples=_SAMPLE_SEPARATOR.join(sample)),
            max_output_tokens=ANSWER_RESERVE_TOKENS,
            timeout_seconds=self._config.timeout_seconds,
        )
        with _provider_step():
            result = await self._client.call(request)
            answer = result.text.strip()
        if not answer:
            raise _GenerationStopped(DescriptionFailureReason.UNUSABLE_OUTPUT, "the model returned an empty answer")
        return answer

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
