"""Credential redaction for log text: the single source of truth for what must never reach a log destination.

Hub errors embed the request URL, and ``HF_ENDPOINT`` may carry credentials in its userinfo
(``http://user:password@host``), so exception messages and tracebacks can hold secrets. ``logging_setup`` applies
:func:`redact_secrets` to every record the ``archon_search`` handlers write (message, traceback, stack);
``model_store`` applies it to the one-line download-failure WARNING.
"""

from __future__ import annotations

import re

REDACTED = "<redacted>"
#: What must never be logged: URL credentials and query strings (signed URLs; a ``?`` only counts when ``name=`` follows, so prose such as ``why?)`` stays), Hugging Face tokens, bearer tokens
#: and ``key=value`` secrets.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<=//)[^/\s@]+@"), ""),
    (re.compile(r"\?[\w.%~-]+=[^\s'\"]*"), f"?{REDACTED}"),
    (re.compile(r"\bhf_[A-Za-z0-9]+"), f"hf_{REDACTED}"),
    (re.compile(r"\bBearer\s+\S+", re.IGNORECASE), f"Bearer {REDACTED}"),
    (re.compile(r"\b(token|api[_-]?key|password|secret|authorization)\s*[=:]\s*\S+", re.IGNORECASE), rf"\1={REDACTED}"),
)


def redact_secrets(text: str) -> str:
    """Return *text* with URL userinfo, query strings, tokens and ``key=value`` secrets replaced."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
