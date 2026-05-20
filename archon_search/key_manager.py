"""API key loading and auto-generation for archon-search (Task 1.1)."""
from __future__ import annotations

import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path

logger = logging.getLogger("archon-search")

_key_file_env = os.environ.get("ARCHON_SEARCH_KEY_FILE") or ""
KEY_FILE: Path = (
    Path(_key_file_env).expanduser()
    if _key_file_env
    else Path.home() / ".archon-search" / ".search.env"
)
ENV_VAR: str = "ARCHON_SEARCH_API_KEY"

_HEX_RE = re.compile(r"^[0-9a-f]+$")


def load_or_generate_key() -> tuple[str, str]:
    """Return (key, source). Source is 'env var', 'file: ...', or 'auto-generated'."""
    key = _load_from_env()
    if key is not None:
        return key, "env var"

    key = _load_from_file()
    if key is not None:
        return key, f"file: {KEY_FILE}"

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
    if not KEY_FILE.exists():
        return None

    # Attempt to tighten permissions if too wide
    try:
        mode = os.stat(KEY_FILE).st_mode & 0o777
        if mode != 0o600:
            _chmod_600(KEY_FILE)
    except OSError:
        pass

    try:
        content = KEY_FILE.read_text()
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
    os.makedirs(KEY_FILE.parent, exist_ok=True)
    tmp = KEY_FILE.parent / ".search.env.tmp"
    key = secrets.token_hex(32)  # 64 hex chars

    for attempt in range(2):
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if attempt == 0:
                time.sleep(0.1)
                existing = _load_from_file()
                if existing is not None:
                    return existing
                # Delete orphaned tmp and retry
                try:
                    os.unlink(str(tmp))
                except OSError:
                    pass
                continue
            else:
                # Second attempt also failed
                existing = _load_from_file()
                if existing is not None:
                    return existing
                raise RuntimeError("key generation failed: concurrent write conflict")
        else:
            break
    else:
        existing = _load_from_file()
        if existing is not None:
            return existing
        raise RuntimeError("key generation failed: concurrent write conflict")

    try:
        os.write(fd, f"{ENV_VAR}={key}\n".encode())
    finally:
        os.close(fd)

    try:
        os.replace(str(tmp), str(KEY_FILE))
    except Exception as exc:
        logger.error("key generation failed: %s: %s", type(exc).__name__, exc)
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise

    _chmod_600(KEY_FILE)
    return key


def _chmod_600(path: Path) -> None:
    if sys.platform == "win32":
        logger.info("permission check skipped on Windows")
        return
    try:
        path.chmod(0o600)
    except PermissionError:
        logger.warning("Could not set permissions to 600 on %s", path)
