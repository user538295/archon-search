"""Regression test for S192 — `--log-format json` leaks non-JSON uvicorn log lines.

Reported symptom (archon-search 26.8.1845):

    archon-search wizard --profile minimal --non-interactive --skip-preload \
        --log-format json
    head -5 ~/.archon-search/logs/archon-search.log
    # AssertionError: log line is not a JSON object:
    #   'INFO:     Started server process [6216]'

Root cause (now fixed — this file is the regression guard):
Before the fix, ``run_server`` (``archon_search/server/app.py``) called
``configure_logging(config)`` which JSON-formats ONLY the ``archon_search``
logger's file handler, then called ``uvicorn.run(app, host=..., port=...)`` with
**no** ``log_config``. Uvicorn therefore installed its own default text
``LOGGING_CONFIG`` (uvicorn 0.52.1:
``handlers.default`` → ``StreamHandler(ext://sys.stderr)`` with a plain-text
formatter, ``propagate=False``), so uvicorn's own startup lines
(``Started server process [PID]``, ``Application startup complete``, ...) are
emitted as PLAIN TEXT.

Why they land in the JSON log file: the macOS launchd plist
(``archon_search/platform/macos.py`` — ``StandardOutPath`` and ``StandardErrorPath``
BOTH point at ``~/.archon-search/logs/archon-search.log``) redirects the server
process's stdout/stderr into the same file the JSON handler writes to. So
uvicorn's plain-text stderr lines interleave with the JSON lines.

This test drives the REAL production seam (``run_server`` with ``uvicorn.run``
patched) and faithfully models the deployed environment:
  * uvicorn's logging is configured through the REAL ``uvicorn.Config(...)
    .configure_logging()`` using whatever ``log_config`` ``run_server`` passes
    (now: the JSON config from ``build_uvicorn_log_config``). The formatting comes
    from uvicorn's own code applying that config, not from the test.
  * the process's stdout/stderr are redirected into the log file exactly as the
    launchd plist does, so uvicorn's startup line lands in the same file the JSON
    handler writes to.

It then asserts every non-empty line in the log file is a JSON object. Before the
fix this failed on the plain-text uvicorn startup line; it now passes because
``run_server`` routes uvicorn's loggers through the JSON formatter — a regression
guard against that leak returning.
"""
from __future__ import annotations

import copy
import json
import logging
import logging.config
import sys
from pathlib import Path

import pytest

from pythonjsonlogger.jsonlogger import JsonFormatter

import archon_search.server.app as app_module
from archon_search.config import SearchConfig
from archon_search.logging_setup import build_uvicorn_log_config

# Loggers uvicorn.Config.configure_logging() mutates globally — snapshot/restore
# so this test does not leak handlers into the rest of the suite.
_TOUCHED_LOGGERS = ("archon_search", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def _restore_logging_state():
    saved: dict[str, tuple[list[logging.Handler], int, bool]] = {}
    for name in _TOUCHED_LOGGERS:
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level, lg.propagate)
    try:
        yield
    finally:
        for name, (handlers, level, propagate) in saved.items():
            lg = logging.getLogger(name)
            for h in lg.handlers[:]:
                lg.removeHandler(h)
                if h not in handlers:
                    h.close()
            for h in handlers:
                lg.addHandler(h)
            lg.level = level
            lg.propagate = propagate


def _make_config(tmp_path: Path, log_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.host = "127.0.0.1"
    cfg.port = 0
    cfg.db_path = str(tmp_path / "search")  # isolate store from ~/.archon-search
    cfg.mcp.enabled = False  # keep the app lean/deterministic
    cfg.log_format = "json"
    cfg.log_file = str(log_path)
    cfg.level = "DEBUG"
    return cfg


def test_run_server_json_log_format_all_log_lines_are_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S192: with ``log_format='json'`` every line in the log file — including
    uvicorn's own startup lines — must be a valid JSON object.

    Before the fix this failed on ``INFO:     Started server process [6216]``
    (uvicorn's default text formatter), exactly as the bug report shows.
    """
    log_path = tmp_path / "archon-search.log"
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(tmp_path / "archon-search.toml"))

    def _fake_uvicorn_run(app, **kwargs):  # noqa: ANN001, ANN003
        # Faithfully reproduce what real uvicorn does on startup: build a real
        # uvicorn.Config with whatever run_server passed (host/port and, once
        # fixed, log_config) and run its REAL configure_logging(). Default
        # resolution (no log_config → uvicorn's text LOGGING_CONFIG) is uvicorn's
        # own, not the test's.
        import uvicorn  # noqa: PLC0415

        # Mimic the launchd plist: StandardOutPath/StandardErrorPath both point
        # at the log file, so the process's stdout/stderr go there. Redirect
        # BEFORE configure_logging() so uvicorn's stderr handler binds to the
        # file stream (dictConfig resolves ext://sys.stderr at config time).
        old_out, old_err = sys.stdout, sys.stderr
        with open(log_path, "a", encoding="utf-8") as redirected:
            sys.stdout = redirected
            sys.stderr = redirected
            try:
                uv_config = uvicorn.Config(app, **kwargs)
                uv_config.configure_logging()
                # Representative uvicorn startup line (the exact one from S192)
                # plus an access line, which is routed through the separate
                # stdout `access` handler — cover both uvicorn handler paths.
                logging.getLogger("uvicorn.error").info(
                    "Started server process [%d]", 6216
                )
                logging.getLogger("uvicorn.access").info(
                    '%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/health", "1.1", 200
                )
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                    for handler in logging.getLogger(name).handlers:
                        handler.flush()
            finally:
                sys.stdout, sys.stderr = old_out, old_err

    monkeypatch.setattr(app_module.uvicorn, "run", _fake_uvicorn_run)

    app_module.run_server(_make_config(tmp_path, log_path))

    # Flush the archon_search JSON file handler(s) too.
    for handler in logging.getLogger("archon_search").handlers:
        handler.flush()

    assert log_path.exists(), "run_server never produced a log file"
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "log file is empty — the uvicorn startup line was never captured"

    non_json = []
    parsed_lines = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith("{"):
            non_json.append(line)
            continue
        try:
            parsed_lines.append(json.loads(stripped))
        except json.JSONDecodeError:
            non_json.append(line)

    assert not non_json, (
        "S192: log_format='json' produced non-JSON log line(s) in the log file "
        f"(uvicorn startup lines bypass the JSON formatter): {non_json}"
    )

    # Anti-vacuity: assert the uvicorn seam itself (not just archon_search's own
    # JSON lines) was actually captured AND correctly field-formatted — otherwise
    # this test could pass green even if the uvicorn line never reached the file.
    startup = next(
        (p for p in parsed_lines if p.get("logger") == "uvicorn.error"), None
    )
    assert startup is not None, (
        "the uvicorn startup line was never captured as JSON — guard is vacuous: "
        f"{parsed_lines}"
    )
    assert startup["message"] == "Started server process [6216]"
    assert "timestamp" in startup and startup["level"] == "INFO"
    assert any(p.get("logger") == "uvicorn.access" for p in parsed_lines), (
        f"the uvicorn.access (stdout handler) line was not captured as JSON: {parsed_lines}"
    )


def test_build_uvicorn_log_config_text_returns_none() -> None:
    """For the text format the helper returns None so uvicorn keeps its own
    default text LOGGING_CONFIG (no JSON reconfiguration of uvicorn)."""
    cfg = SearchConfig()
    cfg.log_format = "text"
    assert build_uvicorn_log_config(cfg) is None


def test_run_server_text_format_omits_log_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the default text format, run_server must NOT pass log_config to
    uvicorn.run — uvicorn keeps its colored text default."""
    captured: dict = {}

    def _fake_uvicorn_run(app, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)

    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(tmp_path / "archon-search.toml"))
    monkeypatch.setattr(app_module.uvicorn, "run", _fake_uvicorn_run)

    cfg = _make_config(tmp_path, tmp_path / "archon-search.log")
    cfg.log_format = "text"
    app_module.run_server(cfg)

    assert "log_config" not in captured
    assert captured.get("host") == "127.0.0.1"


def test_build_uvicorn_log_config_json_is_valid_dictconfig() -> None:
    """The json config is a valid dictConfig: it applies cleanly, routes every
    uvicorn logger through the JSON formatter, and honors config.level."""
    cfg = SearchConfig()
    cfg.log_format = "json"
    cfg.level = "WARNING"

    log_config = build_uvicorn_log_config(cfg)
    assert log_config is not None

    # Applies without error (would raise on a malformed dict).
    logging.config.dictConfig(log_config)

    # config.level is applied to every uvicorn logger, not pinned to INFO.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert log_config["loggers"][name]["level"] == "WARNING"

    # Every handler resolves to the JSON formatter.
    for name in ("uvicorn", "uvicorn.access"):
        handlers = logging.getLogger(name).handlers
        assert handlers, f"{name} has no handler after dictConfig"
        assert all(isinstance(h.formatter, JsonFormatter) for h in handlers)


def test_build_uvicorn_log_config_derives_from_uvicorn_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config is DERIVED from uvicorn's LOGGING_CONFIG, not a hardcoded
    allowlist — so a logger/handler uvicorn adds or renames in a future release
    is routed through the JSON formatter automatically instead of leaking as text
    (the S192 bug class). Guards the derivation itself: a synthetic extra logger
    injected into LOGGING_CONFIG must be covered without being named here.
    """
    import uvicorn.config

    synthetic = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    synthetic["handlers"]["future"] = {
        "class": "logging.StreamHandler",
        "formatter": "default",  # uvicorn's text formatter — must be overridden
        "stream": "ext://sys.stderr",
    }
    synthetic["loggers"]["uvicorn.future"] = {
        "handlers": ["future"],
        "level": "INFO",
        "propagate": False,
    }
    monkeypatch.setattr(uvicorn.config, "LOGGING_CONFIG", synthetic)

    cfg = SearchConfig()
    cfg.log_format = "json"
    cfg.level = "WARNING"
    log_config = build_uvicorn_log_config(cfg)
    assert log_config is not None

    # The new logger/handler are covered without the code (or this test) naming
    # them: every handler is repointed to "json" and every logger gets the level.
    assert "future" in log_config["handlers"]
    assert all(h["formatter"] == "json" for h in log_config["handlers"].values())
    assert "uvicorn.future" in log_config["loggers"]
    assert all(lg["level"] == "WARNING" for lg in log_config["loggers"].values())
