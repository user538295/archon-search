"""Tests for archon_search.description_generator — TDD for ."""
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

        with patch("claude_agent_sdk.ClaudeSDKClient") as mock_cls:
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

        with patch("claude_agent_sdk.ClaudeSDKClient", return_value=mock_client):
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


class TestSDKLazyImport:
    def test_sdk_not_a_module_attribute(self) -> None:
        """C3: All three claude_agent_sdk symbols must not be module-level attributes after the move."""
        import archon_search.description_generator as dg

        assert not hasattr(dg, "ClaudeSDKClient"), (
            "ClaudeSDKClient must not be a module attribute — it must live inside _call_haiku()"
        )
        assert not hasattr(dg, "ClaudeAgentOptions"), (
            "ClaudeAgentOptions must not be a module attribute — it must live inside _call_haiku()"
        )
        assert not hasattr(dg, "ResultMessage"), (
            "ResultMessage must not be a module attribute — it must live inside _call_haiku()"
        )

    @pytest.mark.asyncio
    async def test_sdk_absent_raises_at_call_not_at_import(self, monkeypatch) -> None:
        """S5: SDK absence must be deferred to call time, not import time.

        Masks claude_agent_sdk in sys.modules (the standard absent-dep pattern).
        The module itself must already be importable (its body has no top-level SDK
        import after BE-1). Calling _call_haiku() must raise ImportError because
        the `from claude_agent_sdk import ...` inside the function body fires at
        call time and sees the None sentinel.
        """
        import sys

        import archon_search.description_generator as dg

        # The module is already imported (cached in sys.modules) — its body has no
        # top-level SDK import, so it loaded without touching claude_agent_sdk.
        assert sys.modules.get("archon_search.description_generator") is dg

        # Mask the SDK so that any `from claude_agent_sdk import ...` raises ImportError.
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

        # S5: calling _call_haiku() must raise ImportError from claude_agent_sdk
        # because the deferred `from claude_agent_sdk import ...` in the function body
        # fires at call time and sees the None sentinel — proving deferral to call time.
        with pytest.raises(ImportError, match="claude_agent_sdk"):
            await dg._call_haiku("test prompt")

    @pytest.mark.asyncio
    async def test_call_haiku_imports_sdk_inside_function(self, monkeypatch) -> None:
        """S4: _call_haiku() imports and uses ClaudeSDKClient inside the function body."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        from claude_agent_sdk import ResultMessage

        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.query = AsyncMock()
        mock_client.disconnect = AsyncMock()

        result_msg = AsyncMock(spec=ResultMessage)
        result_msg.result = "Description from inside _call_haiku"

        async def mock_receive():
            yield result_msg

        mock_client.receive_response = mock_receive

        with patch("claude_agent_sdk.ClaudeSDKClient", return_value=mock_client) as mock_cls:
            from archon_search.description_generator import _call_haiku

            result = await _call_haiku("test prompt")

        mock_cls.assert_called_once()
        mock_client.query.assert_awaited_once_with("test prompt")
        assert result == "Description from inside _call_haiku"
