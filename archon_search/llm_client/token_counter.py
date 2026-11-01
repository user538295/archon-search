"""Token counter that sizes ``ollama`` prompts ahead of the call (contract C1, scenario S11).

The count is a conservative estimate built on the gpt2 vocabulary: Ollama's real tokenizer is the model's own, so
the caller adds ``OLLAMA_ESTIMATE_MARGIN`` on top. Plain gpt2 counts stay inside that margin on prose, code and
non-Latin scripts, but two kinds of text break it (T-2 manual run on four real Ollama models of four tokenizer
families: llama3.1 8b, qwen2.5, gemma3, tinyllama): digits (per-digit tokenizers cost up to 1.68x the gpt2 count on
digit tables, 1.9x on single digits separated by spaces, 2.8x on two-digit numbers) and characters outside the Basic
Multilingual Plane such as emoji (byte-fallback tokenizers cost up to 1.55x). :meth:`TokenCounter.count` therefore
counts every ASCII digit as one token (plus one per digit run) and every such character as its four UTF-8 bytes; the
rest of the text is counted by gpt2. The real count of every measured text class then stays within 1.2x this
estimate (worst measured: 1.15, digit-free English prose on tinyllama, a Llama 2 sentencepiece model).

The tokenizer is the pinned ``tokenizer.json`` of
``paths.TOKEN_COUNTER_MODEL_NAME`` at ``TOKEN_COUNTER_MODEL_REVISION``, fetched through
``model_store.ensure_pinned_snapshot`` (``install/prewarm.py`` and the image build fetch it ahead of time).

``tokenizers`` and ``huggingface_hub`` are imported inside :func:`load_gpt2_tokenizer`, and a counter loads its
tokenizer on the first :meth:`TokenCounter.count`, so importing this module (and the CLI that reaches it) costs
neither the import nor a download.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from archon_search.paths import (
    TOKEN_COUNTER_MODEL_FILES,
    TOKEN_COUNTER_MODEL_NAME,
    TOKEN_COUNTER_MODEL_REVISION,
    TOKEN_COUNTER_TOKENIZER_FILE,
    get_token_counter_models_dir,
)

# Every ASCII digit costs one token on a per-digit tokenizer (qwen, gemma, llama 2), and a digit run is separated from the
# space before it (gpt2 merges " 7" into one token, they spend two). A digit run is therefore collapsed to one placeholder
# digit for gpt2 and all its digits are added: the run costs its length plus one, the most such a tokenizer spends.
# Non-ASCII digits stay in the gpt2 text (they are several UTF-8 bytes each, which gpt2 already counts).
_DIGIT_RUN = re.compile(r"[0-9]+")

# Characters outside the Basic Multilingual Plane (emoji and rare symbols) are removed from the gpt2 text and
# counted as their four UTF-8 bytes, the most a byte-fallback tokenizer spends on one.
_ASTRAL_CHARACTER = re.compile("[\U00010000-\U0010ffff]")
_ASTRAL_CHARACTER_TOKENS = 4


class _Encoding(Protocol):
    @property
    def ids(self) -> Sequence[int]: ...


class Tokenizer(Protocol):
    """The slice of ``tokenizers.Tokenizer`` the counter uses."""

    def encode(self, text: str, /, add_special_tokens: bool = ...) -> _Encoding: ...


def load_gpt2_tokenizer() -> Tokenizer:
    """Return the pinned gpt2 tokenizer, downloading its one file when the cache lacks it.

    Raises:
        ModelDownloadError: on an offline miss, a failed download or an incomplete snapshot.
    """
    from tokenizers import Tokenizer as HubTokenizer

    from archon_search.model_store import ensure_pinned_snapshot

    snapshot = ensure_pinned_snapshot(
        TOKEN_COUNTER_MODEL_NAME,
        TOKEN_COUNTER_MODEL_REVISION,
        TOKEN_COUNTER_MODEL_FILES,
        get_token_counter_models_dir(),
    )
    return HubTokenizer.from_file(str(snapshot / TOKEN_COUNTER_TOKENIZER_FILE))


class TokenCounter:
    """Estimates the tokens in a text; the tokenizer loads once, on first use, and is shared across threads.

    ``load_tokenizer`` defaults to :func:`load_gpt2_tokenizer`, looked up when the counter is built (so a test
    can replace it). The first ``count`` may download the tokenizer and block for that time: callers on an event
    loop run it in a thread. A failed load is raised and not cached, so the next ``count`` tries again.
    """

    def __init__(self, load_tokenizer: Callable[[], Tokenizer] | None = None) -> None:
        self._load_tokenizer = load_tokenizer if load_tokenizer is not None else load_gpt2_tokenizer
        self._tokenizer: Tokenizer | None = None
        self._lock = threading.Lock()

    def count(self, text: str) -> int:
        """Conservative token estimate of *text* (see the module docstring): gpt2 plus digit and astral-plane surcharges."""
        digits = sum(len(run) for run in _DIGIT_RUN.findall(text))
        astral_characters = len(_ASTRAL_CHARACTER.findall(text))
        gpt2_text = _ASTRAL_CHARACTER.sub("", _DIGIT_RUN.sub("0", text))
        gpt2_tokens = len(self._get_tokenizer().encode(gpt2_text, add_special_tokens=False).ids)
        return gpt2_tokens + digits + _ASTRAL_CHARACTER_TOKENS * astral_characters

    @staticmethod
    def ceiling(text: str) -> int:
        """An upper bound of :meth:`count` that needs no tokenizer: the UTF-8 length plus one per digit run.

        gpt2 is byte-level, so a token covers at least one byte and the gpt2 part is at most the bytes of the text it is
        given; an astral character is charged its four bytes (as many as it has); a digit run of N digits is charged N plus
        one placeholder that stands for N bytes already counted. Hence ``count(text) <= len(utf8) + digit runs``.
        """
        return len(text.encode("utf-8", "surrogatepass")) + len(_DIGIT_RUN.findall(text))

    def _get_tokenizer(self) -> Tokenizer:
        with self._lock:
            if self._tokenizer is None:
                self._tokenizer = self._load_tokenizer()
            return self._tokenizer
