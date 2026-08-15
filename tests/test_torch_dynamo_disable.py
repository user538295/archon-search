"""Guard: archon_search must disable TorchDynamo on macOS before torch loads.

docling's layout stage runs the RT-DETR V2 model under `torch.compile`. On
macOS/arm64 (MPS) that graph capture raises inside
`transformers.utils.import_utils.torch_compilable_check` →
`torch._check_tensor_all_with`, killing the server process; launchd restarts it
and the crash repeats indefinitely (INFRA-managed_server_torch_compile_crash_loop
— 43 restarts in a 1.5 h session). archon_search/__init__.py pins
`TORCHDYNAMO_DISABLE=1` on macOS so torch never enters the compile path. This
test proves that pin holds in a *clean* subprocess — independent of any
in-process default — so emptying __init__.py can never silently re-arm the crash
loop in production entry points.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the torch.compile RT-DETR crash is macOS/arm64 (MPS)-only; Linux keeps dynamo enabled",
)
def test_importing_archon_search_disables_torch_dynamo_on_macos() -> None:
    # Strip any inherited override so this exercises __init__.py's setdefault, not the env.
    env = {k: v for k, v in os.environ.items() if k != "TORCHDYNAMO_DISABLE"}
    proc = subprocess.run(
        [sys.executable, "-c", "import archon_search, os; print(os.environ.get('TORCHDYNAMO_DISABLE'))"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "1", (
        f"expected TORCHDYNAMO_DISABLE='1' after importing archon_search; got {proc.stdout.strip()!r}"
    )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the torch.compile RT-DETR crash is macOS/arm64 (MPS)-only; Linux keeps dynamo enabled",
)
def test_operator_override_of_torchdynamo_disable_is_preserved() -> None:
    # setdefault must not overwrite a pre-existing operator-supplied value.
    env = {**os.environ, "TORCHDYNAMO_DISABLE": "0"}
    proc = subprocess.run(
        [sys.executable, "-c", "import archon_search, os; print(os.environ.get('TORCHDYNAMO_DISABLE'))"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0", (
        f"expected operator-set TORCHDYNAMO_DISABLE='0' to survive archon_search import; got {proc.stdout.strip()!r}"
    )
