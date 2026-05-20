"""Shared ML stub installer — patches sys.modules for fastembed before any test imports it.

Must be called at conftest module-level (before pytest discovers any test file) so that
sys.modules is patched before any import of archon_search or fastembed can trigger
ONNX model downloads or the HuggingFace-tokenizers Rust-library process explosion.

Idempotent: calling install_stubs() more than once is safe.
"""
from __future__ import annotations

import os
import sys
import types

import numpy as np

_STUBS_INSTALLED = False


def install_stubs() -> None:
    """Patch ML dependencies in sys.modules so tests don't download models."""
    global _STUBS_INSTALLED
    if _STUBS_INSTALLED:
        return

    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("RAYON_NUM_THREADS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    # --- Fake fastembed ---------------------------------------------------------
    if "fastembed" not in sys.modules:
        _fe = types.ModuleType("fastembed")

        class _FakeTextEmbedding:  # noqa: D101
            def __init__(self, model_name: str, **kwargs: object) -> None:
                self._model_name = model_name

            def embed(self, texts: list[str]):  # type: ignore[return]
                """Yields 1-D zero numpy arrays — matches real TextEmbedding.embed() contract."""
                for _ in texts:
                    yield np.zeros(384, dtype=np.float32)

        class _FakeTextCrossEncoder:  # noqa: D101
            def __init__(self, model_name: str, **kwargs: object) -> None:
                self._model_name = model_name

            def rerank(self, query: str, documents: object) -> list[float]:
                """Returns uniform 0.5 floats — plain list, NOT numpy."""
                return [0.5] * len(list(documents))  # type: ignore[arg-type]

        _fe.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
        _fe.TextCrossEncoder = _FakeTextCrossEncoder  # type: ignore[attr-defined]
        sys.modules["fastembed"] = _fe

    # Always ensure submodule paths are registered (independent of top-level guard).
    # Production code uses `from fastembed.rerank.cross_encoder import TextCrossEncoder`
    # which resolves via sys.modules["fastembed.rerank.cross_encoder"] directly.
    if "fastembed.rerank.cross_encoder" not in sys.modules:
        _fe_mod = sys.modules["fastembed"]
        _FakeEncoderClass = getattr(_fe_mod, "TextCrossEncoder")

        _fe_rerank = types.ModuleType("fastembed.rerank")
        _fe_cross_encoder = types.ModuleType("fastembed.rerank.cross_encoder")
        _fe_cross_encoder.TextCrossEncoder = _FakeEncoderClass  # type: ignore[attr-defined]

        sys.modules["fastembed.rerank"] = _fe_rerank
        sys.modules["fastembed.rerank.cross_encoder"] = _fe_cross_encoder

        _fe_mod.rerank = _fe_rerank  # type: ignore[attr-defined]
        _fe_rerank.cross_encoder = _fe_cross_encoder  # type: ignore[attr-defined]

    # Belt-and-braces: also block sentence_transformers
    if "sentence_transformers" not in sys.modules:
        sys.modules["sentence_transformers"] = types.ModuleType("sentence_transformers")

    # Block onnxruntime (64MB native library) — not needed for tests
    if "onnxruntime" not in sys.modules:
        sys.modules["onnxruntime"] = types.ModuleType("onnxruntime")

    _STUBS_INSTALLED = True
