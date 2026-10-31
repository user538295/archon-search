"""gpt2-based token counter that sizes ``ollama`` prompts ahead of the call (contract C1, scenario S11).

The count is an estimate: Ollama's real tokenizer is the model's own, so the caller adds
``OLLAMA_ESTIMATE_MARGIN`` on top. The tokenizer is the pinned ``tokenizer.json`` of
``paths.TOKEN_COUNTER_MODEL_NAME`` at ``TOKEN_COUNTER_MODEL_REVISION``, fetched through
``model_store.ensure_pinned_snapshot`` (``install/prewarm.py`` and the image build fetch it ahead of time).

``tokenizers`` and ``huggingface_hub`` are imported inside :func:`load_gpt2_tokenizer`, and a counter loads its
tokenizer on the first :meth:`TokenCounter.count`, so importing this module (and the CLI that reaches it) costs
neither the import nor a download.
"""

from __future__ import annotations

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
    """Counts gpt2 tokens in a text; the tokenizer loads once, on first use, and is shared across threads.

    ``load_tokenizer`` defaults to :func:`load_gpt2_tokenizer`, looked up when the counter is built (so a test
    can replace it). The first ``count`` may download the tokenizer and block for that time: callers on an event
    loop run it in a thread. A failed load is raised and not cached, so the next ``count`` tries again.
    """

    def __init__(self, load_tokenizer: Callable[[], Tokenizer] | None = None) -> None:
        self._load_tokenizer = load_tokenizer if load_tokenizer is not None else load_gpt2_tokenizer
        self._tokenizer: Tokenizer | None = None
        self._lock = threading.Lock()

    def count(self, text: str) -> int:
        return len(self._get_tokenizer().encode(text, add_special_tokens=False).ids)

    def _get_tokenizer(self) -> Tokenizer:
        with self._lock:
            if self._tokenizer is None:
                self._tokenizer = self._load_tokenizer()
            return self._tokenizer
