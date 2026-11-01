"""BE-8 (collection-descriptions plan, C1, S12, S79): the ``claude_cli`` wrapper of the shared LLM client.

Every test runs the wrapper against a REAL subprocess, the fake ``claude`` of ``tests/_fake_claude.py`` selected by
explicit path (never the real ``claude`` on ``PATH``), which records the argv, environment, working directory and standard
input it received.
"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from archon_search.constants import DEFAULT_FAST_MODEL
from archon_search.llm_client import claude_cli_client
from archon_search.llm_client.claude_cli_client import (
    PROVIDER_NAME,
    ClaudeCliClient,
    child_environment,
)
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
    LlmClient,
    ThinkSetting,
)
from tests import _fake_claude

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is an sh launcher and kills are POSIX signals")
_PROMPT = "describe this collection in one line"
_SECRET_PROMPT = "excerpt-that-must-never-leak-into-a-reason"
_MAX_OUTPUT_TOKENS = 1024
_TIMEOUT_SECONDS = 20.0
_SHORT_TIMEOUT_SECONDS = 0.3
_HANG_SECONDS = 60.0
_MAX_REASON_CHARS = 300
_SPAWN_WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.01
_UNREAD_PROMPT_CHARS = 2 * 1024 * 1024
_FORBIDDEN_NAMES = frozenset({"CLAUDECODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ARCHON_SEARCH_API_KEY", "AWS_SECRET_ACCESS_KEY", "LCX"})


def _request(prompt: str = _PROMPT, *, timeout: float = _TIMEOUT_SECONDS, think: ThinkSetting = ThinkSetting.DEFAULT) -> LlmCallRequest:
    return LlmCallRequest(prompt=prompt, max_output_tokens=_MAX_OUTPUT_TOKENS, timeout_seconds=timeout, think=think)


def _client(fake: _fake_claude.FakeClaude, *, model: str = "", retry: bool = False) -> ClaudeCliClient:
    return ClaudeCliClient(model=model, binary=str(fake.path), retry_on_nonzero_exit=retry)


async def _wait_for_record(fake: _fake_claude.FakeClaude) -> _fake_claude.Invocation:
    """The first invocation the fake recorded: from then on the child is running."""
    deadline = time.monotonic() + _SPAWN_WAIT_SECONDS
    while time.monotonic() < deadline:
        if recorded := fake.invocations():
            return recorded[0]
        await asyncio.sleep(_POLL_SECONDS)
    raise AssertionError("the fake claude never started")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_cli_client, "RETRY_BACKOFF_SECONDS", 0.0)


@pytest.fixture
def fake(tmp_path: Path) -> _fake_claude.FakeClaude:
    return _fake_claude.install(tmp_path / "bin", reply="a one line description")


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.subprocess.Process]:
    """Every subprocess the wrapper starts, so a test can inspect its fate after the call."""
    processes: list[asyncio.subprocess.Process] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await real(*args, **kwargs)  # type: ignore[arg-type]
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return processes


# --- the contract surface -------------------------------------------------------------------------------------------


def test_client_satisfies_the_llm_client_protocol(fake: _fake_claude.FakeClaude) -> None:
    assert isinstance(_client(fake), LlmClient)


async def test_call_returns_the_binary_stdout(fake: _fake_claude.FakeClaude) -> None:
    result = await _client(fake).call(_request())

    assert result.text == "a one line description"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param("\x1b[1mbold\x1b[0m text", "bold text", id="sgr"),
        pytest.param("\x1b[38:2:1:2:3mcolour\x1b[0m", "colour", id="colon-parameters"),
        pytest.param("\x1b[>4;2mtext", "text", id="private-parameter-prefix"),
        pytest.param("\x1b]8;;http://example.test\x07link\x1b]8;;\x07", "link", id="osc-hyperlink"),
        pytest.param("a\x1bMb", "ab", id="two-character-escape"),
    ],
)
async def test_terminal_escape_sequences_are_stripped_from_the_reply(tmp_path: Path, reply: str, expected: str) -> None:
    fake = _fake_claude.install(tmp_path / "bin", reply=reply)

    result = await _client(fake).call(_request())

    assert result.text == expected


async def test_bare_control_characters_are_stripped_but_tabs_and_line_feeds_stay(tmp_path: Path) -> None:
    fake = _fake_claude.install(tmp_path / "bin", reply="clean\x00\x1btext\x07 with\ttab\nand a line")

    result = await _client(fake).call(_request())

    assert result.text == "cleantext with\ttab\nand a line"


async def test_reply_that_is_not_valid_utf8_is_decoded_with_replacement_characters(tmp_path: Path) -> None:
    binary = tmp_path / "claude-printing-binary"
    binary.write_text("#!/bin/sh\ncat >/dev/null\nprintf 'hello \\377\\376 world'\n", encoding="utf-8")
    binary.chmod(stat.S_IRWXU)

    result = await ClaudeCliClient(model="", binary=str(binary)).call(_request())

    assert result.text == "hello \ufffd\ufffd world"


async def test_window_and_budget_are_unbounded_like_a_cloud_provider(fake: _fake_claude.FakeClaude) -> None:
    client = _client(fake)

    assert client.context_window() == UNBOUNDED
    for think in ThinkSetting:
        assert await client.input_budget("template", think=think) == InputBudget(tokens=UNBOUNDED)


async def test_think_is_ignored(tmp_path: Path) -> None:
    argvs = []
    for think in ThinkSetting:
        fake = _fake_claude.install(tmp_path / think.value)
        await _client(fake).call(_request(think=think))
        argvs.append(fake.invocations()[0].argv)

    assert argvs[0] == argvs[1] == argvs[2]


async def test_calls_share_no_state(fake: _fake_claude.FakeClaude) -> None:
    client = _client(fake)

    await client.call(_request("first prompt"))
    await client.call(_request("second prompt"))

    assert [i.stdin for i in fake.invocations()] == ["first prompt", "second prompt"]


async def test_prompt_with_a_lone_surrogate_is_sent_without_crashing(fake: _fake_claude.FakeClaude) -> None:
    await _client(fake).call(_request("before \ud800 after"))

    assert fake.invocations()[0].stdin == "before ? after"


# --- S79: the lockdown ----------------------------------------------------------------------------------------------


async def test_argv_has_lockdown_flags_and_no_prompt(fake: _fake_claude.FakeClaude) -> None:
    await _client(fake).call(_request(_SECRET_PROMPT))

    invocation = fake.invocations()[0]
    argv = invocation.argv
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "text"
    assert argv[argv.index("--tools") + 1] == ""  # every tool disabled
    assert argv[argv.index("--max-turns") + 1] == "1"
    for flag in ("--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config", "--safe-mode"):
        assert flag in argv
    assert "--bare" not in argv  # --bare reads only an API key, never the login claude_cli exists for
    assert "--mcp-config" not in argv
    assert not any(_SECRET_PROMPT in element for element in argv)
    assert invocation.stdin == _SECRET_PROMPT


@pytest.mark.parametrize("model", ["", DEFAULT_FAST_MODEL])
async def test_model_flag_is_omitted_for_a_blank_or_the_default_model(fake: _fake_claude.FakeClaude, model: str) -> None:
    await _client(fake, model=model).call(_request())

    assert "--model" not in fake.invocations()[0].argv


async def test_model_flag_carries_an_explicit_model(fake: _fake_claude.FakeClaude) -> None:
    await _client(fake, model="haiku").call(_request())

    argv = fake.invocations()[0].argv
    assert argv[argv.index("--model") + 1] == "haiku"


async def test_working_directory_is_neutral_empty_and_removed_afterwards(fake: _fake_claude.FakeClaude) -> None:
    await _client(fake).call(_request())

    invocation = fake.invocations()[0]
    cwd = Path(invocation.cwd)
    assert cwd != Path.cwd()
    assert cwd.name.startswith("archon-claude-cli-")
    assert invocation.cwd_entries == []
    assert not cwd.exists()


def test_environment_holds_only_allowlisted_variables() -> None:
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "USER": "u",
        "LOGNAME": "u",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C",
        "LC_CTYPE": "UTF-8",
        "TMPDIR": "/tmp",
        "CLAUDE_CONFIG_DIR": "/cfg",
        "HTTPS_PROXY": "http://proxy:3128",
        "NO_PROXY": "localhost",
        "SSL_CERT_FILE": "/ca.pem",
        "CLAUDE_PROXY_URL": "http://host.docker.internal:18766",
        "CLAUDE_PROXY_TOKEN": "proxy-secret",
        "CLAUDECODE": "1",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "OPENAI_API_KEY": "sk-openai-secret",
        "ARCHON_SEARCH_API_KEY": "bearer-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "LCX": "not an LC_ variable",
    }

    env = child_environment(source)

    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "USER": "u",
        "LOGNAME": "u",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C",
        "LC_CTYPE": "UTF-8",
        "TMPDIR": "/tmp",
        "CLAUDE_CONFIG_DIR": "/cfg",
        "HTTPS_PROXY": "http://proxy:3128",
        "NO_PROXY": "localhost",
        "SSL_CERT_FILE": "/ca.pem",
        "CLAUDE_PROXY_URL": "http://host.docker.internal:18766",
        "CLAUDE_PROXY_TOKEN": "proxy-secret",
    }


def test_unset_allowlisted_variables_are_not_invented() -> None:
    assert child_environment({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


async def test_no_claudecode_api_key_or_bearer_token_reaches_the_child(
    fake: _fake_claude.FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _FORBIDDEN_NAMES:
        monkeypatch.setenv(name, f"{name}-value")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    before = dict(os.environ)

    await _client(fake).call(_request())

    env = fake.invocations()[0].env
    assert _FORBIDDEN_NAMES.isdisjoint(env)
    assert env["LC_ALL"] == "C"
    assert env["CLAUDE_CONFIG_DIR"] == "/cfg"
    assert env["PATH"] == os.environ["PATH"]
    assert dict(os.environ) == before  # no process-wide mutation


# --- S12: process lifetime ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["timeout", "cancellation"])
async def test_child_killed_on_timeout_and_on_cancellation(
    tmp_path: Path, spawned: list[asyncio.subprocess.Process], how: str
) -> None:
    fake = _fake_claude.install(tmp_path / "bin", hang_seconds=_HANG_SECONDS)
    client = _client(fake)

    if how == "timeout":
        started = time.monotonic()
        with pytest.raises(LlmCallTimeout):
            await client.call(_request(timeout=_SHORT_TIMEOUT_SECONDS))
        assert time.monotonic() - started < _HANG_SECONDS / 2
    else:
        task = asyncio.create_task(client.call(_request(timeout=_HANG_SECONDS)))
        running = await _wait_for_record(fake)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not Path(running.cwd).exists()  # the working directory goes only after the child is gone

    assert spawned[0].returncode == -signal.SIGKILL  # killed and reaped, not left to finish


async def test_timeout_is_not_retried_even_when_retry_is_on(
    tmp_path: Path, spawned: list[asyncio.subprocess.Process]
) -> None:
    fake = _fake_claude.install(tmp_path / "bin", hang_seconds=_HANG_SECONDS)

    with pytest.raises(LlmCallTimeout):
        await _client(fake, retry=True).call(_request(timeout=_SHORT_TIMEOUT_SECONDS))

    assert len(spawned) == 1


@pytest.mark.parametrize("leader_exits_first", [False, True])
async def test_kill_reaches_a_descendant_that_holds_the_output_pipes(tmp_path: Path, leader_exits_first: bool) -> None:
    fake = _fake_claude.install(
        tmp_path / "bin", hang_seconds=0.0 if leader_exits_first else _HANG_SECONDS, spawn_grandchild=True
    )
    task = asyncio.create_task(_client(fake).call(_request(timeout=_HANG_SECONDS)))
    started = await _wait_for_record(fake)
    grandchild_pid = started.grandchild_pid
    deadline = time.monotonic() + _SPAWN_WAIT_SECONDS
    while leader_exits_first and _alive(started.pid) and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SECONDS)  # the binary is gone; only its descendant holds the pipes open

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=_SPAWN_WAIT_SECONDS)

    assert grandchild_pid is not None
    deadline = time.monotonic() + _SPAWN_WAIT_SECONDS
    while _alive(grandchild_pid) and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SECONDS)
    assert not _alive(grandchild_pid)


# --- failures and the transport retry -------------------------------------------------------------------------------


async def test_non_zero_exit_is_raised_with_bounded_stderr_and_not_retried_by_default(tmp_path: Path) -> None:
    fake = _fake_claude.install(tmp_path / "bin", exit_codes=(1,), stderr="boom " + "x" * 1000)

    with pytest.raises(LlmCallFailure) as raised:
        await _client(fake).call(_request())

    assert raised.value.provider == PROVIDER_NAME
    assert raised.value.reason.startswith("exit status 1: boom ")
    assert len(raised.value.reason) <= _MAX_REASON_CHARS
    assert not isinstance(raised.value, LlmCallTimeout)
    assert len(fake.invocations()) == 1


async def test_failure_reason_falls_back_to_stdout_when_stderr_is_empty(tmp_path: Path) -> None:
    # What the real ``claude -p`` does on a missing login: the message on standard output, stderr empty, exit status 1.
    fake = _fake_claude.install(tmp_path / "bin", reply="Not logged in \u00b7 Please run /login", exit_codes=(1,))

    with pytest.raises(LlmCallFailure) as raised:
        await _client(fake).call(_request(_SECRET_PROMPT))

    assert raised.value.reason == "exit status 1: Not logged in \u00b7 Please run /login"


async def test_failure_text_never_carries_the_prompt(tmp_path: Path) -> None:
    fake = _fake_claude.install(tmp_path / "bin", exit_codes=(2,), stderr=f"echoed {_SECRET_PROMPT} back")

    with pytest.raises(LlmCallFailure) as raised:
        await _client(fake).call(_request(_SECRET_PROMPT))

    assert _SECRET_PROMPT not in str(raised.value)
    assert _SECRET_PROMPT not in raised.value.reason


async def test_failure_text_carries_no_escape_sequence_or_control_character(tmp_path: Path) -> None:
    fake = _fake_claude.install(tmp_path / "bin", exit_codes=(1,), stderr="\x1b[31mnot logged in\x1b[0m\x00\x07 please run /login")

    with pytest.raises(LlmCallFailure) as raised:
        await _client(fake).call(_request())

    assert raised.value.reason == "exit status 1: not logged in please run /login"


async def test_non_zero_exit_is_retried_once_when_the_caller_asks(tmp_path: Path) -> None:
    fake = _fake_claude.install(tmp_path / "bin", reply="second try", exit_codes=(1, 0))

    result = await _client(fake, retry=True).call(_request())

    assert result.text == "second try"
    assert [i.stdin for i in fake.invocations()] == [_PROMPT, _PROMPT]


async def test_a_second_non_zero_exit_raises_after_exactly_two_attempts(tmp_path: Path) -> None:
    fake = _fake_claude.install(tmp_path / "bin", exit_codes=(1,), stderr="still broken")

    with pytest.raises(LlmCallFailure, match="still broken"):
        await _client(fake, retry=True).call(_request())

    assert len(fake.invocations()) == 2


async def test_missing_binary_is_raised_as_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no ``claude`` here
    client = ClaudeCliClient(model="")

    with pytest.raises(LlmCallFailure, match="not found") as raised:
        await client.call(_request())

    assert raised.value.provider == PROVIDER_NAME


async def test_binary_that_cannot_start_is_raised_as_a_failure(tmp_path: Path) -> None:
    not_executable = tmp_path / "claude-without-the-exec-bit"
    not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    not_executable.chmod(stat.S_IRUSR | stat.S_IWUSR)

    with pytest.raises(LlmCallFailure, match="cannot start") as raised:
        await ClaudeCliClient(model="", binary=str(not_executable)).call(_request())

    assert _PROMPT not in str(raised.value)


def test_binary_defaults_to_claude_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_claude.install(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(fake.path.parent))

    assert ClaudeCliClient(model="").binary == str(fake.path)


async def test_binary_that_exits_without_reading_a_large_prompt_still_answers(tmp_path: Path) -> None:
    fake = _fake_claude.install(tmp_path / "bin", reply="early answer", read_stdin=False)

    result = await _client(fake).call(_request("x" * _UNREAD_PROMPT_CHARS))

    assert result.text == "early answer"


async def test_unusable_working_directory_is_raised_as_a_failure(monkeypatch: pytest.MonkeyPatch, fake: _fake_claude.FakeClaude) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(claude_cli_client.tempfile, "TemporaryDirectory", refuse)

    with pytest.raises(LlmCallFailure, match="working directory.*No space left") as raised:
        await _client(fake).call(_request())

    assert raised.value.provider == PROVIDER_NAME
    assert fake.invocations() == []


async def test_argument_the_os_refuses_is_raised_as_a_failure(fake: _fake_claude.FakeClaude) -> None:
    with pytest.raises(LlmCallFailure, match="cannot start") as raised:
        await _client(fake, model="bad\x00model").call(_request())

    assert raised.value.provider == PROVIDER_NAME
