"""HMAC-SHA256 doc_id hasher for telemetry (D8 / BE-2).

This module intentionally spans two Clean Architecture layers — exactly like
``key_manager.py`` which it mirrors:

- **Entities**: ``hash_doc_id(salt, doc_id) -> str`` — a pure, stateless
  HMAC-SHA256 transform with no dependencies beyond the standard library.
- **Frameworks & Drivers**: ``load_or_create_salt(...)`` — performs
  filesystem I/O (read / generate / atomic write at mode 0o600).

Design rationale:
- HMAC-SHA256 over double-SHA256: the salt breaks correlation without
  knowing it.
- 64-char full output: zero schema friction, no length-validator breaks.
- 32-byte salt: cryptographically strong; written once, never rotated here
  (salt rotation is deferred to a future iteration).
- ``load_or_create_salt`` is called once at lifespan startup; the result is
  stored on ``app.state.salt_bytes`` (for status reporting) and used to
  build a ``doc_id_hasher`` closure stored on ``app.state.doc_id_hasher``
  (injected into routes and the MCP sub-app).
"""

from __future__ import annotations

import hmac
import logging
import secrets
from pathlib import Path

from archon_search._durable_io import atomic_write_bytes

logger = logging.getLogger(__name__)

_SALT_SIZE: int = 32  # bytes — 256-bit entropy, matching the HMAC output width


def hash_doc_id(salt: bytes, doc_id: str) -> str:
    """Return the HMAC-SHA256 of ``doc_id`` keyed by ``salt`` as a 64-char hex string.

    This is a pure, deterministic function:
    - Same inputs always produce the same output.
    - Different ``doc_id`` values produce different outputs (collision-free
      in the HMAC sense for practical inputs).
    - The output is not reversible without knowing the salt.
    """
    return hmac.digest(salt, doc_id.encode(), "sha256").hex()


def load_or_create_salt(
    hash_doc_ids_enabled: bool,
    salt_path: Path,
) -> bytes | None:
    """Load the telemetry salt from disk, creating it if it does not exist.

    Args:
        hash_doc_ids_enabled: When ``False`` returns ``None`` immediately —
            no file is read or created.
        salt_path: Filesystem path where the 32-byte salt is stored.

    Returns:
        32 bytes of salt when hashing is enabled and the salt is available,
        ``None`` otherwise (flag off, unreadable file, or wrong-size file).

    Side-effects:
        - On first call (file absent): generates 32 cryptographically random
          bytes, writes them via ``atomic_write_bytes`` at mode 0o600, and
          logs a WARNING.
        - On subsequent calls (file present, correct size): reads and returns
          the bytes silently.
        - Unreadable file: logs an ERROR and returns ``None`` (hashing falls
          back to disabled for the session; server does not crash).
        - Wrong-size file (< or > 32 bytes): logs an ERROR and returns
          ``None`` — a short key would weaken HMAC.
    """
    if not hash_doc_ids_enabled:
        return None

    if not salt_path.exists():
        # First-time generation: create the salt and persist it atomically.
        new_salt = secrets.token_bytes(_SALT_SIZE)
        try:
            salt_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(salt_path, new_salt, mode=0o600)
        except OSError as exc:
            logger.error(
                "telemetry: failed to write salt file %s: %s — hashing disabled for this session",
                salt_path,
                exc,
            )
            return None
        logger.warning(
            "telemetry: generated new HMAC salt at %s (mode 600); "
            "doc_id hashes are not reversible without this file — keep it safe",
            salt_path,
        )
        return new_salt

    # Existing file: read and validate.
    try:
        data = salt_path.read_bytes()
    except OSError as exc:
        logger.error(
            "telemetry: cannot read salt file %s: %s — hashing disabled for this session",
            salt_path,
            exc,
        )
        return None

    if len(data) != _SALT_SIZE:
        logger.error(
            "telemetry: salt file %s has unexpected size %d bytes (expected %d) — "
            "treating as corrupt; hashing disabled for this session",
            salt_path,
            len(data),
            _SALT_SIZE,
        )
        return None

    return data
