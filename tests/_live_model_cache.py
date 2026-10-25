"""Shared fixtures and prefetch for the real-model lanes (``tests/eval/live_benchmark/``, ``tests/eval/live/``).

One model cache for every real load in a session. The root conftest's function-scoped
``_archon_isolated_data_dir`` points ``ARCHON_SEARCH_DATA_DIR`` at an empty per-worker dir, and
``get_models_dir()`` (where ``archon_search.model_store`` looks) hangs off it, so without these
fixtures every test would resolve an empty cache and download the weights again — whatever
cache CI restored or the ``live_benchmark`` guard checked. Import both fixtures into a lane's
``conftest.py``.

The lanes therefore load from the developer's real ``<ARCHON_SEARCH_DATA_DIR>/models`` (default
``~/.archon-search/models``) — intended: one shared persistent cache, the same one the smoke
lane's ``_link_model_cache`` links — through a symlink that keeps the rest of the isolated
data dir private.

``python -m tests._live_model_cache`` downloads the lanes' models (CI's prefetch step).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.model_specs import (
    EmbedderSpec,
    RerankerSpec,
    get_embedder_spec,
    get_reranker_spec,
)
from archon_search.model_store import ensure_model, model_path
from archon_search.paths import get_models_dir

LIVE_EMBEDDER = "BAAI/bge-small-en-v1.5"
LIVE_RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"


def live_specs() -> tuple[EmbedderSpec, RerankerSpec]:
    """The embedder and reranker both real-model lanes run."""
    return get_embedder_spec(LIVE_EMBEDDER), get_reranker_spec(LIVE_RERANKER)


def missing_live_models() -> list[str]:
    """Hub repos of the live specs that have no complete snapshot at the pinned revision."""
    return [spec.repo for spec in live_specs() if model_path(spec) is None]


def prefetch_live_models() -> None:
    """Download the live specs' pinned snapshots into the models dir (no-op when cached)."""
    for spec in live_specs():
        ensure_model(spec)


def pin_models_dir(live_models_dir: Path) -> None:
    """Make ``get_models_dir()`` resolve to ``live_models_dir`` by linking ``<data dir>/models`` at it."""
    live_models_dir.mkdir(parents=True, exist_ok=True)
    link = get_models_dir()
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        link.rmdir()  # fails loudly if something already stored files in the isolated dir
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(live_models_dir, target_is_directory=True)


@pytest.fixture(scope="session")
def live_models_dir() -> Path:
    """The cache the backends load from, resolved from the shell env at session start.

    Session fixtures set up before the root conftest's function-scoped
    ``_archon_isolated_data_dir``, so this is the operator's / CI's real
    ``<ARCHON_SEARCH_DATA_DIR>/models``.
    """
    return get_models_dir()


@pytest.fixture(autouse=True)
def _pin_live_models_dir(_archon_isolated_data_dir: None, live_models_dir: Path) -> None:
    """Re-link the isolated data dir's ``models/`` at the session's cache, so the backends
    load from it instead of downloading into the per-worker data dir."""
    pin_models_dir(live_models_dir)


if __name__ == "__main__":
    prefetch_live_models()
    print("Live-lane models prefetched")
