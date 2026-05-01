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
    async def test_no_api_key_falls_back_to_path(self, tmp_path, monkeypatch) -> None:
        """When ANTHROPIC_API_KEY is not set, generate_description returns the collection path."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from archon_search.description_generator import generate_description

        result = await generate_description(["chunk1", "chunk2"], str(tmp_path))
        assert result == str(tmp_path)

    @pytest.mark.asyncio
    async def test_api_failure_falls_back_to_path(self, tmp_path, monkeypatch) -> None:
        """When the SDK raises an exception, generate_description returns the collection path."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch("archon_search.description_generator.ClaudeSDKClient") as mock_cls:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(side_effect=RuntimeError("SDK failure"))
            mock_cls.return_value = mock_client

            from archon_search.description_generator import generate_description

            result = await generate_description(["chunk1", "chunk2"], str(tmp_path))
            assert result == str(tmp_path)

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
