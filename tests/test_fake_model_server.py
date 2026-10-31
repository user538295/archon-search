"""The loopback fake model server (collection-descriptions plan, BE-5; reused by BE-6 and the e2e tasks).

It speaks the Ollama endpoints the ``ollama`` SDK calls (``/api/chat``, ``/api/show``) and the llama.cpp endpoints
a ``httpx`` wrapper calls (``GET /props``, ``POST /tokenize``, ``POST /v1/chat/completions``), and records every
request so a test can assert what the wrapper sent.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from urllib.parse import urlsplit

import httpx
import pytest

from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies

_HELD_CHAT_PROBE_SECONDS = 0.3
_POLL_SECONDS = 0.01
_CHATS_BEFORE_THIRD_ARRIVES = 3  # the timed-out probe, the answered request, and the one held here
_SHORT_HOLD_LIMIT_SECONDS = 0.05
_UNAVAILABLE = 503
_SECOND_REPLY = "a changed answer"
_HELD_CHAT_TIMEOUT_SECONDS = 10.0

_PROMPT = "describe this collection please"
_PROMPT_WORDS = 4
_REPLY = "a one line description"
_OLLAMA_CONTEXT_LENGTH = 131072
_LLAMA_N_CTX = 16384


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


@pytest.fixture
def server():
    with FakeModelServer(reply_text=_REPLY, ollama_context_length=_OLLAMA_CONTEXT_LENGTH, llama_n_ctx=_LLAMA_N_CTX) as running:
        yield running


def test_server_listens_on_loopback_only(server: FakeModelServer) -> None:
    host = urlsplit(server.base_url).hostname
    assert host is not None
    assert ipaddress.ip_address(host).is_loopback


def test_ollama_chat_answers_with_the_reply_and_records_the_body(server: FakeModelServer) -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": _PROMPT}], "stream": False}

    response = httpx.post(f"{server.base_url}/api/chat", json=body)

    assert response.status_code == 200
    assert response.json()["message"] == {"role": "assistant", "content": _REPLY}
    assert response.json()["done"] is True
    [recorded] = server.requests_to("/api/chat")
    assert recorded.method == "POST"
    assert recorded.body == body


def test_ollama_show_reports_the_model_maximum_under_its_architecture_key(server: FakeModelServer) -> None:
    response = httpx.post(f"{server.base_url}/api/show", json={"model": "m"})

    model_info = response.json()["model_info"]
    assert model_info["general.architecture"] == "llama"
    assert model_info["llama.context_length"] == _OLLAMA_CONTEXT_LENGTH


def test_llama_props_reports_the_window(server: FakeModelServer) -> None:
    response = httpx.get(f"{server.base_url}/props")

    assert response.json()["default_generation_settings"]["n_ctx"] == _LLAMA_N_CTX
    [recorded] = server.requests_to("/props")
    assert recorded.method == "GET"
    assert recorded.body is None


def test_llama_tokenize_counts_one_token_per_word(server: FakeModelServer) -> None:
    response = httpx.post(f"{server.base_url}/tokenize", json={"content": _PROMPT})

    assert len(response.json()["tokens"]) == _PROMPT_WORDS
    assert server.requests_to("/tokenize")[0].body == {"content": _PROMPT}


def test_llama_chat_completions_answer_in_the_openai_shape(server: FakeModelServer) -> None:
    body = {"messages": [{"role": "user", "content": _PROMPT}], "max_tokens": 64}

    response = httpx.post(f"{server.base_url}/v1/chat/completions", json=body)

    assert response.json()["choices"][0]["message"] == {"role": "assistant", "content": _REPLY}
    assert server.requests_to("/v1/chat/completions")[0].body == body


def test_requests_are_recorded_in_arrival_order(server: FakeModelServer) -> None:
    httpx.get(f"{server.base_url}/props")
    httpx.post(f"{server.base_url}/tokenize", json={"content": _PROMPT})

    assert [recorded.path for recorded in server.requests] == ["/props", "/tokenize"]


def test_status_override_fails_one_endpoint_and_leaves_the_others() -> None:
    with FakeModelServer(status_overrides={"/props": 503}) as failing:
        down = httpx.get(f"{failing.base_url}/props")
        up = httpx.post(f"{failing.base_url}/tokenize", json={"content": _PROMPT})

    assert down.status_code == 503
    assert up.status_code == 200


def test_llama_cpp_errors_are_nested_like_llama_server_nests_them() -> None:
    with FakeModelServer(status_overrides={"/props": 503}) as failing:
        error = httpx.get(f"{failing.base_url}/props").json()["error"]

    assert error["code"] == 503
    assert error["type"] == "unavailable_error"
    assert isinstance(error["message"], str)


def test_ollama_errors_are_a_plain_string_like_ollama_sends_them() -> None:
    with FakeModelServer(status_overrides={"/api/show": 404}) as failing:
        body = httpx.post(f"{failing.base_url}/api/show", json={"model": "m"}).json()

    assert isinstance(body["error"], str)


def test_tokenize_without_content_is_400(server: FakeModelServer) -> None:
    response = httpx.post(f"{server.base_url}/tokenize", json={"add_special": False})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_malformed_json_body_is_400(server: FakeModelServer) -> None:
    response = httpx.post(f"{server.base_url}/api/chat", content=b"{not json", headers={"Content-Type": "application/json"})

    assert response.status_code == 400


def test_query_string_does_not_change_the_route(server: FakeModelServer) -> None:
    assert httpx.get(f"{server.base_url}/props?model=m").status_code == 200
    assert server.requests[0].path == "/props"


def test_unknown_path_is_404(server: FakeModelServer) -> None:
    assert httpx.get(f"{server.base_url}/nowhere").status_code == 404


def test_two_requests_share_one_keep_alive_connection(server: FakeModelServer) -> None:
    with httpx.Client(base_url=server.base_url) as client:
        first = client.get("/props")
        second = client.post("/tokenize", json={"content": _PROMPT})

    assert first.http_version == second.http_version == "HTTP/1.1"
    assert [recorded.path for recorded in server.requests] == ["/props", "/tokenize"]


def _wait_for_chats(server: FakeModelServer, count: int) -> None:
    deadline = time.monotonic() + _HELD_CHAT_TIMEOUT_SECONDS
    while len(server.requests_to("/api/chat")) < count:
        assert time.monotonic() < deadline, f"chat request number {count} never arrived"
        time.sleep(_POLL_SECONDS)


def test_held_chat_waits_for_release_and_other_endpoints_still_answer() -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": _PROMPT}], "stream": False}
    with (
        FakeModelServer(reply_text=_REPLY, hold_chat=True) as held,
        httpx.Client(base_url=held.base_url, timeout=_HELD_CHAT_TIMEOUT_SECONDS) as client,
    ):
        assert client.post("/api/show", json={"model": "m"}).status_code == 200
        with pytest.raises(httpx.ReadTimeout):
            client.post("/api/chat", json=body, timeout=_HELD_CHAT_PROBE_SECONDS)
        _wait_for_chats(held, 1)  # recorded on arrival, answered only after release
        held.release()
        assert client.post("/api/chat", json=body).json()["message"]["content"] == _REPLY
        held.hold()
        answers: list[str] = []
        waiter = threading.Thread(
            target=lambda: answers.append(client.post("/api/chat", json=body).json()["message"]["content"])
        )
        waiter.start()
        _wait_for_chats(held, _CHATS_BEFORE_THIRD_ARRIVES)
        held.reply_text = _SECOND_REPLY
        held.release()
        waiter.join(timeout=_HELD_CHAT_TIMEOUT_SECONDS)
        assert answers == [_SECOND_REPLY]  # a held request is answered with the text current at its release


def test_a_chat_held_past_the_limit_answers_503_in_the_shape_of_its_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tests._fake_model_server._HELD_CHAT_MAX_SECONDS", _SHORT_HOLD_LIMIT_SECONDS)
    ollama_body = {"model": "m", "messages": [{"role": "user", "content": _PROMPT}], "stream": False}
    with FakeModelServer(hold_chat=True) as held:
        ollama = httpx.post(f"{held.base_url}/api/chat", json=ollama_body, timeout=_HELD_CHAT_TIMEOUT_SECONDS)
        llama = httpx.post(f"{held.base_url}/v1/chat/completions", json=ollama_body, timeout=_HELD_CHAT_TIMEOUT_SECONDS)

    assert ollama.status_code == llama.status_code == _UNAVAILABLE
    assert isinstance(ollama.json()["error"], str)
    assert llama.json()["error"]["code"] == _UNAVAILABLE


def test_the_llama_chat_endpoint_is_held_too() -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": _PROMPT}]}
    with FakeModelServer(reply_text=_REPLY, hold_chat=True) as held:
        with pytest.raises(httpx.ReadTimeout):
            httpx.post(f"{held.base_url}/v1/chat/completions", json=body, timeout=_HELD_CHAT_PROBE_SECONDS)
        held.release()
        answered = httpx.post(f"{held.base_url}/v1/chat/completions", json=body, timeout=_HELD_CHAT_TIMEOUT_SECONDS)

    assert answered.json()["choices"][0]["message"]["content"] == _REPLY
