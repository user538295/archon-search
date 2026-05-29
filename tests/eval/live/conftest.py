"""Isolated conftest for live-eval tests.

Shadows the parent conftest's autouse fixture so that
ARCHON_SEARCH_EVAL_BACKENDS is never set to "1" for tests in this
directory.  Live tests must use real model backends.
"""
from __future__ import annotations

from pathlib import Path

import pytest


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
