"""Single source of truth for archon-search's base data directory.

`get_data_dir()` resolves the directory that holds runtime state — db files,
logs, telemetry, the API key file, the jobs file, fasttext model caches, and
ingest history — from the ``ARCHON_SEARCH_DATA_DIR`` environment variable,
falling back to ``~/.archon-search``.

Lazy by design: never evaluated at import time, never creates the directory.
This avoids stale path bindings when tests or runtime code change the env var,
and lets the container image (where HOME may be unset) override the default
via ``ENV ARCHON_SEARCH_DATA_DIR=/data``.

The returned path is NOT guaranteed to exist — callers must create it (or
the parent of any file they intend to write) as needed.

Downstream consumers that derive paths from ``get_data_dir()`` live in their
domain modules, not here, so each subsystem keeps its own naming. See the
"Path derivations" table in ``Documentation/Backlog/C9-container-support-plan.md``
for the canonical list.

TODO(C9): once C9 Phase 2 (tasks 2.2–2.6) is complete, replace the call-site
roadmap below with a verified list of actual consumers. The plan file linked
above remains the source of truth; this note may rot if tasks are renamed,
reordered, or dropped — grep for ``TODO(C9)`` to find this on cleanup.
Planned call sites: ``config.py`` (db / log / telemetry),
``key_manager.get_key_file``, ``jobs.get_jobs_file``,
``language_detector.get_fasttext_models_dir``, and ``cli/ingest.py`` for
history sessions.

Raises ``ValueError`` (not ``ConfigError``) so it can be safely imported by
``archon_search.config`` without a circular import; ``load_config()`` wraps
the call to translate the error into a ``ConfigError``.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR: str = "ARCHON_SEARCH_DATA_DIR"


def get_data_dir() -> Path:
    """Return the base data directory for archon-search runtime state.

    Resolution order:

    1. ``$ARCHON_SEARCH_DATA_DIR`` if set and non-whitespace — stripped of
       surrounding whitespace, expanded via ``Path.expanduser()`` so
       ``~/mydata`` works, then required to be absolute.
    2. ``Path.home() / ".archon-search"`` otherwise.

    Raises ``ValueError`` if:

    - the env var is set to an empty/whitespace-only string,
    - the env var resolves to a relative path (operator error in a container
      where CWD is implementation-dependent),
    - the env var contains ``~`` but HOME is unset (``Path.expanduser`` raises
      ``RuntimeError``), or
    - the env var is unset *and* ``Path.home()`` raises ``RuntimeError``
      (HOME unset in a misconfigured container).
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is not None:
        stripped = raw.strip()
        if not stripped:
            raise ValueError(f"{_ENV_VAR} must not be empty")
        try:
            result = Path(stripped).expanduser()
        except RuntimeError as exc:
            raise ValueError(
                f"{_ENV_VAR}={raw!r} contains '~' but HOME is not set"
            ) from exc
        if not result.is_absolute():
            raise ValueError(
                f"{_ENV_VAR} must be an absolute path, got {raw!r}"
            )
        return result

    try:
        home = Path.home()
    except RuntimeError as exc:
        raise ValueError(
            f"{_ENV_VAR} must be set: HOME is not set and no data "
            "directory can be determined"
        ) from exc
    return home / ".archon-search"
