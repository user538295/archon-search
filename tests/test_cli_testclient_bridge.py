"""The CLI-to-TestClient bridge (collection-descriptions plan, T-5): a CLI command reaches an in-process app, no socket."""

from __future__ import annotations

import socket
import sys
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, Header
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

import archon_search.cli as cli_package
import archon_search.cli._helpers as helpers_module
import archon_search.cli.collection as collection_module
from archon_search.cli.collection import collection
from tests._cli_testclient_bridge import cli_against_app

_KEY_HEX_LENGTH = 64
_TOKEN = "a" * _KEY_HEX_LENGTH  # the key loader accepts a lowercase hex string
_OTHER_TOKEN = "b" * _KEY_HEX_LENGTH
_NAME = "bridged"
_OLD_PATH = "/moved"
_REDIRECT = 307


def _app(seen_authorization: list[str | None]) -> FastAPI:
    app = FastAPI()

    @app.get("/collections/{name}")
    def detail(name: str, authorization: str | None = Header(default=None)) -> dict[str, object]:
        seen_authorization.append(authorization)
        return {"name": name, "doc_count": 3, "chunk_count": 9, "description": "from the in-process app"}

    @app.delete("/collections/{name}")
    def remove(name: str) -> dict[str, str]:
        return {"removed": name}

    @app.get(_OLD_PATH)
    def moved() -> RedirectResponse:
        return RedirectResponse(f"/collections/{_NAME}", status_code=_REDIRECT)

    return app


def test_cli_command_reaches_the_app_with_the_token_and_opens_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the bridge must not open a connection")

    with (
        TestClient(_app(seen)) as client,
        cli_against_app(client, _TOKEN, monkeypatch) as cli,
        monkeypatch.context() as no_sockets,  # entered after the app is up: its event loop may connect a socketpair
    ):
        no_sockets.setattr(socket.socket, "connect", forbidden)
        result = cli.invoke(collection, ["info", _NAME])

    assert result.exit_code == 0, result.output
    assert f"name: {_NAME}" in result.output
    assert "description: from the in-process app" in result.output
    assert seen == [f"Bearer {_TOKEN}"]


def test_a_call_can_use_another_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []
    with TestClient(_app(seen)) as client, cli_against_app(client, _TOKEN, monkeypatch) as cli:
        cli.invoke(collection, ["info", _NAME], token=_OTHER_TOKEN)
        cli.invoke(collection, ["info", _NAME])

    assert seen == [f"Bearer {_OTHER_TOKEN}", f"Bearer {_TOKEN}"]


def test_every_cli_module_that_imports_httpx_is_bridged_inside_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(FastAPI()) as client, cli_against_app(client, _TOKEN, monkeypatch):
        holders = [
            module
            for name, module in sys.modules.items()
            if name.startswith(f"{cli_package.__name__}.") and hasattr(module, "httpx")
        ]
        assert holders
        assert all(module.httpx is not httpx for module in holders)


def test_a_module_whose_httpx_was_replaced_by_another_test_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collection_module, "httpx", MagicMock())
    with (
        TestClient(FastAPI()) as client,
        pytest.raises(RuntimeError, match="already replaced"),
        cli_against_app(client, _TOKEN, monkeypatch),
    ):
        pytest.fail("the bridge did not refuse the replaced module")


def test_a_delete_reaches_the_app_too(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(_app([])) as client, cli_against_app(client, _TOKEN, monkeypatch) as cli:
        result = cli.invoke(collection, ["remove", _NAME])

    assert result.exit_code == 0, result.output
    assert f"Removed collection '{_NAME}'" in result.output


def test_unknown_route_is_the_commands_own_not_found_path(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(FastAPI()) as client, cli_against_app(client, _TOKEN, monkeypatch) as cli:
        result = cli.invoke(collection, ["info", "ghost"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_redirects_are_not_followed_like_the_real_httpx_get(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(_app([])) as client, cli_against_app(client, _TOKEN, monkeypatch):
        response = collection_module.httpx.get(f"http://localhost{_OLD_PATH}")

    assert response.status_code == _REDIRECT


def test_unbridged_network_entry_points_raise_instead_of_opening_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(FastAPI()) as client, cli_against_app(client, _TOKEN, monkeypatch):
        with pytest.raises(NotImplementedError, match="Client"):
            collection_module.httpx.Client()
        with pytest.raises(NotImplementedError, match="stream"):
            collection_module.httpx.stream("GET", "http://localhost/x")


def test_nesting_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        TestClient(FastAPI()) as client,
        cli_against_app(client, _TOKEN, monkeypatch),
        pytest.raises(RuntimeError, match="nest"),
        cli_against_app(client, _TOKEN, monkeypatch),
    ):
        pytest.fail("the nested bridge was not refused")


def test_modules_get_the_real_httpx_back_after_the_block_even_when_it_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(FastAPI()) as client, pytest.raises(ZeroDivisionError), cli_against_app(client, _TOKEN, monkeypatch):
        assert collection_module.httpx is not httpx
        assert helpers_module.httpx is not httpx
        assert collection_module.httpx.HTTPError is httpx.HTTPError
        raise ZeroDivisionError

    assert collection_module.httpx is httpx
    assert helpers_module.httpx is httpx
