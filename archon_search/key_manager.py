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
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from archon_search._durable_io import (
    PRIVATE_FILE_MODE,
    DurabilityUnconfirmedError,
    atomic_write_bytes,
    ensure_private_dir,
)
from archon_search.constants import (
    DEFAULT_NAMESPACE,
    MAX_ROTATE_GRACE_SECONDS,
    MIN_API_KEY_LENGTH,
)
from archon_search.paths import get_data_dir

logger = logging.getLogger(__name__)

#: Random bytes behind every generated API key: 32 bytes are 64 lowercase hex characters.
API_KEY_ENTROPY_BYTES: Final = 32
#: ``stat`` mode bits that are permissions (without the file-type bits).
PERMISSION_BITS: Final = 0o777
#: Tries at creating the key file when a concurrent writer is in the way.
KEY_WRITE_ATTEMPTS: Final = 2
#: Pause between looks at a concurrent writer's key file while it is being published.
KEY_WRITE_CONFLICT_RETRY_SECONDS: Final = 0.1
#: How long a loser waits for a live writer's temp file to become the key file before treating it as an orphan.
KEY_WRITE_CONFLICT_WAIT_SECONDS: Final = 2.0

# ``(st_mtime_ns, st_size, st_ino)`` of keys.json: the key under which KeyStore caches its parsed records.
_FileSignature = tuple[int, int, int]


# ---------------------------------------------------------------------------
# KeyRecord entity (C1)
# ---------------------------------------------------------------------------


class KeyRecord(BaseModel):
    """A single API key record stored in keys.json.

    Raw bearer tokens are never stored — only the SHA-256 hex digest
    (``token_hash``) is persisted. The raw token is printed once to
    stdout at creation time and is never recoverable from disk.

    ``admin`` is the capability to manage keys across namespaces and to run the server-wide control
    endpoints (see ``archon_search.server.authz``). It defaults to ``False``: a record built in code
    is never admin unless the caller says so. Records read from an existing ``keys.json`` that has no
    ``admin`` field are resolved by :func:`_parse_stored_record`.
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None
    token_hash: str
    namespace: str
    label: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    status: Literal["active", "revoked"] = "active"
    admin: bool = False


def _parse_stored_record(item: object) -> KeyRecord:
    """Validate one ``keys.json`` entry, resolving a missing ``admin`` field.

    Compatibility rule for a ``keys.json`` written before the ``admin`` capability existed: a record
    with no ``admin`` field is admin if and only if it is a managed key (``id`` is not null) of the
    default namespace. Before the capability every key could do everything, so the default-namespace
    keys (the rotated default key, keys issued for ``default``) keep their power and operators are not
    locked out of key management after upgrading; keys of every other namespace, and TOML synthetic
    records (``id`` null, re-written on every startup), become non-admin. The resolved value is
    persisted the next time the file is written, so the rule applies once per record.
    """
    if isinstance(item, dict) and "admin" not in item:
        item = {**item, "admin": item.get("id") is not None and item.get("namespace") == DEFAULT_NAMESPACE}
    return KeyRecord.model_validate(item)


class KeyRotationInputError(ValueError):
    """Invalid rotation argument (``grace_seconds``); its message is codebase-built and safe to surface.

    Any other ``ValueError`` from rotation (e.g. a pydantic ``ValidationError`` while
    loading ``keys.json``) may embed stored data and must not reach the caller.
    """


class BlankCurrentKeyError(ValueError):
    """The current default key handed to a rotation is blank; its message is codebase-built.

    A transport reads that key from server state, so this is a server fault rather than
    the caller's input error (MCP answers ``internal_error``, not ``validation_error``).
    A sibling of ``KeyRotationInputError``, not a subclass, so neither handler catches the other.
    """


# Sanitized wire text for a key mutation refused because keys.json exists but cannot be
# read or parsed; the underlying exception (path, stored data) goes to the server log only.
KEY_STORE_UNREADABLE_DETAIL = "keys.json could not be read; no key changes were made; see the server log for details"


class KeyStoreUnreadableError(Exception):
    """``keys.json`` exists but cannot be read or parsed; a mutation refused rather than overwrite it.

    Wire text: ``KEY_STORE_UNREADABLE_DETAIL``. Auth reads (``load`` / ``active_keys``)
    still degrade to an empty key list (S17); only mutations fail closed, because writing
    back the records they loaded would permanently delete every managed key.
    """


# Sanitized rotation-failure wire texts, shared by REST ``POST /keys/rotate`` and MCP
# ``rotate_key``: the underlying exception embeds the key-file / keys.json path or stored
# data, so it goes to the server log only.
ROTATE_WRITE_FAILED_DETAIL = "Failed to write .search.env — rotation aborted; keys.json unchanged."


class KeyFileWriteError(Exception):
    """``.search.env`` could not be written; ``keys.json`` is unchanged. Wire text: ``ROTATE_WRITE_FAILED_DETAIL``."""


ROTATE_KEYS_JSON_FAILED_DETAIL = (
    "Rotation incomplete: .search.env holds the new key but keys.json was not updated; "
    "see the server log for details"
)


class RotationIncompleteError(Exception):
    """``.search.env`` holds the new key but ``keys.json`` was not updated. Wire text: ``ROTATE_KEYS_JSON_FAILED_DETAIL``.

    Operator-recoverable (``Documentation/OperatorGuide/70_key_management_and_rotation.md``, "Recovering from an
    incomplete rotation"); no rollback is attempted.
    """


class KeyWriteConflictError(Exception):
    """The key file could not be created because a concurrent writer's file never became readable."""


def hash_token(token: str) -> str:
    """The SHA-256 hex digest under which *token* is stored in ``keys.json`` and compared on a request."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class CreatedKey:
    """A freshly issued managed key: its id, the raw bearer token (shown once, never stored) and its creation time."""

    id: str
    token: str
    created_at: datetime


@dataclass(frozen=True)
class RotationOutcome:
    """The result of recording a default-key rotation.

    *old_record* is the retired record **after** mutation, the synthesized grace record, or ``None`` when no active
    managed record matched and the grace period was ``0``. *new_token* is the raw token (never stored).
    """

    new_key_id: str
    new_token: str
    old_record: KeyRecord | None


def validate_rotation_grace(grace_seconds: int) -> None:
    """Raise ``KeyRotationInputError`` for a ``grace_seconds`` outside ``[0, MAX_ROTATE_GRACE_SECONDS]``."""
    if grace_seconds < 0:
        raise KeyRotationInputError(f"grace_seconds must be >= 0, got {grace_seconds!r}")
    if grace_seconds > MAX_ROTATE_GRACE_SECONDS:
        raise KeyRotationInputError(f"grace_seconds must be <= {MAX_ROTATE_GRACE_SECONDS}, got {grace_seconds!r}")


def validate_current_token(current_token: str) -> None:
    """Raise ``BlankCurrentKeyError`` for a blank ``current_token``."""
    if not current_token or not current_token.strip():
        raise BlankCurrentKeyError("current_token must not be empty")


# ---------------------------------------------------------------------------
# KeyStore use-case class (C2)
# ---------------------------------------------------------------------------


class KeyStore:
    """Durable key store backed by ``keys.json``.

    Design:
    - No in-memory key list is authoritative. ``active_keys()`` and ``load()`` re-validate their view
      of ``keys.json`` against the file's ``(st_mtime_ns, st_size, st_ino)`` on every call and only
      re-parse when it changed, so a change written by another process or ``KeyStore`` instance on
      the same file (an atomic replace gives it a new inode and mtime) is seen on the next call, while
      an unchanged file costs one ``stat`` instead of a read and a JSON parse per request. Every write
      through this instance also drops the cached view. The REST server creates one instance
      (``app.state.key_store``) and shares it with the mounted MCP app. ``load()`` hands out copies of the
      cached records; mutations re-parse via :meth:`_load_for_update`, never through the cache.
    - An internal ``asyncio.Lock`` serialises every read-modify-write cycle
      (``create``; extended in later tasks). ``active_keys()`` / ``load()``
      do NOT need the lock (each call gets a consistent snapshot of the file).
    - ``rotation_lock`` serialises the whole default-key rotation
      (:func:`rotate_and_persist_default_key`) across every transport sharing this
      instance (REST and the mounted MCP app share ``app.state.key_store``). It is
      separate from ``_lock`` because the rotation's ``keys.json`` step takes ``_lock``.
      Both locks bind to an event loop only when first contended (Python >= 3.10).
    - Mutations fail closed: if ``keys.json`` exists but cannot be read or parsed they
      raise ``KeyStoreUnreadableError`` instead of overwriting it with only new records.
    - ``_logged_expired_ids`` suppresses repeated INFO logs for the same
      expired key across calls within one process lifetime.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self.rotation_lock = asyncio.Lock()
        self._logged_expired_ids: set[str] = set()
        self._cache: tuple[_FileSignature, tuple[KeyRecord, ...]] | None = None

    # ------------------------------------------------------------------
    # Read-only helpers (no lock required)
    # ------------------------------------------------------------------

    async def load(self) -> list[KeyRecord]:
        """Return all records, re-parsing ``keys.json`` only when its file signature changed.

        Returns an empty list (and logs ERROR once per file state) if the file is missing,
        unreadable, non-array JSON, or contains any invalid record —
        implementing the all-or-nothing corruption policy (S17) for reads.
        Mutations use :meth:`_load_for_update`, which fails closed instead.

        The records are copies: a caller mutating one cannot change what the next call sees. A failure caused by
        an I/O error is not cached (it may be transient); a corrupt file is, so it is logged once per file state.
        """
        # The signature is taken BEFORE the read: a file replaced in between is cached under the
        # old signature and simply re-read on the next call; the reverse order could pin stale content
        # to the new signature.
        signature = self._file_signature()
        cached = self._cache
        if signature is not None and cached is not None and cached[0] == signature:
            return [record.model_copy() for record in cached[1]]
        try:
            records = self._load_for_update()
        except KeyStoreUnreadableError as exc:
            if signature is not None and not isinstance(exc.__cause__, OSError):
                self._cache = (signature, ())
            return []
        if signature is not None:
            self._cache = (signature, tuple(records))
        return [record.model_copy() for record in records]

    def _file_signature(self) -> _FileSignature | None:
        """Return ``(st_mtime_ns, st_size, st_ino)`` of ``keys.json``, or ``None`` when it cannot be stat'ed."""
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _load_for_update(self) -> list[KeyRecord]:
        """Return all records; raise ``KeyStoreUnreadableError`` (logged ERROR) if the file exists but is unusable.

        A missing file is an empty store.
        """
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise _unreadable("keys.json read error: %s", exc) from exc

        # Tighten permissions on files that may have been created with a
        # permissive umask (e.g. by a different tool or manual creation).
        _chmod_600(self._path)

        if not raw:
            raise _unreadable("keys.json is empty — treating as corrupted key store")

        try:
            data = json.loads(raw)
        # ValueError covers JSONDecodeError, UnicodeDecodeError and an over-long integer literal;
        # RecursionError covers excessive nesting.
        except (ValueError, RecursionError) as exc:
            raise _unreadable("keys.json is not valid JSON — treating as corrupted key store: %s", exc) from exc

        if not isinstance(data, list):
            raise _unreadable("keys.json does not contain a JSON array — treating as corrupted key store")

        try:
            return [_parse_stored_record(item) for item in data]
        except Exception as exc:
            raise _unreadable("keys.json contains invalid records — treating as corrupted key store: %s", exc) from exc

    async def verify_readable(self) -> None:
        """Raise ``KeyStoreUnreadableError`` if ``keys.json`` exists but cannot be read or parsed."""
        self._load_for_update()

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
            records = self._load_for_update()
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
        *,
        admin: bool = False,
    ) -> CreatedKey:
        """Generate a new API key, persist its hash, return it as a :class:`CreatedKey`.

        The raw bearer token is returned exactly once — it is never stored
        on disk. ``keys.json`` stores only the SHA-256 hex digest.

        ``created_at`` is the UTC datetime used when persisting the record — callers
        should use this value to avoid timestamp divergence between the response
        and what ``GET /keys`` will return from disk.
        """
        if expires_at is not None and expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")

        key_id = str(uuid.uuid4())
        raw_token = secrets.token_hex(API_KEY_ENTROPY_BYTES)  # 64 hex chars
        token_hash = hash_token(raw_token)
        created_at = datetime.now(UTC)

        record = KeyRecord(
            id=key_id,
            token_hash=token_hash,
            namespace=ns,
            label=label,
            created_at=created_at,
            expires_at=expires_at,
            status="active",
            admin=admin,
        )

        async with self._lock:
            records = self._load_for_update()
            records.append(record)
            self._write(records)

        return CreatedKey(id=key_id, token=raw_token, created_at=created_at)

    async def record_rotation(
        self,
        current_token: str,
        grace_seconds: int,
        new_token: str,
    ) -> RotationOutcome:
        """Record an already-validated default-key rotation in ``keys.json``.

        Adds a managed ``KeyRecord`` for *new_token* and, if *current_token* matches an
        active managed record, marks it revoked (``grace_seconds == 0``) or sets its
        ``expires_at = now + grace_seconds`` (it stays ``status="active"`` during the grace
        window). An unmanaged current key (e.g. an auto-generated key living only in
        ``.search.env``) gets a synthesized grace record from its hash when
        ``grace_seconds > 0``; with ``grace_seconds == 0`` only the new record is created.

        This only mutates ``keys.json``. Its caller, :func:`rotate_and_persist_default_key`,
        validates the inputs (:func:`validate_rotation_grace`, :func:`validate_current_token`)
        and writes *new_token* to ``.search.env`` first, so a failed key-file write never
        mutates ``keys.json``.

        Returns:
            The :class:`RotationOutcome`: ``old_record`` is the old record **after** mutation, the
            synthesized grace record, or ``None`` when no active managed record matched and
            ``grace_seconds == 0``.

        Raises:
            KeyStoreUnreadableError: ``keys.json`` exists but cannot be read or parsed.
        """
        current_hash = hash_token(current_token)

        async with self._lock:
            now = datetime.now(UTC)
            records = self._load_for_update()

            # Locate the active old managed key by token_hash.
            # Filtering status=="active" makes double-rotation safe: a retry
            # with the same current_token finds the already-revoked record and
            # skips it, returning old_record=None instead of re-mutating it.
            old_record: KeyRecord | None = None
            current_hash_seen = False
            for record in records:
                if record.id is not None and record.token_hash == current_hash:
                    current_hash_seen = True
                    if record.status == "active":
                        old_record = record
                        break

            # Mark old record revoked or grace-expired.
            if old_record is not None:
                if grace_seconds == 0:
                    old_record.status = "revoked"
                else:
                    old_record.expires_at = now + timedelta(seconds=grace_seconds)
                    # Keep status="active" so the key is accepted during grace window.
            elif grace_seconds > 0 and not current_hash_seen:
                # The old default key was never a managed record (e.g. an
                # auto-generated key living only in .search.env). Synthesize a
                # grace record so it stays accepted during the grace window —
                # otherwise, once .search.env holds the new token, the old key
                # matches neither a managed record nor the legacy fallback and is
                # rejected immediately (S133). Only the SHA-256 hash is persisted;
                # the raw token is never stored (invariant preserved).
                #
                # ``not current_hash_seen`` preserves double-rotation idempotency:
                # a retry with a current_token that matches an already-revoked
                # record must NOT resurrect it as a fresh active grace record.
                old_record = KeyRecord(
                    id=str(uuid.uuid4()),
                    token_hash=current_hash,
                    namespace=DEFAULT_NAMESPACE,
                    label=None,
                    created_at=now,
                    expires_at=now + timedelta(seconds=grace_seconds),
                    status="active",
                    admin=True,
                )
                records.append(old_record)

            new_key_id = str(uuid.uuid4())
            new_token_hash = hash_token(new_token)
            new_record = KeyRecord(
                id=new_key_id,
                token_hash=new_token_hash,
                namespace=DEFAULT_NAMESPACE,
                label=None,
                created_at=now,
                expires_at=None,
                status="active",
                admin=True,
            )
            records.append(new_record)
            self._write(records)

        # model_copy() returns a snapshot — prevents post-write mutation
        # by the caller from diverging from the on-disk state.
        return RotationOutcome(
            new_key_id=new_key_id,
            new_token=new_token,
            old_record=old_record.model_copy() if old_record is not None else None,
        )

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
            existing = self._load_for_update()
            managed = [r for r in existing if r.id is not None]
            merged = managed + synthetic
            self._write(merged)

    def _write(self, records: list[KeyRecord]) -> None:
        """Atomically write records to ``keys.json`` with the private file mode.

        Runs on the calling thread (the event loop, for every async mutation). A failed
        directory fsync after the rename means the records were written, so it is logged
        and the mutation succeeds rather than reporting a write that did happen as failed.
        """
        self._cache = None
        ensure_private_dir(self._path.parent)
        payload = json.dumps(
            [r.model_dump(mode="json") for r in records],
        ).encode()
        try:
            atomic_write_bytes(self._path, payload)
        except DurabilityUnconfirmedError:
            logger.warning("keys.json written but its rename durability is unconfirmed", exc_info=True)


def _unreadable(message: str, *args: object) -> KeyStoreUnreadableError:
    """Log *message* at ERROR and return the ``KeyStoreUnreadableError`` to raise."""
    logger.error(message, *args)
    return KeyStoreUnreadableError(KEY_STORE_UNREADABLE_DETAIL)


ENV_VAR: str = "ARCHON_SEARCH_API_KEY"


def resolve_rotation_key_file() -> Path:
    """Return ``get_key_file()`` for a rotation; an unusable ``ARCHON_SEARCH_KEY_FILE`` is a write failure.

    Raises:
        KeyFileWriteError: ``get_key_file()`` rejected the env value (logged with its text).
    """
    try:
        return get_key_file()
    except ValueError as exc:
        logger.exception("rotate: cannot resolve the key file — rotation aborted; keys.json unchanged")
        raise KeyFileWriteError(ROTATE_WRITE_FAILED_DETAIL) from exc


async def rotate_and_persist_default_key(
    key_store: KeyStore,
    *,
    key_file: Path,
    read_current_token: Callable[[], Awaitable[str]],
    grace_seconds: int,
    publish_new_token: Callable[[str], None] | None = None,
) -> RotationOutcome:
    """Rotate the default key: write the new token to *key_file*, then record it in ``keys.json``.

    The rotate use case shared by REST ``POST /keys/rotate`` and MCP ``rotate_key``; each
    transport supplies how it reads the current default key (and, optionally, publishes the
    new one) and maps the exceptions below to its error envelope. Crash-safe write order: if
    the *key_file* write fails, ``keys.json`` is never mutated and the old key stays active.

    The whole sequence — read current key, write *key_file*, record in ``keys.json``,
    publish — runs under ``key_store.rotation_lock``. That serialises rotations only among
    callers in this process that share the same *key_store* object (REST and the mounted MCP
    app share ``app.state.key_store``); it does not span processes. It also does not by itself
    make two transports agree on the current key: each rotation retires the key the previous
    one issued only when every transport reads and publishes the same current key (both use
    ``app.state.api_key`` when MCP is mounted on the REST server).

    Once the *key_file* write starts, the rest of the sequence runs to completion even if the
    caller is cancelled: it runs in a shielded task, the lock is held until that task ends,
    the outcome is logged (``rotate: cancelled mid-rotation, ...``), and only then does the
    ``CancelledError`` propagate. So cancelling the caller alone never leaves *key_file* ahead of
    ``keys.json``; only a rotation that itself fails (or whose own task is cancelled) can.

    *grace_seconds* is validated before the current key is read; the current key once,
    after it is read. *publish_new_token* runs inside the lock, only after both writes.

    Returns ``KeyStore.record_rotation``'s :class:`RotationOutcome`.

    Raises:
        KeyRotationInputError: *grace_seconds* is out of range, raised before the current key
            is read and before anything is written; its text is curated.
        BlankCurrentKeyError: the current key is blank (a server fault), raised before
            anything is written; its text is curated.
        KeyStoreUnreadableError: ``keys.json`` exists but cannot be read; no rotation was written
            (an MCP *read_current_token* without ``api_key_state`` may already have created
            *key_file* via ``load_or_generate_key``).
        KeyFileWriteError: *key_file* (or its directory) could not be written.
        RotationIncompleteError: *key_file* holds the new key but ``keys.json`` was not updated.
    """
    try:
        validate_rotation_grace(grace_seconds)
    except KeyRotationInputError as exc:
        # WARNING, whatever the source: an MCP caller's argument (validation_error), or on REST an
        # out-of-range ``auth.rotate_grace_seconds`` config (the body is schema-bounded), which
        # routes_keys answers 500. A blank current key below is always a server fault: ERROR.
        logger.warning("rotate: rejected before writing .search.env: %s", exc)
        raise

    async with key_store.rotation_lock:
        current_token = await read_current_token()
        try:
            validate_current_token(current_token)
        except BlankCurrentKeyError as exc:
            logger.error("rotate: rejected before writing .search.env: %s", exc)
            raise
        await key_store.verify_readable()

        commit = asyncio.create_task(
            _commit_rotation(key_store, key_file, current_token, grace_seconds, publish_new_token)
        )
        try:
            return await asyncio.shield(commit)
        except asyncio.CancelledError:
            await _finish_cancelled_rotation(commit, key_file)
            raise


async def _commit_rotation(
    key_store: KeyStore,
    key_file: Path,
    current_token: str,
    grace_seconds: int,
    publish_new_token: Callable[[str], None] | None,
) -> RotationOutcome:
    """Write *key_file*, record the rotation in ``keys.json``, then publish; the post-read half of the use case."""
    new_token = secrets.token_hex(API_KEY_ENTROPY_BYTES)  # 64 hex chars
    await _write_key_file(key_file, new_token)

    try:
        result = await key_store.record_rotation(
            current_token=current_token,
            grace_seconds=grace_seconds,
            new_token=new_token,
        )
    except Exception as exc:
        logger.exception("rotate: keys.json update failed after %s was written", key_file)
        raise RotationIncompleteError(ROTATE_KEYS_JSON_FAILED_DETAIL) from exc

    # Defensive: record_rotation must echo back the token we passed in.
    # If this ever diverges, .search.env and keys.json would be out of sync.
    if result.new_token != new_token:
        logger.error("rotate: keys.json recorded an unexpected token after %s was written — BUG", key_file)
        raise RotationIncompleteError(ROTATE_KEYS_JSON_FAILED_DETAIL)

    if publish_new_token is not None:
        publish_new_token(new_token)
    return result


async def _finish_cancelled_rotation(commit: asyncio.Task[RotationOutcome], key_file: Path) -> None:
    """Wait (ignoring further cancels) for a rotation whose caller was cancelled, then log its outcome.

    The ``.search.env`` write runs in a thread a cancel cannot stop, so abandoning *commit*
    could leave the new key there with ``keys.json`` never updated. The caller re-raises
    its ``CancelledError`` after this returns, still holding the rotation lock until then.
    """
    while not commit.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({commit})
    if commit.cancelled():
        logger.error(
            "rotate: cancelled mid-rotation and the rotation task was cancelled too; check %s and keys.json", key_file
        )
    elif (failure := commit.exception()) is not None:
        logger.error("rotate: cancelled mid-rotation, and the rotation failed", exc_info=failure)
    else:
        logger.warning(
            "rotate: cancelled mid-rotation, but it completed: %s holds the new key (new_key_id=%s); "
            "the caller did not receive its token",
            key_file,
            commit.result().new_key_id,
        )


async def _write_key_file(key_file: Path, new_token: str) -> None:
    """Write *new_token* to *key_file* off the event loop; raise ``KeyFileWriteError`` if it did not land.

    asyncio.to_thread keeps this fsync chain (write → fsync → replace → fsync dir) off the
    event loop; the later ``keys.json`` write (``KeyStore._write``) runs on the loop.
    """
    payload = f"{ENV_VAR}={new_token}\n".encode()
    try:
        await asyncio.to_thread(key_file.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(atomic_write_bytes, key_file, payload)
    except DurabilityUnconfirmedError:
        # os.replace already published the new key: treat it as written.
        logger.warning("rotate: %s written but its rename durability is unconfirmed", key_file, exc_info=True)
    except OSError as exc:
        logger.exception("rotate: failed to write %s — keys.json NOT modified; rotation aborted", key_file)
        raise KeyFileWriteError(ROTATE_WRITE_FAILED_DETAIL) from exc


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


def get_keys_file() -> Path:
    """Return the ``keys.json`` path of the durable key store, resolved fresh on every call from ``get_data_dir()``."""
    return get_data_dir() / "keys.json"


def load_or_generate_key() -> tuple[str, str]:
    """Return (key, source). Source is 'env var', 'file: ...', or 'auto-generated'.

    The key file path is resolved once at the start of the file/auto-generate
    branches and threaded through so the reported source path always matches
    the path that was actually read or written (no TOCTOU between resolve
    and report).

    When the key comes from the env var, it is also written to the key file so
    that CLI commands running in the same environment (Docker terminal,
    docker exec) can authenticate without any manual setup.
    """
    key = _load_from_env()
    if key is not None:
        persist_key(key)
        return key, "env var"

    key_file = get_key_file()
    key, problem = _parse_key_file(key_file)
    if problem is not None:
        # The operator's file holds a key that cannot be used. Generating a new one over it would lock every client
        # out with only a log line to say why, so — like an invalid env var — refuse and leave the file untouched.
        from archon_search.config import ConfigError

        raise ConfigError(
            f"{key_file} holds an invalid {ENV_VAR}: {problem}. "
            "Fix the value (or the file's permissions), or delete the file to have the server generate a new key; "
            "the file was left untouched."
        )
    if key is not None:
        return key, f"file: {key_file}"

    key = _generate_and_write(key_file)
    return key, "auto-generated"


def load_key() -> str | None:
    """Return the API key from env or key file, or None if not found.

    Never generates or writes a new key — safe to call in read-only environments.
    """
    key = _load_from_env()
    if key is not None:
        return key
    return _load_from_file(get_key_file())


def persist_key(key: str) -> None:
    """Write *key* to the key file so local CLI commands can authenticate.

    Called by the server at startup when the key comes from an env var —
    the env var is not visible to CLI processes that weren't given it
    explicitly, but the key file is always readable from the data dir.

    Silently swallows all I/O errors (PermissionError, EROFS, ENOSPC, …)
    so a read-only or full data dir never crashes the server startup path.
    """
    try:
        key_file = get_key_file()
        _generate_and_write(key_file, key=key)
    except (ValueError, OSError, KeyWriteConflictError):
        pass


def _load_from_env() -> str | None:
    """Return the default key from ``ARCHON_SEARCH_API_KEY``, ``None`` when unset.

    Raises:
        ConfigError: the variable is set but is not a usable key (see :func:`api_key_problem`). The server
            refuses to start rather than silently ignore an explicitly configured key. The message never
            contains the value.
    """
    val = os.environ.get(ENV_VAR, "")
    if not val:
        return None
    problem = api_key_problem(val)
    if problem is not None:
        from archon_search.config import ConfigError

        raise ConfigError(
            f"{ENV_VAR} is not a valid API key: {problem}. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return val


def _parse_key_file(key_file: Path) -> tuple[str | None, str | None]:
    """Read the API key of *key_file*: ``(key, None)``, ``(None, None)`` when it holds none, or ``(None, problem)``.

    *problem* (:func:`api_key_problem`, never the value) is set when the file has an ``ARCHON_SEARCH_API_KEY`` line
    whose value cannot be the default key.

    *key_file* must be the path resolved by ``get_key_file()`` at the start
    of the current call to ``load_or_generate_key()``. Callers thread the
    resolved path in so the reported source string (``f"file: {key_file}"``)
    cannot drift if ``ARCHON_SEARCH_KEY_FILE``/``ARCHON_SEARCH_DATA_DIR``
    change between resolve and report (TOCTOU).
    """
    if not key_file.exists():
        return None, None

    # Attempt to tighten permissions if too wide (a regular file only: chmod 600 on a directory would lock it)
    try:
        mode = os.stat(key_file).st_mode & PERMISSION_BITS
        if mode != PRIVATE_FILE_MODE and key_file.is_file():
            _chmod_600(key_file)
    except OSError as exc:
        logger.warning("Could not check the permissions of %s: %s", key_file, exc)

    try:
        content = key_file.read_text(encoding="utf-8-sig")  # a BOM (Windows editors) must not hide the key line
    except OSError as exc:
        # An existing file that cannot be read is not "no key": treating it so would have the caller generate a new
        # key over the operator's file.
        logger.error("Key file %s exists but cannot be read: %s", key_file, exc)
        return None, f"it could not be read ({exc.strerror or type(exc).__name__})"
    except UnicodeDecodeError:
        # Never echo the decode error: it carries the offending bytes of a credential file.
        logger.error("Key file %s exists but is not valid UTF-8", key_file)
        return None, "it is not valid UTF-8 text"

    for line in content.splitlines():
        if line.startswith(f"{ENV_VAR}="):
            raw = line[len(f"{ENV_VAR}="):]
            val = raw.strip()
            problem = api_key_problem(val)
            if problem is not None:
                return None, problem
            return val, None

    return None, None


def _load_from_file(key_file: Path) -> str | None:
    """Read and validate the API key from *key_file*; ``None`` (and an ERROR log for an invalid value) when unusable.

    For callers that must not fail on a bad file (the CLI's :func:`load_key`, the concurrent-write retry of
    :func:`_generate_and_write`); :func:`load_or_generate_key` refuses to start instead.
    """
    key, problem = _parse_key_file(key_file)
    if problem is not None:
        logger.error("Key file contains an invalid value for %s: %s", ENV_VAR, problem)
    return key


def api_key_problem(value: str) -> str | None:
    """Return why *value* cannot be the default API key, or ``None`` when it can.

    The default key must be lowercase hex and at least ``MIN_API_KEY_LENGTH`` characters (keys the
    server generates are 64). The returned text names the rule and the length, never the value.
    """
    if not value:
        return "it is empty"
    if not _HEX_RE.fullmatch(value):
        return "it must be a lowercase hex string"
    if len(value) < MIN_API_KEY_LENGTH:
        return f"it must be at least {MIN_API_KEY_LENGTH} characters long (got {len(value)})"
    return None


def namespace_token_problem(token: str) -> str | None:
    """Return why *token* cannot be a TOML ``[namespaces]`` bearer token, or ``None`` when it can.

    Such a token must be at least ``MIN_API_KEY_LENGTH`` characters of printable, non-whitespace ASCII
    (an empty token would otherwise authenticate an empty ``Bearer`` value). The returned text never
    contains the token.
    """
    if not token:
        return "it is empty"
    if not token.isascii() or not token.isprintable() or any(ch.isspace() for ch in token):
        return "it must be printable ASCII without whitespace"
    if len(token) < MIN_API_KEY_LENGTH:
        return f"it must be at least {MIN_API_KEY_LENGTH} characters long (got {len(token)})"
    return None


def _await_concurrent_key(key_file: Path, tmp: Path) -> tuple[str | None, bool]:
    """Wait for the key a concurrent writer is publishing: ``(key, False)`` once *key_file* holds one.

    While *tmp* exists its writer is live, so the wait lasts as long as *tmp* does, up to
    ``KEY_WRITE_CONFLICT_WAIT_SECONDS``; a *tmp* that outlives that is an orphan, and the caller removes it. Without
    a key the result is ``(None, leftover)``: *leftover* is true when *key_file* existed, keyless, when it was last
    read. That is checked *before* the key is read, so a file that appears after the read, a writer's finished key,
    is never mistaken for a leftover and replaced.
    """
    deadline = time.monotonic() + KEY_WRITE_CONFLICT_WAIT_SECONDS
    while True:
        leftover = key_file.exists()
        existing = _load_from_file(key_file)
        if existing is not None:
            return existing, False
        if not tmp.exists() or time.monotonic() >= deadline:
            return None, leftover
        time.sleep(KEY_WRITE_CONFLICT_RETRY_SECONDS)


def _generate_and_write(key_file: Path, key: str | None = None) -> str:
    """Atomically write *key* (or a freshly generated one) to *key_file*; return the key the file holds.

    *key_file* must be the path resolved by ``get_key_file()`` at the start
    of the current call to ``load_or_generate_key()`` — see
    ``_load_from_file`` for the same contract.

    A freshly generated key is *created*, never swapped in over an existing file: when two processes start together
    the loser finds the winner's key on disk and returns that one, so both run with the key the file holds. A loser
    never removes a temp file whose writer is still running (see :func:`_await_concurrent_key`). An explicit *key*
    (the operator's env var) replaces the file, as does a leftover file that holds no key at all.

    Raises:
        KeyWriteConflictError: a concurrent writer's file never became readable.
    """
    ensure_private_dir(key_file.parent)
    generated = key is None
    if key is None:
        key = secrets.token_hex(API_KEY_ENTROPY_BYTES)
    payload = f"{ENV_VAR}={key}\n".encode()
    tmp = key_file.with_suffix(key_file.suffix + ".tmp")

    replace = not generated
    for _ in range(KEY_WRITE_ATTEMPTS):
        try:
            atomic_write_bytes(key_file, payload, replace=replace)
            return key
        except FileExistsError:
            existing, leftover = _await_concurrent_key(key_file, tmp)
            if existing is not None:
                return existing
            try:
                os.unlink(str(tmp))
            except OSError:
                logger.debug("could not remove the stale key temp file %s", tmp, exc_info=True)
            replace = replace or leftover
    raise KeyWriteConflictError("key generation failed: concurrent write conflict")


def _chmod_600(path: Path) -> None:
    if sys.platform == "win32":
        logger.info("permission check skipped on Windows")
        return
    try:
        path.chmod(PRIVATE_FILE_MODE)
    except PermissionError:
        logger.warning("Could not set permissions to 600 on %s", path)
