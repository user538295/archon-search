"""LlamaCppClient — the ``llama_cpp`` wrapper of the shared LLM client (contract C1, scenarios S8, S11).

Thin wrapper over ``httpx`` (a core dependency) talking to a llama-server (llama.cpp ``tools/server``):

* ``GET /props`` holds the window under ``default_generation_settings.n_ctx``; :meth:`LlamaCppClient.probe_context_window`
  reads it once at composition time (``context_window`` is synchronous). Until it succeeds, and when it fails, the
  window is ``ASSUMED_CONTEXT_TOKENS`` and one WARNING per host and process says so (S8).
* ``POST /apply-template`` (``{"messages": [...]}`` answers ``{"prompt": "..."}``) renders a message through the model's
  chat template and ``POST /tokenize`` (``{"content": text}`` answers ``{"tokens": [...]}``) counts it: the exact count
  :meth:`input_budget` sizes with (S11, ``llm_client.budget.exact_budget``), chat framing included.
* ``POST /v1/chat/completions`` carries every call: one fresh user message, nothing carried over (S6). llama-server
  rejects an oversized prompt with an error, which is raised as a failure.
* ``think``: ``ON`` / ``OFF`` send ``chat_template_kwargs: {"enable_thinking": true / false}`` (the chat template decides
  whether it has any effect; a template without thinking ignores it); ``DEFAULT`` sends nothing.
* Every failure is raised as ``LlmCallFailure`` (a timeout as ``LlmCallTimeout``). The reason carries the server's own
  status and error text where it gave one, never the prompt.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

import httpx

from archon_search.constants import ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client.budget import exact_budget
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.protocol import (
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallResult,
    LlmCallTimeout,
)
from archon_search.llm_client.server_address import host_port
from archon_search.llm_client.server_text import bounded_server_text

PROVIDER_NAME: Final = "llama_cpp"

_logger = logging.getLogger(__name__)

#: Longest wait for one ``/tokenize`` or ``/apply-template`` answer; ``input_budget`` takes no timeout of its own.
TOKENIZE_TIMEOUT_SECONDS: Final = 10.0

_PROPS_PATH: Final = "/props"
_TOKENIZE_PATH: Final = "/tokenize"
_APPLY_TEMPLATE_PATH: Final = "/apply-template"
_CHAT_PATH: Final = "/v1/chat/completions"

#: The per-request thinking switch of llama-server: ``chat_template_kwargs: {"enable_thinking": <bool>}`` (llama.cpp
#: ``tools/server/README.md``, "chat_template_kwargs"; the server reads it in ``tools/server/server-common.cpp``).
#: ``DEFAULT`` sends nothing, so the server's own default (``--reasoning``, ``--chat-template-kwargs``) applies.
_TEMPLATE_KWARGS_FIELD: Final = "chat_template_kwargs"
_ENABLE_THINKING_KWARG: Final = "enable_thinking"

#: ``warn_once`` keys, one per server and process. An unreachable server and a reachable one with no usable window are
#: different findings and keep different keys; the unreachable one is shared with the description startup probe.
_UNREACHABLE_KEY_PREFIX: Final = "llama_cpp_unreachable:"
_NO_WINDOW_KEY_PREFIX: Final = "llama_cpp_no_window:"


def _assumed_window() -> ContextWindowTokens:
    return ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)


def _unexpected_reply(exc: Exception) -> LlmCallFailure:
    return LlmCallFailure(PROVIDER_NAME, f"unexpected reply ({type(exc).__name__}); is the base URL a llama-server?")


def warn_llama_cpp_unreachable(server: str, reason: str) -> None:
    """The one WARNING per server (``host:port``, see ``server_address.host_port``) and process that a llama-server cannot be reached.

    The context-window lookup of every ``llama_cpp`` feature and the description startup probe ask the same server the same
    question at boot; both report an outage through here, so one outage is one WARNING whichever finds it first. The one text
    carries both consequences: calls to the server fail, and the window of every feature on it is the assumed one until a
    lookup succeeds (S8, S34). *reason* names the cause and never a prompt.
    """
    warn_once(
        _logger,
        f"{_UNREACHABLE_KEY_PREFIX}{server}",
        "llama_cpp: the server at %s is not reachable (%s); requests to it fail until it answers, and a context window of %d tokens is assumed until then",
        server,
        reason,
        ASSUMED_CONTEXT_TOKENS,
    )


class LlamaCppClient:
    """``LlmClient`` for one llama-server feature: one model on one host."""

    def __init__(self, *, model: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """
        Args:
            model: the ``model`` field sent with every chat completion.
            base_url: the llama-server URL.
            transport: replaces the HTTP transport; tests only.
        """
        self._model = model
        self._base_url = base_url
        self._transport = transport
        self._window = _assumed_window()

    async def probe_context_window(self, timeout_seconds: float) -> ContextWindowTokens:
        """Read the window from ``GET /props`` and make it the client's window; never raises.

        A failed probe leaves the window as it was (assumed until a probe succeeds) and logs one WARNING per server and
        process (S8): the shared "not reachable" one (``warn_llama_cpp_unreachable``, also the description startup probe's)
        when the server does not answer properly, a "no usable window" one when it answers without an ``n_ctx``. A WARNING
        names host and port only, never credentials or a query string of the configured URL.
        """
        server = host_port(self._base_url)
        try:
            body = await self._request("GET", _PROPS_PATH, timeout_seconds)
        except LlmCallFailure as failure:
            warn_llama_cpp_unreachable(server, failure.reason)
            return self._window
        try:
            window_size = body["default_generation_settings"]["n_ctx"]
            if not isinstance(window_size, int) or isinstance(window_size, bool) or window_size < 1:
                raise ValueError("n_ctx is not a positive integer")
        except (KeyError, TypeError, ValueError) as exc:
            warn_once(
                _logger,
                f"{_NO_WINDOW_KEY_PREFIX}{server}",
                "llama_cpp: the server at %s answered %s without a usable context window (%s); assuming %d tokens",
                server,
                _PROPS_PATH,
                type(exc).__name__,
                ASSUMED_CONTEXT_TOKENS,
            )
        else:
            self._window = ContextWindowTokens(size=window_size, source=ContextWindowSource.PROBED)
        return self._window

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_output_tokens,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        enable_thinking = request.think.enabled
        if enable_thinking is not None:
            payload[_TEMPLATE_KWARGS_FIELD] = {_ENABLE_THINKING_KWARG: enable_thinking}
        body = await self._request("POST", _CHAT_PATH, request.timeout_seconds, payload, prompt=request.prompt)
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise _unexpected_reply(exc) from exc
        if not isinstance(text, str):
            raise _unexpected_reply(TypeError("content is not a string"))
        return LlmCallResult(text=text)

    def context_window(self) -> ContextWindowTokens:
        return self._window

    async def input_budget(self, template_text: str) -> InputBudget:
        return await exact_budget(self._window, template_text, self._count_framed_tokens)

    async def _count_framed_tokens(self, text: str) -> int:
        """Tokens of *text* as one user message after the server's chat template (system block, role markers, generation prompt).

        The server cuts the reserved answer space by these tokens when it builds the prompt of a call, so the budget counts them
        (T-2: with the plain count a prompt of exactly the budget overran ``n_ctx - ANSWER_RESERVE_TOKENS`` by 29 tokens on Qwen2.5).
        """
        body = await self._request("POST", _APPLY_TEMPLATE_PATH, TOKENIZE_TIMEOUT_SECONDS, {"messages": [{"role": "user", "content": text}]}, prompt=text)
        try:
            framed = body["prompt"]
        except (KeyError, TypeError) as exc:
            raise _unexpected_reply(exc) from exc
        if not isinstance(framed, str):
            raise _unexpected_reply(TypeError("prompt is not a string"))
        return await self.count_tokens(framed)

    async def count_tokens(self, text: str) -> int:
        body = await self._request("POST", _TOKENIZE_PATH, TOKENIZE_TIMEOUT_SECONDS, {"content": text}, prompt=text)
        try:
            tokens = body["tokens"]
        except (KeyError, TypeError) as exc:
            raise _unexpected_reply(exc) from exc
        if not isinstance(tokens, list):
            raise _unexpected_reply(TypeError("tokens is not a list"))
        return len(tokens)

    async def _request(
        self, method: str, path: str, timeout_seconds: float, payload: dict[str, Any] | None = None, *, prompt: str = ""
    ) -> Any:
        """One HTTP exchange on a fresh client; the parsed JSON answer or a raised ``LlmCallFailure``."""
        try:
            async with (
                httpx.AsyncClient(base_url=self._base_url, timeout=timeout_seconds, transport=self._transport) as client,
                asyncio.timeout(timeout_seconds),
            ):
                response = await client.request(method, path, json=payload)
            if response.is_error:
                raise LlmCallFailure(PROVIDER_NAME, f"HTTP {response.status_code}: {self._error_text(response, prompt)}")
            return response.json()
        except LlmCallFailure:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise LlmCallTimeout(PROVIDER_NAME, f"no answer within {timeout_seconds:g}s") from exc
        except httpx.ConnectError as exc:
            raise LlmCallFailure(PROVIDER_NAME, "cannot connect to the llama-server") from exc
        except httpx.HTTPError as exc:
            raise LlmCallFailure(PROVIDER_NAME, f"the connection to the llama-server failed ({type(exc).__name__})") from exc
        except ValueError as exc:  # json.JSONDecodeError and UnicodeDecodeError of a non-JSON body
            raise _unexpected_reply(exc) from exc
        except Exception as exc:  # e.g. httpx.InvalidURL from a malformed base URL: not an httpx.HTTPError
            raise LlmCallFailure(PROVIDER_NAME, f"unexpected {type(exc).__name__}") from exc

    @staticmethod
    def _error_text(response: httpx.Response, prompt: str) -> str:
        """llama-server nests its errors as ``{"error": {"message": ...}}``; anything else is read as plain text."""
        try:
            error = response.json()["error"]
            text = error["message"] if isinstance(error, dict) else str(error)
        except (ValueError, KeyError, TypeError):
            text = response.text
        return bounded_server_text(str(text), prompt)
