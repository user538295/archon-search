"""Cold-start concurrency on the Apple GPU (real weights, ``mps`` hosts only).

Measured on an M4 Pro (PyTorch 2.13): two models loading and running their first inference on
``mps`` from two threads at once hang forever (3 of 3 runs), while concurrent steady-state
inference and loads serialized by one lock never did. A server start does exactly this: the global
embedder warm-up, the reranker warm-up and the startup validation probe load models from different
worker threads. Run with ``ARCHON_RUNTIME_PARITY_DATA_DIR=<dir with the bge-small weights>
uv run pytest -m runtime_parity tests/runtime_parity/test_mps_concurrency.py -o addopts= --no-cov``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.runtime_parity

_HANG_LIMIT_SECONDS = 90
_ATTEMPTS = 3
_COLD_START_SCRIPT = textwrap.dedent(
    """
    import threading

    from archon_search.embedder import ModelEmbedder

    def cold_start() -> None:
        ModelEmbedder("BAAI/bge-small-en-v1.5", device="mps").encode(["warmup"])

    threads = [threading.Thread(target=cold_start, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print("both models answered")
    """
)


def test_two_models_cold_starting_on_mps_at_once_both_answer(parity_data_dir: Path) -> None:
    import torch

    if not torch.backends.mps.is_available():
        pytest.skip("needs an Apple GPU (mps)")

    # A subprocess with a hard limit: a deadlock must fail the test, not hang the suite.
    for _ in range(_ATTEMPTS):
        result = subprocess.run(
            [sys.executable, "-c", _COLD_START_SCRIPT],
            capture_output=True,
            text=True,
            timeout=_HANG_LIMIT_SECONDS,
            check=False,
            env={**os.environ, "ARCHON_SEARCH_DATA_DIR": str(parity_data_dir)},
        )

        assert result.stdout.strip() == "both models answered", result.stderr[-2000:]
