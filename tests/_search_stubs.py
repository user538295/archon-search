"""Shared ML stub installer — keeps the default suite free of model downloads and heavy imports.

Must be called at conftest module-level (before pytest discovers any test file) so the thread
env defaults and the chonkie stub are in place before any import of archon_search triggers a
tokenizer download or the HuggingFace-tokenizers Rust-library process explosion.

The PyTorch embedder/reranker are NOT stubbed here: ``tests/conftest.py`` patches
``ModelEmbedder.encode`` / ``ModelReranker.predict`` per test (``_stub_model_backends``), so no
torch, transformers or weights are touched unless a test opts out with ``real_torch_backends``.

Idempotent: calling install_stubs() more than once is safe.
"""
from __future__ import annotations

import os
import sys
import types

_STUBS_INSTALLED = False


def install_stubs() -> None:
    """Set thread env defaults and stub chonkie so tests don't download a tokenizer."""
    global _STUBS_INSTALLED
    if _STUBS_INSTALLED:
        return

    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("RAYON_NUM_THREADS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    # Stub chonkie: real RecursiveChunker downloads a gpt2 tokenizer over the network, which
    # fails in offline test environments. The fake splits on whitespace into <= chunk_size word
    # groups and reports token_count as the word count, preserving the size contract that
    # DocumentChunker and its tests depend on (multiple chunks for long text, bounded size).
    if "chonkie" not in sys.modules:
        _chonkie = types.ModuleType("chonkie")

        class _FakeChunk:  # noqa: D101
            def __init__(self, text: str, token_count: int, start_index: int, end_index: int) -> None:
                self.text = text
                self.token_count = token_count
                self.start_index = start_index
                self.end_index = end_index

        class _FakeRecursiveChunker:  # noqa: D101
            def __init__(self, tokenizer: str = "gpt2", chunk_size: int = 512, **kwargs: object) -> None:
                self._chunk_size = max(1, chunk_size)

            def chunk(self, text: str):  # type: ignore[return]
                import re as _re
                # Split on whitespace, but track the actual byte offsets in `text`
                # so that start_index/end_index point into the original string.
                tokens = list(_re.finditer(r"\S+", text))
                if not tokens:
                    return []
                size = self._chunk_size
                chunks = []
                for i in range(0, len(tokens), size):
                    group = tokens[i : i + size]
                    start = group[0].start()
                    end = group[-1].end()
                    chunk_text = text[start:end]
                    chunks.append(
                        _FakeChunk(chunk_text, token_count=len(group), start_index=start, end_index=end)
                    )
                return chunks

        _chonkie.RecursiveChunker = _FakeRecursiveChunker  # type: ignore[attr-defined]
        sys.modules["chonkie"] = _chonkie

    _STUBS_INSTALLED = True
