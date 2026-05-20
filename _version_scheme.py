"""Custom CalVer version scheme for hatch-vcs / setuptools-scm.

Format: ``YY.M.<total-commit-count>`` (e.g. ``26.5.4521``).

The total commit count is taken from ``git rev-list --count HEAD`` so every
push to ``main`` produces a monotonically increasing patch component. If the
git invocation fails (no repo / shallow checkout / git missing), the count
falls back to ``0`` so builds never crash on the version step.
"""
from __future__ import annotations

import datetime
import subprocess


def calver_total_count(version: object) -> str:
    """Return ``YY.M.<count>``. Signature matches setuptools-scm version_scheme."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        count = result.stdout.strip() if result.returncode == 0 else "0"
    except (FileNotFoundError, OSError):
        count = "0"
    if not count:
        count = "0"
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{now.strftime('%y')}.{now.month}.{count}"
