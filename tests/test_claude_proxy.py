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
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import _fake_claude

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


CAT = "/bin/cat"
WRAPPER_TIMEOUT_S = 30
OVERSIZED_FOR_THE_OLD_CAP = "x" * 2_000_000  # the proxy's former body cap was 1 MB


def test_stdin_reaches_claude(proxy_module, monkeypatch: pytest.MonkeyPatch, server) -> None:
    monkeypatch.setattr(proxy_module, "CLAUDE", CAT)
    body = json.dumps({"args": [], "stdin": "prompt text é"}).encode()
    response, payload = _post(server, body, AUTH)
    assert response.status == 200
    assert payload.decode() == "prompt text é"


def test_large_stdin_is_accepted(proxy_module, monkeypatch: pytest.MonkeyPatch, server) -> None:
    monkeypatch.setattr(proxy_module, "CLAUDE", CAT)
    body = json.dumps({"args": [], "stdin": OVERSIZED_FOR_THE_OLD_CAP}).encode()
    response, payload = _post(server, body, AUTH)
    assert response.status == 200
    assert payload.decode() == OVERSIZED_FOR_THE_OLD_CAP


def test_missing_stdin_gives_claude_an_empty_input(proxy_module, monkeypatch: pytest.MonkeyPatch, server) -> None:
    monkeypatch.setattr(proxy_module, "CLAUDE", CAT)
    response, payload = _post(server, json.dumps({"args": []}).encode(), AUTH)
    assert response.status == 200
    assert payload == b""


def test_non_string_stdin_is_a_bad_request(server) -> None:
    response, _ = _post(server, json.dumps({"args": [], "stdin": 5}).encode(), AUTH)
    assert response.status == 400


def _run_wrapper(server, args: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "CLAUDE_PROXY_URL": f"http://127.0.0.1:{server[1]}",
        "CLAUDE_PROXY_TOKEN": TOKEN,
        "NO_PROXY": "127.0.0.1",
    }
    return subprocess.run(
        [sys.executable, str(WRAPPER_PATH), *args],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=WRAPPER_TIMEOUT_S,
        check=False,
    )


def test_wrapper_forwards_the_stdin_of_a_print_call_to_the_host_claude(
    proxy_module, monkeypatch: pytest.MonkeyPatch, server, tmp_path: Path
) -> None:
    fake = _fake_claude.install(tmp_path, reply="host answer")
    monkeypatch.setattr(proxy_module, "CLAUDE", str(fake.path))
    done = _run_wrapper(server, ["-p", "--model", "haiku"], "from the wrapper's stdin é")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "host answer"
    (invocation,) = fake.invocations()
    assert invocation.argv == ["-p", "--model", "haiku"]
    assert invocation.stdin == "from the wrapper's stdin é"


def test_wrapper_does_not_wait_on_the_stdin_of_a_call_that_is_not_print_mode(
    proxy_module, monkeypatch: pytest.MonkeyPatch, server, tmp_path: Path
) -> None:
    fake = _fake_claude.install(tmp_path)
    monkeypatch.setattr(proxy_module, "CLAUDE", str(fake.path))
    done = _run_wrapper(server, ["--version"], "not a prompt")
    assert done.returncode == 0, done.stderr
    (invocation,) = fake.invocations()
    assert invocation.argv == ["--version"]
    assert invocation.stdin == ""


def test_wrapper_still_forwards_argv_when_stdin_is_empty(proxy_module, monkeypatch: pytest.MonkeyPatch, server) -> None:
    monkeypatch.setattr(proxy_module, "CLAUDE", ECHO)
    done = _run_wrapper(server, ["-p", "hi"], "")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "-p hi\n"
