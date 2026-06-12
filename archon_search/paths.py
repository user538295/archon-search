"""Single source of truth for archon-search's base data directory.

`get_data_dir()` resolves the directory that holds runtime state — db files,
logs, telemetry, the API key file, the jobs file, fasttext model caches, and
ingest history — from the ``ARCHON_SEARCH_DATA_DIR`` environment variable,
falling back to ``~/.archon-search``.

Lazy by design: never evaluated at import time, never creates the directory.
This avoids stale path bindings when tests or runtime code change the env var,
and lets the container image (where HOME may be unset) override the default
via ``ENV ARCHON_SEARCH_DATA_DIR=/data``.

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

    1. ``$ARCHON_SEARCH_DATA_DIR`` if set and non-whitespace — expanded via
       ``Path.expanduser()`` so ``~/mydata`` works.
    2. ``Path.home() / ".archon-search"`` otherwise.

    Raises ``ValueError`` if the env var is set to an empty/whitespace-only
    string, or if both the env var is unset *and* ``Path.home()`` raises
    ``RuntimeError`` (HOME unset in a misconfigured container).
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is not None:
        if not raw.strip():
            raise ValueError(f"{_ENV_VAR} must not be empty")
        return Path(raw).expanduser()

    try:
        home = Path.home()
    except RuntimeError as exc:
        raise ValueError(
            f"{_ENV_VAR} must be set: HOME is not set and no data "
            "directory can be determined"
        ) from exc
    return home / ".archon-search"
