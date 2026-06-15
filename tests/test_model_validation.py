"""Tests for archon_search.model_validation."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.model_validation import ModelValidationError, validate_embedding_model


@pytest.mark.asyncio
async def test_validate_known_model_returns_dim():
    """If the model is in list_supported_models, return its dim directly."""
    with patch("archon_search.model_validation.TextEmbedding") as mock_te:
        mock_te.list_supported_models.return_value = [{"name": "model-X", "dim": 384}]
        result = await validate_embedding_model("model-X")
    assert result == 384


@pytest.mark.asyncio
async def test_validate_unknown_model_falls_back_to_instantiation():
    """If model is not in list, fall back to instantiating make_embedder."""
    mock_embedder = MagicMock()
    mock_embedder.embedding_dim = 512
    mock_embedder.embed = AsyncMock(return_value=[[0.0] * 512])

    with patch("archon_search.model_validation.TextEmbedding") as mock_te, \
         patch("archon_search.model_validation.make_embedder", return_value=mock_embedder):
        mock_te.list_supported_models.return_value = []
        result = await validate_embedding_model("custom-model")

    assert result == 512
    mock_embedder.embed.assert_awaited_once_with(["probe"])


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::RuntimeWarning")
async def test_validate_timeout_raises_model_validation_error():
    """If the embed probe times out, raise ModelValidationError."""
    mock_embedder = MagicMock()
    # AsyncMock raises TimeoutError when awaited — the real wait_for propagates it.
    # Do NOT mock asyncio.wait_for directly: it would leak the inner coroutine (RuntimeWarning).
    mock_embedder.embed = AsyncMock(side_effect=asyncio.TimeoutError)

    with patch("archon_search.model_validation.TextEmbedding") as mock_te, \
         patch("archon_search.model_validation.make_embedder", return_value=mock_embedder):
        mock_te.list_supported_models.return_value = []
        with pytest.raises(ModelValidationError) as exc_info:
            await validate_embedding_model("slow-model")

    assert "could not determine model output dimension" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::RuntimeWarning")
async def test_validate_list_supported_models_unavailable_falls_back():
    """If list_supported_models raises AttributeError, fall back to instantiation."""
    mock_embedder = MagicMock()
    mock_embedder.embedding_dim = 256
    mock_embedder.embed = AsyncMock(return_value=[[0.0] * 256])

    with patch("archon_search.model_validation.TextEmbedding") as mock_te, \
         patch("archon_search.model_validation.make_embedder", return_value=mock_embedder):
        mock_te.list_supported_models.side_effect = AttributeError
        result = await validate_embedding_model("custom-model")

    assert result == 256
    mock_embedder.embed.assert_awaited_once_with(["probe"])
