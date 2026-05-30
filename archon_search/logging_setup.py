"""Logging configuration helpers for archon-search (B7)."""
from __future__ import annotations

import logging
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from pythonjsonlogger.jsonlogger import JsonFormatter

from archon_search.config import SearchConfig
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


def configure_logging(config: SearchConfig) -> None:
    """Configure the archon_search logger based on SearchConfig.

    Idempotent — calling twice removes the old handler before adding a new one.
    When log_file is empty, all handlers are removed and propagation is restored.
    """
    logger = logging.getLogger("archon_search")

    # Idempotency: remove and close all existing handlers, reset propagate.
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True

    # Set the log level.
    logger.setLevel(config.level)

    # Empty log_file means stderr only (via root logger propagation).
    if not config.log_file:
        return

    # Expand ~ in the path.
    log_path = Path(config.log_file).expanduser()

    # Create the directory tree if needed.
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.warning("archon-search: could not create log directory %s", log_path.parent)
        return

    # Build the rotating file handler.
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

    # Attach the correlation-id filter to the handler (not the logger).
    handler.addFilter(CorrelationIdFilter())

    # Build and attach the formatter.
    datefmt = "%Y-%m-%dT%H:%M:%SZ"
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    if config.log_format == "json":
        formatter: logging.Formatter = JsonFormatter(
            fmt,
            rename_fields={"levelname": "level", "name": "logger", "asctime": "timestamp"},
            datefmt=datefmt,
        )
    else:
        formatter = logging.Formatter(fmt, datefmt=datefmt)
    formatter.converter = time.gmtime  # type: ignore[method-assign]
    handler.setFormatter(formatter)

    logger.addHandler(handler)

    # Prevent duplicate output on stderr via root logger.
    logger.propagate = False
