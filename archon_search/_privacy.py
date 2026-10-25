"""Shared privacy utilities for archon-search.

Provides non-reversible log correlation tokens so that per-request
log messages can be correlated without leaking raw user-query text.
"""
from __future__ import annotations

import hmac
import secrets

_FINGERPRINT_LENGTH = 16
_FINGERPRINT_RANDOM_KEY_BYTES = 32

# Keys the query fingerprints. Without a configured salt it is a random per-process secret, so a log line
# can be correlated with another from the same process but a short query cannot be confirmed by hashing a
# guess (a bare ``sha256(query)`` could be dictionary-checked straight from the logs).
_fingerprint_key: bytes = secrets.token_bytes(_FINGERPRINT_RANDOM_KEY_BYTES)


def configure_query_fingerprint_salt(salt: bytes | None) -> None:
    """Key every later :func:`_query_fingerprint` with *salt* (the telemetry HMAC salt).

    ``None`` — the salt is unavailable, e.g. ``[telemetry].hash_doc_ids`` is off — keys it with a fresh
    random per-process secret instead.
    """
    global _fingerprint_key
    _fingerprint_key = salt if salt is not None else secrets.token_bytes(_FINGERPRINT_RANDOM_KEY_BYTES)


def _query_fingerprint(query: str) -> str:
    """Return the first 16 hex chars of HMAC-SHA256(query) — a keyed, non-reversible log correlation token."""
    return hmac.digest(_fingerprint_key, query.encode(), "sha256").hex()[:_FINGERPRINT_LENGTH]
