"""Loopback fake model server for the shared LLM client tests (collection-descriptions plan, BE-5).

One server answers the endpoints of both local providers, so a wrapper test talks to a real HTTP server through the
real SDK or ``httpx`` instead of a mock:

* Ollama (what the ``ollama`` SDK calls): ``POST /api/chat`` and ``POST /api/show``, and ``GET /api/tags`` (the model list the description reachability probe reads).
* llama.cpp ``llama-server``: ``GET /props`` and ``GET /v1/models`` (the startup reachability probe), ``POST /tokenize``, ``POST /apply-template`` and ``POST /v1/chat/completions``.

Every request is recorded (method, path, decoded JSON body) for assertions on what a wrapper sent. The server listens
on ``127.0.0.1`` with an OS-chosen port and runs in a daemon thread; use it as a context manager::

    with FakeModelServer(reply_text="hello", llama_n_ctx=8192) as server:
        ...  # point the wrapper at server.base_url
        assert server.requests_to("/api/chat")[0].body["options"]["num_ctx"] == 8192

Tokenizing counts one token per whitespace-separated word, like ``tests/_token_counter_stub.StubTokenizer``.
Response shapes follow the Ollama API documentation (``docs/api.md``) and the llama.cpp server README.

While a server is entered, :func:`is_fake_server_port` answers ``True`` for its port: the session guard of
``tests/conftest.py`` (``tests/_model_host_guard.py``) lets an ``openai`` or ``ollama`` SDK client be built only for one.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import pytest

_LOOPBACK = "127.0.0.1"
_OK = 200

# ``POST /apply-template`` wraps one user message as ``<open> <content> <close>``: two words the fake tokenizer counts.
FAKE_CHAT_OPEN = "<|user|>"
FAKE_CHAT_CLOSE = "<|assistant|>"
FAKE_CHAT_FRAME_TOKENS = 2
_BAD_REQUEST = 400
_NOT_FOUND = 404

_CHAT_PATHS = frozenset({"/api/chat", "/v1/chat/completions"})
_HELD_CHAT_MAX_SECONDS = 120.0  # a test that forgot to release answers 503 instead of parking a handler thread forever
_UNAVAILABLE = 503
_DEFAULT_REPLY = "fake model reply"
_DEFAULT_OLLAMA_CONTEXT_LENGTH = 8192
_DEFAULT_LLAMA_N_CTX = 4096
_OLLAMA_ARCHITECTURE = "llama"
_MODEL_NAME = "fake-model"
_POLL_INTERVAL_SECONDS = 0.01  # how soon ``shutdown`` is noticed; the default 0.5 s would slow every test

# llama-server nests its errors as {"error": {"code", "message", "type"}}; Ollama answers {"error": "<text>"}.
_LLAMA_PATHS = frozenset({"/props", "/v1/models", "/tokenize", "/apply-template", "/v1/chat/completions"})
_LLAMA_ERROR_TYPES = {400: "invalid_request_error", 404: "not_found_error", 503: "unavailable_error"}
_LLAMA_OTHER_ERROR_TYPE = "server_error"

_listening_ports: set[int] = set()
_listening_lock = threading.Lock()


def is_fake_server_port(port: int) -> bool:
    """Whether a ``FakeModelServer`` is entered, or an :func:`unreachable_port` is held, on loopback ``port`` right now."""
    with _listening_lock:
        return port in _listening_ports


@contextlib.contextmanager
def unreachable_port() -> Iterator[int]:
    """A loopback port nothing answers on, for the test of a server that is down.

    A bound socket that never listens keeps its port from being handed to another test. On macOS a connection attempt to
    it is dropped instead of refused (measured: the connect attempt ends after about 8 s), so a call to it ends at the
    caller's own timeout or that connect timeout, whichever is first. The session guard counts the port as a fake
    server's, so an SDK client may be built for it.
    """
    with socket.socket() as refusing:
        refusing.bind((_LOOPBACK, 0))
        port = refusing.getsockname()[1]
        with _listening_lock:
            _listening_ports.add(port)
        try:
            yield port
        finally:
            with _listening_lock:
                _listening_ports.discard(port)


@dataclass(frozen=True)
class RecordedRequest:
    """One request the server received; ``body`` is the decoded JSON object, ``None`` when there was none."""

    method: str
    path: str
    body: dict[str, Any] | None


class FakeModelServer:
    """Loopback server for both providers; see the module docstring.

    Args:
        reply_text: the model's answer on ``/api/chat`` and ``/v1/chat/completions`` (a public attribute: a test may change it).
        ollama_context_length: the model maximum ``/api/show`` reports under ``<architecture>.context_length``.
        llama_n_ctx: the window ``GET /props`` reports under ``default_generation_settings.n_ctx``.
        status_overrides: path to HTTP status; such a path answers that status with an ``error`` body in the shape its
            real server uses (the request is still recorded), so a test can fail one endpoint and leave the others alone.
        response_delay_seconds: pause before every answer, so a test can make a client's timeout fire.
        hold_chat: the chat endpoints record a request on arrival but answer only after :meth:`release` (or when the
            server exits); the other endpoints answer at once. A test can then observe the state while a model call is in flight,
            and :meth:`hold` closes the gate again.
    """

    _httpd: _Server  # bound by ``__enter__``, so a server that is never entered holds no socket
    _thread: threading.Thread

    def __init__(
        self,
        *,
        reply_text: str = _DEFAULT_REPLY,
        ollama_context_length: int = _DEFAULT_OLLAMA_CONTEXT_LENGTH,
        llama_n_ctx: int = _DEFAULT_LLAMA_N_CTX,
        status_overrides: Mapping[str, int] | None = None,
        response_delay_seconds: float = 0.0,
        hold_chat: bool = False,
    ) -> None:
        self.reply_text = reply_text
        self._ollama_context_length = ollama_context_length
        self._llama_n_ctx = llama_n_ctx
        self._status_overrides = dict(status_overrides or {})
        self.response_delay_seconds = response_delay_seconds
        self._requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._chat_gate = threading.Event()
        if not hold_chat:
            self._chat_gate.set()

    @property
    def base_url(self) -> str:
        return f"http://{_LOOPBACK}:{self._httpd.server_address[1]}"

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self._requests)

    def requests_to(self, path: str) -> list[RecordedRequest]:
        return [recorded for recorded in self.requests if recorded.path == path]

    def __enter__(self) -> Self:
        self._httpd = _Server((_LOOPBACK, 0), _Handler, self)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": _POLL_INTERVAL_SECONDS},
            name="fake-model-server",
            daemon=True,
        )
        self._thread.start()
        with _listening_lock:
            _listening_ports.add(self._httpd.server_address[1])
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()  # let a held handler finish promptly (it is a daemon thread; ``server_close`` does not join it)
        with _listening_lock:
            _listening_ports.discard(self._httpd.server_address[1])
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()

    def hold(self) -> None:
        """Hold every chat request that arrives from now on, until :meth:`release`."""
        self._chat_gate.clear()

    def release(self) -> None:
        """Let every held chat request (and every later one) be answered."""
        self._chat_gate.set()

    def wait_until_released(self) -> bool:
        """Block while the gate is closed; ``False`` when it stayed closed for ``_HELD_CHAT_MAX_SECONDS``."""
        return self._chat_gate.wait(timeout=_HELD_CHAT_MAX_SECONDS)

    def record(self, request: RecordedRequest) -> None:
        with self._lock:
            self._requests.append(request)

    def respond(self, request: RecordedRequest) -> tuple[int, dict[str, Any]]:
        """Status and JSON answer for a recorded request."""
        forced = self._status_overrides.get(request.path)
        if forced is not None:
            return self.error(request.path, forced, f"forced status {forced}")
        route = (request.method, request.path)
        if route == ("POST", "/api/chat"):
            return _OK, self._ollama_chat()
        if route == ("POST", "/api/show"):
            return _OK, self._ollama_show()
        if route == ("GET", "/api/tags"):
            return _OK, {"models": [{"name": _MODEL_NAME, "model": _MODEL_NAME}]}
        if route == ("GET", "/v1/models"):
            return _OK, {"object": "list", "data": [{"id": _MODEL_NAME, "object": "model"}]}
        if route == ("GET", "/props"):
            return _OK, {"default_generation_settings": {"n_ctx": self._llama_n_ctx}, "total_slots": 1}
        if route == ("POST", "/tokenize"):
            return self._llama_tokenize(request.body)
        if route == ("POST", "/apply-template"):
            return self._llama_apply_template(request.body)
        if route == ("POST", "/v1/chat/completions"):
            return _OK, self._llama_chat()
        return self.error(request.path, _NOT_FOUND, f"no route for {request.method} {request.path}")

    @staticmethod
    def error(path: str, status: int, message: str) -> tuple[int, dict[str, Any]]:
        """An error answer in the shape the real server of that path uses: llama.cpp nests the error, Ollama does not."""
        if path in _LLAMA_PATHS:
            error_type = _LLAMA_ERROR_TYPES.get(status, _LLAMA_OTHER_ERROR_TYPE)
            return status, {"error": {"code": status, "message": message, "type": error_type}}
        return status, {"error": message}

    def _ollama_chat(self) -> dict[str, Any]:
        return {
            "model": _MODEL_NAME,
            "created_at": "2026-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": self.reply_text},
            "done": True,
            "done_reason": "stop",
        }

    def _ollama_show(self) -> dict[str, Any]:
        return {
            "details": {"family": _OLLAMA_ARCHITECTURE},
            "model_info": {
                "general.architecture": _OLLAMA_ARCHITECTURE,
                f"{_OLLAMA_ARCHITECTURE}.context_length": self._ollama_context_length,
            },
        }

    def _llama_tokenize(self, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        content = None if body is None else body.get("content")
        if not isinstance(content, str):
            return self.error("/tokenize", _BAD_REQUEST, "`content` is required")
        return _OK, {"tokens": list(range(len(content.split())))}

    def _llama_apply_template(self, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        messages = None if body is None else body.get("messages")
        if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict) or not isinstance(messages[0].get("content"), str):
            return self.error("/apply-template", _BAD_REQUEST, "`messages` is required")
        return _OK, {"prompt": f"{FAKE_CHAT_OPEN} {messages[0]['content']} {FAKE_CHAT_CLOSE}"}

    def _llama_chat(self) -> dict[str, Any]:
        return {
            "object": "chat.completion",
            "model": _MODEL_NAME,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.reply_text}, "finish_reason": "stop"}],
        }


class ClientDisconnectQuietHTTPServer(ThreadingHTTPServer):
    """A threading HTTP server that stays silent when a client resets or drops a connection.

    A kept-alive connection's handler thread outlives ``server_close``: it waits for the next request until the client
    closes. A pooled client closed (or collected) in a later test resets that connection, and ``socketserver`` then
    prints the traceback to a stderr that may be gone, which pytest reports against whatever test is running. A client
    that leaves is not an error of a stand-in server; any other error still reaches ``handle_error``. ``socketserver``
    calls it from inside its ``except`` block, which is what makes ``sys.exception()`` the exception being handled.
    """

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exception(), ConnectionError):
            return
        super().handle_error(request, client_address)


class _Server(ClientDisconnectQuietHTTPServer):
    """The HTTP server, holding the ``FakeModelServer`` its handlers record into and answer from."""

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], fake: FakeModelServer) -> None:
        super().__init__(address, handler)
        self.fake = fake


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like the real servers
    server: _Server

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        malformed = False
        body: object = None
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                malformed = True
        request = RecordedRequest(method=method, path=urlsplit(self.path).path, body=body if isinstance(body, dict) else None)
        fake.record(request)
        held_too_long = request.path in _CHAT_PATHS and not fake.wait_until_released()
        if held_too_long:
            status, answer = fake.error(request.path, _UNAVAILABLE, "a held chat request was never released")
        elif malformed:
            status, answer = fake.error(request.path, _BAD_REQUEST, "invalid JSON body")
        else:  # answered only now, so a held request carries the reply text current at its release
            status, answer = fake.respond(request)
        time.sleep(fake.response_delay_seconds)
        payload = json.dumps(answer).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return  # the client gave up (its timeout fired) before the delayed answer

    def log_message(self, format: str, *args: Any) -> None:
        return


def keep_loopback_out_of_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a developer's ``HTTP_PROXY`` unable to capture requests to the fake server (both spellings are read)."""
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, _LOOPBACK)
