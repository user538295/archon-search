"""RAG Fusion (Multi-Query Decomposition) query expansion for archon-search.

Decomposes a user query into N semantic variants through the shared LLM client
(``app.state.rag_fusion_client``), enabling downstream parallel search and second-pass RRF fusion.

The client raises on every failure (:class:`~archon_search.llm_client.protocol.LlmCallFailure`); this module
turns a failure into a :class:`RAGFusionFailure` — a typed reason and one fixed warning, never the query, the prompt or
the provider's text — and logs it once per process with the query fingerprint only. The pipeline catches it, searches the
single query, reports ``rag_fusion_applied: false`` and puts the warning into ``expansion_warning``. The query is
sized to the client's window before the call (``llm_client.fit``): a cloud client gets it whole, a local one the
head that fits, inside this call only (the original query still drives the search); a window with no room for
it is the same failure.

Privacy note: ``generate_variants`` sends the raw query text to the configured
LLM provider.  Operators who cannot allow this must keep
``[rag_fusion] enabled = false``.  No raw query text is ever written to logs —
only fingerprints.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from archon_search.llm_client.protocol import LlmClient

from archon_search._privacy import _query_fingerprint, mask_query_echo
from archon_search.config import RAGFusionConfig
from archon_search.llm_client.fit import NoRoomForInput, fit_query_to_window
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.protocol import (
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
)
from archon_search.llm_client.provider_key import provider_key_available
from archon_search.rag_fusion_failure import RAGFusionFailure, RAGFusionFailureReason

_logger = logging.getLogger(__name__)

# Control characters to reject in validated variants.
# Rejects \x00–\x1F (except \t \n \r), \x7F (DEL), and \x80–\x9F (C1 controls).
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)

# Line separators of the model's answer: LF, CRLF and a bare CR (``str.splitlines`` would also cut at
# the control characters ``_validate_variant`` rejects, hiding them instead of dropping the variant).
_LINE_BREAK_RE = re.compile(r"\r\n|\n|\r")

RAG_FUSION_PROMPT_TEMPLATE: Final = """\
You are a search query decomposer. Given a user query, generate {num_queries} alternative \
search queries that capture different facets of the same information need.
Rules: each query on its own line, plain text, under {max_chars} characters.
Output exactly {num_queries} queries, one per line.

---
{query}
---"""

#: Answer ceiling per requested variant: the call's ``max_output_tokens`` is this times ``num_queries``, and the input
#: budget holds back exactly that much of the window (the larger ``num_queries``, the less room for the query).
RAG_FUSION_TOKENS_PER_QUERY: Final = 150
#: Longest variant (characters) kept; a longer line is dropped (the prompt asks for under this).
RAG_FUSION_MAX_VARIANT_CHARS: Final = 500
#: ``warn_once`` key: one WARNING per process for every failed or empty RAG Fusion generation.
_RAG_FUSION_WARN_KEY: Final = "rag_fusion_generation"
#: ``warn_once`` key: one WARNING per process for every call whose window has no room for the query.
_RAG_FUSION_NO_ROOM_WARN_KEY: Final = "rag_fusion_no_room_for_query"
#: ``warn_once`` key: one WARNING per process for every generation that returned fewer valid variants than asked for.
_RAG_FUSION_SHORTFALL_WARN_KEY: Final = "rag_fusion_variant_shortfall"
_RATE_BUCKET_WINDOW_SECONDS: Final = 60.0
#: Providers with no request cap of their own to protect: local or login-billed, so the bucket skips them.
_UNTHROTTLED_PROVIDERS: Final = ("ollama", "claude_cli", "llama_cpp")


class RAGFusionGenerator:
    """Generates semantic query variants for RAG Fusion.

    Text generation goes through the injected shared :class:`~archon_search.llm_client.protocol.LlmClient`;
    this class owns the decomposition prompt, the parsing and validation of the answer, and the per-process
    token bucket (``[rag_fusion].max_requests_per_minute``).
    """

    def __init__(self, config: RAGFusionConfig, client: LlmClient) -> None:
        self._config = config
        self._client = client

        # Token bucket state for rate limiting (per-process, in-memory)
        self._lock = asyncio.Lock()
        self._rpm_tokens: int = config.max_requests_per_minute
        self._rpm_refill_at: float = time.monotonic() + _RATE_BUCKET_WINDOW_SECONDS

        # Last rate-limit WARNING (monotonic seconds); None until the first one
        self._rate_limit_warned_at: float | None = None

    def is_key_available(self) -> bool:
        """Return ``True`` when the provider's required API key is set at call time (G10 BE-5 Root-2)."""
        return provider_key_available(self._config.provider)

    @staticmethod
    def _validate_variant(text: str) -> str | None:
        """Return stripped text if valid, else None.

        A variant is invalid if:
        - empty after stripping whitespace
        - longer than 500 characters after stripping
        - contains Unicode control sequences (\\x00–\\x1F excluding \\t\\n\\r,
          or \\x7F–\\x9F)
        """
        stripped = text.strip()
        if not stripped:
            return None
        if len(stripped) > RAG_FUSION_MAX_VARIANT_CHARS:
            return None
        if _CONTROL_CHARS_RE.search(stripped):
            return None
        return stripped

    def _render_prompt(self, query: str) -> str:
        """The decomposition template around *query* (an empty one gives the template alone, the fixed part of every prompt)."""
        return RAG_FUSION_PROMPT_TEMPLATE.format(
            num_queries=self._config.num_queries, max_chars=RAG_FUSION_MAX_VARIANT_CHARS, query=query
        )

    def _parse_variants(self, raw_text: str) -> list[str]:
        """The valid lines of the model's answer, in order, cut to ``num_queries``."""
        variants: list[str] = []
        for line in _LINE_BREAK_RE.split(raw_text):
            variant = self._validate_variant(line)
            if variant is not None:
                variants.append(variant)
                if len(variants) >= self._config.num_queries:
                    break
        return variants

    async def generate_variants(self, query: str) -> list[str]:
        """Generate up to ``config.num_queries`` validated semantic variant strings.

        The query is sized to the client's window first (``llm_client.fit``): a cloud client has no budget and gets it
        whole; a local one gets the head that fits next to the template and its own answer ceiling (150 per variant), inside this call only.

        Raises :class:`RAGFusionFailure` — the pipeline then searches the single query and warns — when the rate limit is
        exhausted (its own WARNING, at most once a minute), the window has no room for the query (one WARNING per process
        naming the window), the client failed (unreachable, key missing, package missing, timeout, provider error, a failed
        token count) or the answer holds no valid variant. The first failure or empty answer per process logs one
        WARNING with the query fingerprint — never the query or a variant; the failure text goes through
        :func:`~archon_search._privacy.mask_query_echo` first. A shortfall (some valid variants, fewer than asked) is not
        a failure: the valid ones are returned.
        """
        # Ollama, claude_cli, and llama_cpp are local/free paths with no API cap —
        # skip rate limiting entirely.
        if self._config.provider not in _UNTHROTTLED_PROVIDERS:
            async with self._lock:
                now = time.monotonic()
                if now >= self._rpm_refill_at:
                    self._rpm_tokens = self._config.max_requests_per_minute
                    self._rpm_refill_at = now + _RATE_BUCKET_WINDOW_SECONDS

                if self._rpm_tokens <= 0:
                    # Rate limit warning at most once per minute
                    warned_at = self._rate_limit_warned_at
                    if warned_at is None or now - warned_at >= _RATE_BUCKET_WINDOW_SECONDS:
                        _logger.warning(
                            "RAG Fusion rate limit exhausted (fp=%s); falling back to single-query search",
                            _query_fingerprint(query),
                        )
                        self._rate_limit_warned_at = now
                    raise RAGFusionFailure(RAGFusionFailureReason.RATE_LIMITED)

                self._rpm_tokens -= 1

        num_queries = self._config.num_queries
        answer_ceiling = RAG_FUSION_TOKENS_PER_QUERY * num_queries
        sent_query = query  # what reaches the provider: the part of the query that fits the window
        try:
            sent_query = await fit_query_to_window(
                self._client,
                self._render_prompt(""),
                query,
                provider=self._config.provider,
                max_output_tokens=answer_ceiling,
            )
            request = LlmCallRequest(
                prompt=self._render_prompt(sent_query),
                max_output_tokens=answer_ceiling,
                timeout_seconds=self._config.timeout_seconds,
            )
            raw_text = (await self._client.call(request)).text
        except NoRoomForInput as no_room:
            warn_once(
                _logger,
                _RAG_FUSION_NO_ROOM_WARN_KEY,
                "RAG Fusion skipped: %s; searching the single query (later cases in this process are not logged)",
                no_room.detail,
            )
            raise RAGFusionFailure(RAGFusionFailureReason.NO_ROOM) from None
        except LlmCallFailure as failure:
            # C1: the failure text never holds the prompt, but a server may echo part of it back; mask the query.
            reason = mask_query_echo(str(failure), query, cut_chars=len(sent_query))
            warn_once(
                _logger,
                _RAG_FUSION_WARN_KEY,
                "RAG Fusion generation failed (fp=%s): %s; searching the single query (later generation failures in this process are not logged)",
                _query_fingerprint(query),
                reason,
            )
            timed_out = isinstance(failure, LlmCallTimeout)
            raise RAGFusionFailure(
                RAGFusionFailureReason.TIMEOUT if timed_out else RAGFusionFailureReason.PROVIDER_FAILED
            ) from None

        variants = self._parse_variants(raw_text)
        if not variants:
            warn_once(
                _logger,
                _RAG_FUSION_WARN_KEY,
                "RAG Fusion generation returned no valid variant (fp=%s); searching the single query (later generation failures in this process are not logged)",
                _query_fingerprint(query),
            )
            raise RAGFusionFailure(RAGFusionFailureReason.NO_VARIANTS)

        if len(variants) < num_queries:
            warn_once(
                _logger,
                _RAG_FUSION_SHORTFALL_WARN_KEY,
                "RAG Fusion: requested %d variants but got %d valid ones (fp=%s; later shortfalls in this process are not logged)",
                num_queries,
                len(variants),
                _query_fingerprint(query),
            )

        return variants
