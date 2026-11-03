"""BE-8 (collection-descriptions plan, C1, S12, S79): ``claude_cli`` end to end against a real subprocess.

* The wrapper spawns the fake ``claude`` of ``tests/_fake_claude.py`` by explicit path (never the real ``claude``) and a
  prompt far larger than any argv limit arrives whole on standard input, under the locked-down argv and environment.
* The dev-container path: ``scripts/claude-container-wrapper`` stands in for ``claude`` and talks to a real
  ``scripts/claude-proxy.py`` server on loopback. The wrapper drops standard input unless it forwards it, and it needs
  ``CLAUDE_PROXY_URL`` and ``CLAUDE_PROXY_TOKEN``, which the child environment allowlist must therefore let through.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from archon_search.llm_client.claude_cli_client import ClaudeCliClient
from archon_search.llm_client.protocol import LlmCallRequest
from tests import _fake_claude
from tests._fake_model_server import keep_loopback_out_of_proxies
from tests._threaded_http import serving_in_thread

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is an sh launcher"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
PROXY_PATH = REPO_ROOT / "scripts" / "claude-proxy.py"
WRAPPER_PATH = REPO_ROOT / "scripts" / "claude-container-wrapper"

_MIB = 1024 * 1024
_LARGE_PROMPT_BYTES = 8 * _MIB  # an argv element is capped near 128 KiB (Linux) and all of argv near 1 MiB (macOS)
_PROXIED_PROMPT_BYTES = 3 * _MIB  # beyond the proxy's former 1 MB body cap
_ARGV_ELEMENT_LIMIT = 200  # no lockdown flag or model name is longer; a prompt in argv would be
_TIMEOUT_SECONDS = 120.0
_MAX_OUTPUT_TOKENS = 1024
_PROXY_TOKEN = "integration-proxy-token"
_REPLY = "reply from the fake claude"
_BEARER = "integration-bearer-token"
_API_KEY = "sk-ant-integration-secret"


def _request(prompt: str) -> LlmCallRequest:
    return LlmCallRequest(prompt=prompt, max_output_tokens=_MAX_OUTPUT_TOKENS, timeout_seconds=_TIMEOUT_SECONDS)


def _large_prompt(size_bytes: int) -> str:
    unit = "excerpt \u00e9\u4e2d\U0001f600 line\n"  # multi-byte characters: the size is in bytes, not characters
    return unit * (size_bytes // len(unit.encode("utf-8")))


async def test_fake_claude_gets_large_prompt_on_stdin_and_records_argv_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", _API_KEY)
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", _BEARER)
    monkeypatch.setenv("CLAUDECODE", "1")
    fake = _fake_claude.install(tmp_path / "bin", reply=_REPLY)
    prompt = _large_prompt(_LARGE_PROMPT_BYTES)

    result = await ClaudeCliClient(model="", binary=str(fake.path)).call(_request(prompt))

    assert result.text == _REPLY
    (invocation,) = fake.invocations()
    assert invocation.stdin == prompt
    assert len(prompt.encode("utf-8")) > _LARGE_PROMPT_BYTES // 2
    assert all(len(element) < _ARGV_ELEMENT_LIMIT for element in invocation.argv)
    assert {"-p", "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config", "--safe-mode"} <= set(
        invocation.argv
    )
    assert invocation.argv[invocation.argv.index("--max-turns") + 1] == "1"
    assert invocation.argv[invocation.argv.index("--tools") + 1] == ""
    assert {"ANTHROPIC_API_KEY", "ARCHON_SEARCH_API_KEY", "CLAUDECODE"}.isdisjoint(invocation.env)
    assert Path(invocation.cwd) != Path.cwd()


@pytest.fixture(scope="module")
def proxy_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("claude_proxy_for_wrapper_test", PROXY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def proxy_url(
    proxy_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, _fake_claude.FakeClaude]]:
    """A real claude-proxy on loopback whose ``claude`` is the fake binary; yields its URL and the fake."""
    fake = _fake_claude.install(tmp_path / "host-bin", reply=_REPLY)
    monkeypatch.setattr(proxy_module, "CLAUDE", str(fake.path))
    httpd = proxy_module.make_server("127.0.0.1", 0, _PROXY_TOKEN)
    with serving_in_thread(httpd):
        yield f"http://127.0.0.1:{httpd.server_address[1]}", fake


async def test_container_wrapper_forwards_stdin_and_proxy_variables_through_the_allowlist(
    proxy_url: tuple[str, _fake_claude.FakeClaude], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, host_fake = proxy_url
    keep_loopback_out_of_proxies(monkeypatch)
    monkeypatch.setenv("CLAUDE_PROXY_URL", url)
    monkeypatch.setenv("CLAUDE_PROXY_TOKEN", _PROXY_TOKEN)
    prompt = _large_prompt(_PROXIED_PROMPT_BYTES)

    result = await ClaudeCliClient(model="haiku", binary=str(WRAPPER_PATH)).call(_request(prompt))

    assert result.text == _REPLY
    (invocation,) = host_fake.invocations()
    assert invocation.stdin == prompt  # the wrapper forwarded standard input to the host-side claude
    assert invocation.argv[invocation.argv.index("--model") + 1] == "haiku"
    assert "--safe-mode" in invocation.argv
    assert all(len(element) < _ARGV_ELEMENT_LIMIT for element in invocation.argv)
