"""Composition of the per-feature shared LLM clients (collection-descriptions plan, BE-10, BE-14, contract C1, S5, S64, S67, S68).

Frameworks & Drivers: the server startup builds one client per feature from that feature's own section
(``build_llm_client``), so HyDE, RAG Fusion, graph enrichment and description each keep their own provider, model, host
and context setting; no two features share a client.

``resolve_context_windows`` then settles each client's window, per client and without blocking boot:

* ``llama_cpp``: the window is probed from ``GET /props`` (the client logs its own WARNING on a failed probe).
* ``ollama`` with no ``ollama_context_tokens``: 4096 is assumed and nothing is sent as ``num_ctx``, so the server's own
  window applies; one WARNING per feature and process tells the operator to set the key.
* ``ollama`` with a value: it is compared with the model's maximum from ``/api/show``; above it, one WARNING per feature and
  process says so, because Ollama lowers the window silently.
* Cloud providers and ``claude_cli`` have no window to resolve.

Nothing here raises: an unreachable server only means the check could not run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from archon_search.constants import ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client.anthropic_client import AnthropicClient
from archon_search.llm_client.claude_cli_client import ClaudeCliClient
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.openai_client import OpenAIClient
from archon_search.llm_client.protocol import (
    ContextWindowSource,
    LlmCallFailure,
    LlmClient,
)
from archon_search.llm_client.rate_limit import FixedWindowRateLimiter

_logger = logging.getLogger(__name__)

#: Longest wait for one window read at startup (the model details or the server properties); the answer is metadata, not
#: a generation, so a server that takes longer is treated as not answering.
WINDOW_RESOLVE_TIMEOUT_SECONDS: Final = 5.0


@dataclass(frozen=True, slots=True)
class FeatureClient:
    """One feature's client; ``feature`` is its TOML section name (``hyde``, ``rag_fusion``, ``graph``, ``description``)."""

    feature: str
    client: LlmClient


def build_llm_client(
    *,
    provider: str,
    model: str,
    ollama_base_url: str,
    llama_cpp_base_url: str,
    ollama_context_tokens: int | None,
    retry_on_nonzero_exit: bool = False,
    rate_limit_rpm: int | None = None,
) -> LlmClient:
    """Build the client of one feature from that feature's own settings.

    Args:
        provider: one of the config registry's providers (``anthropic``, ``openai``, ``ollama``, ``claude_cli``, ``llama_cpp``).
        model: the feature's model name.
        ollama_base_url: the feature's Ollama host; used only for ``ollama``.
        llama_cpp_base_url: the feature's llama-server host; used only for ``llama_cpp``.
        ollama_context_tokens: the feature's ``ollama_context_tokens`` (``None`` when unset); used only for ``ollama``.
        retry_on_nonzero_exit: ``claude_cli`` only, one retry on a non-zero exit; HyDE and RAG Fusion pass ``True``, graph and
            description leave it off (their prompts hold excerpts a retry would send twice).
        rate_limit_rpm: the graph rate limit (``[graph].extraction_rate_limit_rpm``), requests per minute for this one
            client. Only ``anthropic`` and ``claude_cli`` honour it (a Claude login and an Anthropic key have an account
            quota); ``openai`` stays unthrottled as it was, and ``ollama`` and ``llama_cpp`` have no account quota, so the
            graph composition may pass the value whatever its provider. ``None`` (HyDE, RAG Fusion, description) builds
            an unthrottled client.

    Raises:
        ValueError: *provider* is not a known provider (the config loader rejects those, so only programmatic callers can hit it).
    """
    match provider:
        case "anthropic":
            return AnthropicClient(model=model, rate_limiter=_graph_limiter(rate_limit_rpm))
        case "openai":
            return OpenAIClient(model=model)
        case "ollama":
            return OllamaClient(model=model, base_url=ollama_base_url, context_tokens=ollama_context_tokens)
        case "claude_cli":
            return ClaudeCliClient(model=model, retry_on_nonzero_exit=retry_on_nonzero_exit, rate_limiter=_graph_limiter(rate_limit_rpm))
        case "llama_cpp":
            return LlamaCppClient(model=model, base_url=llama_cpp_base_url)
        case _:
            raise ValueError(f"unknown LLM provider {provider!r}")


def _graph_limiter(rate_limit_rpm: int | None) -> FixedWindowRateLimiter | None:
    """A fresh limiter for one client (limiters are never shared between features), or ``None`` when no limit is set."""
    return None if rate_limit_rpm is None else FixedWindowRateLimiter(rate_limit_rpm)


async def resolve_context_windows(features: Iterable[FeatureClient]) -> None:
    """Resolve the window of every client concurrently, each against its own server; never raises.

    Only the ``llama_cpp`` and ``ollama`` wrappers have a window to resolve; every other client (cloud providers,
    ``claude_cli``) is left alone.
    """
    await asyncio.gather(*(_resolve_context_window(entry) for entry in features))


async def _resolve_context_window(entry: FeatureClient) -> None:
    try:
        if isinstance(entry.client, LlamaCppClient):
            await entry.client.probe_context_window(WINDOW_RESOLVE_TIMEOUT_SECONDS)
        elif isinstance(entry.client, OllamaClient):
            await _check_ollama_window(entry.feature, entry.client)
    except Exception as exc:
        _logger.warning("[%s] the context window could not be resolved (%s)", entry.feature, type(exc).__name__)
        _logger.debug("[%s] context window resolution failed", entry.feature, exc_info=True)


async def _check_ollama_window(feature: str, client: OllamaClient) -> None:
    window = client.context_window()
    if window.source is ContextWindowSource.ASSUMED:
        warn_once(
            _logger,
            f"ollama_window_unset:{feature}",
            "[%s].ollama_context_tokens is not set: assuming %d tokens and sending no num_ctx, so the window "
            "Ollama picked for itself applies. Set [%s].ollama_context_tokens in archon-search.toml.",
            feature,
            ASSUMED_CONTEXT_TOKENS,
            feature,
        )
        return
    try:
        model_maximum = await client.model_max_context_tokens(WINDOW_RESOLVE_TIMEOUT_SECONDS)
    except LlmCallFailure as failure:
        _logger.info(
            "[%s] could not read the model's maximum context from Ollama (%s); ollama_context_tokens = %d is not checked",
            feature,
            failure.reason,
            window.size,
        )
        return
    if model_maximum is not None and window.size > model_maximum:
        warn_once(
            _logger,
            f"ollama_window_oversized:{feature}",
            "[%s].ollama_context_tokens = %d is above the model's maximum of %d tokens (read from Ollama's model "
            "details); Ollama lowers the window to that maximum without saying so. Set [%s].ollama_context_tokens "
            "to %d or less.",
            feature,
            window.size,
            model_maximum,
            feature,
            model_maximum,
        )
