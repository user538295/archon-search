"""Tests for archon_search.logging_setup — CorrelationIdFilter."""
from __future__ import annotations

import logging

import pytest

from archon_search.logging_setup import CorrelationIdFilter
from archon_search.observability import correlation_id


@pytest.fixture(autouse=True)
def isolate_logger():
    """Save and restore archon_search logger state around each test."""
    logger = logging.getLogger("archon_search")
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    saved_propagate = logger.propagate
    yield
    # Remove ALL current handlers; close those that weren't there before
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        if handler not in saved_handlers:
            handler.close()
    # Restore only the originally-saved handlers
    for handler in saved_handlers:
        logger.addHandler(handler)
    logger.level = saved_level
    logger.propagate = saved_propagate


@pytest.fixture(autouse=True)
def reset_correlation_id():
    """Reset the correlation_id ContextVar before and after each test."""
    token = correlation_id.set(None)
    yield
    correlation_id.reset(token)


def _make_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test message",
        args=(),
        exc_info=None,
    )


def test_filter_sets_correlation_id_when_present():
    correlation_id.set("abc123")
    record = _make_record()
    f = CorrelationIdFilter()
    f.filter(record)
    assert record.correlation_id == "abc123"


def test_filter_omits_correlation_id_when_absent():
    # ContextVar has default=None; never explicitly set in this context
    record = _make_record()
    f = CorrelationIdFilter()
    f.filter(record)
    assert not hasattr(record, "correlation_id")


def test_filter_always_returns_true():
    f = CorrelationIdFilter()

    # Without ContextVar set
    record_absent = _make_record()
    assert f.filter(record_absent) is True

    # With ContextVar set
    correlation_id.set("xyz")
    record_present = _make_record()
    assert f.filter(record_present) is True


def test_filter_does_not_set_none():
    # Explicitly reset to None
    correlation_id.set(None)
    record = _make_record()
    f = CorrelationIdFilter()
    f.filter(record)
    assert not hasattr(record, "correlation_id")
