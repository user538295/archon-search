"""RAG Fusion (Multi-Query Decomposition) query expansion for archon-search.

Decomposes a user query into N semantic variants through the shared LLM client
(``app.state.rag_fusion_client``), enabling downstream parallel search and second-pass RRF fusion.

The client raises on every failure (:class:`~archon_search.llm_client.protocol.LlmCallFailure`); this module
turns a failure into "no variants" — the pipeline then searches the single query and reports
``rag_fusion_applied: false`` — and logs it once per process with the query fingerprint only.

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
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.protocol import LlmCallFailure, LlmCallRequest
from archon_search.llm_client.provider_key import provider_key_available

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

#: Answer ceiling per requested variant: the call's ``max_output_tokens`` is this times ``num_queries``.
RAG_FUSION_TOKENS_PER_QUERY: Final = 150
#: Longest query (characters) put into the prompt; the rest is cut.
RAG_FUSION_MAX_QUERY_CHARS: Final = 2000
#: Longest variant (characters) kept; a longer line is dropped (the prompt asks for under this).
RAG_FUSION_MAX_VARIANT_CHARS: Final = 500
#: ``warn_once`` key: one WARNING per process for every failed or empty RAG Fusion generation.
_RAG_FUSION_WARN_KEY: Final = "rag_fusion_generation"
#: ``warn_once`` key: one WARNING per process for every generation that returned fewer valid variants than asked for.
_RAG_FUSION_SHORTFALL_WARN_KEY: Final = "rag_fusion_variant_shortfall"
_RATE_BUCKET_WINDOW_SECONDS: Final = 60.0
#: Providers with no request cap of their own to protect: local or login-billed, so the bucket skips them.
_UNTHROTTLED_PROVIDERS: Final = ("ollama", "claude_cli", "llama_cpp")


# Wire-safe pair for RAGFusionDependencyError. The exception message also carries the
# install command for this interpreter (``extra_install_hint``), which describes the
# server's own install — it is logged, never sent to the client.
RAG_FUSION_DEPENDENCY_DETAIL = "Install archon-search[rag_fusion] to use RAG Fusion"
RAG_FUSION_DEPENDENCY_CODE = "validation_error"


class RAGFusionDependencyError(RuntimeError):
    """A missing ``rag_fusion`` provider package, answered by every transport with the fixed detail.

    :class:`RAGFusionGenerator` no longer raises it: the shared client reports a missing package as a call
    failure and the search falls back to the single query. The transports' mapping stays until BE-13 decides
    whether the error remains reachable. Transports answer it with :data:`RAG_FUSION_DEPENDENCY_DETAIL` /
    :data:`RAG_FUSION_DEPENDENCY_CODE`, never ``str(exc)``.
    """


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

        Returns an empty list — the pipeline then searches the single query — when the rate limit is exhausted
        (its own WARNING, at most once a minute), the client failed (unreachable, key missing, package missing,
        timeout, provider error) or the answer holds no valid variant. The first failure or empty answer per
        process logs one WARNING with the query fingerprint — never the query or a variant; the failure text goes
        through :func:`~archon_search._privacy.mask_query_echo` first.
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
                    return []

                self._rpm_tokens -= 1

        num_queries = self._config.num_queries
        request = LlmCallRequest(
            prompt=RAG_FUSION_PROMPT_TEMPLATE.format(
                num_queries=num_queries,
                max_chars=RAG_FUSION_MAX_VARIANT_CHARS,
                query=query[:RAG_FUSION_MAX_QUERY_CHARS],
            ),
            max_output_tokens=RAG_FUSION_TOKENS_PER_QUERY * num_queries,
            timeout_seconds=self._config.timeout_seconds,
        )
        try:
            raw_text = (await self._client.call(request)).text
        except LlmCallFailure as failure:
            # C1: the failure text never holds the prompt, but a server may echo part of it back; mask the query.
            reason = mask_query_echo(str(failure), query, cut_chars=RAG_FUSION_MAX_QUERY_CHARS)
            warn_once(
                _logger,
                _RAG_FUSION_WARN_KEY,
                "RAG Fusion generation failed (fp=%s): %s; searching the single query (later generation failures in this process are not logged)",
                _query_fingerprint(query),
                reason,
            )
            return []

        variants = self._parse_variants(raw_text)
        if not variants:
            warn_once(
                _logger,
                _RAG_FUSION_WARN_KEY,
                "RAG Fusion generation returned no valid variant (fp=%s); searching the single query (later generation failures in this process are not logged)",
                _query_fingerprint(query),
            )
            return []

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
