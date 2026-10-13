"""The live llama.cpp e2e suite must skip, not fail, when :8080 is not a llama-server.

Any local web app on the port answers ``GET /v1/models`` with HTTP 200 (a single-page UI serves its
HTML for every GET path) or 405; counting that as "reachable" ran the live tests against it and
failed them on the first POST. Only an OpenAI-style model list (``{"object": "list", "data": [...]}``,
llama-server's documented response) counts as a llama-server.
"""
from __future__ import annotations

import httpx
import pytest

from tests.integration.test_llama_cpp_e2e import _is_llama_cpp_reachable


def _serve(monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception) -> None:
    def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(httpx, "get", fake_get)


def test_reachable_when_the_server_returns_an_openai_model_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, httpx.Response(200, json={"object": "list", "data": [{"id": "m"}]}))

    assert _is_llama_cpp_reachable() is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<!doctype html><html></html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"models": []}),
        httpx.Response(405, text="Method Not Allowed"),
        httpx.Response(401, json={"error": "unauthorized"}),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
    ids=["html-200", "json-list", "no-data-key", "405", "401", "connect-error", "timeout"],
)
def test_not_reachable_for_anything_else(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception
) -> None:
    _serve(monkeypatch, response)

    assert _is_llama_cpp_reachable() is False
