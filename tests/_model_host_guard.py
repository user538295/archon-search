"""Policy and patches behind the session guard in ``tests/conftest.py`` (collection-descriptions plan, BE-9, C1).

A test must not reach a real model provider, whatever the developer's shell holds: an API key, a running Ollama or
llama-server, a logged-in ``claude``. :func:`install_guards` refuses the doors a provider is reached through:

* **SDK clients.** Constructing an ``anthropic`` client always raises. Constructing an ``openai`` or ``ollama`` client raises
  unless it is aimed at a loopback server a test started (``tests/_fake_model_server.py``: :func:`is_fake_server_port` knows its
  port) or is bound to an injected in-process client (``openai``: ``http_client``; ``ollama``: ``transport`` or ``mounts``):
  that client reaches only what the injected one reaches, and the next guard refuses its real network transports.
* **HTTP to a model host.** A request through the real network transports (``HTTPTransport``, ``AsyncHTTPTransport``) of
  httpx or of ``httpx2`` (see ``_HTTPX_FLAVOURS``: the Hugging Face hub, ``openai`` and ``anthropic`` never touch ``httpx``)
  raises when its host is not loopback (a cloud provider, a remote model server, the Hub: a model download) or when its port
  is the default of a local model server (Ollama, llama-server: what a developer's own install listens on). Other loopback
  servers pass: the suite starts real ones of its own. The in-process ``MockTransport`` and ``ASGITransport`` are separate
  classes and are never touched.
* **``claude`` spawns.** A spawn raises when the program is a ``claude`` binary other than a fake that
  ``tests/_fake_claude.py`` installed (its resolved path is what counts, so the SDK-bundled and every other real ``claude``
  are refused whatever the directory), when it is ``claude`` by bare name (the OS would find it through ``PATH`` at exec time
  and the guard cannot say which binary that is), when it resolves to the real ``claude`` found on ``PATH`` when the session
  started (``REAL_CLAUDE_BINARIES``, symlinks followed, so no other name gets it past), or when a shell command line names
  ``claude``. A launcher in front of the binary (``env claude``, ``sh -c claude``) is not looked through.

What the guard does not cover: ``urllib``, ``requests``, ``aiohttp`` and ``hf_xet`` traffic. A refusal is a ``RuntimeError``,
which production code that catches ``Exception`` can absorb, so the guarantee is that nothing leaves the machine, not that a
test turns red. A shell command line is refused when it mentions ``claude`` as a word, so one that only names a directory or
an option value ``claude`` is refused too (fail closed). Tests that carry a live lane marker (``LIVE_LANE_MARKERS``) run
unguarded: reaching the real thing is their purpose, and the lanes are not part of the default run.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import ipaddress
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

from archon_search.config import LLAMA_CPP_BASE_URL_DEFAULT, OLLAMA_BASE_URL_DEFAULT
from tests import _fake_claude
from tests._fake_model_server import is_fake_server_port

#: Markers of the lanes that exist to reach the real thing (real weights, real servers, a real login).
LIVE_LANE_MARKERS: Final = frozenset(
    {"live", "live_eval", "live_benchmark", "runtime_parity", "smoke", "docling", "graph_real_artifact"}
)

#: The message of the ``anthropic`` refusal; the integration stub of the ``anthropic`` module refuses with the same one.
ANTHROPIC_REFUSAL: Final = (
    "Test suite attempted to instantiate the Anthropic client — "
    "this would burn real tokens. Add monkeypatch.setenv('ANTHROPIC_API_KEY', ...) "
    "and patch the client in your test instead."
)

_CLAUDE_NAME: Final = "claude"
#: ``claude`` as a word of a shell command line, in any case (macOS volumes ignore it): not ``.claude`` (a config directory),
#: ``claude-proxy`` or ``claude_cli``.
_CLAUDE_WORD: Final = re.compile(r"(?<![\w.-])claude(?![\w-])", re.IGNORECASE)
#: ``httpx2`` (a separate package, not an alias) is what ``huggingface_hub``, ``openai`` and ``anthropic`` import; ``ollama``
#: and this repository's ``llama_cpp`` client use ``httpx``.
_HTTPX_FLAVOURS: Final = ("httpx", "httpx2")
_OPENAI_DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_FAKE_SERVER_HINT: Final = "Aim it at a loopback fake server (tests/_fake_model_server.py) or inject a fake instead."
_POPEN_PARAMETERS: Final = inspect.signature(subprocess.Popen.__init__)


def _port_of(url: str) -> int:
    port = urlsplit(url).port
    if port is None:
        raise ValueError(f"the default model server URL {url!r} names no port")
    return port


_OLLAMA_DEFAULT_PORT: Final = _port_of(OLLAMA_BASE_URL_DEFAULT)
#: The default ports of the local model servers: a developer's own Ollama and llama-server listen there.
_LOCAL_MODEL_SERVER_PORTS: Final = frozenset({_OLLAMA_DEFAULT_PORT, _port_of(LLAMA_CPP_BASE_URL_DEFAULT)})


def _real_claude_binaries() -> frozenset[str]:
    """The resolved path of every ``claude`` an executable search of the current ``PATH`` finds."""
    found = (Path(directory) / _CLAUDE_NAME for directory in os.get_exec_path())
    return frozenset(os.path.realpath(path) for path in found if path.is_file() and os.access(path, os.X_OK))


#: Resolved paths of the real ``claude`` binaries, taken when this module is first imported (session start), before any test
#: edits ``PATH``.
REAL_CLAUDE_BINARIES: frozenset[str] = _real_claude_binaries()

_suspended = False


def set_suspended(suspended: bool) -> None:
    """Lift (``True``) or restore (``False``) every guard but the ``anthropic`` one, for the live lanes.

    Process-wide on purpose: the handler and event-loop threads of a test reach the patches too.
    """
    global _suspended
    _suspended = suspended


def suspend_for_markers(marker_names: Iterable[str]) -> None:
    """Lift the guards when ``marker_names`` holds a live lane marker, else (re)impose them."""
    set_suspended(not LIVE_LANE_MARKERS.isdisjoint(marker_names))


def _enforcing() -> bool:
    return not _suspended


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _effective_port(url: httpx.URL) -> int:
    return url.port or _DEFAULT_PORTS.get(url.scheme, 0)


def _http_refusal(host: str, port: int) -> str | None:
    """Why an HTTP call to ``host:port`` is refused in a test, or ``None`` when it may go ahead."""
    if not _is_loopback(host):
        return f"{host} is not a loopback host"
    if port in _LOCAL_MODEL_SERVER_PORTS:
        return f"port {port} is the default of a local model server"
    return None


def _client_refusal(host: str, port: int) -> str | None:
    """Why an SDK client for ``host:port`` is refused in a test, or ``None``: only a fake server a test started qualifies."""
    reason = _http_refusal(host, port)
    if reason is None and not is_fake_server_port(port):
        return f"no FakeModelServer owns port {port}"
    return reason


def _raise_if_refused(subject: str, host: str, port: int, reason: str | None) -> None:
    if reason is not None:
        raise RuntimeError(f"Test suite attempted {subject} {host}:{port} ({reason}). {_FAKE_SERVER_HINT}")


def _openai_target(*, base_url: str | httpx.URL | None = None, http_client: object | None = None, **_: Any) -> tuple[str, int] | None:
    """Where an ``openai`` client built with these arguments would connect; ``None`` when it reaches only ``http_client``."""
    if http_client is not None:
        return None
    url = httpx.URL(str(base_url or os.environ.get("OPENAI_BASE_URL") or _OPENAI_DEFAULT_BASE_URL))
    return url.host, _effective_port(url)


def _ollama_target(
    host: str | None = None, *, transport: object | None = None, mounts: object | None = None, **_: Any
) -> tuple[str, int] | None:
    """Where an ``ollama`` client built with this ``host`` would connect; ``None`` when it reaches only its own transport.

    The rules are those of the SDK's own ``_parse_host``: a missing host is loopback, a scheme-less ``host`` takes Ollama's
    port, ``http://host`` port 80. ``tests/test_model_host_guards.py`` pins this copy to the SDK function. ``transport`` and
    ``mounts`` go to httpx unchanged: an in-process one is exempt like ``openai``'s ``http_client``.
    """
    if transport is not None or mounts:
        return None
    raw = host or os.environ.get("OLLAMA_HOST") or ""
    scheme, separator, rest = raw.partition("://")
    if not separator:
        scheme, rest = "http", raw
    parts = urlsplit(f"{scheme}://{rest}")
    default_port = _DEFAULT_PORTS.get(scheme, 0) if separator else _OLLAMA_DEFAULT_PORT
    return parts.hostname or "127.0.0.1", parts.port or default_port


def _refusing_client_class(real: type, label: str, target: Callable[..., tuple[str, int] | None]) -> type:
    """A subclass of the SDK client class that refuses a target :func:`_client_refusal` refuses, before any SDK code runs.

    ``target`` is called with the arguments of the constructor, so its signature is the SDK's own.
    """

    class Guarded(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            endpoint = target(*args, **kwargs) if _enforcing() else None
            if endpoint is not None:
                _raise_if_refused(f"to instantiate the {label} client for", *endpoint, _client_refusal(*endpoint))
            super().__init__(*args, **kwargs)

    Guarded.__name__, Guarded.__qualname__, Guarded.__module__ = real.__name__, real.__qualname__, real.__module__
    return Guarded


def _refuse_request(request: Any) -> None:
    if not _enforcing():
        return
    port = _effective_port(request.url)
    _raise_if_refused("a real HTTP call to", request.url.host, port, _http_refusal(request.url.host, port))


def _words(command: Any) -> str:
    """The text of a shell command given as a string, or as the list of strings ``Popen`` accepts."""
    if isinstance(command, (str, bytes, os.PathLike)):
        return os.fsdecode(command)
    return " ".join(os.fsdecode(part) for part in command)


def _resolved_program(program: str, cwd: Any) -> str | None:
    """The resolved path of ``program`` as ``Popen`` would find it (``cwd`` anchors a relative path), or ``None``."""
    if os.path.dirname(program):
        return os.path.realpath(os.path.join(os.fsdecode(cwd), program) if cwd else program)
    found = shutil.which(program)
    return os.path.realpath(found) if found else None


def _spawn_refusal(arguments: Mapping[str, Any]) -> str | None:
    """Why the ``subprocess.Popen`` call with these bound arguments is refused in a test, or ``None``."""
    args = arguments.get("args")
    if arguments.get("shell") and _CLAUDE_WORD.search(_words(args)):
        return "a shell command line that names claude"
    program = _program(args, arguments.get("executable"))
    if program is None:
        return None
    resolved = _resolved_program(program, arguments.get("cwd"))
    if resolved in REAL_CLAUDE_BINARIES:
        return f"the real claude binary ({resolved})"
    if Path(program).stem.lower() != _CLAUDE_NAME:
        return None
    if not os.path.dirname(program):
        return "'claude' from an unresolved path (a bare name is left to the PATH lookup of the OS)"
    if not _fake_claude.is_installed_fake(resolved):
        return f"a claude binary that is not a fake of tests/_fake_claude.py ({resolved})"
    return None


def _program(args: Any, executable: Any) -> str | None:
    """The program a ``subprocess.Popen`` call starts: ``executable`` when given, else the first word of ``args``."""
    if executable:
        return os.fsdecode(executable)
    if isinstance(args, (str, bytes, os.PathLike)):
        return os.fsdecode(args)
    return os.fsdecode(args[0]) if args else None


def _patch_anthropic(stack: contextlib.ExitStack) -> None:
    """Refuse every construction of an ``anthropic`` client, unconditionally; the package may not be installed."""
    try:
        module = importlib.import_module("anthropic")
    except ImportError:
        return

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError(ANTHROPIC_REFUSAL)

    for class_name in ("Anthropic", "AsyncAnthropic"):
        stack.enter_context(patch.object(module, class_name, side_effect=refuse))


def _patch_sdk_clients(stack: contextlib.ExitStack) -> None:
    """Replace the ``openai`` and ``ollama`` client classes of the installed SDKs; a package that is absent needs no guard."""
    targets = (
        ("openai", ("OpenAI", "AsyncOpenAI"), "OpenAI", _openai_target),
        ("ollama", ("Client", "AsyncClient"), "Ollama", _ollama_target),
    )
    for module_name, class_names, label, target in targets:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for class_name in class_names:
            guarded = _refusing_client_class(getattr(module, class_name), label, target)
            stack.enter_context(patch.object(module, class_name, guarded))


def _refusing_sync(real: Callable[..., Any]) -> Callable[..., Any]:
    def handle_request(self: Any, request: Any) -> Any:
        _refuse_request(request)
        return real(self, request)

    return handle_request


def _refusing_async(real: Callable[..., Any]) -> Callable[..., Any]:
    async def handle_async_request(self: Any, request: Any) -> Any:
        _refuse_request(request)
        return await real(self, request)

    return handle_async_request


def _patch_httpx_transports(stack: contextlib.ExitStack) -> None:
    """Refuse at the real network transports of every installed httpx flavour (``_HTTPX_FLAVOURS``)."""
    guarded: set[int] = set()
    for flavour in _HTTPX_FLAVOURS:
        try:
            module = importlib.import_module(flavour)
        except ImportError:
            continue
        for transport, method, refusing in (
            (module.HTTPTransport, "handle_request", _refusing_sync),
            (module.AsyncHTTPTransport, "handle_async_request", _refusing_async),
        ):
            if id(transport) not in guarded:  # ``httpx2.alias_httpx()`` makes the two flavours one set of classes
                guarded.add(id(transport))
                stack.enter_context(patch.object(transport, method, refusing(getattr(transport, method))))


def _patch_spawns(stack: contextlib.ExitStack) -> None:
    """Guard ``subprocess.Popen``, the one place ``subprocess.run`` and ``asyncio.create_subprocess_exec`` both end up."""
    real_init = subprocess.Popen.__init__

    def init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        if _enforcing():
            try:
                bound = _POPEN_PARAMETERS.bind(self, *args, **kwargs).arguments
            except TypeError:
                bound = {}  # a malformed call: ``Popen`` raises its own ``TypeError`` below
            reason = _spawn_refusal(bound)
            if reason is not None:
                raise RuntimeError(
                    f"Test suite attempted to spawn {reason}. Pass the fake of tests/_fake_claude.py by its explicit path instead."
                )
        real_init(self, *args, **kwargs)

    stack.enter_context(patch.object(subprocess.Popen, "__init__", init))


def install_guards(stack: contextlib.ExitStack) -> None:
    """Patch every door a model provider is reached through; the patches end when ``stack`` closes."""
    _patch_anthropic(stack)
    _patch_sdk_clients(stack)
    _patch_httpx_transports(stack)
    _patch_spawns(stack)
