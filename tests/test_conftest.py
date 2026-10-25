"""packages/archon-search/tests/test_conftest.py — verify that the model-backend stub in conftest.py works correctly."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from archon_search.embedder import ModelEmbedder
from archon_search.model_specs import UnsupportedModelError
from archon_search.reranker import ModelReranker

_BGE_SMALL = "BAAI/bge-small-en-v1.5"
_BGE_SMALL_DIM = 384
_E5_LARGE = "intfloat/multilingual-e5-large"
_E5_LARGE_DIM = 1024
_RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"
_PROBE_FILE = Path(__file__).parent / "_torch_free_probe.py"
_PROBE_TIMEOUT_SECONDS = 120


def test_model_embedder_returns_zero_vectors_of_the_spec_dimension() -> None:
    assert ModelEmbedder(_BGE_SMALL).encode(["a", "b"]) == [[0.0] * _BGE_SMALL_DIM] * 2
    assert ModelEmbedder(_E5_LARGE).encode(["a"]) == [[0.0] * _E5_LARGE_DIM]


def test_model_embedder_encodes_empty_input_to_nothing() -> None:
    assert ModelEmbedder(_BGE_SMALL).encode([]) == []


def test_model_embedder_is_warm_only_after_its_first_encode() -> None:
    embedder = ModelEmbedder(_BGE_SMALL)
    assert embedder.is_warm is False

    embedder.encode(["a"])

    assert embedder.is_warm is True


def test_model_reranker_scores_every_pair_0_5() -> None:
    assert ModelReranker(_RERANKER).predict([("q", "d1"), ("q", "d2")]) == [0.5, 0.5]


def test_model_reranker_scores_empty_input_to_nothing() -> None:
    assert ModelReranker(_RERANKER).predict([]) == []


def test_model_reranker_is_warm_only_after_its_first_predict() -> None:
    reranker = ModelReranker(_RERANKER)
    assert reranker.is_warm is False

    reranker.predict([("q", "d")])

    assert reranker.is_warm is True


def test_the_stub_still_rejects_a_model_outside_the_registry() -> None:
    with pytest.raises(UnsupportedModelError):
        ModelEmbedder("custom/unknown-model")
    with pytest.raises(UnsupportedModelError):
        ModelReranker("custom/unknown-model")


def test_warming_stubbed_backends_keeps_torch_unimported() -> None:
    """A fresh interpreter: the stub must hold before any test of the worker could import torch."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(_PROBE_FILE), "-o", "addopts=", "-p", "no:cacheprovider", "-q"],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
