"""ModelEmbedder (PyTorch) against the frozen fastembed vectors, for every registry embedder on every available device.

Run (downloads the 19 embedders into the data dir on first use, ~11.3 GB; reuses them after)::

    ARCHON_RUNTIME_PARITY_DATA_DIR=/tmp/rt_data uv run pytest -m runtime_parity \\
        tests/runtime_parity/test_embedder_parity.py --no-cov -q -p no:cacheprovider -o addopts= -rP

``-o addopts=`` drops the default ``-n 8``/coverage/``-m`` filter; ``-rP`` prints the measured
worst cosine per model. The suite is excluded from the default run (``norecursedirs`` + ``-m``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from archon_search.embedder import ModelEmbedder
from archon_search.model_specs import EMBEDDER_SPECS, EmbedderSpec
from tests.runtime_parity.conftest import available_devices, untruncated_tokenizer

pytestmark = pytest.mark.runtime_parity

MIN_COSINE = 0.99994
NORM_RATIO_TOLERANCE = 1e-3
#: Batched and one-by-one runs differ only by float reduction order.
BATCHED_VS_SINGLE_MIN_COSINE = 0.99999

MODEL_NAMES = tuple(EMBEDDER_SPECS)


@dataclass(frozen=True)
class Case:
    spec: EmbedderSpec
    device: str
    embedder: ModelEmbedder

    @property
    def name(self) -> str:
        return self.spec.name


@pytest.fixture(
    scope="module",
    params=[(name, device) for name in MODEL_NAMES for device in available_devices()],
    ids=lambda p: f"{p[0].split('/')[-1]}-{p[1]}",
)
def case(request: pytest.FixtureRequest, parity_data_dir: Path) -> Iterator[Case]:
    """One loaded model per (model, device); pytest groups tests so each is loaded once."""
    name, device = request.param
    yield Case(EMBEDDER_SPECS[name], device, ModelEmbedder(name, device=device))


@pytest.fixture(scope="module")
def batched(case: Case, corpus: dict[str, Any]) -> np.ndarray:
    return np.array(case.embedder.encode(corpus["texts"]))


def _cosines(actual: np.ndarray, golden: np.ndarray) -> np.ndarray:
    return (actual * golden).sum(1) / (np.linalg.norm(actual, axis=1) * np.linalg.norm(golden, axis=1))


def _golden(golden: dict[str, np.ndarray], name: str) -> np.ndarray:
    return golden[name.replace("/", "__")]


def test_embedder_matches_golden(case: Case, batched: np.ndarray, embedder_golden: dict[str, np.ndarray]) -> None:
    golden = _golden(embedder_golden, case.name)
    cosines = _cosines(batched, golden)
    norm_ratios = np.linalg.norm(batched, axis=1) / np.linalg.norm(golden, axis=1)
    print(f"PARITY {case.name} {case.device}: min cosine {cosines.min():.7f}, norm ratio {norm_ratios.min():.6f}-{norm_ratios.max():.6f}")

    assert batched.shape == golden.shape
    assert batched.shape[1] == case.spec.dim
    assert cosines.min() >= MIN_COSINE
    assert np.abs(norm_ratios - 1).max() <= NORM_RATIO_TOLERANCE


def test_long_text_truncation_matches(
    case: Case, batched: np.ndarray, corpus: dict[str, Any], embedder_golden: dict[str, np.ndarray]
) -> None:
    tokenizer = untruncated_tokenizer(case.spec)
    indices = [i for i, text in enumerate(corpus["texts"]) if len(tokenizer.encode(text).ids) > case.spec.max_length]
    if not indices:
        pytest.skip(f"no corpus text exceeds {case.name}'s max_length of {case.spec.max_length} tokens")
    golden = _golden(embedder_golden, case.name)

    cosines = _cosines(batched[indices], golden[indices])

    assert cosines.min() >= MIN_COSINE


def test_one_by_one_equals_batched(case: Case, batched: np.ndarray, corpus: dict[str, Any]) -> None:
    single = np.array([case.embedder.encode([text])[0] for text in corpus["texts"]])

    assert _cosines(single, batched).min() >= BATCHED_VS_SINGLE_MIN_COSINE
