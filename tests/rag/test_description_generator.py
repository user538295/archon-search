"""TDD tests for archon/rag/description_generator.py (FEAT-022 Task 1.3)."""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ResultMessage

from archon.rag.description_generator import _should_regenerate, generate_description


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result_msg(text: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id="test",
        result=text,
    )


def _mock_sdk_client(result_text: str = "A useful description.") -> MagicMock:
    """Return a mock ClaudeSDKClient that yields a single ResultMessage."""
    result_msg = _make_result_msg(result_text)

    async def _gen() -> AsyncGenerator[ResultMessage, None]:
        yield result_msg

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.receive_response = MagicMock(side_effect=lambda: _gen())
    return client


# ---------------------------------------------------------------------------
# generate_description() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_description_calls_haiku() -> None:
    """generate_description() creates a ClaudeSDKClient with DEFAULT_FAST_MODEL and bypassPermissions."""
    from archon.ai.constants import DEFAULT_FAST_MODEL

    mock_client = _mock_sdk_client("Test description.")

    with patch("archon.rag.description_generator.ClaudeSDKClient", return_value=mock_client) as MockSDK:
        result = await generate_description(["chunk one", "chunk two"], "my-collection")

    assert result == "Test description."
    MockSDK.assert_called_once()
    options = MockSDK.call_args.kwargs["options"]
    assert options.model == DEFAULT_FAST_MODEL
    assert options.permission_mode == "bypassPermissions"
    mock_client.connect.assert_awaited_once()
    mock_client.query.assert_awaited_once()
    mock_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_description_on_failure_returns_none() -> None:
    """generate_description() returns None when the SDK raises, without propagating the error."""
    mock_client = MagicMock()
    mock_client.connect = AsyncMock(side_effect=RuntimeError("SDK connection failed"))
    mock_client.disconnect = AsyncMock()

    with patch("archon.rag.description_generator.ClaudeSDKClient", return_value=mock_client):
        result = await generate_description(["chunk text"], "test-collection")

    assert result is None


@pytest.mark.asyncio
async def test_generate_description_timeout_returns_none() -> None:
    """generate_description() returns None when the 30-second timeout is exceeded."""

    async def _slow_connect() -> None:
        await asyncio.sleep(100)  # far exceeds any real timeout

    mock_client = MagicMock()
    mock_client.connect = AsyncMock(side_effect=_slow_connect)
    mock_client.disconnect = AsyncMock()

    with patch("archon.rag.description_generator.ClaudeSDKClient", return_value=mock_client):
        with patch("archon.rag.description_generator._TIMEOUT_SECONDS", 0.01):
            result = await generate_description(["chunk"], "test-collection")

    assert result is None


@pytest.mark.asyncio
async def test_generate_description_returns_none_on_empty_chunks() -> None:
    """generate_description() returns None immediately for an empty chunk list — no SDK call."""
    with patch("archon.rag.description_generator.ClaudeSDKClient") as MockSDK:
        result = await generate_description([], "test-collection")

    assert result is None
    MockSDK.assert_not_called()


# ---------------------------------------------------------------------------
# _should_regenerate() unit tests
# ---------------------------------------------------------------------------


def test_regeneration_trigger_at_20pct_change() -> None:
    """_should_regenerate returns True when doc_count change is >= 20%."""
    # 10 → 12: +20%
    assert _should_regenerate(doc_count=12, chunk_count=5, described_at_doc_count=10)
    # 10 → 8: -20%
    assert _should_regenerate(doc_count=8, chunk_count=5, described_at_doc_count=10)


def test_no_regeneration_below_threshold() -> None:
    """_should_regenerate returns False when doc_count change is < 20%."""
    # 10 → 11: +10%
    assert not _should_regenerate(doc_count=11, chunk_count=5, described_at_doc_count=10)
    # 10 → 9: -10%
    assert not _should_regenerate(doc_count=9, chunk_count=5, described_at_doc_count=10)


def test_regeneration_trigger_when_described_at_doc_count_is_none() -> None:
    """_should_regenerate returns True when described_at_doc_count is None."""
    assert _should_regenerate(doc_count=5, chunk_count=10, described_at_doc_count=None)


def test_regeneration_trigger_when_described_at_doc_count_is_zero() -> None:
    """_should_regenerate returns True when described_at_doc_count is 0 (division-by-zero guard)."""
    assert _should_regenerate(doc_count=5, chunk_count=10, described_at_doc_count=0)


def test_no_regeneration_when_chunk_count_is_zero() -> None:
    """_should_regenerate returns False when chunk_count == 0, regardless of other args."""
    assert not _should_regenerate(doc_count=5, chunk_count=0, described_at_doc_count=None)
    assert not _should_regenerate(doc_count=5, chunk_count=0, described_at_doc_count=0)
    assert not _should_regenerate(doc_count=5, chunk_count=0, described_at_doc_count=10)
