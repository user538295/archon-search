"""Text chunker: split a document into overlapping fixed-size chunks."""
from __future__ import annotations


def chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """Split *text* into overlapping chunks of at most *chunk_size* characters.

    Args:
        text: Input text to split.
        chunk_size: Maximum characters per chunk.
        overlap: Number of characters each chunk shares with the previous one.

    Returns:
        List of non-empty chunk strings.
    """
    if not text:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")

    step = chunk_size - overlap
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + chunk_size])
        start += step
    return chunks
