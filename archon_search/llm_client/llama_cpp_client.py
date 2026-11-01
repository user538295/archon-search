"""LlamaCppClient — the ``llama_cpp`` wrapper of the shared LLM client (contract C1, scenarios S8, S11).

Thin wrapper over ``httpx`` (a core dependency) talking to a llama-server (llama.cpp ``tools/server``):

* ``GET /props`` holds the window under ``default_generation_settings.n_ctx``; :meth:`LlamaCppClient.probe_context_window`
  reads it once at composition time (``context_window`` is synchronous). Until it succeeds, and when it fails, the
  window is ``ASSUMED_CONTEXT_TOKENS`` and one WARNING per server (``host:port``) and process says so (S8).
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
from archon_search.llm_client.budget import TemplateCostCache, exact_budget
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

#: The dummy-prefix token a SentencePiece vocabulary may put ahead of a text, which a byte count does not include
#: (``"€"`` on Llama 2 is the prefix plus three byte tokens). The ``+ 1`` bounds the whole text only because ``/tokenize``
#: parses special tokens by default and the text of every special token is at least two bytes (``<s>``): such a token costs
#: one token for at least two bytes, so it saves at least one token against the byte count, which pays for the dummy
#: prefix of the fragment that follows it. A special token of one byte would break the bound.
_SPM_DUMMY_PREFIX_TOKENS: Final = 1


def _assumed_window() -> ContextWindowTokens:
    return ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)


def _unexpected_reply(exc: Exception) -> LlmCallFailure:
    return LlmCallFailure(PROVIDER_NAME, f"unexpected reply ({type(exc).__name__}); is the base URL a llama-server?")


def exception_types(exc: BaseException) -> str:
    """The types of *exc* and of its causes, outermost first (``LlmCallFailure < ConnectError < OSError``). Never the
    messages: those of a malformed base URL repeat the credentials it carries."""
    types: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        types.append(type(current).__name__)
        current = current.__cause__
    return " < ".join(types)


def warn_llama_cpp_unreachable(server: str, reason: str | None) -> None:
    """The one WARNING per server (``host:port``, see ``server_address.host_port``) and process that a llama-server cannot be reached.

    The context-window lookup of every ``llama_cpp`` feature, the startup reachability probe and the description startup
    probe ask a server the same question at boot; all report an outage through here, so one outage is one WARNING whichever
    finds it first (servers whose labels differ are different servers and each get a line). The one text carries both
    consequences: calls to the server fail, and the window of a feature on it is the assumed one where none could be read
    (S8, S34). The line carries the server label and *reason*, the failing path's own one-line cause (an exception type, an
    HTTP status, or the server's error text of at most ``MAX_SERVER_TEXT_CHARS`` characters); never a prompt, and nothing
    of the configured URL but its host and port. The record is attributed to this module's logger (``stacklevel`` of
    ``warn_once`` points at this function), whichever path reported.
    """
    warn_once(
        _logger,
        f"{_UNREACHABLE_KEY_PREFIX}{server}",
        "llama_cpp: the server at %s is not reachable (%s); requests to it fail until it answers, and where its context window could not be read, %d tokens are assumed",
        server,
        reason,
        ASSUMED_CONTEXT_TOKENS,
    )


class LlamaCppClient:
    """``LlmClient`` for one llama-server feature: one model on one server."""

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
        self._template_costs = TemplateCostCache()

    async def probe_context_window(self, timeout_seconds: float) -> ContextWindowTokens:
        """Read the window from ``GET /props`` and make it the client's window; never raises.

        A failed probe leaves the window as it was (assumed until a probe succeeds) and logs one WARNING per server and
        process (S8): the shared "not reachable" one (``warn_llama_cpp_unreachable``, also the other startup probes') when
        the server does not answer properly, a "no usable window" one when it answers without an ``n_ctx``. A WARNING names
        the server as host and port only, never credentials, path or query of the configured URL; the cause in the
        "not reachable" one is the failure's own bounded one-line reason (see ``warn_llama_cpp_unreachable``), and the
        types of its exception chain go to DEBUG (never the messages: a malformed URL's carries the credentials).
        """
        server = host_port(self._base_url)
        try:
            body = await self._request("GET", _PROPS_PATH, timeout_seconds)
        except LlmCallFailure as failure:
            _logger.debug("llama_cpp: GET %s on %s failed (%s)", _PROPS_PATH, server, exception_types(failure))
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
            self._template_costs.clear()  # the server that answers may be another model than the one counted before
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
        # The template's framed count is asked of the server once; the window is read live, so a window resolved later is used.
        return await exact_budget(self._window, template_text, self._template_costs.memoize_async(self._count_framed_tokens))

    def token_ceiling(self, text: str) -> int:
        """Bytes of *text* plus the SentencePiece dummy-prefix token. ``/tokenize`` adds no BOS (``add_special`` defaults to
        false); a byte-fallback vocabulary then spends at most one token per byte, and a SentencePiece one may put its
        dummy prefix ahead of them as a token of its own (``"€"`` on Llama 2: prefix plus three byte tokens). The one extra
        token holds for the whole text because ``/tokenize`` parses special tokens by default and every special token's
        text is at least two bytes (see ``_SPM_DUMMY_PREFIX_TOKENS``)."""
        return len(text.encode("utf-8", "surrogatepass")) + _SPM_DUMMY_PREFIX_TOKENS

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
