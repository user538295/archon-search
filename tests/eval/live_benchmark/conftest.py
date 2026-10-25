"""Isolated conftest for live_benchmark tests.

Module-level operation (before any fixture fires): reset ML thread-count env vars to
production defaults, so the real PyTorch CPU code path runs as a server would run it.

Exclusion from the default ``uv run pytest`` run is enforced at two levels:
1. ``norecursedirs = ["tests/eval/live_benchmark"]`` in pyproject.toml prevents
   pytest from auto-traversing this directory, so this conftest is never imported
   unless the path is passed explicitly on the command line.
2. ``-m "not live_benchmark"`` in ``addopts`` is a secondary guard that filters
   out any test items that do get collected with the marker.

The ``_require_model_cache`` fixture below is defense-in-depth for the
explicit-path case (skips gracefully when the model cache is absent). It checks
the models dir as resolved at session start, and ``_pin_live_models_dir``
(``tests/_live_model_cache.py``) makes every test's backends load from that same dir.
"""
from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Module-level: reset ML thread-count env vars to production defaults
# ---------------------------------------------------------------------------
_cpu = str(os.cpu_count() or 4)
for _var in (
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
# Fixtures: one model cache for the guard AND the backends
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from tests._live_model_cache import (  # noqa: E402, F401 — fixtures, used by name
    _pin_live_models_dir,
    live_models_dir,
    missing_live_models,
)


@pytest.fixture(autouse=True, scope="session")
def _require_model_cache(live_models_dir: Path) -> None:  # noqa: F811 — fixture param
    """Skip the session unless both models are cached at their pinned revisions.

    The guard runs before the per-test pin, so it checks the session's models dir itself.
    """
    missing = missing_live_models()
    if missing:
        pytest.skip(
            f"model cache {live_models_dir} lacks {missing!r} at the pinned revisions — "
            "run the CI prefetch step first (python -m tests._live_model_cache)"
        )
