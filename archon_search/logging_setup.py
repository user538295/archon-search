"""Logging configuration helpers for archon-search (B7)."""
from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from pythonjsonlogger.jsonlogger import JsonFormatter

from archon_search.config import SearchConfig
from archon_search.constants import LOG_FILE_DISABLED_WARNING
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


def _build_formatter(log_format: str) -> logging.Formatter:
    """Build a formatter matching the configured log_format (text or JSON)."""
    datefmt = "%Y-%m-%dT%H:%M:%SZ"
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    if log_format == "json":
        formatter: logging.Formatter = JsonFormatter(
            fmt,
            rename_fields={"levelname": "level", "name": "logger", "asctime": "timestamp"},
            datefmt=datefmt,
        )
    else:
        formatter = logging.Formatter(fmt, datefmt=datefmt)
    formatter.converter = time.gmtime  # type: ignore[method-assign]
    return formatter


def _attach_file_handler(logger: logging.Logger, config: SearchConfig) -> None:
    """Attach a TimedRotatingFileHandler to ``logger`` based on ``config.log_file``.

    Silently logs a warning and returns if the directory cannot be created or
    the handler cannot be constructed (preserves prior behaviour).
    """
    log_path = Path(config.log_file).expanduser()

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.warning(
            "archon-search: could not create log directory %s", log_path.parent
        )
        return

    try:
        handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            utc=True,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
    except OSError:
        logging.warning("archon-search: could not open log file %s", log_path)
        return

    handler.addFilter(CorrelationIdFilter())
    handler.setFormatter(_build_formatter(config.log_format))
    logger.addHandler(handler)
    # Prevent duplicate output on stderr via root logger.
    logger.propagate = False


def configure_logging(config: SearchConfig) -> None:
    """Configure the archon_search logger based on SearchConfig.

    Idempotent — calling twice removes all existing handlers before reattaching
    new ones. When log_file is empty, no file handler is attached. When
    ARCHON_SEARCH_CONTAINER=1, a StreamHandler(sys.stderr) is added in addition
    to (or instead of) the file handler.
    """
    logger = logging.getLogger("archon_search")

    # Idempotency: remove and close all existing handlers, reset propagate.
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True

    # Set the log level.
    logger.setLevel(config.level)

    container_mode = os.environ.get("ARCHON_SEARCH_CONTAINER") == "1"

    # File handler: only when log_file is configured.
    if config.log_file:
        _attach_file_handler(logger, config)
    elif not container_mode:
        # Empty log_file outside container mode means file logging is disabled.
        # load_config() already warns about this at config-load time, and in
        # the serve path it always runs first — so operators may see this
        # warning twice. We also emit it here, via the root logger (not
        # gated by [logging].level), so it is still surfaced when logging is
        # configured from a programmatically-built config that bypassed
        # load_config. No archon_search handler is attached in this state, so
        # — absent any configured root handler — the warning reaches stderr
        # only via Python's last-resort handler, not a formatted pipeline.
        # Use the root logger's method directly (getLogger().warning) rather
        # than logging.warning(), which would call basicConfig() and mutate
        # global logging state by attaching a root handler as a side effect.
        logging.getLogger().warning(LOG_FILE_DISABLED_WARNING)

    # Container handler: always check, regardless of log_file state.
    if container_mode:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.addFilter(CorrelationIdFilter())
        stderr_handler.setFormatter(_build_formatter(config.log_format))
        logger.addHandler(stderr_handler)
        # Prevent duplicate output through the root logger.
        logger.propagate = False
