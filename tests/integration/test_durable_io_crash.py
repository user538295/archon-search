"""Crash-injection integration tests for ``archon_search._durable_io``.

These prove the durable-write CALL SEQUENCE survives a process killed mid-write.
Each case runs the helper in a subprocess that monkeypatches one syscall to
``os._exit(137)`` (which bypasses Python ``finally``), then the parent asserts
on-disk state:

* crash before ``os.replace`` -> the prior committed content is intact;
* crash after ``os.replace`` returns -> the new content is visible.

Because ``os._exit`` skips ``finally``, a mid-helper crash may leave the ``.tmp``
sidecar behind; these tests therefore never assert tmp absence in crash cases.

Requires a disk-backed filesystem: on tmpfs the data lives in RAM and the test
is meaningless, so each test skips when ``tmp_path`` is on tmpfs (rerun with
``pytest --basetemp=/var/tmp/...``).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._helpers import _tmp_is_tmpfs

# returncode produced by os._exit(137)
_KILL_CODE = 137

# --- subprocess scripts -----------------------------------------------------
# Each script imports the real helper, optionally monkeypatches a syscall to
# os._exit(137) BEFORE running, then invokes the helper with sys.argv[1] as the
# target path.

_JSON_KILL_ON_REPLACE = """
import os, sys
from pathlib import Path
from archon_search._durable_io import atomic_write_json
path = Path(sys.argv[1])
def kill(*a, **k):
    os._exit(137)
os.replace = kill
# Sentinel proves the kill path was reached: os._exit skips buffer flushes,
# so we flush explicitly to push "REACHED" into the OS pipe before the helper
# (and the kill) runs. Without it a "preserve prior" test could false-pass if
# the subprocess died before ever invoking the helper.
sys.stdout.write("REACHED\\n")
sys.stdout.flush()
atomic_write_json(path, {"new": True})
"""

_JSON_KILL_ON_FSYNC = """
import os, sys
from pathlib import Path
from archon_search._durable_io import atomic_write_json
path = Path(sys.argv[1])
def kill(*a, **k):
    os._exit(137)
os.fsync = kill
sys.stdout.write("REACHED\\n")
sys.stdout.flush()
atomic_write_json(path, {"new": True})
"""

_JSON_KILL_AFTER_RETURN = """
import os, sys
from pathlib import Path
from archon_search._durable_io import atomic_write_json
path = Path(sys.argv[1])
sys.stdout.write("REACHED\\n")
sys.stdout.flush()
atomic_write_json(path, {"new": True})
os._exit(137)
"""

_BYTES_KILL_ON_REPLACE = """
import os, sys
from pathlib import Path
from archon_search._durable_io import atomic_write_bytes
path = Path(sys.argv[1])
def kill(*a, **k):
    os._exit(137)
os.replace = kill
sys.stdout.write("REACHED\\n")
sys.stdout.flush()
atomic_write_bytes(path, b"k" * 256)
"""

_BYTES_KILL_ON_FSYNC = """
import os, sys
from pathlib import Path
from archon_search._durable_io import atomic_write_bytes
path = Path(sys.argv[1])
def kill(*a, **k):
    os._exit(137)
os.fsync = kill
sys.stdout.write("REACHED\\n")
sys.stdout.flush()
atomic_write_bytes(path, b"k" * 256)
"""

_BYTES_KILL_AFTER_RETURN = """
import os, sys
from pathlib import Path
from archon_search._durable_io import atomic_write_bytes
path = Path(sys.argv[1])
sys.stdout.write("REACHED\\n")
sys.stdout.flush()
atomic_write_bytes(path, b"k" * 256)
os._exit(137)
"""

_NEW_PAYLOAD = b"k" * 256


def _run_script(script: str, target: Path) -> subprocess.CompletedProcess:
    """Run ``script`` in a fresh venv-python subprocess against ``target``."""
    return subprocess.run(
        [sys.executable, "-c", script, str(target)],
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def _require_disk_backed_fs(tmp_path: Path):
    """Skip if the per-test tmp dir is tmpfs (crash injection needs real disk)."""
    if _tmp_is_tmpfs(tmp_path):
        pytest.skip(
            "crash-injection requires disk-backed FS; "
            "rerun with pytest --basetemp=/var/tmp/..."
        )


# --- atomic JSON tests (Task 2.8) -------------------------------------------


@pytest.mark.integration
def test_atomic_write_json_crash_before_replace_preserves_prior(tmp_path: Path):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"prior": True}))

    result = _run_script(_JSON_KILL_ON_REPLACE, target)

    assert result.returncode == _KILL_CODE, result.stderr
    # positive proof the helper was actually entered (not an early unrelated exit).
    assert b"REACHED" in result.stdout, result.stderr
    # replace never happened: committed file still holds prior content.
    assert json.loads(target.read_text()) == {"prior": True}


@pytest.mark.integration
def test_atomic_write_json_crash_before_fsync_preserves_prior(tmp_path: Path):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"prior": True}))

    result = _run_script(_JSON_KILL_ON_FSYNC, target)

    assert result.returncode == _KILL_CODE, result.stderr
    assert b"REACHED" in result.stdout, result.stderr
    # killed before the file fsync (the first fsync), so before replace.
    assert json.loads(target.read_text()) == {"prior": True}


@pytest.mark.integration
def test_atomic_write_json_crash_after_replace_has_new(tmp_path: Path):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"prior": True}))

    result = _run_script(_JSON_KILL_AFTER_RETURN, target)

    assert result.returncode == _KILL_CODE, result.stderr
    assert b"REACHED" in result.stdout, result.stderr
    # helper returned (replace committed) before the crash: new content visible.
    assert json.loads(target.read_text()) == {"new": True}


# --- atomic bytes tests (Task 2.9) ------------------------------------------


@pytest.mark.integration
def test_atomic_write_bytes_crash_before_replace_preserves_prior(tmp_path: Path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"p" * 128)

    result = _run_script(_BYTES_KILL_ON_REPLACE, target)

    assert result.returncode == _KILL_CODE, result.stderr
    assert b"REACHED" in result.stdout, result.stderr
    # replace never happened: committed file still holds prior bytes.
    assert target.read_bytes() == b"p" * 128


@pytest.mark.integration
def test_atomic_write_bytes_crash_before_fsync_preserves_prior(tmp_path: Path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"p" * 128)

    result = _run_script(_BYTES_KILL_ON_FSYNC, target)

    assert result.returncode == _KILL_CODE, result.stderr
    assert b"REACHED" in result.stdout, result.stderr
    # killed at the file fsync (before replace).
    assert target.read_bytes() == b"p" * 128


@pytest.mark.integration
def test_atomic_write_bytes_crash_after_replace_has_new_with_mode_0600(
    tmp_path: Path,
):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"p" * 128)

    result = _run_script(_BYTES_KILL_AFTER_RETURN, target)

    assert result.returncode == _KILL_CODE, result.stderr
    assert b"REACHED" in result.stdout, result.stderr
    # helper returned (replace committed) before the crash: new payload visible.
    assert target.read_bytes() == _NEW_PAYLOAD
    # creation-time mode is preserved through os.replace.
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
