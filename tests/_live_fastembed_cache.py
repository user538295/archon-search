"""Shared fixtures for the real-model lanes (``tests/eval/live_benchmark/``, ``tests/eval/live/``).

One fastembed cache for every real load in a session (S493). The root conftest's
function-scoped ``_archon_isolated_data_dir`` points ``ARCHON_SEARCH_DATA_DIR`` at an
empty per-worker dir and deletes ``FASTEMBED_CACHE_PATH``, so without these fixtures
``get_fastembed_cache_dir()`` would resolve to that empty dir inside every test and
each session would re-download the weights — whatever cache CI restored or the
``live_benchmark`` guard checked. Import both fixtures into a lane's ``conftest.py``.

With ``FASTEMBED_CACHE_PATH`` unset the lanes therefore load from the developer's real
``<ARCHON_SEARCH_DATA_DIR>/models`` (default ``~/.archon-search/models``) — intended: one
shared persistent cache, the same one the smoke lane's ``_link_model_cache`` links.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.paths import get_fastembed_cache_dir


@pytest.fixture(scope="session")
def live_fastembed_cache_dir() -> Path:
    """The cache the backends load from, resolved from the shell env at session start.

    Session fixtures set up before the root conftest's function-scoped
    ``_archon_isolated_data_dir``, so this is the operator's / CI's real cache:
    ``$FASTEMBED_CACHE_PATH`` if exported, else ``<ARCHON_SEARCH_DATA_DIR>/models``.
    """
    return get_fastembed_cache_dir()


@pytest.fixture(autouse=True)
def _pin_live_fastembed_cache(
    _archon_isolated_data_dir: None,
    monkeypatch: pytest.MonkeyPatch,
    live_fastembed_cache_dir: Path,
) -> None:
    """Re-pin ``FASTEMBED_CACHE_PATH`` after the root fixture deletes it, so the backends
    load from the session's cache instead of downloading into the per-worker data dir."""
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(live_fastembed_cache_dir))
