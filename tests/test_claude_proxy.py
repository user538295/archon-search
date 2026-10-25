"""scripts/claude-proxy.py: a dev-only server that runs ``claude`` with caller-supplied args.

It must refuse everything that is not the container wrapper: no/incorrect shared secret, a Host
header that is not this machine (DNS rebinding), or an Origin header (a browser, i.e. CSRF).
The tests drive a real ``HTTPServer`` on an ephemeral loopback port; ``claude`` itself is replaced
by ``/bin/echo`` so an accepted request is observable without running the real CLI.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_PATH = REPO_ROOT / "scripts" / "claude-proxy.py"
WRAPPER_PATH = REPO_ROOT / "scripts" / "claude-container-wrapper"
TOKEN = "s3cret-token-for-tests"
ECHO = "/bin/echo"


@pytest.fixture(scope="module")
def proxy_module():
    spec = importlib.util.spec_from_file_location("claude_proxy_under_test", PROXY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(proxy_module, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, int]]:
    monkeypatch.setattr(proxy_module, "CLAUDE", ECHO)
    httpd = proxy_module.make_server("127.0.0.1", 0, TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def _post(address, body: bytes, headers: dict[str, str], host: str | None = None):
    conn = http.client.HTTPConnection(*address, timeout=10)
    conn.putrequest("POST", "/", skip_host=True)
    conn.putheader("Host", host if host is not None else f"127.0.0.1:{address[1]}")
    for name, value in headers.items():
        conn.putheader(name, value)
    conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response, payload


GOOD_BODY = json.dumps({"args": ["hello", "world"]}).encode()
AUTH = {"X-Claude-Proxy-Token": TOKEN}


def test_authenticated_request_runs_claude(server) -> None:
    response, payload = _post(server, GOOD_BODY, AUTH)
    assert response.status == 200
    assert payload == b"hello world\n"
    assert response.getheader("X-Exit-Code") == "0"


@pytest.mark.parametrize("host_name", ["localhost", "host.docker.internal", "127.0.0.1"])
def test_expected_host_names_are_served(server, host_name: str) -> None:
    response, _ = _post(server, GOOD_BODY, AUTH, host=f"{host_name}:{server[1]}")
    assert response.status == 200


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Claude-Proxy-Token": "wrong"}, {"X-Claude-Proxy-Token": ""}],
    ids=["missing", "wrong", "empty"],
)
def test_missing_or_wrong_token_is_unauthorized(server, headers: dict[str, str]) -> None:
    response, payload = _post(server, GOOD_BODY, headers)
    assert response.status == 401
    assert payload == b""


@pytest.mark.parametrize("host", ["evil.example.com:80", "rebind.attacker.test", ""])
def test_foreign_host_header_is_forbidden_even_with_the_token(server, host: str) -> None:
    """DNS rebinding: the browser sends the attacker's hostname as Host."""
    response, _ = _post(server, GOOD_BODY, AUTH, host=host)
    assert response.status == 403


def test_origin_header_is_forbidden_even_with_the_token(server) -> None:
    """Browsers always attach Origin to cross-origin POSTs; the container wrapper never does."""
    response, _ = _post(server, GOOD_BODY, {**AUTH, "Origin": "http://evil.example.com"})
    assert response.status == 403


@pytest.mark.parametrize(
    "body",
    [b"not json", json.dumps({"args": "rm -rf /"}).encode(), json.dumps({"args": [1, 2]}).encode(), b"[]"],
    ids=["not-json", "args-not-list", "args-not-str", "not-object"],
)
def test_malformed_body_is_a_bad_request(server, body: bytes) -> None:
    response, _ = _post(server, body, AUTH)
    assert response.status == 400


def test_refused_request_never_runs_claude(proxy_module, monkeypatch: pytest.MonkeyPatch, server) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(proxy_module.subprocess, "run", lambda *a, **k: ran.append(a[0]))
    _post(server, GOOD_BODY, {})
    _post(server, GOOD_BODY, AUTH, host="evil.example.com")
    assert ran == []


def test_server_refuses_an_empty_token(proxy_module) -> None:
    with pytest.raises(ValueError, match="CLAUDE_PROXY_TOKEN"):
        proxy_module.make_server("127.0.0.1", 0, "")


def test_wrapper_sends_the_shared_secret_header() -> None:
    text = WRAPPER_PATH.read_text()
    assert "CLAUDE_PROXY_TOKEN" in text and "X-Claude-Proxy-Token" in text
