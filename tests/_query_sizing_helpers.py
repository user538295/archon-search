"""Shared helpers of the query-sizing tests (``llm_client.fit``, HyDE and RAG Fusion sizing)."""
from __future__ import annotations

from types import SimpleNamespace

#: A fitted text uses at least this share (percent) of its budget: the helper's own acceptance rule, restated here.
ACCEPTED_FILL_PERCENT = 90
#: ``OLLAMA_ESTIMATE_MARGIN``: real tokens may be this much more than the estimate. Restated apart from the code under test.
LARGEST_REAL_OVER_ESTIMATE = 1.2


def words(count: int, prefix: str = "word") -> str:
    """*count* distinct space-separated words."""
    return " ".join(f"{prefix}{index}" for index in range(count))


def per_character_cost(text: str) -> int:
    """One token per non-space character: how a CJK text counts."""
    return sum(1 for character in text if not character.isspace())


class PerCharacterTokenizer:
    """A ``TokenCounter`` tokenizer with :func:`per_character_cost`."""

    def encode(self, text: str, add_special_tokens: bool = True) -> SimpleNamespace:
        return SimpleNamespace(ids=[0] * per_character_cost(text))
