"""What the opt-in container test modules share: the repo root and the docker/opt-in gates."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_OPT_IN_ENV = "ARCHON_SEARCH_RUN_DOCKER_SMOKE"
DOCKER_INFO_TIMEOUT_S = 10


@functools.cache
def docker_available() -> bool:
    """A running daemon, not merely the CLI binary. ``shutil.which`` alone is a
    false positive: Docker Desktop can be installed (``docker`` on PATH) but
    stopped, in which case every ``docker build`` errors instead of skipping.
    ``docker info`` is the cheapest call that actually touches the daemon."""
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DOCKER_INFO_TIMEOUT_S,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def smoke_opted_in() -> bool:
    """Smoke tests are skipped unless the operator opts in.

    Without this gate every ``uv run pytest`` on a developer laptop with
    Docker Desktop installed would trigger the ~5-minute image build.
    The C9 plan called for the suite to run via ``-m docker``; this
    env var is the project-convention equivalent of marker exclusion,
    matching how ``live`` / ``live_eval`` skip when their infrastructure
    is absent.
    """
    return os.environ.get(SMOKE_OPT_IN_ENV) == "1"
