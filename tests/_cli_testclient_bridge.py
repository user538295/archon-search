"""Run the Click CLI against an in-process app (collection-descriptions plan, T-5; reused by FE-10, FE-11, T-9, T-10).

The CLI commands call the module-level ``httpx.get`` / ``post`` / ``delete`` ... of their own module, aimed at a real
socket (``--api-url``). The bridge swaps the ``httpx`` name inside every ``archon_search.cli`` module for a shim that
sends the same calls through a Starlette ``TestClient`` (an ``httpx.Client`` over an ASGI transport), so the command under
test reads and writes the same app a REST or MCP call in the test reaches, with no port bound. The exception classes and
constants on the shim are the real ``httpx``'s, so the commands' error handling is unchanged.

    with cli_against_app(client, api_key, monkeypatch) as cli:
        result = cli.invoke(collection, ["info", "my-collection"])
        assert result.exit_code == 0, result.output

What the bridge covers and what it does not:

* The token is handed over as ``ARCHON_SEARCH_API_KEY`` (what every command falls back to without ``--api-key``). The
  host of a command's URL is ignored by the transport; only the path reaches the app.
* Redirects are not followed, like the real ``httpx.get`` (``TestClient`` would follow them): a command that calls a path
  without its trailing slash sees the 307 here too.
* Connect failures cannot happen in-process, and the ``timeout=`` a command passes is dropped (the ASGI call cannot time
  out); use ``tests/_cli_server_stub.py`` for the connect-failure messages.
* A command that builds its own ``httpx.Client`` or streams (``key_cmd``) is not bridged. Reaching ``Client``, ``stream``
  or another network entry point raises ``NotImplementedError`` instead of silently opening a real socket.
* The bridge does not nest: a second ``cli_against_app`` inside the first raises, and so does a CLI module whose ``httpx``
  another test already replaced (the bridge would otherwise skip it silently).
* Keyword arguments only the real ``httpx.get`` accepts (``verify=``, ``proxy=``) are a ``TypeError`` here.
* Starlette's ``TestClient`` is built on ``httpx2`` in this repo: a response is an ``httpx2.Response`` and
  ``isinstance(response, httpx.Response)`` is false. No CLI command checks that.
* ``--wait`` polling sleeps ``_POLL_INTERVAL_SECONDS`` (2 s; ``backup_cmd``, ``export_cmd`` and ``maintenance_cmd`` keep
  their own copy): monkeypatch the constant of the module under test to keep a test fast.
* ``TestClient`` re-raises a server exception inside the command; boot the app with ``raise_server_exceptions=False`` to
  see the command's own "server returned 500" path. ``CliRunner`` swaps ``sys.stdout``/``sys.stderr`` process-wide, so
  text printed by app threads can land in ``result.output``: prefer ``in`` over ``not in`` assertions.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from types import ModuleType
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner, Result
from fastapi.testclient import TestClient

import archon_search.cli as cli_package

_API_KEY_ENV = "ARCHON_SEARCH_API_KEY"
_CLI_MODULE_PREFIX = f"{cli_package.__name__}."
_ROUTED_METHODS = ("get", "post", "put", "patch", "delete", "request")
_UNBRIDGED_ENTRY_POINTS = frozenset({"Client", "AsyncClient", "stream", "head", "options"})


def _without_redirects(send: Callable[..., Any]) -> Callable[..., Any]:
    def routed(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("follow_redirects", False)
        kwargs.pop("timeout", None)  # nothing can time out in-process, and Starlette's TestClient warns about the argument
        return send(*args, **kwargs)

    return routed


class _RoutedHttpx:
    """The ``httpx`` module as the CLI sees it, with the request helpers sent to a ``TestClient``."""

    def __init__(self, client: TestClient) -> None:
        for method in _ROUTED_METHODS:
            setattr(self, method, _without_redirects(getattr(client, method)))

    def __getattr__(self, name: str) -> Any:
        if name in _UNBRIDGED_ENTRY_POINTS:
            raise NotImplementedError(f"httpx.{name} is not bridged to the TestClient; the command would open a real socket")
        return getattr(httpx, name)


class CliBridge:
    """Invokes CLI commands whose HTTP calls reach the bridged app."""

    def __init__(self, token: str) -> None:
        self._runner = CliRunner()
        self._token = token

    def invoke(
        self, command: click.Command, args: Sequence[str], *, token: str | None = None, input: str | None = None
    ) -> Result:
        """Run ``command`` with ``args``; the result carries ``exit_code``, ``output`` and ``exception`` like ``CliRunner``.

        ``token`` overrides the bridge's key for this call (a test with two namespaces); ``input`` feeds stdin (prompts).
        """
        env = {_API_KEY_ENV: self._token if token is None else token}
        return self._runner.invoke(command, list(args), env=env, input=input)


def _cli_modules_using_httpx() -> list[ModuleType]:
    for module_info in pkgutil.walk_packages(cli_package.__path__, _CLI_MODULE_PREFIX):
        importlib.import_module(module_info.name)
    modules = [
        module
        for name, module in list(sys.modules.items())
        if (name == cli_package.__name__ or name.startswith(_CLI_MODULE_PREFIX)) and hasattr(module, "httpx")
    ]
    if any(isinstance(module.httpx, _RoutedHttpx) for module in modules):
        raise RuntimeError("cli_against_app does not nest: the CLI modules are already bridged")
    replaced = [module for module in modules if module.httpx is not httpx]
    if replaced:
        names = ", ".join(module.__name__ for module in replaced)
        raise RuntimeError(f"httpx is already replaced in {names}; the bridge would skip them")
    return modules


@contextmanager
def cli_against_app(client: TestClient, token: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[CliBridge]:
    """Aim every CLI module's ``httpx`` at ``client`` for the block; the patches are undone when the block ends."""
    routed = _RoutedHttpx(client)
    with monkeypatch.context() as patched:
        for module in _cli_modules_using_httpx():
            patched.setattr(module, "httpx", routed)
        yield CliBridge(token)
