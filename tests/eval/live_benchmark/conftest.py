"""Isolated conftest for live_benchmark tests.

Module-level operations (before any fixture fires):
1. Remove fastembed stubs from sys.modules so real package is used.
2. Reset ML thread-count env vars to production defaults.

These run at module load time so that when pytest imports the test file,
real fastembed/ONNX code paths are active.

Exclusion from the default ``uv run pytest`` run is enforced at two levels:
1. ``norecursedirs = ["tests/eval/live_benchmark"]`` in pyproject.toml prevents
   pytest from auto-traversing this directory, so this conftest is never imported
   unless the path is passed explicitly on the command line.
2. ``-m "not live_benchmark"`` in ``addopts`` is a secondary guard that filters
   out any test items that do get collected with the marker.

The ``_require_model_cache`` fixture below is defense-in-depth for the
explicit-path case (skips gracefully when the fastembed model cache is absent). It
checks ``get_fastembed_cache_dir()`` as resolved at session start, and
``_pin_live_fastembed_cache`` (``tests/_live_fastembed_cache.py``) makes every test's
backends load from that same dir.
"""
from __future__ import annotations

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Module-level: remove fastembed stubs so real package is resolved
# ---------------------------------------------------------------------------
for _key in ("fastembed", "fastembed.rerank", "fastembed.rerank.cross_encoder"):
    sys.modules.pop(_key, None)

# ---------------------------------------------------------------------------
# Module-level: reset ML thread-count env vars to production defaults
# ---------------------------------------------------------------------------
_cpu = str(os.cpu_count() or 4)
for _var in (
    "ORT_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "RAYON_NUM_THREADS",
):
    os.environ[_var] = _cpu
os.environ.pop("TOKENIZERS_PARALLELISM", None)


# ---------------------------------------------------------------------------
# Fixture: shadow parent _activate_deterministic_eval_backends
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _activate_deterministic_eval_backends() -> None:
    pass  # shadow parent (function-scoped); live_benchmark must NOT activate deterministic stubs


# ---------------------------------------------------------------------------
# Fixtures: one fastembed cache for the guard AND the backends (S493)
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from tests._live_fastembed_cache import (  # noqa: E402, F401 — fixtures, used by name
    _pin_live_fastembed_cache,
    live_fastembed_cache_dir,
)


@pytest.fixture(autouse=True, scope="session")
def _require_model_cache(live_fastembed_cache_dir: Path) -> None:  # noqa: F811 — fixture param
    missing = []
    if not any(live_fastembed_cache_dir.glob("*bge-small-en-v1.5*")):
        missing.append("BAAI/bge-small-en-v1.5")
    if not any(live_fastembed_cache_dir.glob("*ms-marco-MiniLM-L-6-v2*")):
        missing.append("Xenova/ms-marco-MiniLM-L-6-v2")
    if missing:
        pytest.skip(
            f"fastembed model cache {live_fastembed_cache_dir} lacks {missing!r} — "
            "run the CI prefetch step first (or export FASTEMBED_CACHE_PATH)"
        )
