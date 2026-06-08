"""Shared privacy utilities for archon-search.

Provides non-reversible log correlation tokens so that per-request
log messages can be correlated without leaking raw user-query text.
"""
from __future__ import annotations

import hashlib


def _query_fingerprint(query: str) -> str:
    """Return sha256(query)[:16] — a non-reversible log correlation token."""
    return hashlib.sha256(query.encode()).hexdigest()[:16]
