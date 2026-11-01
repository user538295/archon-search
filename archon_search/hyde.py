"""HyDE (Hypothetical Document Embeddings) query expansion for archon-search.

Generates a short hypothetical answer passage through the shared LLM client (``app.state.hyde_client``)
and uses its embedding as the ANN lookup vector in place of the original query embedding.

The client raises on every failure (:class:`~archon_search.llm_client.protocol.LlmCallFailure`); this module
turns a failure into "no hypothetical document" — the caller then reports ``hyde_applied: false`` and searches
the plain query — and logs it once per process with the query fingerprint only. The query is sized to the
client's window before the call (``llm_client.fit``); a window with no room for it is the same "no document".
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from archon_search.embedder import Embedder
    from archon_search.llm_client.protocol import LlmClient

from archon_search._privacy import _query_fingerprint, mask_query_echo
from archon_search.config import HyDEConfig
from archon_search.llm_client.fit import NoRoomForInput, fit_query_to_window
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.protocol import LlmCallFailure, LlmCallRequest
from archon_search.llm_client.provider_key import provider_key_available

_logger = logging.getLogger(__name__)

HYDE_PROMPT_TEMPLATE: Final = """\
Write a short passage that would directly answer the following question.
Output only the passage — no preamble, no explanation.

---
{query}
---"""

#: Answer ceiling of the one HyDE call: a short passage, well inside an embedder's own token limit.
HYDE_MAX_OUTPUT_TOKENS: Final = 200
#: ``warn_once`` key: one WARNING per process for every failed or empty HyDE generation.
_HYDE_WARN_KEY: Final = "hyde_generation"
#: ``warn_once`` key: one WARNING per process for every call whose window has no room for the query.
_HYDE_NO_ROOM_WARN_KEY: Final = "hyde_no_room_for_query"
_RATE_BUCKET_WINDOW_SECONDS: Final = 60.0
#: Providers with no request cap of their own to protect: local or login-billed, so the bucket skips them.
_UNTHROTTLED_PROVIDERS: Final = ("ollama", "claude_cli", "llama_cpp")


class HyDEGenerator:
    """Generates hypothetical document embeddings for HyDE query expansion.

    Text generation goes through the injected shared :class:`~archon_search.llm_client.protocol.LlmClient`;
    the embedding step stays inside this generator, on the embedder the caller passes to ``generate()`` — the
    one serving the searched collection, so the hypothesis lands in that collection's vector space.

    The per-process token bucket (``[hyde].max_requests_per_minute``) lives here, not in the client.
    """

    def __init__(self, config: HyDEConfig, client: LlmClient) -> None:
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

    async def generate(self, query: str, *, embedder: Embedder) -> list[float] | None:
        """Generate a hypothetical document embedding for the given query.

        The hypothesis is embedded with *embedder*, which must be the embedder
        serving the searched collection — any other model yields a vector in the
        wrong space (or the wrong dimension).

        The query is sized to the client's window first (``llm_client.fit``): a cloud client has no budget and gets it
        whole; a local one gets the head that fits next to the template and the answer reserve, inside this call only —
        the caller keeps searching with the original query.

        Returns a vector (list of floats) on success, or ``None`` when the search must run on the plain query:
        rate limit exhausted (its own WARNING, at most once a minute), the window has no room for the query (one
        WARNING per process naming the window), the client failed (unreachable, key missing, timeout, provider error,
        a failed token count) or the hypothesis is empty. The first failure or empty hypothesis per process logs one
        WARNING with the query fingerprint — never the query or the hypothesis; the failure text goes through
        :func:`~archon_search._privacy.mask_query_echo` first.
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
                            "HyDE rate limit exhausted (fp=%s); falling back to query embedding",
                            _query_fingerprint(query),
                        )
                        self._rate_limit_warned_at = now
                    return None

                self._rpm_tokens -= 1

        sent_query = query  # what reaches the provider: the part of the query that fits the window
        try:
            sent_query = await fit_query_to_window(
                self._client, HYDE_PROMPT_TEMPLATE.format(query=""), query, provider=self._config.provider
            )
            request = LlmCallRequest(
                prompt=HYDE_PROMPT_TEMPLATE.format(query=sent_query),
                max_output_tokens=HYDE_MAX_OUTPUT_TOKENS,
                timeout_seconds=self._config.timeout_seconds,
            )
            hypothesis_text = (await self._client.call(request)).text.strip()
        except NoRoomForInput as no_room:
            warn_once(
                _logger,
                _HYDE_NO_ROOM_WARN_KEY,
                "HyDE skipped: %s; searching without it (later cases in this process are not logged)",
                no_room.detail,
            )
            return None
        except LlmCallFailure as failure:
            # C1: the failure text never holds the prompt, but a server may echo part of it back; mask the query.
            reason = mask_query_echo(str(failure), query, cut_chars=len(sent_query))
            warn_once(
                _logger,
                _HYDE_WARN_KEY,
                "HyDE generation failed (fp=%s): %s; searching without it (later generation failures in this process are not logged)",
                _query_fingerprint(query),
                reason,
            )
            return None

        if not hypothesis_text:
            warn_once(
                _logger,
                _HYDE_WARN_KEY,
                "HyDE generation returned an empty hypothesis (fp=%s); searching without it (later generation failures in this process are not logged)",
                _query_fingerprint(query),
            )
            return None

        # Embed hypothesis and return vector
        # Hypothesis text is untrusted — never logged verbatim
        return await embedder.embed_one(hypothesis_text)
