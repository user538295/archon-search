"""Unit tests for ClaudeCLIQueryExpansionProvider.

Tests the Claude Code CLI adapter that implements QueryExpansionProvider.
All tests mock ``asyncio.create_subprocess_exec`` and ``shutil.which`` so no
real ``claude`` subprocess is spawned and results are environment-independent.

Privacy invariant (S14): error-path log messages must contain
``_query_fingerprint(query)`` and must NOT contain the raw query string.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import archon_search.providers.claude_cli_provider as ccp
from archon_search._privacy import _query_fingerprint
from archon_search.constants import DEFAULT_FAST_MODEL
from archon_search.providers.claude_cli_provider import ClaudeCLIQueryExpansionProvider
from archon_search.query_expansion_protocol import QueryExpansionProvider

_FAKE_CLAUDE = "/usr/bin/claude"


def _available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force shutil.which('claude') to resolve to a fake path."""
    monkeypatch.setattr(ccp.shutil, "which", lambda _name: _FAKE_CLAUDE)


def _mock_proc(stdout: bytes = b"", returncode: int = 0, *, timeout: bool = False) -> MagicMock:
    """Build a fake asyncio subprocess Process."""
    proc = MagicMock()
    if timeout:
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    else:
        proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


def _patch_exec(monkeypatch: pytest.MonkeyPatch, proc: MagicMock) -> MagicMock:
    """Patch create_subprocess_exec to return *proc*; return the spy mock."""
    spy = AsyncMock(return_value=proc)
    monkeypatch.setattr(ccp.asyncio, "create_subprocess_exec", spy)
    return spy


# ---------------------------------------------------------------------------
# generate_hypothetical_doc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"A hypothetical passage about the query.\n"))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result == "A hypothetical passage about the query."


@pytest.mark.asyncio
async def test_generate_strips_ansi_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case: ANSI/status codes in output are stripped before returning."""
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"\x1b[32mgreen answer\x1b[0m"))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("query")

    assert result == "green answer"


@pytest.mark.asyncio
async def test_generate_timeout_returns_none_and_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    proc = _mock_proc(timeout=True)
    _patch_exec(monkeypatch, proc)

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("query")

    assert result is None
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_generate_nonzero_exit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"error", returncode=1))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("query")

    assert result is None


@pytest.mark.asyncio
async def test_generate_spawn_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_subprocess_exec raising (e.g. FileNotFoundError) → None, no raise."""
    _available(monkeypatch)
    monkeypatch.setattr(
        ccp.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("claude vanished")),
    )

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("query")

    assert result is None


@pytest.mark.asyncio
async def test_generate_empty_output_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"   \n"))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("query")

    assert result is None


@pytest.mark.asyncio
async def test_generate_claude_not_in_path_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ccp.shutil, "which", lambda _name: None)

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("query")

    assert result is None


# ---------------------------------------------------------------------------
# decompose_query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_returns_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"variant one\nvariant two\nvariant three"))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.decompose_query("search query", num_queries=3)

    assert result == ["variant one", "variant two", "variant three"]


@pytest.mark.asyncio
async def test_decompose_filters_and_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank / overlong / control-char lines dropped; result truncated to num_queries."""
    long_line = "x" * 501
    payload = f"\nvariant one\n{long_line}\nbad\x00line\nvariant two\nvariant three\nvariant four"
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(payload.encode()))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.decompose_query("search query", num_queries=3)

    assert result == ["variant one", "variant two", "variant three"]


@pytest.mark.asyncio
async def test_decompose_timeout_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(timeout=True))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.decompose_query("query")

    assert result == []


@pytest.mark.asyncio
async def test_decompose_claude_not_in_path_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ccp.shutil, "which", lambda _name: None)

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.decompose_query("query")

    assert result == []


# ---------------------------------------------------------------------------
# --model flag handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_alias_passed_as_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    spy = _patch_exec(monkeypatch, _mock_proc(b"answer"))

    provider = ClaudeCLIQueryExpansionProvider(model="sonnet")
    await provider.generate_hypothetical_doc("query")

    argv = spy.call_args.args
    assert "--model" in argv
    assert "sonnet" in argv
    assert argv[:2] == (_FAKE_CLAUDE, "-p")
    assert "--output-format" in argv and "text" in argv


@pytest.mark.asyncio
async def test_default_model_omits_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """model == DEFAULT_FAST_MODEL → --model omitted (Claude Code uses its default)."""
    _available(monkeypatch)
    spy = _patch_exec(monkeypatch, _mock_proc(b"answer"))

    provider = ClaudeCLIQueryExpansionProvider(model=DEFAULT_FAST_MODEL)
    await provider.generate_hypothetical_doc("query")

    assert "--model" not in spy.call_args.args


@pytest.mark.asyncio
async def test_blank_model_omits_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    spy = _patch_exec(monkeypatch, _mock_proc(b"answer"))

    provider = ClaudeCLIQueryExpansionProvider(model="")
    await provider.generate_hypothetical_doc("query")

    assert "--model" not in spy.call_args.args


# ---------------------------------------------------------------------------
# no rate-limit state / no key
# ---------------------------------------------------------------------------


def test_no_rate_limit_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    assert not hasattr(provider, "_rpm_tokens")
    assert not hasattr(provider, "_rpm_lock")


def test_is_key_available_always_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    assert provider.is_key_available() is True


def test_satisfies_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    _available(monkeypatch)
    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    assert isinstance(provider, QueryExpansionProvider)


# ---------------------------------------------------------------------------
# privacy (S14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_path_uses_fingerprint_not_raw_query(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    raw_query = "this is the raw secret query text that must not appear in logs"
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"", returncode=1))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    with caplog.at_level(logging.WARNING, logger="archon_search.providers.claude_cli_provider"):
        result = await provider.generate_hypothetical_doc(raw_query)

    assert result is None
    assert _query_fingerprint(raw_query) in caplog.text
    assert raw_query not in caplog.text


# ---------------------------------------------------------------------------
# decode guard / sanitization (C1-I-40, C1-I-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_non_utf8_bytes_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid UTF-8 in stdout must not raise (errors='replace' guard) — C1-I-40."""
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"hello \xff\xfe world"))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("query")

    assert isinstance(result, str)
    assert "hello" in result and "world" in result


@pytest.mark.asyncio
async def test_generate_strips_bare_control_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """HyDE output with bare control chars (NUL, lone ESC) is sanitized — C1-I-2."""
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"clean\x00\x1btext"))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    result = await provider.generate_hypothetical_doc("query")

    assert result == "cleantext"


# ---------------------------------------------------------------------------
# one-time not-found warning (C1-I-45)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_warning_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The 'claude not found' warning fires at most once across many calls."""
    monkeypatch.setattr(ccp.shutil, "which", lambda _name: None)

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    with caplog.at_level(logging.WARNING, logger="archon_search.providers.claude_cli_provider"):
        await provider.generate_hypothetical_doc("q1")
        await provider.generate_hypothetical_doc("q2")
        await provider.decompose_query("q3")

    not_found = [r for r in caplog.records if "not found in PATH" in r.message]
    assert len(not_found) == 1, f"expected exactly one not-found warning, got {len(not_found)}"


# ---------------------------------------------------------------------------
# decompose-path privacy (C1-I-41/42/44)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_error_path_uses_fingerprint_not_raw_query(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """decompose_query error logs contain the fingerprint and NOT the raw query."""
    raw_query = "another raw secret query string that must never be logged verbatim"
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(b"", returncode=1))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    with caplog.at_level(logging.WARNING, logger="archon_search.providers.claude_cli_provider"):
        result = await provider.decompose_query(raw_query)

    assert result == []
    assert caplog.text, "expected at least one warning log"
    assert _query_fingerprint(raw_query) in caplog.text
    assert raw_query not in caplog.text


@pytest.mark.asyncio
async def test_decompose_timeout_path_uses_fingerprint_not_raw_query(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """decompose_query timeout log contains the fingerprint and NOT the raw query."""
    raw_query = "timeout-path raw query that must stay out of the logs entirely"
    _available(monkeypatch)
    _patch_exec(monkeypatch, _mock_proc(timeout=True))

    provider = ClaudeCLIQueryExpansionProvider(model="haiku")
    with caplog.at_level(logging.WARNING, logger="archon_search.providers.claude_cli_provider"):
        result = await provider.decompose_query(raw_query)

    assert result == []
    assert caplog.text, "expected a timeout warning log"
    assert _query_fingerprint(raw_query) in caplog.text
    assert raw_query not in caplog.text
