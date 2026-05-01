"""Tests for archon_search.description_generator — TDD for Task 2.2."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestConstants:
    def test_constants_independent(self) -> None:
        """archon_search.constants is importable without archon package."""
        from archon_search.constants import DEFAULT_FAST_MODEL, DEFAULT_MODEL

        assert DEFAULT_FAST_MODEL == "claude-haiku-4-5-20251001"
        assert DEFAULT_MODEL == "claude-sonnet-4-6"


class TestDescriptionGeneratorFallback:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self, tmp_path, monkeypatch) -> None:
        """When ANTHROPIC_API_KEY is not set, generate_description returns None."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from archon_search.description_generator import generate_description

        result = await generate_description(["chunk1", "chunk2"], str(tmp_path))
        assert result is None

    @pytest.mark.asyncio
    async def test_api_failure_returns_none(self, tmp_path, monkeypatch) -> None:
        """When the SDK raises an exception, generate_description returns None."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch("archon_search.description_generator.ClaudeSDKClient") as mock_cls:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(side_effect=RuntimeError("SDK failure"))
            mock_cls.return_value = mock_client

            from archon_search.description_generator import generate_description

            result = await generate_description(["chunk1", "chunk2"], str(tmp_path))
            assert result is None

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_none(self) -> None:
        """Empty chunks list skips generation and returns None."""
        from archon_search.description_generator import generate_description

        result = await generate_description([], "test-collection")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_generation_returns_description(self, monkeypatch) -> None:
        """Happy path: SDK returns a description string."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        from claude_agent_sdk import ResultMessage

        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.query = AsyncMock()
        mock_client.disconnect = AsyncMock()

        result_msg = AsyncMock(spec=ResultMessage)
        result_msg.result = "A test description of this collection"

        async def mock_receive():
            yield result_msg

        mock_client.receive_response = mock_receive

        with patch("archon_search.description_generator.ClaudeSDKClient", return_value=mock_client):
            from archon_search.description_generator import generate_description

            result = await generate_description(["chunk text"], "test-collection")

        assert result == "A test description of this collection"

    @pytest.mark.asyncio
    async def test_no_archon_imports(self) -> None:
        """description_generator must not import from archon.*."""
        import importlib
        import importlib.util

        spec = importlib.util.find_spec("archon_search.description_generator")
        assert spec is not None
        # Load source and verify no archon.* imports
        source_file = spec.origin
        assert source_file is not None
        with open(source_file) as f:
            source = f.read()
        assert "from archon." not in source
        assert "import archon." not in source
        assert "import archon\n" not in source
        assert "from archon import" not in source
