"""Guard: archon_search must disable ONNX Runtime's 1DS telemetry before onnxruntime initializes.

Official onnxruntime wheels (gliner, magika and docling's RapidOCR pull it in) ship Microsoft's 1DS
telemetry uploader ON by default on macOS and Linux (onnxruntime `docs/Privacy.md`). Two
defects follow:

- Egress: the process makes HTTPS round trips to Microsoft, contradicting this project's
  "nothing leaves the host" privacy baseline.
- Exit crash: at `exit()`, `PosixEnv::~PosixEnv → PosixTelemetry::Shutdown` tears the
  uploader down while its worker thread is still decoding an HTTP response; the worker locks
  an already-destroyed `recursive_mutex` and the process aborts with SIGABRT
  (`recursive_mutex lock failed: Invalid argument`). Observed as a flaky -6 exit of the
  `graph_real_artifact` determinism child.

`ORT_DISABLE_TELEMETRY=1` is the only runtime switch that prevents the uploader from being
created; `onnxruntime.disable_telemetry_events()` only suppresses non-essential events after an
initialization event may already have been sent. It must therefore be in the environment before
onnxruntime loads, which `archon_search/__init__.py` guarantees for every entry point. These
tests prove the pin in a *clean* subprocess, independent of any in-process default.
"""
from __future__ import annotations

import os
import subprocess
import sys

_ENV_VAR = "ORT_DISABLE_TELEMETRY"
_PRINT_ENV_AFTER_IMPORT = f"import archon_search, os; print(os.environ.get({_ENV_VAR!r}))"


def _value_after_import(env: dict[str, str]) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _PRINT_ENV_AFTER_IMPORT],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_importing_archon_search_disables_onnxruntime_telemetry() -> None:
    # Strip any inherited value so this exercises __init__.py's setdefault, not the env.
    env = {k: v for k, v in os.environ.items() if k != _ENV_VAR}
    assert _value_after_import(env) == "1"


def test_operator_override_of_onnxruntime_telemetry_is_preserved() -> None:
    # setdefault must not overwrite a pre-existing operator-supplied value.
    assert _value_after_import({**os.environ, _ENV_VAR: "0"}) == "0"
