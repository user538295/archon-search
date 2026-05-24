"""Crash-injection integration test for ``TelemetryWriter`` date rotation.

``TelemetryWriter._append`` holds a PERSISTENT per-date file descriptor with
rotate-only fsync (per ADR-06): within a UTC date ``os.write`` appends without
fsync, and only a date rollover triggers ``os.fsync(old_fd)`` -> ``os.close(old_fd)``
-> ``os.open(new_date_file)``.

This proves the rotation CALL SEQUENCE reaches ``os.fsync(old_fd)`` before
``os.close(old_fd)`` and before the date2 ``os.open``, on a real disk-backed fd.
The script monkeypatches ``os.close`` to ``os._exit(137)`` (which bypasses Python
``finally``) so the kill fires on the rotation's close of the date1 fd — after
``os.fsync(date1_fd)`` has run, but before the date2 ``os.open`` is ever reached.
Because ``os.close`` is only invoked on a date rollover, a 137 exit code is
positive proof the rotation path executed (not merely that the script started).

Scope note (see plan §"Known limitations"): ``os._exit`` kills the process but
leaves the kernel page cache intact, and the parent reads the file back from the
SAME OS, so this test verifies the rotation's call-sequence reachability on a
real filesystem — NOT power-loss durability (the assertions would also hold if
the rotation fsync were a no-op). The fsync-before-close ORDERING is covered
deterministically by the unit test ``test_rotation_fsyncs_before_closing_old_fd``
in ``tests/telemetry/test_writer.py``; this is the real-fd / subprocess
complement (Sub-case A: rollover boundary).

Requires a disk-backed filesystem: on tmpfs the file lives in RAM, so it skips
when ``tmp_path`` is on tmpfs (rerun with ``pytest --basetemp=/var/tmp/...``).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._helpers import _tmp_is_tmpfs

# returncode produced by os._exit(137)
_KILL_CODE = 137

_DATE1 = "2026-05-15"
_DATE2 = "2026-05-16"

# --- subprocess script ------------------------------------------------------
# Imports the real writer, performs a date1 write (no fsync — rotate-only),
# flushes the "REACHED" sentinel, then monkeypatches os.close to os._exit(137)
# BEFORE the second (date2) write. The second write triggers rotation:
# os.fsync(date1_fd) runs (flushing date1 durably), then os.close(date1_fd)
# fires the kill. The date2 os.open is never reached.
_KILL_ON_ROTATION_CLOSE = """
import os, sys
from datetime import datetime, timezone
from pathlib import Path
from archon_search.telemetry.writer import TelemetryWriter

log_dir = Path(sys.argv[1])
date1_dt = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
date2_dt = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)

writer = TelemetryWriter(log_dir)
# First write: opens the date1 fd (O_APPEND, after the pre-populated line) and
# appends the date1 entry. Rotate-only means this is NOT yet fsynced.
writer._append(date1_dt, b'{"d1":true}\\n')

# Sentinel proves the kill path was reached: os._exit skips buffer flushes, so
# we flush explicitly to push "REACHED" into the OS pipe before the kill point.
# Without it a durability test could false-pass if the subprocess died before
# the rotation ever ran.
sys.stdout.write("REACHED\\n")
sys.stdout.flush()

# Next os.close is the rotation's close of the date1 fd. The rotation fsync runs
# first (durably flushing the date1 write), then this fires.
os.close = lambda *a, **k: os._exit(137)

# Second write: date change triggers rotation -> os.fsync(date1_fd) then the
# patched os.close(date1_fd) -> os._exit(137). The date2 os.open never happens.
writer._append(date2_dt, b'{"d2":true}\\n')
"""


def _run_script(script: str, log_dir: Path) -> subprocess.CompletedProcess:
    """Run ``script`` in a fresh venv-python subprocess against ``log_dir``."""
    return subprocess.run(
        [sys.executable, "-c", script, str(log_dir)],
        capture_output=True,
    )


@pytest.mark.integration
def test_rotation_fsyncs_old_file_before_opening_new(tmp_path: Path):
    if _tmp_is_tmpfs(tmp_path):
        pytest.skip(
            "crash-injection requires a disk-backed FS; "
            "rerun with pytest --basetemp=/var/tmp/..."
        )

    log_dir = tmp_path / "search-logs"
    log_dir.mkdir()
    date1_file = log_dir / f"{_DATE1}.jsonl"
    date2_file = log_dir / f"{_DATE2}.jsonl"

    # Parent pre-populates date1 with one committed entry (written + flushed).
    date1_file.write_bytes(b'{"pre":true}\n')

    result = _run_script(_KILL_ON_ROTATION_CLOSE, log_dir)

    assert result.returncode == _KILL_CODE, result.stderr
    # Positive proof the rotation path was actually reached (false-pass guard).
    assert b"REACHED" in result.stdout, result.stderr

    # The rotation reached the date1 write and its fsync before the kill, so
    # BOTH the pre-existing entry and the date1 entry are present on date1's
    # file (page-cache visible; see the module docstring's scope note).
    date1_text = date1_file.read_text()
    assert '{"pre":true}' in date1_text, date1_text
    assert '{"d1":true}' in date1_text, date1_text

    # The date2 os.open is never reached, so its file should not have been
    # created. Kept non-strict per the plan (absent-or-empty).
    assert not date2_file.exists() or date2_file.read_bytes() == b""
