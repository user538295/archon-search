"""Logging configuration helpers for archon-search (B7)."""
from __future__ import annotations

import logging

from archon_search.observability import correlation_id


class CorrelationIdFilter(logging.Filter):
    """Inject the current correlation_id into each log record.

    If the ContextVar has no value (default=None), the attribute is left
    UNSET on the record — python-json-logger will omit the field entirely
    rather than emitting null.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        value = correlation_id.get()
        if value is not None:
            record.correlation_id = value  # type: ignore[attr-defined]
        return True
