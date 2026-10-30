"""Warn-once helper: one WARNING per caller per process (contract C1, scenario S13).

A caller whose provider keeps failing (HyDE on every search, graph on every community) must tell the
operator once, not once per call. The caller picks the key (its own name), so two features that fail
independently each report once.
"""

from __future__ import annotations

import logging
import threading

#: Keys that already logged. Process-wide; the test suite clears it between tests (``tests/conftest.py``).
_warned_keys: set[str] = set()
_WARNED_LOCK = threading.Lock()


def warn_once(logger: logging.Logger, key: str, message: str, *args: object) -> bool:
    """Log *message* at WARNING the first time *key* is seen in this process.

    *message* and *args* follow ``logger.warning``. The caller owns the text: it MUST NOT contain a
    prompt, a query, a hypothetical document or an excerpt (C1).

    Returns:
        ``True`` when this call logged, ``False`` when *key* had already logged.
    """
    with _WARNED_LOCK:
        first_report = key not in _warned_keys
        _warned_keys.add(key)
    if first_report:
        logger.warning(message, *args, stacklevel=2)
    return first_report
