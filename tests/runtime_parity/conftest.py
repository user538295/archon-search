"""Shared fixtures of the runtime-parity suite (real weights, frozen fastembed reference outputs).

Every test here needs ``ARCHON_RUNTIME_PARITY_DATA_DIR``: a data directory whose ``models/``
holds the pinned snapshots. Missing snapshots are downloaded into it on first use and reused
afterwards (about 14 GB for the 19 embedders, 4.3 GB for the five rerankers). The root conftest points
``ARCHON_SEARCH_DATA_DIR`` at an empty per-test directory; ``parity_data_dir`` re-points it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tokenizers import Tokenizer

from archon_search.model_specs import EmbedderSpec, RerankerSpec
from archon_search.model_store import model_path
from archon_search.torch_runtime import TOKENIZER_FILE

PARITY_DATA_DIR_ENV = "ARCHON_RUNTIME_PARITY_DATA_DIR"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "runtime_parity"


def available_devices() -> list[str]:
    """Every torch device of this host, ``cpu`` first."""
    import torch

    devices = ["cpu"]
    if torch.backends.mps.is_available():
        devices.append("mps")
    if torch.cuda.is_available():
        devices.append("cuda")
    return devices


def untruncated_tokenizer(spec: EmbedderSpec | RerankerSpec) -> Tokenizer:
    """The cached snapshot's tokenizer of *spec* with truncation off, to count real token lengths.

    The snapshot must already be downloaded (a loaded model guarantees it).
    """
    snapshot = model_path(spec)
    assert snapshot is not None, f"{spec.name} is not in the parity data dir"
    tokenizer = Tokenizer.from_file(str(snapshot / TOKENIZER_FILE))
    tokenizer.no_padding()
    tokenizer.no_truncation()
    return tokenizer


@pytest.fixture(scope="module")
def parity_data_dir() -> Iterator[Path]:
    """The shared parity data directory, active for module-scoped model loads.

    Module-scoped fixtures run before the root conftest's per-test isolation, so the loads
    they trigger need the directory pinned here. Skips unless the suite is opted into.
    """
    data_dir = os.environ.get(PARITY_DATA_DIR_ENV)
    if not data_dir:
        pytest.skip(f"set {PARITY_DATA_DIR_ENV} to a data directory to run the runtime parity suite")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ARCHON_SEARCH_DATA_DIR", data_dir)
        yield Path(data_dir)


@pytest.fixture(autouse=True)
def _parity_data_dir_in_tests(
    parity_data_dir: Path, _archon_isolated_data_dir: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undo the root conftest's per-test data-dir isolation for the test body."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(parity_data_dir))


@pytest.fixture(scope="session")
def corpus() -> dict[str, Any]:
    """``corpus.json``: ``texts`` (27 strings) and ``pairs`` (14 query/document pairs)."""
    return json.loads((FIXTURE_DIR / "corpus.json").read_text())  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def embedder_golden() -> dict[str, np.ndarray]:
    """Frozen fastembed vectors, ``(27, dim)`` float32 per model id with ``/`` replaced by ``__``."""
    with np.load(FIXTURE_DIR / "embedder_golden.npz") as archive:
        return {key: archive[key] for key in archive.files}


@pytest.fixture(scope="session")
def reranker_golden() -> dict[str, list[float]]:
    """Frozen fastembed raw cross-encoder scores: 14 floats per reranker name, in ``corpus["pairs"]`` order."""
    return json.loads((FIXTURE_DIR / "reranker_golden.json").read_text())  # type: ignore[no-any-return]
