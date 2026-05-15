"""
packages/archon-search/tests/conftest.py — ML-model isolation and shared store fixture.

ALL sys.modules injections run at module level (import time), before pytest
discovers any test file.  This prevents ONNX model downloads and the
HuggingFace-tokenizers Rust-library process explosion.
"""
from __future__ import annotations

import os
import sys
import types
import uuid

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _search_stubs_shim import install_stubs  # noqa: E402

install_stubs()

# Fixed test API key injected into all tests so create_app() uses a known key.
TEST_API_KEY = "0" * 64
os.environ["ARCHON_SEARCH_API_KEY"] = TEST_API_KEY

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

# Always ensure submodule paths are registered (independent of top-level guard).
# Production code uses `from fastembed.rerank.cross_encoder import TextCrossEncoder`
# which resolves via sys.modules["fastembed.rerank.cross_encoder"] directly.
if "fastembed.rerank.cross_encoder" not in sys.modules:
    _fe_mod = sys.modules["fastembed"]

    # Look up the fake class from the registered fastembed module
    _FakeEncoderClass = getattr(_fe_mod, "TextCrossEncoder")

    _fe_rerank = types.ModuleType("fastembed.rerank")
    _fe_cross_encoder = types.ModuleType("fastembed.rerank.cross_encoder")
    _fe_cross_encoder.TextCrossEncoder = _FakeEncoderClass  # type: ignore[attr-defined]

    # Register in sys.modules
    sys.modules["fastembed.rerank"] = _fe_rerank
    sys.modules["fastembed.rerank.cross_encoder"] = _fe_cross_encoder

    # Link as attributes on parent modules for dotted-access compatibility
    _fe_mod.rerank = _fe_rerank  # type: ignore[attr-defined]
    _fe_rerank.cross_encoder = _fe_cross_encoder  # type: ignore[attr-defined]

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
    """One shared SearchStore per test module (sync connect/disconnect via asyncio.run).

    LanceDB's Rust/Tokio runtime is independent of the Python asyncio event loop,
    so the connected store is safely reusable across test-function event loops.
    """
    import asyncio

    from archon_search.store import SearchStore

    tmp_path = tmp_path_factory.mktemp("rag_db")
    store = SearchStore(tmp_path)
    asyncio.run(store.connect())
    yield store
    asyncio.run(store.disconnect())


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer auth headers using the test API key."""
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
def col_name() -> str:
    """Unique LanceDB collection name per test (avoids cross-test pollution)."""
    return f"test-{uuid.uuid4().hex[:8]}"
