"""Tests for the RAG conftest ML model isolation fixture."""

import numpy as np
import sentence_transformers


def test_sentence_transformer_is_patched() -> None:
    """SentenceTransformer instantiation must not hit the network (it's a mock)."""
    from unittest.mock import MagicMock

    model = sentence_transformers.SentenceTransformer("all-MiniLM-L6-v2")
    assert isinstance(model, MagicMock), (
        "SentenceTransformer should be patched to a mock in non-live tests"
    )


def test_sentence_transformer_encode_returns_zeros() -> None:
    """Mock encode() must return zeroed numpy array of dimension 768."""
    model = sentence_transformers.SentenceTransformer("any-model")
    result = model.encode(["hello world"])
    assert isinstance(result, np.ndarray)
    assert result.shape == (1, 768)
    assert np.all(result == 0.0)


def test_cross_encoder_is_patched() -> None:
    """CrossEncoder instantiation must not hit the network (it's a mock)."""
    from unittest.mock import MagicMock

    model = sentence_transformers.CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    assert isinstance(model, MagicMock), (
        "CrossEncoder should be patched to a mock in non-live tests"
    )


def test_cross_encoder_predict_returns_uniform_scores() -> None:
    """Mock predict() must return uniform float scores."""
    model = sentence_transformers.CrossEncoder("any-model")
    scores = model.predict([("query", "passage")])
    assert isinstance(scores, np.ndarray)
    assert scores.shape == (1,)
    assert all(isinstance(s, float) for s in scores.tolist())
