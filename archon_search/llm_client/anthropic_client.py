"""AnthropicClient — the ``anthropic`` wrapper of the shared LLM client (contract C1, scenarios S6, S64).

Thin wrapper over the ``anthropic`` SDK (``AsyncAnthropic.messages.create``).

* Lazy: the SDK is imported and its client built on the first call, never at construction, so a process that never
  calls it needs neither the package nor the key. The built client is kept: the SDK pools its connections.
* Stateless (S6): every call sends exactly one user message; nothing carries over between calls.
* Key guard: a missing ``ANTHROPIC_API_KEY`` is raised as ``LlmCallFailure`` before any SDK client is built (so the
  SDK never waits on a missing credential, and tests that clear the key never reach the network).
* Cloud: no context window and no trimming; ``input_budget`` is ``cloud_budget()``. ``think`` has no verified switch
  and is ignored.
* Rate limit (S64): a graph client is built with a ``FixedWindowRateLimiter``; the slot is taken after the key guard and
  once the SDK client exists, before the request is sent. The other features build it without one.
* Every failure is raised as ``LlmCallFailure`` (a timeout as ``LlmCallTimeout``); the reason carries the provider's own
  status and error text where the server gave one, bounded and with the prompt masked, never the prompt.

The ``anthropic`` package is an optional extra (``hyde``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final

from archon_search.llm_client.budget import cloud_budget
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindow,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallResult,
    LlmCallTimeout,
)
from archon_search.llm_client.provider_key import provider_key_available
from archon_search.llm_client.rate_limit import FixedWindowRateLimiter
from archon_search.llm_client.server_text import bounded_server_text

PROVIDER_NAME: Final = "anthropic"
_KEY_VARIABLE: Final = "ANTHROPIC_API_KEY"
#: The SDK retries 429, 5xx and connection errors by itself, resending the prompt and sleeping inside the caller's
#: timeout; a retry is the caller's decision (C1), so the SDK client is built with none.
_SDK_MAX_RETRIES: Final = 0
_SDK_EXTRA: Final = "hyde"

#: Builds the SDK client: an ``anthropic.AsyncAnthropic`` unless replaced in tests.
SdkClientFactory = Callable[[], Any]


def _build_sdk_client() -> Any:
    import anthropic

    return anthropic.AsyncAnthropic(max_retries=_SDK_MAX_RETRIES)


class AnthropicClient:
    """``LlmClient`` for one Anthropic feature: one model, the key from the environment."""

    def __init__(
        self,
        *,
        model: str,
        client_factory: SdkClientFactory = _build_sdk_client,
        rate_limiter: FixedWindowRateLimiter | None = None,
    ) -> None:
        """
        Args:
            model: the Anthropic model name.
            client_factory: builds the SDK client on first use; replaced in tests.
            rate_limiter: the graph rate limit (``extraction_rate_limit_rpm``); every call takes one slot and an
                exhausted window raises ``LlmCallFailure``. ``None`` (HyDE, RAG Fusion, description) is unthrottled.
        """
        self._model = model
        self._client_factory = client_factory
        self._rate_limiter = rate_limiter
        self._client: Any | None = None

    def is_key_available(self) -> bool:
        """``True`` when ``ANTHROPIC_API_KEY`` is set in the environment right now."""
        return provider_key_available(PROVIDER_NAME)

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        if not self.is_key_available():
            raise LlmCallFailure(PROVIDER_NAME, f"{_KEY_VARIABLE} is not set")
        sdk_client = self._ensure_sdk_client(request)
        if self._rate_limiter is not None:
            self._rate_limiter.acquire(PROVIDER_NAME)
        try:
            async with asyncio.timeout(request.timeout_seconds):
                response = await sdk_client.messages.create(
                    model=self._model,
                    max_tokens=request.max_output_tokens,
                    messages=[{"role": "user", "content": request.prompt}],
                )
        except Exception as exc:
            raise self._failure(exc, request.timeout_seconds, request.prompt) from exc
        try:
            return LlmCallResult(text="".join(block.text for block in response.content if block.type == "text"))
        except (AttributeError, TypeError) as exc:
            raise LlmCallFailure(PROVIDER_NAME, f"unexpected reply ({type(exc).__name__})") from exc

    def _ensure_sdk_client(self, request: LlmCallRequest) -> Any:
        """Build the SDK client on the first call and keep it; a missing package or a failed build is raised before a rate-limit slot is taken."""
        if self._client is None:
            try:
                self._client = self._client_factory()
            except Exception as exc:
                raise self._failure(exc, request.timeout_seconds, request.prompt) from exc
        return self._client

    def context_window(self) -> ContextWindow:
        return UNBOUNDED

    async def input_budget(self, template_text: str) -> InputBudget:
        return cloud_budget()

    async def aclose(self) -> None:
        """Close the SDK client's connection pool when one was built; a no-op otherwise. The startup probe closes its
        throwaway client with it; the server lifespan closes the long-lived feature clients at shutdown."""
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    @staticmethod
    def _failure(exc: Exception, timeout_seconds: float, prompt: str) -> LlmCallFailure:
        """Map an SDK, transport or import error onto the C1 failure contract, without the prompt."""
        if isinstance(exc, TimeoutError):
            return LlmCallTimeout(PROVIDER_NAME, f"no answer within {timeout_seconds:g}s")
        if isinstance(exc, ImportError):
            if isinstance(exc, ModuleNotFoundError) and exc.name == PROVIDER_NAME:
                from archon_search.uv_tool import extra_install_hint

                return LlmCallFailure(PROVIDER_NAME, f"the anthropic package is not installed; run: {extra_install_hint(_SDK_EXTRA)}")
            return LlmCallFailure(PROVIDER_NAME, f"the anthropic package failed to import ({type(exc).__name__})")
        import anthropic

        if isinstance(exc, anthropic.APITimeoutError):
            return LlmCallTimeout(PROVIDER_NAME, "the request timed out")
        if isinstance(exc, anthropic.APIConnectionError):
            return LlmCallFailure(PROVIDER_NAME, "cannot connect to the Anthropic API")
        if isinstance(exc, anthropic.APIStatusError):
            return LlmCallFailure(PROVIDER_NAME, f"HTTP {exc.status_code}: {bounded_server_text(exc.message, prompt)}")
        return LlmCallFailure(PROVIDER_NAME, f"unexpected {type(exc).__name__}")
