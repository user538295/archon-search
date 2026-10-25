"""ModelReranker (PyTorch) against the frozen fastembed scores, for every registry reranker on every available device.

Run (downloads the five rerankers into the data dir on first use, ~4.2 GB; reuses them after)::

    ARCHON_RUNTIME_PARITY_DATA_DIR=/tmp/rt_data uv run pytest -m runtime_parity \\
        tests/runtime_parity/test_reranker_parity.py --no-cov -q -p no:cacheprovider -o addopts= -rP

``-o addopts=`` drops the default ``-n 8``/coverage/``-m`` filter; ``-rP`` prints the measured
worst |score - golden| and the 14-pair timing per model and device. The suite is excluded from
the default run (``norecursedirs`` + ``-m``).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from archon_search.model_specs import RERANKER_SPECS, RerankerSpec
from archon_search.reranker import ModelReranker
from tests.runtime_parity.conftest import available_devices, untruncated_tokenizer

pytestmark = pytest.mark.runtime_parity

MAX_SCORE_DIFFERENCE = 1e-4
#: Batched and one-pair runs differ only by float reduction order.
SINGLE_VS_BATCHED_TOLERANCE = 1e-4

MODEL_NAMES = tuple(RERANKER_SPECS)


@dataclass(frozen=True)
class Case:
    spec: RerankerSpec
    device: str
    reranker: ModelReranker

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass(frozen=True)
class Scored:
    scores: np.ndarray
    seconds: float


@pytest.fixture(
    scope="module",
    params=[(name, device) for name in MODEL_NAMES for device in available_devices()],
    ids=lambda p: f"{p[0].split('/')[-1]}-{p[1]}",
)
def case(request: pytest.FixtureRequest, parity_data_dir: Path) -> Iterator[Case]:
    """One loaded model per (model, device); pytest groups tests so each is loaded once."""
    name, device = request.param
    reranker = ModelReranker(name, device=device)
    reranker.predict([("warmup", "warmup")])  # download, load and first-kernel costs stay out of the timings
    yield Case(RERANKER_SPECS[name], device, reranker)


@pytest.fixture(scope="module")
def batched(case: Case, corpus: dict[str, Any]) -> Scored:
    pairs = [tuple(pair) for pair in corpus["pairs"]]
    started = time.perf_counter()
    scores = case.reranker.predict(pairs)
    return Scored(np.array(scores), time.perf_counter() - started)


def test_reranker_matches_golden(case: Case, batched: Scored, reranker_golden: dict[str, list[float]]) -> None:
    golden = np.array(reranker_golden[case.name])
    worst = np.abs(batched.scores - golden).max()
    print(f"{case.name} [{case.device}]: max|diff| {worst:.2e}, 14 pairs in {batched.seconds:.3f}s")

    assert batched.scores.shape == golden.shape == (14,)
    assert worst < MAX_SCORE_DIFFERENCE


def test_single_pair_equals_batched(case: Case, batched: Scored, corpus: dict[str, Any]) -> None:
    single = np.array([case.reranker.predict([tuple(pair)])[0] for pair in corpus["pairs"]])

    assert np.abs(single - batched.scores).max() < SINGLE_VS_BATCHED_TOLERANCE


def test_long_pair_truncation_matches_golden(
    case: Case, batched: Scored, corpus: dict[str, Any], reranker_golden: dict[str, list[float]]
) -> None:
    tokenizer = untruncated_tokenizer(case.spec)
    indices = [
        i for i, (query, document) in enumerate(corpus["pairs"]) if len(tokenizer.encode(query, document).ids) > case.spec.max_length
    ]
    if not indices:
        pytest.skip(f"no corpus pair exceeds {case.name}'s max_length of {case.spec.max_length} tokens")
    golden = np.array(reranker_golden[case.name])

    for index in indices:
        assert abs(batched.scores[index] - golden[index]) < MAX_SCORE_DIFFERENCE, index
