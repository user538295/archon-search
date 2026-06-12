"""API key loading and auto-generation for archon-search (Task 1.1).

Path resolution (C9 Task 2.3): the key file location is resolved lazily via
``get_key_file()`` on every call. ``ARCHON_SEARCH_KEY_FILE`` (if set and
non-empty) wins; otherwise the path falls under ``get_data_dir() / .search.env``
so ``ARCHON_SEARCH_DATA_DIR`` (the container-friendly base data dir) also
redirects the key file. No module-level capture of either env var: stale
bindings would break tests that flip the env after import and the container
bootstrap where the env is set after the package is loaded.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path

from archon_search._durable_io import atomic_write_bytes
from archon_search.paths import get_data_dir

logger = logging.getLogger(__name__)

ENV_VAR: str = "ARCHON_SEARCH_API_KEY"

_HEX_RE = re.compile(r"^[0-9a-f]+$")


def get_key_file() -> Path:
    """Return the API key file path, resolved fresh on every call.

    Resolution order:

    1. ``$ARCHON_SEARCH_KEY_FILE`` if set and non-empty —
       ``Path(env).expanduser()``.
    2. ``get_data_dir() / ".search.env"`` otherwise.

    ``ARCHON_SEARCH_KEY_FILE`` takes precedence over ``ARCHON_SEARCH_DATA_DIR``
    so operators can pin the key file location independently of the rest of
    the runtime state directory.
    """
    raw = os.environ.get("ARCHON_SEARCH_KEY_FILE") or ""
    if raw:
        return Path(raw).expanduser()
    return get_data_dir() / ".search.env"


def load_or_generate_key() -> tuple[str, str]:
    """Return (key, source). Source is 'env var', 'file: ...', or 'auto-generated'."""
    key = _load_from_env()
    if key is not None:
        return key, "env var"

    key = _load_from_file()
    if key is not None:
        return key, f"file: {get_key_file()}"

    key = _generate_and_write()
    return key, "auto-generated"


def _load_from_env() -> str | None:
    val = os.environ.get(ENV_VAR, "")
    if not val:
        return None
    if not _validate_key(val):
        logger.warning("%s is set but contains an invalid value (expected lowercase hex string)", ENV_VAR)
        return None
    return val


def _load_from_file() -> str | None:
    key_file = get_key_file()
    if not key_file.exists():
        return None

    # Attempt to tighten permissions if too wide
    try:
        mode = os.stat(key_file).st_mode & 0o777
        if mode != 0o600:
            _chmod_600(key_file)
    except OSError:
        pass

    try:
        content = key_file.read_text()
    except OSError:
        return None

    for line in content.splitlines():
        if line.startswith(f"{ENV_VAR}="):
            raw = line[len(f"{ENV_VAR}="):]
            val = raw.strip()
            if not _validate_key(val):
                logger.error("Key file contains invalid value for %s", ENV_VAR)
                return None
            return val

    return None


def _validate_key(value: str) -> bool:
    return bool(value) and bool(_HEX_RE.fullmatch(value))


def _generate_and_write() -> str:
    key_file = get_key_file()
    os.makedirs(key_file.parent, exist_ok=True)
    key = secrets.token_hex(32)  # 64 hex chars
    payload = f"{ENV_VAR}={key}\n".encode()
    tmp = key_file.with_suffix(key_file.suffix + ".tmp")

    for attempt in range(2):
        try:
            atomic_write_bytes(key_file, payload, mode=0o600)
            return key
        except FileExistsError:
            if attempt == 0:
                time.sleep(0.1)
                existing = _load_from_file()
                if existing is not None:
                    return existing
                try:
                    os.unlink(str(tmp))
                except OSError:
                    pass
                continue
            existing = _load_from_file()
            if existing is not None:
                return existing
            raise RuntimeError("key generation failed: concurrent write conflict")
    raise RuntimeError("key generation failed: concurrent write conflict")


def _chmod_600(path: Path) -> None:
    if sys.platform == "win32":
        logger.info("permission check skipped on Windows")
        return
    try:
        path.chmod(0o600)
    except PermissionError:
        logger.warning("Could not set permissions to 600 on %s", path)
