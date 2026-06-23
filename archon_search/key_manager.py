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

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from archon_search._durable_io import atomic_write_bytes
from archon_search.paths import get_data_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KeyRecord entity (C1)
# ---------------------------------------------------------------------------


class KeyRecord(BaseModel):
    """A single API key record stored in keys.json.

    Raw bearer tokens are never stored — only the SHA-256 hex digest
    (``token_hash``) is persisted. The raw token is printed once to
    stdout at creation time and is never recoverable from disk.
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None
    token_hash: str
    namespace: str
    label: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    status: Literal["active", "revoked"] = "active"


# ---------------------------------------------------------------------------
# KeyStore use-case class (C2)
# ---------------------------------------------------------------------------


class KeyStore:
    """Durable key store backed by ``keys.json``.

    Design:
    - No in-memory key list is maintained. ``active_keys()`` and ``load()``
      always re-read from disk (disk-read-on-demand). This eliminates
      cross-process staleness between the HTTP and MCP servers that each
      create their own ``KeyStore`` pointing to the same file.
    - An internal ``asyncio.Lock`` serialises every read-modify-write cycle
      (``create``; extended in later tasks). ``active_keys()`` / ``load()``
      do NOT need the lock (each call gets a fresh snapshot from disk).
    - ``_logged_expired_ids`` suppresses repeated INFO logs for the same
      expired key across calls within one process lifetime.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._logged_expired_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Read-only helpers (no lock required)
    # ------------------------------------------------------------------

    async def load(self) -> list[KeyRecord]:
        """Read ``keys.json`` from disk and return all records.

        Returns an empty list (and logs ERROR) if the file is missing,
        unreadable, non-array JSON, or contains any invalid record —
        implementing the all-or-nothing corruption policy (S17).
        """
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.error("keys.json read error: %s", exc)
            return []

        # Tighten permissions on files that may have been created with a
        # permissive umask (e.g. by a different tool or manual creation).
        _chmod_600(self._path)

        if not raw:
            logger.error("keys.json is empty — treating as corrupted key store")
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("keys.json is not valid JSON — treating as corrupted key store: %s", exc)
            return []

        if not isinstance(data, list):
            logger.error(
                "keys.json does not contain a JSON array — treating as corrupted key store"
            )
            return []

        try:
            return [KeyRecord.model_validate(item) for item in data]
        except Exception as exc:
            logger.error(
                "keys.json contains invalid records — treating as corrupted key store: %s", exc
            )
            return []

    async def active_keys(self) -> list[KeyRecord]:
        """Return all non-expired active records.

        Reads ``keys.json`` from disk on every call. Filters by:
        - ``status == "active"``
        - ``expires_at is None`` OR ``expires_at > now`` (strict: key at
          exact expiry instant is considered expired)
        """
        records = await self.load()
        now = datetime.now(UTC)
        active = []
        for record in records:
            if record.status != "active":
                continue
            if record.expires_at is not None and record.expires_at <= now:
                # Log only once per key ID per process lifetime.
                # Synthetic TOML records (id=None) are never tracked in the set —
                # synthetic records do not expire (expires_at is always None at creation),
                # so this branch is only reached by managed keys with a non-None id.
                if record.id is not None and record.id not in self._logged_expired_ids:
                    logger.info("Key %s has expired and will no longer be accepted", record.id)
                    self._logged_expired_ids.add(record.id)
                continue
            active.append(record)
        return active

    async def list_keys(self) -> list[KeyRecord]:
        """Return all records for operator display — no expiry filtering.

        Unlike ``active_keys()``, this returns revoked and expired-by-time
        records too, so operators can audit the full key history.
        """
        return await self.load()

    # ------------------------------------------------------------------
    # Write operations (lock required)
    # ------------------------------------------------------------------

    async def revoke(self, key_id: str) -> None:
        """Mark a managed key as revoked.

        - Idempotent for already-revoked keys (no-op, no disk write).
        - Raises ``KeyError`` for unknown IDs (never-existed keys).
        - ``key_id`` must be a non-None string; ``None`` is explicitly rejected
          to prevent accidental matches against synthetic TOML records
          (which have ``id=None``).
        """
        if not isinstance(key_id, str):
            raise KeyError(f"Key not found: {key_id!r}")

        async with self._lock:
            records = await self.load()
            found = False
            for record in records:
                if record.id == key_id:
                    found = True
                    if record.status == "revoked":
                        # Already revoked — idempotent no-op; no disk write needed.
                        return
                    record.status = "revoked"
                    break
            if not found:
                raise KeyError(f"Key not found: {key_id!r}")
            self._write(records)

    async def create(
        self,
        ns: str,
        label: str | None,
        expires_at: datetime | None,
    ) -> dict[str, str | datetime]:
        """Generate a new API key, persist its hash, return ``{id, token, created_at}``.

        The raw bearer token is returned exactly once — it is never stored
        on disk. ``keys.json`` stores only the SHA-256 hex digest.

        Returns:
            dict with ``id`` (UUID4 str), ``token`` (raw 64-hex-char bearer token),
            and ``created_at`` (UTC datetime used when persisting the record — callers
            should use this value to avoid timestamp divergence between the response
            and what ``GET /keys`` will return from disk).
        """
        if expires_at is not None and expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")

        key_id = str(uuid.uuid4())
        raw_token = secrets.token_hex(32)  # 64 hex chars
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        created_at = datetime.now(UTC)

        record = KeyRecord(
            id=key_id,
            token_hash=token_hash,
            namespace=ns,
            label=label,
            created_at=created_at,
            expires_at=expires_at,
            status="active",
        )

        async with self._lock:
            records = await self.load()
            records.append(record)
            self._write(records)

        return {"id": key_id, "token": raw_token, "created_at": created_at}

    async def rotate_default_key(
        self,
        current_token: str,
        grace_seconds: int,
    ) -> dict[str, str | KeyRecord | None]:
        """Rotate the default API key.

        Generates a new managed ``KeyRecord`` for the default key and, if
        ``current_token`` matches an existing managed key record in
        ``keys.json``, marks that old record revoked (``grace_seconds=0``) or
        grace-expired (``grace_seconds > 0``).

        If no matching record exists (the current default was never in
        ``keys.json``), only the new record is created — no revocation.

        **File I/O responsibility:** this method only mutates ``keys.json``.
        Writing ``.search.env`` with the new raw token is the caller's
        responsibility (i.e. the ``POST /keys/rotate`` route handler in BE-8).

        Args:
            current_token: The raw bearer token of the current default key.
                The caller (route handler) reads this from
                ``APIKeyMiddleware._api_key`` or re-reads ``.search.env``.
            grace_seconds: Seconds the old key remains valid after rotation.
                Must be >= 0. If 0, the old key is immediately revoked.
                If > 0, the old key gets ``expires_at = now + grace_seconds``
                (and remains ``status="active"`` so it is accepted during the
                grace window). Negative values raise ``ValueError``.

        Returns:
            dict with keys:
            - ``new_key_id`` (str): UUID of the newly created key.
            - ``new_token`` (str): Raw bearer token for the new key (printed
              once; never stored on disk).
            - ``old_record`` (KeyRecord | None): The old key record **after**
              mutation (status/expires_at already updated), or ``None`` if no
              active matching managed record was found.

        Raises:
            ValueError: If ``grace_seconds`` is negative.
        """
        if grace_seconds < 0:
            raise ValueError(f"grace_seconds must be >= 0, got {grace_seconds!r}")

        current_hash = hashlib.sha256(current_token.encode()).hexdigest()

        async with self._lock:
            now = datetime.now(UTC)
            records = await self.load()

            # Locate the active old managed key by token_hash.
            # Filtering status=="active" makes double-rotation safe: a retry
            # with the same current_token finds the already-revoked record and
            # skips it, returning old_record=None instead of re-mutating it.
            old_record: KeyRecord | None = None
            for record in records:
                if (
                    record.id is not None
                    and record.token_hash == current_hash
                    and record.status == "active"
                ):
                    old_record = record
                    break

            # Mark old record revoked or grace-expired.
            if old_record is not None:
                if grace_seconds == 0:
                    old_record.status = "revoked"
                else:
                    old_record.expires_at = now + timedelta(seconds=grace_seconds)
                    # Keep status="active" so the key is accepted during grace window.

            # Create the new key record.
            new_key_id = str(uuid.uuid4())
            new_raw_token = secrets.token_hex(32)
            new_token_hash = hashlib.sha256(new_raw_token.encode()).hexdigest()
            new_record = KeyRecord(
                id=new_key_id,
                token_hash=new_token_hash,
                namespace="default",
                label=None,
                created_at=now,
                expires_at=None,
                status="active",
            )
            records.append(new_record)
            self._write(records)

        return {
            "new_key_id": new_key_id,
            "new_token": new_raw_token,
            "old_record": old_record,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def load_synthetic_records(self, synthetic: list[KeyRecord]) -> None:
        """Replace all synthetic (TOML) records in ``keys.json`` with ``synthetic``.

        Called once at ``create_app()`` startup to persist the current TOML
        ``[namespaces]`` tokens as synthetic ``KeyRecord`` objects.  Existing
        managed records (``id is not None``) are preserved; synthetic records
        (``id is None``) from the previous run are discarded and replaced with
        the current set.  This ensures that removing a namespace from TOML is
        reflected after the next restart.

        The write acquires the internal asyncio.Lock to serialise against
        concurrent ``create()`` / ``revoke()`` calls.
        """
        async with self._lock:
            existing = await self.load()
            managed = [r for r in existing if r.id is not None]
            merged = managed + synthetic
            self._write(merged)

    def _write(self, records: list[KeyRecord]) -> None:
        """Atomically write records to ``keys.json`` with mode 0o600."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [r.model_dump(mode="json") for r in records],
        ).encode()
        atomic_write_bytes(self._path, payload, mode=0o600)

ENV_VAR: str = "ARCHON_SEARCH_API_KEY"

_HEX_RE = re.compile(r"^[0-9a-f]+$")


def get_key_file() -> Path:
    """Return the API key file path, resolved fresh on every call.

    Resolution order:

    1. ``$ARCHON_SEARCH_KEY_FILE`` if set and non-whitespace — stripped,
       expanded via ``Path.expanduser()``, required to be absolute. Empty
       or whitespace-only is treated as "not set" and falls through to
       step 2 (the plan deliberately keeps this lenient for ``KEY_FILE``
       — unlike ``ARCHON_SEARCH_DATA_DIR`` which raises on empty — so
       operators can unset an override without unsetting the env var
       entirely).
    2. ``get_data_dir() / ".search.env"`` otherwise.

    ``ARCHON_SEARCH_KEY_FILE`` takes precedence over ``ARCHON_SEARCH_DATA_DIR``
    so operators can pin the key file location independently of the rest of
    the runtime state directory.

    Raises ``ValueError`` if ``ARCHON_SEARCH_KEY_FILE`` resolves to a
    relative path (CWD inside a container is not contractually stable) or
    contains ``~`` but HOME is unset (``Path.expanduser`` raises
    ``RuntimeError`` — translated for parity with ``get_data_dir``).
    """
    raw = os.environ.get("ARCHON_SEARCH_KEY_FILE")
    if raw is not None:
        stripped = raw.strip()
        if stripped:
            try:
                result = Path(stripped).expanduser()
            except RuntimeError as exc:
                raise ValueError(
                    f"ARCHON_SEARCH_KEY_FILE={raw!r} contains '~' but HOME is not set"
                ) from exc
            if not result.is_absolute():
                raise ValueError(
                    f"ARCHON_SEARCH_KEY_FILE must be an absolute path, got {raw!r}"
                )
            return result
    return get_data_dir() / ".search.env"


def load_or_generate_key() -> tuple[str, str]:
    """Return (key, source). Source is 'env var', 'file: ...', or 'auto-generated'.

    The key file path is resolved once at the start of the file/auto-generate
    branches and threaded through so the reported source path always matches
    the path that was actually read or written (no TOCTOU between resolve
    and report).
    """
    key = _load_from_env()
    if key is not None:
        return key, "env var"

    key_file = get_key_file()
    key = _load_from_file(key_file)
    if key is not None:
        return key, f"file: {key_file}"

    key = _generate_and_write(key_file)
    return key, "auto-generated"


def _load_from_env() -> str | None:
    val = os.environ.get(ENV_VAR, "")
    if not val:
        return None
    if not _validate_key(val):
        logger.warning("%s is set but contains an invalid value (expected lowercase hex string)", ENV_VAR)
        return None
    return val


def _load_from_file(key_file: Path) -> str | None:
    """Read and validate the API key from *key_file*.

    *key_file* must be the path resolved by ``get_key_file()`` at the start
    of the current call to ``load_or_generate_key()``. Callers thread the
    resolved path in so the reported source string (``f"file: {key_file}"``)
    cannot drift if ``ARCHON_SEARCH_KEY_FILE``/``ARCHON_SEARCH_DATA_DIR``
    change between resolve and report (TOCTOU).
    """
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


def _generate_and_write(key_file: Path) -> str:
    """Atomically write a freshly generated key to *key_file* and return it.

    *key_file* must be the path resolved by ``get_key_file()`` at the start
    of the current call to ``load_or_generate_key()`` — see
    ``_load_from_file`` for the same contract.
    """
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
                existing = _load_from_file(key_file)
                if existing is not None:
                    return existing
                try:
                    os.unlink(str(tmp))
                except OSError:
                    pass
                continue
            existing = _load_from_file(key_file)
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
