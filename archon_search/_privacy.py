"""Shared privacy utilities for archon-search.

Provides non-reversible log correlation tokens so that per-request
log messages can be correlated without leaking raw user-query text.
"""
from __future__ import annotations

import hmac
import json
import secrets
from typing import Final

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


_QUERY_MASK: Final = "[query]"
#: What ``llm_client.server_text.bounded_server_text`` appends to a text it cut at its length bound.
_CUT_MARKER: Final = "..."
#: Shortest closing fragment of a cut-off echo that is masked: shorter ones would also hit ordinary words.
_MIN_CUT_ECHO_CHARS: Final = 12
#: Most characters of a query a failure text can echo (the clients cut it at 200, the escaped forms may be a few times longer).
_MAX_ECHO_CHARS: Final = 2000


def _echo_forms(query: str, cut_chars: int) -> list[str]:
    """Every form in which a server may echo *query* back, longest first.

    Only the first *cut_chars* characters of the query reach the provider (the prompt carries no more), so only
    those can come back, and the clients bound a server's failure text to a few hundred characters
    (``llm_client.server_text``), so no more than ``_MAX_ECHO_CHARS`` of them can: that bound keeps the cost of masking
    a failure independent of the query's length (a cloud provider is sent the whole of a query of many megabytes).
    Each form is the text itself or its JSON, Python ``repr`` or escape encoding, and each one again
    whitespace-collapsed (the clients collapse a server's text after the server encoded it).
    """
    sent = query[: min(cut_chars, _MAX_ECHO_CHARS)]
    forms: set[str] = set()
    for base in (sent, " ".join(sent.split())):
        for encoded in (
            base,
            json.dumps(base)[1:-1],
            json.dumps(base, ensure_ascii=False)[1:-1],
            base.encode("unicode_escape").decode("ascii"),
            repr(base)[1:-1],
        ):
            forms.add(encoded)
            forms.add(" ".join(encoded.split()))
    return sorted((form for form in forms if form), key=len, reverse=True)


def mask_query_echo(text: str, query: str, *, cut_chars: int) -> str:
    """*text* with every echo of *query* replaced by a marker, so a provider's failure text can be logged.

    *cut_chars* is how much of the query the caller puts into its prompt: a server may echo that cut form back.
    The clients bound a server's text to a length (``llm_client.server_text``) and mark the cut with ``...``, which
    can leave only the beginning of an echo: a tail of at least ``_MIN_CUT_ECHO_CHARS`` characters that starts an
    echo form is masked too.
    """
    forms = _echo_forms(query, cut_chars)
    masked = text
    for form in forms:
        masked = masked.replace(form, _QUERY_MASK)
    for form in forms:
        masked = _mask_cut_echo(masked, form)
    return masked


def _mask_cut_echo(text: str, form: str) -> str:
    """*text* with a closing fragment of *form* (an echo cut off by the text bound, ``...`` after it) masked."""
    for tail in ("", _CUT_MARKER):
        body = text.removesuffix(tail)
        for length in range(min(len(form) - 1, len(body)), _MIN_CUT_ECHO_CHARS - 1, -1):
            if body.endswith(form[:length]):
                return f"{body[:-length]}{_QUERY_MASK}{tail}"
    return text
