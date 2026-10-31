"""Stub tokenizer for tests that need a token counter without the gpt2 vocabulary (BE-3, later BE-4 sizing tests).

``StubTokenizer`` stands in for ``tokenizers.Tokenizer``: ``encode`` returns an object with the ``ids`` the real
encoding carries. One token per whitespace-separated word, so a test states the expected count by counting words.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StubEncoding:
    ids: list[int]


@dataclass
class StubTokenizer:
    """One token per whitespace-separated word; records every ``encode`` call."""

    calls: list[str] = field(default_factory=list)
    add_special_tokens_seen: list[bool] = field(default_factory=list)

    def encode(self, text: str, add_special_tokens: bool = True) -> StubEncoding:
        self.calls.append(text)
        self.add_special_tokens_seen.append(add_special_tokens)
        return StubEncoding(ids=list(range(len(text.split()))))
