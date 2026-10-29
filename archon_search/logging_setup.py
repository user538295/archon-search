"""Logging configuration helpers for archon-search (B7)."""
from __future__ import annotations

import copy
import logging
import os
import re
import sys
import time
from io import TextIOWrapper
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import cast

from pythonjsonlogger.json import JsonFormatter

from archon_search._durable_io import (
    PRIVATE_FILE_MODE,
    ensure_private_dir,
    restrict_fd_to_owner,
)
from archon_search.config import SearchConfig
from archon_search.constants import LOG_FILE_DISABLED_WARNING, VIEW_TOKEN_QUERY_PARAM
from archon_search.log_redaction import redact_secrets
from archon_search.observability import correlation_id

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]


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


# Query parameters that carry a credential; uvicorn's access log prints the full request target, so the value
# of each is replaced before the line is formatted.
_CREDENTIAL_QUERY_PARAMS = (VIEW_TOKEN_QUERY_PARAM, "token", "api_key", "access_token")
_CREDENTIAL_QUERY_RE = re.compile(rf"(?P<param>[?&](?:{'|'.join(_CREDENTIAL_QUERY_PARAMS)}))=[^&\s]*")
REDACTED_VALUE = "[REDACTED]"


class QueryCredentialRedactionFilter(logging.Filter):
    """Redact credential-bearing query parameters from uvicorn access-log request targets.

    uvicorn's ``uvicorn.access`` record carries ``(client, method, full_path, http_version, status)``;
    ``full_path`` includes the raw query string, e.g. ``/graph/docs/view?view_token=...``. The value of any
    parameter in ``_CREDENTIAL_QUERY_PARAMS`` is replaced with ``[REDACTED]`` so a credential never reaches
    the log, whatever the log format.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                _CREDENTIAL_QUERY_RE.sub(rf"\g<param>={REDACTED_VALUE}", arg) if isinstance(arg, str) else arg
                for arg in args
            )
        return True


#: Attribute names every ``LogRecord`` has; anything else on a record came from ``extra=`` (or a filter).
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class _RedactingFormatter(logging.Formatter):
    """A formatter that redacts credentials from everything it renders: message, traceback and stack text.

    The redaction runs on the rendered text of a copy of the record, so every call site is covered at once,
    including string ``extra`` fields and ``exc_info`` chains whose raw hub errors embed the credentialed
    ``HF_ENDPOINT`` URL. The record other handlers (a test's ``caplog``) see stays untouched.
    """

    def format(self, record: logging.LogRecord) -> str:
        redacted = copy.copy(record)
        redacted.msg = redact_secrets(record.getMessage())
        redacted.args = None
        for name, value in vars(record).items():
            if name not in _STANDARD_RECORD_ATTRS and isinstance(value, str):
                setattr(redacted, name, redact_secrets(value))
        redacted.exc_text = redact_secrets(record.exc_text) if record.exc_text else None
        return super().format(redacted)

    def formatException(self, ei: ExcInfo) -> str:
        return redact_secrets(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return redact_secrets(super().formatStack(stack_info))


class _RedactingJsonFormatter(_RedactingFormatter, JsonFormatter):
    """The JSON counterpart of :class:`_RedactingFormatter`."""


def _build_formatter(log_format: str, *, redact: bool = False) -> logging.Formatter:
    """Build a formatter matching the configured log_format (text or JSON).

    ``redact=True`` scrubs credentials from every rendered record (see :func:`redact_secrets`); the
    ``archon_search`` handlers use it. uvicorn's request lines do not: their request targets legitimately
    hold query strings, which ``QueryCredentialRedactionFilter`` already redacts selectively.
    """
    datefmt = "%Y-%m-%dT%H:%M:%SZ"
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    if log_format == "json":
        json_cls = _RedactingJsonFormatter if redact else JsonFormatter
        formatter: logging.Formatter = json_cls(
            fmt,
            rename_fields={"levelname": "level", "name": "logger", "asctime": "timestamp"},
            datefmt=datefmt,
        )
    else:
        formatter = (_RedactingFormatter if redact else logging.Formatter)(fmt, datefmt=datefmt)
    formatter.converter = time.gmtime  # type: ignore[method-assign]
    return formatter


def build_uvicorn_log_config(config: SearchConfig) -> dict[str, object] | None:
    """Build a uvicorn dictConfig that routes uvicorn's own loggers through the
    configured JSON formatter (S192).

    Returns ``None`` for the text format so uvicorn keeps its own default text
    ``LOGGING_CONFIG``. For ``json`` it is derived by deep-copying uvicorn's own
    ``LOGGING_CONFIG``, adding a JSON formatter, and repointing every handler at
    it, so uvicorn's startup lines (``Started server process [PID]``, ...) are
    emitted as JSON objects instead of plain text. Deriving from uvicorn's config
    (rather than restating its logger/handler topology) means any logger uvicorn
    adds or renames in a future release is covered automatically — a hardcoded
    allowlist would silently leak that logger's lines as text, re-opening the
    S192 bug.

    Every uvicorn logger's level is set to ``config.level`` so the operator's
    ``[logging].level`` governs uvicorn output too, matching the archon_search
    logger (note ``level >= WARNING`` therefore silences uvicorn access lines).
    The correlation-id filter is intentionally NOT attached: uvicorn's lines are
    emitted outside archon_search's request middleware, so they never carry a
    correlation_id.
    """
    if config.log_format != "json":
        return None
    from uvicorn.config import LOGGING_CONFIG  # lazy: uvicorn is only needed to serve

    log_config = copy.deepcopy(LOGGING_CONFIG)
    # Add a JSON formatter and point every handler at it, but KEEP uvicorn's own
    # "default"/"access" formatter entries: uvicorn's configure_logging() indexes
    # log_config["formatters"]["default"|"access"] to inject use_colors *before*
    # applying the dict, so deleting them would KeyError if a caller ever passed
    # use_colors. The kept formatters simply end up unattached to any handler.
    log_config["formatters"]["json"] = {"()": _build_formatter, "log_format": "json"}
    for handler in log_config["handlers"].values():
        handler["formatter"] = "json"
    for logger in log_config["loggers"].values():
        logger["level"] = config.level
    return log_config


class _PrivateTimedRotatingFileHandler(TimedRotatingFileHandler):
    """A rotating log handler whose files (current and rotated) are owner-only whatever the umask."""

    def _open(self) -> TextIOWrapper:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE_MODE)  # durable-write-exempt — append-only log
        try:
            restrict_fd_to_owner(fd)  # a log an older version created world-readable is tightened too
            return cast(TextIOWrapper, os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors))
        except BaseException:
            os.close(fd)  # fdopen did not take ownership of it
            raise


def _attach_file_handler(logger: logging.Logger, config: SearchConfig) -> None:
    """Attach a TimedRotatingFileHandler to ``logger`` based on ``config.log_file``.

    Silently logs a warning and returns if the directory cannot be created or
    the handler cannot be constructed (preserves prior behaviour).
    """
    log_path = Path(config.log_file).expanduser()

    try:
        ensure_private_dir(log_path.parent)
    except OSError:
        logging.warning(  # noqa: LOG015 — the file handler is not configured yet; the root logger is the only sink
            "archon-search: could not create log directory %s", log_path.parent
        )
        return

    try:
        handler = _PrivateTimedRotatingFileHandler(
            log_path,
            when="midnight",
            utc=True,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
    except OSError:
        logging.warning("archon-search: could not open log file %s", log_path)  # noqa: LOG015 — handler not configured yet; root logger is the only sink
        return

    handler.addFilter(CorrelationIdFilter())
    handler.setFormatter(_build_formatter(config.log_format, redact=True))
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

    # uvicorn's access log prints the request target with its query string; never let a credential reach it.
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QueryCredentialRedactionFilter) for f in access_logger.filters):
        access_logger.addFilter(QueryCredentialRedactionFilter())

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
        stderr_handler.setFormatter(_build_formatter(config.log_format, redact=True))
        logger.addHandler(stderr_handler)
        # Prevent duplicate output through the root logger.
        logger.propagate = False
