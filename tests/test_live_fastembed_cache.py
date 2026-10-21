"""Default-suite guard for ``tests/_live_fastembed_cache.py`` (S493).

Both real-model lanes (``tests/eval/live/``, ``tests/eval/live_benchmark/``) are excluded
from the default run, so a conftest that stops importing the re-pin fixture would only
show up as a silent re-download in a lane nobody runs locally. This module checks the
imports structurally and exercises the fixtures themselves here.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from archon_search.paths import get_fastembed_cache_dir
from tests._live_fastembed_cache import (  # noqa: F401 — fixtures, used by name
    _pin_live_fastembed_cache,
    live_fastembed_cache_dir,
)

_TESTS_DIR = Path(__file__).parent
_HELPER_MODULE = "tests._live_fastembed_cache"
_REQUIRED_FIXTURES = {"_pin_live_fastembed_cache", "live_fastembed_cache_dir"}


def _names_imported_from_helper(conftest: Path) -> set[str]:
    tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == _HELPER_MODULE
        for alias in node.names
    }


@pytest.mark.parametrize("lane", ["live", "live_benchmark"])
def test_real_model_lane_conftest_imports_the_cache_pin(lane: str) -> None:
    conftest = _TESTS_DIR / "eval" / lane / "conftest.py"

    assert _names_imported_from_helper(conftest) >= _REQUIRED_FIXTURES


def test_pin_restores_the_session_cache_after_the_root_isolation(
    live_fastembed_cache_dir: Path,  # noqa: F811 — pytest injects the imported fixture by name
) -> None:
    """The root ``_archon_isolated_data_dir`` deletes FASTEMBED_CACHE_PATH and points
    the data dir at an empty per-worker dir; the autouse pin re-sets it afterwards, so
    the backends resolve the session's cache rather than that empty dir."""
    assert os.environ["FASTEMBED_CACHE_PATH"] == str(live_fastembed_cache_dir)
    assert get_fastembed_cache_dir() == live_fastembed_cache_dir
