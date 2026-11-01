"""ClaudeCliClient — the ``claude_cli`` wrapper of the shared LLM client (contract C1, scenarios S12, S64, S79).

Runs the Claude Code CLI as a subprocess (``claude -p``), on the operator's own Claude login: no API key, no SDK.

* The prompt goes on standard input for every feature, never in the argument list (which shows in the process list and
  is capped near 1 MiB) so a very large sample works.
* Locked down (S79): every tool disabled, no session saved, no slash commands, skills or MCP servers, ``--max-turns 1``,
  ``--safe-mode`` (the operator's CLAUDE.md, hooks and plugins neither change the output nor see the text) and a neutral,
  empty working directory that is removed after the call. ``--bare`` is NOT used: it reads only an API key, never the
  login this provider exists for. Flags verified against ``claude`` 2.1.308: all are listed by ``claude --help`` except
  ``--max-turns``, which the binary accepts without listing it. A ``claude`` that rejects one fails the call visibly.
* The child's environment is an ALLOWLIST (:func:`child_environment`), never a copy of the server's: no ``CLAUDECODE``, no
  API key, no bearer token, so the call bills the login and never a key, and nothing is mutated process-wide.
* The child is killed and reaped on timeout and on cancellation (S12), with everything it started (it leads its own
  session), so a shutdown leaves no orphan. A hard kill of the server itself (SIGKILL, a crash) cannot reach the child.
* A transport retry (one retry after a short backoff on a non-zero exit) happens only when the caller asked for it at
  construction, for HyDE and RAG Fusion; graph and description prompts hold document excerpts that a retry would send
  twice. A timeout is never retried.
* The graph rate limit (S64) is applied here when the graph composition hands the wrapper a ``FixedWindowRateLimiter``:
  a Claude login has an account-level quota. HyDE, RAG Fusion and description are built without one.
* Every failure is raised as ``LlmCallFailure`` (a timeout as ``LlmCallTimeout``); the reason carries the CLI's stderr
  (its stdout when stderr is empty: ``claude -p`` prints "Not logged in" there), bounded and with the prompt masked, never
  the prompt. ``think`` is ignored (no verified switch) and so is ``max_output_tokens`` (the CLI has no token cap); window
  and budget are unbounded like a cloud provider.

In the dev container the ``claude`` on ``PATH`` is ``scripts/claude-container-wrapper``, which forwards standard input and
its arguments to the host-side ``scripts/claude-proxy.py``; it needs ``CLAUDE_PROXY_URL`` and ``CLAUDE_PROXY_TOKEN``, which
the allowlist therefore lets through. Dev-only limits of that path: the host-side ``claude`` runs under the proxy's own
environment and working directory (``--safe-mode`` still applies), keeps running until the proxy's own 120 s limit when
the wrapper is killed, and its stderr never comes back (a failure reads ``exit status N``).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Mapping
from typing import Final

from archon_search.constants import DEFAULT_FAST_MODEL
from archon_search.llm_client.budget import cloud_budget
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindow,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallResult,
    LlmCallTimeout,
    ThinkSetting,
)
from archon_search.llm_client.rate_limit import FixedWindowRateLimiter
from archon_search.llm_client.server_text import bounded_server_text

PROVIDER_NAME: Final = "claude_cli"
_BINARY_NAME: Final = "claude"

#: Pause before the single transport retry (HyDE and RAG Fusion): a ``claude`` run beside another active Claude Code
#: session can exit non-zero under contention, and one retry absorbs it.
RETRY_BACKOFF_SECONDS: Final = 0.5

#: The lockdown flags of every call; the prompt is never among them. ``--tools ""`` disables all built-in tools.
_LOCKDOWN_ARGV: Final = (
    "-p",
    "--output-format",
    "text",
    "--tools",
    "",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--max-turns",
    "1",
    "--safe-mode",
)

_WORKDIR_PREFIX: Final = "archon-claude-cli-"

# The only variables the child sees. ``USER``, ``LOGNAME`` and ``TMPDIR`` are needed on macOS to find the keychain login.
_ENV_NAMES: Final = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "TMPDIR",
        "CLAUDE_CONFIG_DIR",
        # Windows ([INFERENCE]: not tested).
        "SYSTEMROOT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        # Proxy and CA settings of the operator's network ([INFERENCE]: not tested).
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        # The dev-container ``claude`` wrapper reaches the host-side claude-proxy with these.
        "CLAUDE_PROXY_URL",
        "CLAUDE_PROXY_TOKEN",
    }
)
_ENV_PREFIXES: Final = ("LC_",)

# Terminal escape sequences: belt and braces, ``--output-format text`` is plain but older versions may emit status codes.
# A CSI sequence (parameter bytes, intermediates, a final byte), an OSC string up to BEL or ST, and a two-character escape.
_ANSI_RE: Final = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
# What is left of the control characters once the sequences are gone (a bare ESC, NUL, BEL ...): never text, and an
# embedder, a one-line description or a failure message must not carry them. Tab, line feed and carriage return stay.
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_ATTEMPTS_WITHOUT_RETRY: Final = 1
_ATTEMPTS_WITH_RETRY: Final = 2  # one retry

# POSIX: the child gets its own session so a kill reaches its descendants too (one that holds the output pipes would
# otherwise outlive the call). Elsewhere only the child itself is killed.
_HAS_PROCESS_GROUPS: Final = hasattr(os, "killpg")


def _clean_reply(reply: str) -> str:
    """*reply* without terminal escape sequences and without control characters (tab, line feed, carriage return stay)."""
    return _CONTROL_RE.sub("", _ANSI_RE.sub("", reply))


def child_environment(source: Mapping[str, str]) -> dict[str, str]:
    """The allowlisted subset of ``source``: what the child ``claude`` may see, and nothing else."""
    return {
        name: value
        for name, value in source.items()
        if name in _ENV_NAMES or name.startswith(_ENV_PREFIXES)
    }


def resolve_claude_binary() -> str | None:
    """The ``claude`` program resolved from ``PATH`` now, or ``None`` when there is none (the startup probe of a graph on
    ``claude_cli`` asks the same question the client answers when it is built)."""
    return shutil.which(_BINARY_NAME)


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the child and everything it started: it leads its own session, so its pid is its process group."""
    try:
        if _HAS_PROCESS_GROUPS:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass  # already gone (a group that only holds a zombie can answer EPERM on macOS)


class ClaudeCliClient:
    """``LlmClient`` for one ``claude_cli`` feature: one model on the operator's Claude login."""

    def __init__(
        self,
        *,
        model: str,
        retry_on_nonzero_exit: bool = False,
        binary: str | None = None,
        rate_limiter: FixedWindowRateLimiter | None = None,
    ) -> None:
        """
        Args:
            model: passed as ``--model``; blank or ``DEFAULT_FAST_MODEL`` (the config default sentinel) leaves the
                choice to Claude Code.
            retry_on_nonzero_exit: one retry after ``RETRY_BACKOFF_SECONDS`` on a non-zero exit; HyDE and RAG Fusion
                only, never for prompts that hold document excerpts.
            binary: the executable to run; ``claude`` resolved from ``PATH`` now when ``None``. Tests pass the fake.
            rate_limiter: the graph rate limit (``extraction_rate_limit_rpm``); a call takes one slot (a transport retry
                inside it does not take a second) and an exhausted window raises ``LlmCallFailure`` without a spawn.
                ``None`` (HyDE, RAG Fusion, description) is unthrottled.
        """
        self._model = model
        self.binary: str | None = binary if binary is not None else resolve_claude_binary()
        self._attempts = _ATTEMPTS_WITH_RETRY if retry_on_nonzero_exit else _ATTEMPTS_WITHOUT_RETRY
        self._rate_limiter = rate_limiter

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        if self.binary is None:
            raise LlmCallFailure(PROVIDER_NAME, f"'{_BINARY_NAME}' not found on PATH")
        if self._rate_limiter is not None:
            self._rate_limiter.acquire(PROVIDER_NAME)
        argv = [self.binary, *_LOCKDOWN_ARGV, *self._model_argv()]
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
            exit_status, stdout, stderr = await self._run_once(argv, request)
            if exit_status == 0:
                return LlmCallResult(text=_clean_reply(stdout.decode("utf-8", errors="replace")))
        # ``claude -p`` reports a failed login ("Not logged in") on standard output with stderr empty, so fall back to it.
        detail = stderr.strip() or stdout
        reason = bounded_server_text(_clean_reply(detail.decode("utf-8", errors="replace")), request.prompt)
        raise LlmCallFailure(PROVIDER_NAME, f"exit status {exit_status}: {reason}" if reason else f"exit status {exit_status}")

    def context_window(self) -> ContextWindow:
        return UNBOUNDED

    async def input_budget(
        self, template_text: str, *, max_output_tokens: int, think: ThinkSetting = ThinkSetting.DEFAULT
    ) -> InputBudget:
        return cloud_budget()

    def _model_argv(self) -> list[str]:
        return ["--model", self._model] if self._model and self._model != DEFAULT_FAST_MODEL else []

    @staticmethod
    async def _run_once(argv: list[str], request: LlmCallRequest) -> tuple[int, bytes, bytes]:
        """One spawn: the prompt on standard input, the exit status and both output streams back.

        A call left early (timeout, cancellation) kills the child and everything it started, and reaps it, before the
        working directory is removed.
        """
        try:
            workdir = tempfile.TemporaryDirectory(prefix=_WORKDIR_PREFIX, ignore_cleanup_errors=True)
        except OSError as exc:
            raise LlmCallFailure(PROVIDER_NAME, f"cannot create a working directory: {exc.strerror or type(exc).__name__}") from exc
        with workdir as workdir_path:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir_path,
                    env=child_environment(os.environ),
                    start_new_session=_HAS_PROCESS_GROUPS,
                )
            except (OSError, ValueError) as exc:
                reason = exc.strerror if isinstance(exc, OSError) else None
                raise LlmCallFailure(PROVIDER_NAME, f"cannot start '{_BINARY_NAME}': {reason or type(exc).__name__}") from exc
            completed = False
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    stdout, stderr = await process.communicate(request.prompt.encode("utf-8", errors="replace"))
                completed = True
            except TimeoutError as exc:
                raise LlmCallTimeout(PROVIDER_NAME, f"no answer within {request.timeout_seconds:g}s") from exc
            finally:
                if not completed:
                    # Even when the child itself already exited: a descendant may still hold the output pipes.
                    _kill_process_tree(process)
                    await process.wait()
        return process.returncode, stdout, stderr
