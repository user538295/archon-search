"""
tests/rag/conftest.py — ML-model isolation and shared store fixture.

ALL sys.modules injections run at module level (import time), before pytest
discovers any test file.  This prevents ONNX model downloads and the
HuggingFace-tokenizers Rust-library process explosion.
"""
from __future__ import annotations

import os
import sys
import types
import uuid

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("RAYON_NUM_THREADS", "1")

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Module-level: inject fake ML modules BEFORE anything else imports them.
# ---------------------------------------------------------------------------
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

# Belt-and-braces: also block sentence_transformers
if "sentence_transformers" not in sys.modules:
    sys.modules["sentence_transformers"] = types.ModuleType("sentence_transformers")

# Block onnxruntime (64MB native library) — not needed for tests
if "onnxruntime" not in sys.modules:
    sys.modules["onnxruntime"] = types.ModuleType("onnxruntime")

# ---------------------------------------------------------------------------
# Module-scoped store fixture — one LanceDB connection per test module to
# avoid spawning a new Tokio thread pool for every test.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def connected_store(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """One shared RagStore per test module (sync connect/disconnect via asyncio.run).

    LanceDB's Rust/Tokio runtime is independent of the Python asyncio event loop,
    so the connected store is safely reusable across test-function event loops.
    """
    import asyncio

    from archon.search.store import RagStore

    tmp_path = tmp_path_factory.mktemp("rag_db")
    store = RagStore(tmp_path)
    asyncio.run(store.connect())
    yield store
    asyncio.run(store.disconnect())


@pytest.fixture
def col_name() -> str:
    """Unique LanceDB collection name per test (avoids cross-test pollution)."""
    return f"test-{uuid.uuid4().hex[:8]}"
