"""Isolated conftest for live-eval tests.

Shadows the parent conftest's autouse fixture so that
ARCHON_SEARCH_EVAL_BACKENDS is never set to "1" for tests in this
directory.  Live tests must use real model backends.

Imports the shared model-cache fixtures (``tests/_live_model_cache.py``) so the real
models load from the session's cache — the one CI restores — not from the root conftest's
empty per-worker data dir. The root conftest's autouse stubs of ``ModelEmbedder.encode`` /
``ModelReranker.predict`` skip tests marked ``live_eval``, so the real PyTorch backends run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._live_model_cache import (  # noqa: F401 — fixtures, used by name
    _pin_live_models_dir,
    live_models_dir,
)


@pytest.fixture(autouse=True)
def _activate_deterministic_eval_backends() -> None:
    pass  # shadows parent; live tests must not activate deterministic stubs


@pytest.fixture(scope="session")
def live_corpus_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def live_runtime_cfg_path(live_corpus_root: Path) -> Path:
    return live_corpus_root / "runtime.toml"


@pytest.fixture(scope="session")
def live_thresholds_path(live_corpus_root: Path) -> Path:
    return live_corpus_root / "live_thresholds.toml"


@pytest.fixture()
def live_artifacts_dir(live_corpus_root: Path) -> Path:
    path = live_corpus_root / "live_baselines" / "_artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path
