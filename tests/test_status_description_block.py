"""BE-37 (collection-descriptions plan, C7, S39, S66): the ``description`` block of ``GET /status``.

The builder reads ``app.state`` (client, reachability, lifecycle), the config and the API-key environment variables, so
stand-ins are enough.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from archon_search.config import DescriptionConfig, SearchConfig
from archon_search.description_lifecycle import LastAttempt
from archon_search.llm_client.claude_cli_client import ClaudeCliClient
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindowSource,
    ContextWindowTokens,
)
from archon_search.server.description_backfill import DescriptionReachability
from archon_search.server.routes_status import _build_description_status
from archon_search.server.schemas import DescriptionStatusDetail

_WINDOW = 8192
_ATTEMPT_AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class _Client:
    def __init__(self, window: Any) -> None:
        self._window = window

    def context_window(self) -> Any:
        return self._window


class _Lifecycle:
    def __init__(self, attempt: LastAttempt | None) -> None:
        self.last_attempt = attempt


def _request(
    *,
    client: Any = None,
    reachability: DescriptionReachability | None = None,
    attempt: LastAttempt | None = None,
    with_lifecycle: bool = True,
) -> Any:
    state = SimpleNamespace(
        description_client=client,
        description_reachability=reachability,
        description_lifecycle=_Lifecycle(attempt) if with_lifecycle else None,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _config(**changes: Any) -> SearchConfig:
    values: dict[str, Any] = {"enabled": True, "provider": "ollama", "backfill_on_startup": False}
    values.update(changes)
    return SearchConfig(description=DescriptionConfig(**values))


def test_status_description_block_is_null_when_disabled() -> None:
    config = SearchConfig(description=DescriptionConfig(enabled=False))
    request = _request(client=_Client(UNBOUNDED), attempt=LastAttempt(ok=True, at=_ATTEMPT_AT))

    assert _build_description_status(request, config) is None


def test_status_reachability_null_until_probed() -> None:
    window = ContextWindowTokens(_WINDOW, ContextWindowSource.CONFIGURED)
    config = _config()

    before = _build_description_status(_request(client=_Client(window)), config)
    after_ok = _build_description_status(
        _request(client=_Client(window), reachability=DescriptionReachability(True, None)), config
    )
    after_down = _build_description_status(
        _request(client=_Client(window), reachability=DescriptionReachability(False, "no route to host")), config
    )

    assert before is not None and before.reachable is None
    assert after_ok is not None and after_ok.reachable is True
    assert after_down is not None and after_down.reachable is False
    assert "no route" not in after_down.model_dump_json()  # the reason is log-only, not part of the wire shape


def test_status_block_fields_for_a_local_provider() -> None:
    window = ContextWindowTokens(_WINDOW, ContextWindowSource.PROBED)
    config = _config(provider="llama_cpp", backfill_on_startup=True)
    request = _request(client=_Client(window), attempt=LastAttempt(ok=False, at=_ATTEMPT_AT))

    block = _build_description_status(request, config)

    assert block == DescriptionStatusDetail(
        provider="llama_cpp",
        key_available=True,
        reachable=None,
        effective_context_tokens=_WINDOW,
        context_window_source="probed",
        backfill_on_startup=True,
        last_attempt_ok=False,
        last_attempt_at=_ATTEMPT_AT,
    )


def test_status_block_cloud_provider_has_no_window_and_no_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config(provider="anthropic")

    block = _build_description_status(_request(client=_Client(UNBOUNDED)), config)

    assert block is not None
    assert block.key_available is False
    assert block.effective_context_tokens is None and block.context_window_source is None
    assert block.last_attempt_ok is None and block.last_attempt_at is None


@pytest.mark.parametrize(("key_set", "expected"), [(True, True), (False, False)])
def test_status_block_key_follows_the_providers_api_key(
    monkeypatch: pytest.MonkeyPatch, key_set: bool, expected: bool
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if key_set:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    block = _build_description_status(_request(client=_Client(UNBOUNDED)), _config(provider="openai"))

    assert block is not None and block.provider == "openai" and block.key_available is expected


@pytest.mark.parametrize(("binary", "expected"), [("/usr/local/bin/claude", True), (None, False)])
def test_status_block_claude_cli_key_means_the_client_found_its_program(binary: str | None, expected: bool) -> None:
    client = ClaudeCliClient(model="m", binary="/usr/local/bin/claude")
    client.binary = binary  # what the client resolved when it was built

    block = _build_description_status(_request(client=client), _config(provider="claude_cli"))

    assert block is not None and block.key_available is expected


def test_status_block_without_lifecycle_has_no_attempt() -> None:
    window = ContextWindowTokens(_WINDOW, ContextWindowSource.ASSUMED)

    block = _build_description_status(_request(client=_Client(window), with_lifecycle=False), _config())

    assert block is not None
    assert block.context_window_source == "assumed"
    assert block.last_attempt_ok is None
