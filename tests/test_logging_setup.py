"""Tests for archon_search.logging_setup — CorrelationIdFilter and configure_logging."""
from __future__ import annotations

import json
import logging
import time
from logging.handlers import TimedRotatingFileHandler

import pytest

from archon_search.config import SearchConfig
from archon_search.logging_setup import CorrelationIdFilter, configure_logging
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


# ---------------------------------------------------------------------------
# configure_logging() tests
# ---------------------------------------------------------------------------

def _make_config(tmp_path, log_format="text", level="DEBUG", backup_count=7):
    cfg = SearchConfig()
    cfg.log_file = str(tmp_path / "test.log")
    cfg.log_format = log_format
    cfg.level = level
    cfg.backup_count = backup_count
    return cfg


def test_configure_logging_attaches_handler_when_log_file_set(tmp_path):
    cfg = _make_config(tmp_path)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(handlers) >= 1
    assert str(tmp_path / "test.log") in handlers[0].baseFilename


def test_configure_logging_no_handler_when_log_file_empty():
    logger = logging.getLogger("archon_search")
    logger.addHandler(logging.StreamHandler())
    cfg = SearchConfig()
    cfg.log_file = ""
    configure_logging(cfg)
    assert len(logger.handlers) == 0


def test_configure_logging_sets_level(tmp_path):
    cfg = _make_config(tmp_path, level="WARNING")
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    assert logger.level == logging.WARNING


def test_configure_logging_json_format_produces_valid_json(tmp_path):
    cfg = _make_config(tmp_path, log_format="json")
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    logger.debug("hello json")
    logger.handlers[0].flush()
    log_path = tmp_path / "test.log"
    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    assert lines, "No log lines written"
    parsed = json.loads(lines[0])
    assert "timestamp" in parsed
    assert "level" in parsed
    assert "logger" in parsed
    assert "message" in parsed
    assert "asctime" not in parsed
    assert "levelname" not in parsed
    assert "name" not in parsed


def test_configure_logging_text_format_is_not_json(tmp_path):
    cfg = _make_config(tmp_path, log_format="text")
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    logger.debug("hello text")
    logger.handlers[0].flush()
    log_path = tmp_path / "test.log"
    line = log_path.read_text().splitlines()[0]
    try:
        json.loads(line)
        raise AssertionError("Line parsed as JSON but should be plain text")
    except json.JSONDecodeError:
        pass  # Expected — text format is not JSON


def test_configure_logging_json_includes_correlation_id(tmp_path):
    cfg = _make_config(tmp_path, log_format="json")
    configure_logging(cfg)
    correlation_id.set("req-xyz")
    logger = logging.getLogger("archon_search")
    logger.debug("with corr id")
    logger.handlers[0].flush()
    log_path = tmp_path / "test.log"
    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    parsed = json.loads(lines[0])
    assert parsed.get("correlation_id") == "req-xyz"


def test_configure_logging_json_omits_correlation_id_when_absent(tmp_path):
    cfg = _make_config(tmp_path, log_format="json")
    configure_logging(cfg)
    # correlation_id is reset to None by autouse fixture
    logger = logging.getLogger("archon_search")
    logger.debug("no corr id")
    logger.handlers[0].flush()
    log_path = tmp_path / "test.log"
    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    parsed = json.loads(lines[0])
    assert "correlation_id" not in parsed


def test_configure_logging_text_format_no_correlation_id_in_output(tmp_path):
    cfg = _make_config(tmp_path, log_format="text")
    configure_logging(cfg)
    correlation_id.set("req-xyz")
    logger = logging.getLogger("archon_search")
    logger.debug("text with corr")
    logger.handlers[0].flush()
    log_path = tmp_path / "test.log"
    content = log_path.read_text()
    assert "req-xyz" not in content


def test_configure_logging_directory_created(tmp_path):
    cfg = SearchConfig()
    subdir = tmp_path / "nested" / "subdir"
    cfg.log_file = str(subdir / "test.log")
    cfg.log_format = "text"
    cfg.level = "DEBUG"
    cfg.backup_count = 7
    configure_logging(cfg)
    assert subdir.exists()


def test_configure_logging_directory_failure_does_not_crash(tmp_path, monkeypatch):
    from pathlib import Path
    original_mkdir = Path.mkdir

    def fail_mkdir(self, *args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    cfg = _make_config(tmp_path)
    # Should not raise
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(handlers) == 0


def test_configure_logging_handler_construction_failure_does_not_crash(tmp_path, monkeypatch):
    import archon_search.logging_setup as ls_module

    original_init = TimedRotatingFileHandler.__init__

    def fail_init(self, *args, **kwargs):
        raise PermissionError("simulated permission error")

    monkeypatch.setattr(ls_module.TimedRotatingFileHandler, "__init__", fail_init)
    cfg = _make_config(tmp_path)
    # Should not raise
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(handlers) == 0


def test_configure_logging_filter_on_handler_not_root_logger(tmp_path):
    cfg = _make_config(tmp_path)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert handlers, "Expected a TimedRotatingFileHandler"
    handler = handlers[0]
    assert any(isinstance(f, CorrelationIdFilter) for f in handler.filters)
    assert not any(isinstance(f, CorrelationIdFilter) for f in logger.filters)


def test_configure_logging_idempotent_no_duplicate_handlers(tmp_path):
    cfg = _make_config(tmp_path)
    configure_logging(cfg)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(handlers) == 1


def test_configure_logging_handler_parameters(tmp_path):
    cfg = _make_config(tmp_path, backup_count=3)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert handlers
    handler = handlers[0]
    assert handler.when == "MIDNIGHT"
    assert handler.utc is True
    assert handler.backupCount == 3
    assert handler.encoding == "utf-8"
    assert str(tmp_path / "test.log") in handler.baseFilename


@pytest.mark.parametrize("log_format", ["text", "json"])
def test_configure_logging_formatter_uses_utc(tmp_path, log_format):
    cfg = _make_config(tmp_path, log_format=log_format)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert handlers
    handler = handlers[0]
    assert handler.formatter.converter is time.gmtime


def test_json_formatter_importable():
    from pythonjsonlogger.jsonlogger import JsonFormatter  # noqa: F401


def test_configure_logging_sets_propagate_false_when_file_handler_attached(tmp_path):
    cfg = _make_config(tmp_path)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    assert logger.propagate is False


def test_configure_logging_propagate_true_when_log_file_empty():
    cfg = SearchConfig()
    cfg.log_file = ""
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    assert logger.propagate is True


def test_configure_logging_level_filters_messages(tmp_path):
    cfg = _make_config(tmp_path, level="WARNING")
    configure_logging(cfg)
    child = logging.getLogger("archon_search.child_test")
    child.debug("this is debug")
    child.warning("this is warning")
    logger = logging.getLogger("archon_search")
    logger.handlers[0].flush()
    content = (tmp_path / "test.log").read_text()
    assert "this is warning" in content
    assert "this is debug" not in content


def test_configure_logging_idempotent_closes_old_handler(tmp_path):
    cfg = _make_config(tmp_path)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    old_handler = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)][0]
    old_stream = old_handler.stream
    configure_logging(cfg)
    assert old_stream.closed is True


def test_configure_logging_transition_file_to_empty(tmp_path):
    cfg = _make_config(tmp_path)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    old_handler = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)][0]
    old_stream = old_handler.stream

    cfg2 = SearchConfig()
    cfg2.log_file = ""
    configure_logging(cfg2)

    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(handlers) == 0
    assert logger.propagate is True
    assert old_stream.closed is True


def test_configure_logging_handler_backup_count_zero(tmp_path):
    cfg = _make_config(tmp_path, backup_count=0)
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert handlers
    assert handlers[0].backupCount == 0


def test_configure_logging_tilde_path_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = SearchConfig()
    cfg.log_file = "~/test-archon.log"
    cfg.log_format = "text"
    cfg.level = "DEBUG"
    cfg.backup_count = 7
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert handlers
    assert "~" not in handlers[0].baseFilename
    assert handlers[0].baseFilename.startswith(str(tmp_path))


def test_configure_logging_json_exception_is_valid_json(tmp_path):
    cfg = _make_config(tmp_path, log_format="json")
    configure_logging(cfg)
    logger = logging.getLogger("archon_search")
    try:
        raise ValueError("test error")
    except ValueError:
        logger.error("caught exception", exc_info=True)
    logger.handlers[0].flush()
    lines = [l for l in (tmp_path / "test.log").read_text().splitlines() if l.strip()]
    assert lines
    for line in lines:
        parsed = json.loads(line)
        assert "message" in parsed


def test_configure_logging_json_timestamp_is_utc(tmp_path):
    """Verify the JSON timestamp field is UTC, not local time."""
    import datetime

    cfg = _make_config(tmp_path, log_format="json")
    configure_logging(cfg)
    before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    logger = logging.getLogger("archon_search")
    logger.debug("timestamp check")
    logger.handlers[0].flush()
    after = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0) + datetime.timedelta(seconds=1)
    lines = [l for l in (tmp_path / "test.log").read_text().splitlines() if l.strip()]
    parsed = json.loads(lines[0])
    ts_str = parsed["timestamp"]
    # datefmt is "%Y-%m-%dT%H:%M:%SZ" — parse it back as UTC
    ts = datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    assert before <= ts <= after, f"Timestamp {ts_str!r} not in UTC range [{before}, {after}]"
