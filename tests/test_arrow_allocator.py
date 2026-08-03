"""Guard: archon_search must route pyarrow off the crash-prone mimalloc pool.

pyarrow 25's default bundled allocator (mimalloc) segfaults on macOS/arm64
during per-thread heap init (`mi_thread_init`, apache/arrow #37010/#41696/
#44342). archon_search/__init__.py pins the system allocator on macOS. This
test proves that pin holds in a *clean* subprocess — independent of the
in-process default set by tests/conftest.py — so emptying __init__.py can never
silently re-arm the crash in production entry points.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="mimalloc segfault is macOS/arm64-only; Linux keeps the faster mimalloc default",
)
def test_importing_archon_search_pins_system_allocator_on_macos() -> None:
    # Strip any inherited override so this exercises __init__.py's setdefault, not the env.
    env = {k: v for k, v in os.environ.items() if k != "ARROW_DEFAULT_MEMORY_POOL"}
    proc = subprocess.run(
        [sys.executable, "-c", "import archon_search; import pyarrow as pa; print(pa.default_memory_pool().backend_name)"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "system", (
        f"expected pyarrow 'system' pool after importing archon_search; got {proc.stdout.strip()!r}"
    )
