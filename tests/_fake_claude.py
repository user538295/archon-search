"""Fake ``claude`` binary for the ``claude_cli`` wrapper tests (collection-descriptions plan, BE-8, S12, S79).

A REAL subprocess stands in for the Claude Code CLI, so a test sees exactly what the real binary would receive: the argv,
the environment, the working directory (and what it holds) and the standard input (read to the end, however large). Use
:func:`install`::

    fake = _fake_claude.install(tmp_path, reply="hello")
    client = ClaudeCliClient(model="", binary=str(fake.path))
    ...
    invocation = fake.invocations()[0]
    assert invocation.argv[0] == "-p" and invocation.stdin == prompt

``fake.path`` is an executable ``sh`` launcher that runs this module as a script and passes its own directory as the first
argument. The binary's behaviour comes from a JSON file in that directory, because the child's environment is locked down
and cannot carry settings. POSIX only.

Behaviour (keyword arguments of :func:`install`):

* ``reply``: what the binary prints on standard output.
* ``exit_codes``: the exit status of the 1st, 2nd, ... invocation; the last one repeats.
* ``stderr``: what the binary prints on standard error.
* ``hang_seconds``: sleep this long after recording the invocation, to be killed by a timeout or a cancellation.
* ``read_stdin``: ``False`` exits without reading standard input (a closed pipe under a large prompt).
* ``spawn_grandchild``: start a long-lived child of the binary that inherits its output pipes, to prove a kill reaches
  descendants; its pid is in :attr:`Invocation.grandchild_pid`.

Every invocation is recorded atomically before it replies or hangs (argv, environment, working directory, process id,
standard input), so a test can wait for the record, then act on the live process. The module is stdlib-only: it also runs
as the child.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CONFIG_FILE = "fake_claude_config.json"
_RECORDS_DIR = "records"
_RECORD_SUFFIX = ".json"
_CLAIM_SUFFIX = ".claim"
_PARTIAL_SUFFIX = ".partial"
_LAUNCHER_NAME = "claude"
_DEFAULT_REPLY = "fake claude reply"
_SLOT_DIGITS = 6
_GRANDCHILD_LIFETIME_SECONDS = 120


@dataclass(frozen=True)
class Invocation:
    """What one spawn of the fake binary received."""

    argv: list[str]  # without the program name
    env: dict[str, str]
    cwd: str
    cwd_entries: list[str]  # what the working directory held when the binary started
    pid: int
    stdin: str
    grandchild_pid: int | None


@dataclass(frozen=True)
class FakeClaude:
    """An installed fake binary: where it is and what it recorded."""

    path: Path
    records_dir: Path

    def invocations(self) -> list[Invocation]:
        """Every complete record, oldest first."""
        return [
            Invocation(**json.loads(record.read_text(encoding="utf-8")))
            for record in sorted(self.records_dir.glob(f"*{_RECORD_SUFFIX}"))
        ]


def install(
    directory: Path,
    *,
    reply: str = _DEFAULT_REPLY,
    exit_codes: tuple[int, ...] = (0,),
    stderr: str = "",
    hang_seconds: float = 0.0,
    read_stdin: bool = True,
    spawn_grandchild: bool = False,
) -> FakeClaude:
    """Write the launcher and its configuration into ``directory`` and return the handle."""
    directory.mkdir(parents=True, exist_ok=True)
    records_dir = directory / _RECORDS_DIR
    records_dir.mkdir(exist_ok=True)
    config = {
        "reply": reply,
        "exit_codes": list(exit_codes),
        "stderr": stderr,
        "hang_seconds": hang_seconds,
        "read_stdin": read_stdin,
        "spawn_grandchild": spawn_grandchild,
    }
    (directory / _CONFIG_FILE).write_text(json.dumps(config), encoding="utf-8")
    launcher = directory / _LAUNCHER_NAME
    launcher.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "$(dirname "$0")" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return FakeClaude(path=launcher, records_dir=records_dir)


def _record(records_dir: Path, body: dict[str, Any]) -> int:
    """Write ``body`` as the next free record, atomically, and return its slot number."""
    slot = 0
    while True:
        stem = records_dir / f"{slot:0{_SLOT_DIGITS}d}"
        try:
            stem.with_suffix(_CLAIM_SUFFIX).open("x").close()
            break
        except FileExistsError:
            slot += 1
    partial = stem.with_suffix(_PARTIAL_SUFFIX)
    partial.write_text(json.dumps(body), encoding="utf-8")
    partial.replace(stem.with_suffix(_RECORD_SUFFIX))
    return slot


def _main(launcher_dir: Path, argv: list[str]) -> int:
    config = json.loads((launcher_dir / _CONFIG_FILE).read_text(encoding="utf-8"))
    stdin = sys.stdin.buffer.read().decode("utf-8", errors="replace") if config["read_stdin"] else ""
    grandchild = (
        subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({_GRANDCHILD_LIFETIME_SECONDS})"])
        if config["spawn_grandchild"]
        else None
    )
    slot = _record(
        launcher_dir / _RECORDS_DIR,
        {
            "argv": argv,
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "cwd_entries": sorted(os.listdir(os.getcwd())),
            "pid": os.getpid(),
            "stdin": stdin,
            "grandchild_pid": grandchild.pid if grandchild else None,
        },
    )
    time.sleep(config["hang_seconds"])
    sys.stderr.write(config["stderr"])
    sys.stdout.write(config["reply"])
    sys.stdout.flush()
    codes = config["exit_codes"]
    return codes[min(slot, len(codes) - 1)]


if __name__ == "__main__":
    sys.exit(_main(Path(sys.argv[1]), sys.argv[2:]))
