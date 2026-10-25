"""Inner probe for tests/test_conftest.py: run in a child pytest, never collected by the default suite.

Its file name does not match ``test_*.py``, so only an explicit path runs it. It proves the autouse
``_stub_model_backends`` fixture keeps a constructed-and-warmed ``Embedder`` and ``Reranker`` free
of PyTorch: ``import torch`` must not have happened in the worker.
"""
from __future__ import annotations

import sys

from archon_search.embedder import make_embedder
from archon_search.reranker import make_reranker


async def test_warming_stubbed_backends_does_not_import_torch() -> None:
    embedder = make_embedder("BAAI/bge-small-en-v1.5")
    reranker = make_reranker("Xenova/ms-marco-MiniLM-L-6-v2")

    await embedder.warmup()
    await reranker.warmup()

    assert embedder.is_warm
    assert reranker.is_warm
    assert "torch" not in sys.modules
    assert "transformers" not in sys.modules
