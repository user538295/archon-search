"""tests/rag/test_conftest.py — verify that fastembed is safely patched."""
from __future__ import annotations


def test_fastembed_is_patched() -> None:
    """fastembed.TextEmbedding() must complete without network access."""
    import fastembed  # noqa: PLC0415

    emb = fastembed.TextEmbedding("any-model")
    vectors = list(emb.embed(["hello"]))
    assert len(vectors) == 1
    assert hasattr(vectors[0], "shape")  # numpy array


def test_textcrossencoder_is_patched() -> None:
    """fastembed.TextCrossEncoder().rerank() must return plain floats."""
    import fastembed  # noqa: PLC0415

    enc = fastembed.TextCrossEncoder("any-model")
    scores = enc.rerank("query", ["doc1", "doc2"])
    assert scores == [0.5, 0.5]


def test_sentence_transformers_is_blocked() -> None:
    """sentence_transformers must be a stub — not the real package."""
    import sentence_transformers  # noqa: PLC0415

    # Real package has SentenceTransformer class; our stub does not
    assert not hasattr(sentence_transformers, "SentenceTransformer")
