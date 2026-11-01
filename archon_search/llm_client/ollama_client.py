"""OllamaClient — the ``ollama`` wrapper of the shared LLM client (contract C1, scenarios S6, S7, S11).

Thin wrapper over the native ``ollama`` SDK (``AsyncClient.chat``), which is why it can send ``num_ctx``: the
OpenAI-compatible endpoint has no such field.

* Stateless (S6): every call builds its own SDK client and sends exactly one user message; nothing carries over.
* Context (S7): a configured ``context_tokens`` is sent as ``options.num_ctx`` and is the window the prompt is sized
  to; unset, the window is ``ASSUMED_CONTEXT_TOKENS``, assumed and NOT sent, so the server's own window applies.
* Sizing (S11): :meth:`OllamaClient.input_budget` counts estimate tokens (gpt2 plus digit and astral-plane surcharges, ``llm_client.token_counter``) plus ``OLLAMA_ESTIMATE_MARGIN`` (``llm_client.budget``).
* ``think``: ``DEFAULT`` sends no thinking control; ``ON`` and ``OFF`` send the SDK's ``think=True`` / ``think=False``.
* Every failure is raised as ``LlmCallFailure`` (a timeout as ``LlmCallTimeout``, ``think = true`` on a model without a
  thinking mode as ``LlmThinkingNotSupported``). The reason carries the provider's own
  status and error text where the server gave one (``model not found``, ``does not support thinking``), never the prompt.

The ``ollama`` package is an optional extra: it is imported when a call or the model query needs it, never at import.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Final

from archon_search.constants import ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client.budget import estimated_budget
from archon_search.llm_client.protocol import (
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallResult,
    LlmCallTimeout,
    LlmThinkingNotSupported,
)
from archon_search.llm_client.server_text import bounded_server_text
from archon_search.llm_client.token_counter import TokenCounter

PROVIDER_NAME: Final = "ollama"

#: Ollama answers ``HTTP 400 "<model>" does not support thinking`` when ``think`` is true for a model without a thinking
#: mode (its server returns it from ``/api/chat``; seen on Ollama 0.40.13 with ``llama3.1:8b``).
_BAD_REQUEST_STATUS: Final = 400
_NO_THINKING_TEXT: Final = "does not support thinking"

#: ``/api/show`` model_info key that names the architecture; the model maximum sits under ``<architecture>.context_length``.
_ARCHITECTURE_KEY: Final = "general.architecture"
_CONTEXT_LENGTH_SUFFIX: Final = "context_length"

#: Opens an SDK client for a host: an async context manager with ``chat`` and ``show``. The default is ``ollama.AsyncClient``.
SdkClientFactory = Callable[[str], AbstractAsyncContextManager[Any]]


def _open_sdk_client(host: str) -> AbstractAsyncContextManager[Any]:
    import ollama

    return ollama.AsyncClient(host=host)


class OllamaClient:
    """``LlmClient`` for one Ollama feature: one model on one host with its own context setting."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        context_tokens: int | None,
        token_counter: TokenCounter | None = None,
        client_factory: SdkClientFactory = _open_sdk_client,
    ) -> None:
        """
        Args:
            model: the Ollama model name.
            base_url: the Ollama host URL.
            context_tokens: the configured ``ollama_context_tokens``, or ``None`` when unset.
            token_counter: estimates tokens (gpt2-based) for sizing; one is built when omitted.
            client_factory: opens the SDK client; replaced in tests.
        """
        self._model = model
        self._base_url = base_url
        self._context_tokens = context_tokens
        self._token_counter = token_counter if token_counter is not None else TokenCounter()
        self._client_factory = client_factory
        if context_tokens is None:
            self._window = ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)
        else:
            self._window = ContextWindowTokens(size=context_tokens, source=ContextWindowSource.CONFIGURED)

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        options: dict[str, int] = {"num_predict": request.max_output_tokens}
        if self._context_tokens is not None:
            options["num_ctx"] = self._context_tokens
        arguments: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": request.prompt}],
            "options": options,
        }
        think = request.think.enabled
        if think is not None:
            arguments["think"] = think
        try:
            async with self._client_factory(self._base_url) as client, asyncio.timeout(request.timeout_seconds):
                response = await client.chat(**arguments)
        except Exception as exc:
            raise self._failure(exc, request.timeout_seconds, request.prompt) from exc
        return LlmCallResult(text=response.message.content or "")

    def context_window(self) -> ContextWindowTokens:
        return self._window

    async def input_budget(self, template_text: str) -> InputBudget:
        try:
            return await estimated_budget(self._window, template_text, self._token_counter.count)
        except Exception as exc:
            raise LlmCallFailure(PROVIDER_NAME, f"cannot size the prompt: the gpt2 token counter failed ({type(exc).__name__})") from exc

    async def count_tokens(self, text: str) -> int:
        """The token estimate of *text* (gpt2 plus the digit and astral-plane surcharges), the unit :meth:`input_budget` is measured in (callers that size passages to the
        budget count with this); the count runs in a thread because the counter's first use may download its tokenizer."""
        try:
            return await asyncio.to_thread(self._token_counter.count, text)
        except Exception as exc:
            raise LlmCallFailure(PROVIDER_NAME, f"cannot count tokens: the gpt2 token counter failed ({type(exc).__name__})") from exc

    async def model_max_context_tokens(self, timeout_seconds: float) -> int | None:
        """The model's own maximum context, from ``/api/show``; ``None`` when the server does not state one.

        Raises:
            LlmCallTimeout: no answer within *timeout_seconds*.
            LlmCallFailure: any other failure (unreachable, unknown model, missing package).
        """
        try:
            async with self._client_factory(self._base_url) as client, asyncio.timeout(timeout_seconds):
                response = await client.show(self._model)
        except Exception as exc:
            raise self._failure(exc, timeout_seconds) from exc
        model_info = response.modelinfo or {}
        architecture = model_info.get(_ARCHITECTURE_KEY)
        if architecture is None:
            return None
        maximum = model_info.get(f"{architecture}.{_CONTEXT_LENGTH_SUFFIX}")
        if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 0:
            return maximum
        return None

    @staticmethod
    def _failure(exc: Exception, timeout_seconds: float, prompt: str = "") -> LlmCallFailure:
        """Map an SDK, transport or import error onto the C1 failure contract, without the prompt."""
        if isinstance(exc, TimeoutError):
            return LlmCallTimeout(PROVIDER_NAME, f"no answer within {timeout_seconds:g}s")
        if isinstance(exc, ConnectionError):
            return LlmCallFailure(PROVIDER_NAME, "cannot connect to the Ollama server")
        if isinstance(exc, ImportError):
            if isinstance(exc, ModuleNotFoundError) and exc.name == "ollama":
                from archon_search.uv_tool import extra_install_hint

                return LlmCallFailure(PROVIDER_NAME, f"the ollama package is not installed; run: {extra_install_hint('ollama')}")
            return LlmCallFailure(PROVIDER_NAME, f"the ollama package failed to import ({type(exc).__name__})")
        import httpx
        from ollama import ResponseError
        from pydantic import ValidationError

        if isinstance(exc, ResponseError):
            server_text = str(exc.error)
            reason = f"HTTP {exc.status_code}: {bounded_server_text(server_text, prompt)}"
            if exc.status_code == _BAD_REQUEST_STATUS and _NO_THINKING_TEXT in server_text:
                return LlmThinkingNotSupported(PROVIDER_NAME, reason)
            return LlmCallFailure(PROVIDER_NAME, reason)
        if isinstance(exc, httpx.TransportError):
            return LlmCallFailure(PROVIDER_NAME, f"the connection to the Ollama server failed ({type(exc).__name__})")
        if isinstance(exc, ValidationError | json.JSONDecodeError):
            return LlmCallFailure(PROVIDER_NAME, f"unexpected reply ({type(exc).__name__}); is the base URL an Ollama server?")
        return LlmCallFailure(PROVIDER_NAME, f"unexpected {type(exc).__name__}")
