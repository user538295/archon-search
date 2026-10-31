"""OpenAIClient — the ``openai`` wrapper of the shared LLM client (contract C1, scenario S6).

Thin wrapper over the ``openai`` SDK (``AsyncOpenAI.chat.completions.create``).

* Lazy: the SDK is imported and its client built on the first call, never at construction, so a process that never
  calls it needs neither the package nor the key. The built client is kept: the SDK pools its connections.
* Stateless (S6): every call sends exactly one user message; nothing carries over between calls.
* Key guard: a missing ``OPENAI_API_KEY`` is raised as ``LlmCallFailure`` before any SDK client is built.
* Output ceiling: sent as ``max_completion_tokens``, which bounds visible and reasoning tokens alike and is accepted by
  reasoning models; the older ``max_tokens`` is deprecated by the SDK and rejected by them.
* Cloud: no context window and no trimming; ``input_budget`` is ``cloud_budget()``. ``think`` has no verified switch
  and is ignored.
* Every failure is raised as ``LlmCallFailure`` (a timeout as ``LlmCallTimeout``); the reason carries the provider's own
  status and error text where the server gave one, bounded and with the prompt masked, never the prompt.

The ``openai`` package is an optional extra (``openai-provider``).
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
from archon_search.llm_client.server_text import bounded_server_text

PROVIDER_NAME: Final = "openai"
_KEY_VARIABLE: Final = "OPENAI_API_KEY"
#: The SDK retries 429, 5xx and connection errors by itself, resending the prompt and sleeping inside the caller's
#: timeout; a retry is the caller's decision (C1), so the SDK client is built with none.
_SDK_MAX_RETRIES: Final = 0
_SDK_EXTRA: Final = "openai-provider"

#: Builds the SDK client: an ``openai.AsyncOpenAI`` unless replaced in tests.
SdkClientFactory = Callable[[], Any]


def _build_sdk_client() -> Any:
    import openai

    return openai.AsyncOpenAI(max_retries=_SDK_MAX_RETRIES)


class OpenAIClient:
    """``LlmClient`` for one OpenAI feature: one model, the key from the environment."""

    def __init__(self, *, model: str, client_factory: SdkClientFactory = _build_sdk_client) -> None:
        """
        Args:
            model: the OpenAI model name.
            client_factory: builds the SDK client on first use; replaced in tests.
        """
        self._model = model
        self._client_factory = client_factory
        self._client: Any | None = None

    def is_key_available(self) -> bool:
        """``True`` when ``OPENAI_API_KEY`` is set in the environment right now."""
        return provider_key_available(PROVIDER_NAME)

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        if not self.is_key_available():
            raise LlmCallFailure(PROVIDER_NAME, f"{_KEY_VARIABLE} is not set")
        try:
            if self._client is None:
                self._client = self._client_factory()
            async with asyncio.timeout(request.timeout_seconds):
                response = await self._client.chat.completions.create(
                    model=self._model,
                    max_completion_tokens=request.max_output_tokens,
                    messages=[{"role": "user", "content": request.prompt}],
                )
        except Exception as exc:
            raise self._failure(exc, request.timeout_seconds, request.prompt) from exc
        try:
            return LlmCallResult(text=response.choices[0].message.content or "")
        except (AttributeError, IndexError, TypeError) as exc:
            raise LlmCallFailure(PROVIDER_NAME, f"unexpected reply ({type(exc).__name__})") from exc

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

                return LlmCallFailure(PROVIDER_NAME, f"the openai package is not installed; run: {extra_install_hint(_SDK_EXTRA)}")
            return LlmCallFailure(PROVIDER_NAME, f"the openai package failed to import ({type(exc).__name__})")
        import openai

        if isinstance(exc, openai.APITimeoutError):
            return LlmCallTimeout(PROVIDER_NAME, "the request timed out")
        if isinstance(exc, openai.APIConnectionError):
            return LlmCallFailure(PROVIDER_NAME, "cannot connect to the OpenAI API")
        if isinstance(exc, openai.APIStatusError):
            return LlmCallFailure(PROVIDER_NAME, f"HTTP {exc.status_code}: {bounded_server_text(exc.message, prompt)}")
        return LlmCallFailure(PROVIDER_NAME, f"unexpected {type(exc).__name__}")
