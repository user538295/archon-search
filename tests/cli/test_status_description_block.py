"""FE-12: ``archon-search status`` renders the ``description`` block of GET /status (S39, S66)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.platform.service import ServiceStatus
from archon_search.server.schemas import DescriptionStatusDetail


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


_ABSENT: Any = object()


def _invoke(runner: CliRunner, description: Any) -> Any:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=None, uptime_seconds=None)
    payload: dict[str, Any] = {"telemetry": None}
    if description is not _ABSENT:
        payload["description"] = description
    with (
        patch("archon_search.cli.status._get_service", return_value=svc),
        patch("archon_search.cli.status._fetch_server_status", return_value=payload),
        patch("archon_search.cli.status._print_active_jobs"),
    ):
        return runner.invoke(main, ["status"])


def _block(**overrides: Any) -> dict[str, Any]:
    """A payload built from the server schema so a field rename fails these tests too."""
    block = DescriptionStatusDetail(
        provider="ollama",
        key_available=True,
        reachable=True,
        effective_context_tokens=8192,
        context_window_source="configured",
        backfill_on_startup=True,
        last_attempt_ok=False,
        last_attempt_at=datetime(2026, 11, 1, 10, 20, 30, tzinfo=UTC),
    ).model_dump(mode="json")
    block.update(overrides)
    return block


def test_status_prints_description_block_fields(runner: CliRunner) -> None:
    result = _invoke(runner, _block())
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "\nCollection descriptions:\n" in out
    assert "provider: ollama" in out
    assert "key: available" in out
    assert "reachable (startup probe): yes" in out
    assert "context window: 8192 tokens (configured)" in out
    assert "backfill on startup: yes" in out
    assert "last attempt: failed at 2026-11-01T10:20:30Z" in out


def test_status_prints_unprobed_and_never_attempted_states(runner: CliRunner) -> None:
    result = _invoke(
        runner,
        _block(
            reachable=None,
            effective_context_tokens=None,
            context_window_source=None,
            backfill_on_startup=False,
            last_attempt_ok=None,
            last_attempt_at=None,
        ),
    )
    out = result.stdout
    assert "reachable (startup probe): not probed yet" in out
    assert "context window: not limited" in out
    assert "backfill on startup: no" in out
    assert "last attempt: none since start" in out
    assert "reachable (startup probe): no\n" in _invoke(runner, _block(reachable=False)).stdout
    assert "last attempt: ok at" in _invoke(runner, _block(last_attempt_ok=True)).stdout


def test_status_description_block_never_reads_non_bool_as_true(runner: CliRunner) -> None:
    out = _invoke(
        runner, _block(key_available="false", reachable="false", backfill_on_startup="false", last_attempt_ok="false")
    ).stdout
    assert "key: unknown" in out
    assert "reachable (startup probe): not probed yet" in out
    assert "backfill on startup: unknown" in out
    assert "last attempt: unknown at" in out


@pytest.mark.parametrize("description", [None, _ABSENT, "text", ["x"]])
def test_status_prints_nothing_when_block_null(runner: CliRunner, description: Any) -> None:
    result = _invoke(runner, description)
    assert result.exit_code == 0, result.output
    assert "Collection descriptions" not in result.stdout
    assert result.stdout == "running\n"
    assert result.stderr == ""


def test_status_description_block_cleans_untrusted_text(runner: CliRunner) -> None:
    result = _invoke(
        runner,
        _block(
            provider="evil\nforged: line\x1b[31m",
            last_attempt_at="x\ny",
            effective_context_tokens="1\nfake: row",
            context_window_source="s\nsecond",
        ),
    )
    out = result.stdout
    assert "provider: evil forged: line [31m" in out
    assert "last attempt: failed at x y" in out
    assert "context window: 1 fake: row tokens (s second)" in out
    assert "\x1b" not in out
    assert all(not line.startswith(("forged", "fake", "second", "y")) for line in out.splitlines())


def test_status_description_block_shows_no_description_text(runner: CliRunner) -> None:
    out = _invoke(runner, _block(last_attempt_error="SECRET EXCERPT", description="SECRET EXCERPT")).stdout
    assert "SECRET EXCERPT" not in out


def test_status_description_key_warning_wording(runner: CliRunner) -> None:
    result = _invoke(runner, _block(provider="anthropic", key_available=False))
    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "ANTHROPIC_API_KEY" in err
    assert "descriptions will fail until ANTHROPIC_API_KEY is set" in err
    assert "expansion" not in err
    assert "plain search" not in err
    assert "key: missing" in result.stdout


def test_status_description_key_warning_names_openai_and_claude_program(runner: CliRunner) -> None:
    openai_err = _invoke(runner, _block(provider="openai", key_available=False)).stderr
    assert "descriptions will fail until OPENAI_API_KEY is set" in openai_err
    claude_err = _invoke(runner, _block(provider="claude_cli", key_available=False)).stderr
    assert "the `claude` program was not found" in claude_err
    assert "API key" not in claude_err
    assert "expansion" not in claude_err


def test_status_description_key_warning_cleans_hostile_provider(runner: CliRunner) -> None:
    err = _invoke(runner, _block(provider="x\x1b[31m\nforged", key_available=False)).stderr
    assert "\x1b" not in err
    assert err.count("\n") == 1
    assert "the 'x [31m forged' API key" in err


def test_status_description_no_key_warning_when_key_available(runner: CliRunner) -> None:
    result = _invoke(runner, _block(provider="anthropic"))
    assert "key: available" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("flag", [None, 0, 1, "yes"])
def test_status_description_key_flag_that_is_not_false_never_warns(runner: CliRunner, flag: Any) -> None:
    result = _invoke(runner, _block(provider="anthropic", key_available=flag))
    assert result.stderr == ""
    assert "key: unknown" in result.stdout


def test_status_description_missing_provider_uses_generic_warning(runner: CliRunner) -> None:
    block = _block(key_available=False)
    del block["provider"]
    result = _invoke(runner, block)
    assert "provider: unknown" in result.stdout
    assert "the provider's API key is not set" in result.stderr
    assert "'unknown'" not in result.stderr


@pytest.mark.parametrize("source", ["configured", "probed", "assumed"])
def test_status_description_shows_every_window_source(runner: CliRunner, source: str) -> None:
    out = _invoke(runner, _block(effective_context_tokens=4096, context_window_source=source)).stdout
    assert f"context window: 4096 tokens ({source})" in out


def test_status_description_flags_render_independently(runner: CliRunner) -> None:
    out = _invoke(
        runner, _block(key_available=False, reachable=True, backfill_on_startup=False, last_attempt_ok=True)
    ).stdout
    assert "key: missing" in out
    assert "reachable (startup probe): yes" in out
    assert "backfill on startup: no" in out
    assert "last attempt: ok at" in out


def test_status_description_warning_is_independent_of_hyde_warning(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=None, uptime_seconds=None)
    payload = {"hyde": {"key_available": False, "provider": "openai"}, "description": _block(), "telemetry": None}
    with (
        patch("archon_search.cli.status._get_service", return_value=svc),
        patch("archon_search.cli.status._fetch_server_status", return_value=payload),
        patch("archon_search.cli.status._print_active_jobs"),
    ):
        result = runner.invoke(main, ["status"])
    assert "HyDE enabled but OPENAI_API_KEY is not set" in result.stderr
    assert "descriptions will fail" not in result.stderr
