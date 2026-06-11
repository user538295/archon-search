# B7 — Structured Logs + Log Rotation
**Purpose**: Wire the dead `[logging]` config section — attach a `TimedRotatingFileHandler`, enforce the configured log level, add optional JSON output, inject `correlation_id`, and normalize all logger names to the `archon_search.*` hierarchy.
**Audience**: archon-search contributors implementing B7; reviewers of the resulting PRs.
**Status**: Done

---

## Background

`archon_search` currently ignores its own `[logging]` configuration: `level` and `log_file` are parsed from the TOML but never applied. All output goes to uvicorn's stderr. Operators who set `log_file = "..."` get nothing, eroding trust in the config system.

This plan also normalises the 33 files in `archon_search/` that use inconsistent logger names (`"archon"`, `"archon.search"`, `"archon-search"`, bare `"archon_search"`). Without normalisation, `logging.getLogger("archon_search").setLevel(...)` does not propagate to those loggers and the JSON field `logger` is meaningless.

The full design is in `Documentation/Backlog/B7-structured-logs-rotation-brief.md`. All architectural decisions are locked.

> **Note**: The brief specifies `ValueError` for invalid config values; this plan uses `ConfigError` throughout, matching the existing codebase convention (21 existing `raise ConfigError` calls in `config.py`). This is an intentional deviation from the brief.

---

## Goal

After B7 ships: setting `log_file`, `level`, `format`, and `backup_count` in `[logging]` takes immediate effect on the next server start. A `TimedRotatingFileHandler` writes to the configured path with daily UTC-midnight rotation; `format = "json"` produces structured JSON lines understood by ELK/Loki/Datadog; every log record emitted during a request carries the `correlation_id` as a field; and a CI guard prevents the logger-name regression from ever reappearing.

---

## Scope

### In Scope
- `log_format: str = "text"` and `backup_count: int = 7` added to `SearchConfig` (TOML keys: `format`, `backup_count`)
- `load_config()` extended to parse and validate all four `[logging]` keys (`level`, `log_file`, `log_format`, `backup_count`)
- `archon_search/logging_setup.py` — new module containing `CorrelationIdFilter` and `configure_logging()`
- `configure_logging(config: SearchConfig) -> None` wired at the top of `run_server()` before `create_app()`
- Logger name normalisation: all 33 `getLogger("...")` calls in `archon_search/` changed to `logging.getLogger(__name__)`
- CI guard `tests/test_logger_names.py` — must ship in the same commit as the normalisation
- `archon-search.toml.example` — add `format` and `backup_count` to `[logging]` section
- `Documentation/UserManual/02_configuration.md` — update logging section

### Out of Scope
- File logging for CLI subcommands (`ingest`, `sync`, `collection`, etc.) — these do not go through `run_server()` and continue to log to stderr only
- Telemetry JSONL rotation — existing date-based rotation + `retention_days` pruning is sufficient
- structlog migration
- External log shipping / remote transport
- Size-based rotation (`RotatingFileHandler`)
- Changing log message content or levels at call sites

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.2 — Final verification & documentation update].

---

## What does NOT change
- `SearchConfig.level` and `SearchConfig.log_file` field names and defaults — only validation is added
- `load_config()` behaviour for all non-`[logging]` keys
- Telemetry JSONL infrastructure
- `correlation_id` ContextVar in `observability.py` — reused as-is
- `RequestContextMiddleware` — unchanged; still populates the ContextVar
- `save_config()` — not touched; it only writes `collections` / `pinned_collections`
- Existing test suite — logger-name change is transparent to unit tests

---

## Known limitations / accepted trade-offs
- `TimedRotatingFileHandler` is not multi-process safe; multi-worker uvicorn deployments must set `log_file = ""` and use an external aggregator
- Rotation occurs at UTC midnight; operators in non-UTC timezones see UTC-dated file suffixes
- `backup_count = 0` means rotated files accumulate indefinitely (stdlib behaviour); use `backup_count = 1` to keep only 1 rotated file in addition to the current log file
- CLI subcommands (`ingest`, `sync`, etc.) do not write to the log file — stderr only
- File logging activates on upgrade for any operator who has the current non-empty default (`~/.archon-search/logs/archon-search.log`); operators who want no file logging must set `log_file = ""` explicitly
- Valid `level` values are `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"`, `"CRITICAL"` (case-insensitive). `"WARN"` is also accepted as an alias and normalized to `"WARNING"` — it is not rejected as invalid. All other strings raise `ConfigError` at load time.

---

## Architecture

### New module: `archon_search/logging_setup.py`

Two public symbols:

```python
class CorrelationIdFilter(logging.Filter):
    """Injects correlation_id from the ContextVar into LogRecord attributes.
    
    Attached to the file handler only (not the root logger) so stderr is
    not affected. When the ContextVar has no value (default=None), the
    attribute is left UNSET on the record — python-json-logger will omit
    the field entirely rather than emitting null.
    """
    def filter(self, record: logging.LogRecord) -> bool: ...

def configure_logging(config: SearchConfig) -> None:
    """Wire the [logging] config to Python's logging machinery.

    This function is idempotent — calling it multiple times replaces the
    previous configuration entirely (no handler accumulation).

    Steps:
    1. Obtain logger = logging.getLogger("archon_search").
    1a. Remove all existing handlers from the logger and close them, then
        remove each handler from the logger. Reset logger.propagate = True.
        (This reset ensures idempotency: step 10 will override it back to
        False if a file handler is successfully attached.)
    2. Set the effective level: logger.setLevel(config.level).
    3. If config.log_file is empty, return early (stderr-only mode).
    4. Expand the path: Path(config.log_file).expanduser().
    5. Try log_path.parent.mkdir(parents=True, exist_ok=True). On OSError,
       call logging.warning(...) and return without attaching a handler.
       Wrap TimedRotatingFileHandler(...) construction in a try/except
       OSError: on failure, call logging.warning(...)
       and return without attaching a handler. This handles directories that
       exist but are not writable (e.g., containerised deployments with
       read-only /var/log/).
    6. Build handler = TimedRotatingFileHandler(path, when="midnight", utc=True,
                                                backupCount=config.backup_count,
                                                encoding="utf-8").
    7. Attach CorrelationIdFilter to the handler.
    8. Attach formatter (set formatter.converter = time.gmtime on both text and
       json formatters — this ensures the "Z" suffix in datefmt is truthful, since
       logging.Formatter defaults to time.localtime(). Requires `import time`):
       - log_format="text": logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                                               datefmt="%Y-%m-%dT%H:%M:%SZ")
       - log_format="json": JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                               rename_fields={"levelname": "level", "name": "logger",
                                              "asctime": "timestamp"},
                               datefmt="%Y-%m-%dT%H:%M:%SZ")
         Note: Without an explicit format string, JsonFormatter defaults to
         %(message)s only — rename_fields has nothing to rename and the
         timestamp/level/logger fields are absent from output.
    9. Add handler to logger.
    10. Set logger.propagate = False when a file handler is attached. This
        prevents duplicate output on stderr caused by uvicorn's internal
        logging.config.dictConfig() call (which does not remove the archon_search
        handler but causes records to propagate to root). When log_file = "",
        leave propagate unchanged so uvicorn's stderr output works normally.

    Note on import path: python-json-logger is a hard dependency listed in
    [project.dependencies]. Import JsonFormatter at module level, not conditionally.
    The correct import for v2.x (pin >=2.0,<3) is:
        from pythonjsonlogger.jsonlogger import JsonFormatter
    If upgrading to v3.x, change pin to >=3.0,<4 and use:
        from pythonjsonlogger.json import JsonFormatter
    Verify the import path against the resolved version.

    Non-transactional behaviour: configure_logging() is non-transactional —
    if handler construction fails after handlers have been removed and
    propagate reset to True, the function returns with no file handler and
    stderr propagation enabled, which is the correct safe fallback.
    """
```

### `SearchConfig` extensions (flat fields, `archon_search/config.py`)

```python
# [logging]
level: str = "INFO"          # existing
log_file: str = "~/.archon-search/logs/archon-search.log"  # existing
log_format: str = "text"     # NEW — TOML key: format
backup_count: int = 7        # NEW — TOML key: backup_count
```

New validation in `load_config()`:

```python
# level — case-insensitive; normalized to uppercase
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
if "level" in log_cfg:
    level = str(log_cfg["level"]).upper()
    # Normalize "WARN" → "WARNING" before validation.
    # "WARN" is not in _VALID_LEVELS (even though Python's logging.WARN
    # alias exists), so without this normalization any operator who used
    # "WARN" would get a ConfigError on upgrade.
    if level == "WARN":
        level = "WARNING"
    if level not in _VALID_LEVELS:
        raise ConfigError(f"[logging].level must be one of {sorted(_VALID_LEVELS)}, got {str(log_cfg['level'])!r}")
    config.level = level

# log_file — no validation; empty string is valid opt-out
if "log_file" in log_cfg:
    config.log_file = str(log_cfg["log_file"])

# format → log_format (only [logging] key where TOML key ≠ Python field name)
if "format" in log_cfg:
    fmt = str(log_cfg["format"])
    if fmt not in {"text", "json"}:
        raise ConfigError(f"[logging].format must be 'text' or 'json', got {fmt!r}")
    config.log_format = fmt

# backup_count
if "backup_count" in log_cfg:
    bc = _coerce_int(log_cfg["backup_count"], "[logging].backup_count")
    if bc < 0:
        raise ConfigError(f"[logging].backup_count must be >= 0, got {bc}")
    config.backup_count = bc
```

### `run_server()` change (`archon_search/server/app.py`)

```python
def run_server(config: SearchConfig) -> None:
    configure_logging(config)          # NEW — first action
    job_store = JobStore()
    app = create_app(config, job_store)
    uvicorn.run(app, host=config.host, port=config.port)
```

Note: uvicorn's `run()` internally calls `logging.config.dictConfig()` with
`disable_existing_loggers: False`. This does not remove the `archon_search` handler,
but log records propagate to root, causing duplicate output on stderr. To prevent
duplicate output, `configure_logging()` sets `logging.getLogger('archon_search').propagate = False`
when a file handler is attached (step 10). When `log_file = ""`, propagate is left
unchanged so uvicorn's stderr output still works normally.

### Logger name normalisation

32 files below change `logging.getLogger("<hardcoded>")` to `logging.getLogger(__name__)`. The final row (`logging_setup.py`) is a new file that intentionally keeps a literal string — it is included in the table for completeness but does NOT change an existing call:

| File | Before | After (`__name__` resolves to) |
|------|--------|-------------------------------|
| `acl.py` | `"archon_search"` | `archon_search.acl` |
| `cli/collection.py` | `"archon.search"` | `archon_search.cli.collection` |
| `cli/ingest.py` | `"archon.search"` | `archon_search.cli.ingest` |
| `config.py` | `"archon.search"` | `archon_search.config` |
| `description_generator.py` | `"archon"` | `archon_search.description_generator` |
| `eval/live_report.py` | `"archon"` | `archon_search.eval.live_report` |
| `eval/runner.py` | `"archon"` | `archon_search.eval.runner` |
| `install.py` | `"archon"` | `archon_search.install` |
| `jobs/store.py` | `"archon"` | `archon_search.jobs.store` |
| `key_manager.py` | `"archon-search"` | `archon_search.key_manager` |
| `observability.py` | `"archon.search"` | `archon_search.observability` |
| `pipeline.py` | `"archon"` | `archon_search.pipeline` |
| `platform/linux.py` | `"archon_search"` | `archon_search.platform.linux` |
| `platform/macos.py` | `"archon_search"` | `archon_search.platform.macos` |
| `progress.py` | `"archon"` | `archon_search.progress` |
| `router.py` | `"archon"` | `archon_search.router` |
| `server/_ingested_by.py` | `"archon"` | `archon_search.server._ingested_by` |
| `server/app.py` | `"archon-search"` | `archon_search.server.app` |
| `server/mcp.py` | `"archon.search"` | `archon_search.server.mcp` |
| `server/middleware_auth.py` | `"archon-search"` | `archon_search.server.middleware_auth` |
| `server/routes_collections.py` | `"archon-search"` | `archon_search.server.routes_collections` |
| `server/routes_explain.py` | `"archon.search"` | `archon_search.server.routes_explain` |
| `server/routes_jobs.py` | `"archon-search"` | `archon_search.server.routes_jobs` |
| `server/routes_route.py` | `"archon.search"` | `archon_search.server.routes_route` |
| `server/routes_search.py` | `"archon.search"` | `archon_search.server.routes_search` |
| `server/routes_telemetry.py` | `"archon.search"` | `archon_search.server.routes_telemetry` |
| `store.py` | `"archon"` | `archon_search.store` |
| `sync.py` | `"archon"` | `archon_search.sync` |
| `telemetry/pruner.py` | `"archon.search"` | `archon_search.telemetry.pruner` |
| `telemetry/reader.py` | `"archon.search"` | `archon_search.telemetry.reader` |
| `telemetry/writer.py` | `"archon.search"` | `archon_search.telemetry.writer` |
| `watcher.py` | `"archon"` | `archon_search.watcher` |
| `logging_setup.py` (new) | `"archon_search"` (root) | intentional literal — root logger |

`eval/metrics.py` already uses `__name__` — no change.

### New dependency

`python-json-logger>=2.0,<3` added to `pyproject.toml` dependencies (hard dependency,
listed in `[project.dependencies]`, not optional).

Import path verification: The library `python-json-logger` v2.x uses
`from pythonjsonlogger.jsonlogger import JsonFormatter`, and v3.x (the maintained fork)
uses `from pythonjsonlogger.json import JsonFormatter`. The pin `>=2.0,<3` resolves to
v2.x, so the correct import is: `from pythonjsonlogger.jsonlogger import JsonFormatter`.
If upgrading to v3.x, change pin to `>=3.0,<4` and update the import accordingly.
Since this is a hard dependency, import `JsonFormatter` at module level (not conditionally).

---

## Task breakdown

### Phase 1 — Config extensions

> **Releasable**: after Task 1.1 — config validation errors surface at load time; new fields are available on `SearchConfig` for Phase 2 to consume.

#### Task 1.1 — Extend `SearchConfig` and `load_config()` for new `[logging]` keys
- [x] **File**: `archon_search/config.py`
- **Depends on**: nothing
- **Description**:
  - Add two new flat fields to `SearchConfig` immediately after `log_file`:
    - `log_format: str = "text"`
    - `backup_count: int = 7`
  - Extend the `[logging]` block in `load_config()` to parse and validate all four keys:
    - `level`: read as `str`, normalise with `.upper()`, then normalise `"WARN"` → `"WARNING"` (before the set-membership check), raise `ConfigError` if not in `{"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}`. Note: `"WARN"` is NOT in `_VALID_LOG_LEVELS` even though Python's `logging.WARN` exists as an alias — accept it and normalize it rather than rejecting it as an undocumented breaking change.
    - `log_file`: read as `str`, no validation (empty string is a valid opt-out)
    - `format` → `config.log_format`: read TOML key `"format"`, assign to `config.log_format`; raise `ConfigError` if not in `{"text", "json"}`. This is the only `[logging]` key whose TOML name (`format`) differs from its Python attribute (`log_format`).
    - `backup_count`: use `_coerce_int`; raise `ConfigError` if value < 0
  - Add module-level constant: `_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})`
  - Also update `cli/config_cmd.py:_default_toml()` — the `[logging]` table construction must include `format` (TOML key) and `backup_count` keys so that `archon-search config show` displays all four logging keys to operators.
- **Releasable**: invalid `[logging]` config values now raise `ConfigError` at load time; `SearchConfig.log_format` and `SearchConfig.backup_count` are available.
- **Tests (TDD)** — `tests/test_config.py` (extend existing file):
  - Unit: `test_logging_level_default` — `SearchConfig().level == "INFO"` (existing field, confirm unchanged)
  - Unit: `test_logging_log_file_default` — `SearchConfig().log_file == "~/.archon-search/logs/archon-search.log"` (existing field, confirm unchanged)
  - Unit: `test_logging_level_valid_values` — all five valid levels (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) parse without error and are stored uppercase
  - Unit: `test_logging_level_case_insensitive` — `"info"`, `"Info"`, `"WARNING"` all accepted and normalised to uppercase
  - Unit: `test_logging_level_invalid_raises` — `"VERBOSE"`, `"ALL"`, `""` each raise `ConfigError` with a message containing the invalid value
  - Unit: `test_logging_level_warn_normalized_to_warning` — `level = "WARN"` in TOML produces `config.level == "WARNING"` without raising `ConfigError`
  - Unit: `test_logging_format_text_default` — `SearchConfig().log_format == "text"`
  - Unit: `test_logging_format_json_parsed` — TOML `format = "json"` sets `config.log_format == "json"`
  - Unit: `test_logging_format_invalid_raises` — `format = "xml"` raises `ConfigError`
  - Unit: `test_logging_backup_count_default` — `SearchConfig().backup_count == 7`
  - Unit: `test_logging_backup_count_zero_allowed` — `backup_count = 0` is valid; stored as `0`
  - Unit: `test_logging_backup_count_negative_raises` — `backup_count = -1` raises `ConfigError`
  - Unit: `test_logging_backup_count_parsed` — `backup_count = 14` sets `config.backup_count == 14`
  - Unit: `test_logging_log_file_empty_string_allowed` — `log_file = ""` is valid; stored as `""`
  - Unit: `test_logging_toml_key_format_maps_to_log_format_field` — confirms the TOML key `format` (not `log_format`) is what `load_config()` reads
  - Unit: `test_default_toml_logging_section_includes_all_keys` — call `cli.config_cmd._default_toml()` (or equivalent), parse the output as TOML, assert the `[logging]` table contains keys `"level"`, `"log_file"`, `"format"` (TOML key, not `log_format`), and `"backup_count"`
  - Checkpoint: `uv run pytest tests/test_config.py -v`

---

### Phase 2 — Logging infrastructure

> **Releasable**: after Task 2.3 — a running server writes to the configured log file with the configured level and format; `correlation_id` appears in JSON records when a request is active.

#### Task 2.1 — `CorrelationIdFilter`
- [x] **File**: `archon_search/logging_setup.py` (new file)
- **Depends on**: nothing (standalone; uses only stdlib `logging` and `archon_search.observability`)
- **Description**:
  - Create new module `archon_search/logging_setup.py`
  - Define `CorrelationIdFilter(logging.Filter)`:
    - `filter(self, record: logging.LogRecord) -> bool`
    - Reads `correlation_id.get()` from `archon_search.observability` (ContextVar default is `None`)
    - If the value is not `None`, sets `record.correlation_id = value`
    - If the value is `None`, does **not** set `record.correlation_id` at all — attribute must remain absent (never set to `None`), so that `python-json-logger` omits the field rather than emitting `"correlation_id": null`
    - Always returns `True` (never drops records)
  - Add module-level `import logging` and `from archon_search.observability import correlation_id`
- **Releasable**: `CorrelationIdFilter` is importable and injectable into any handler.
- **Tests (TDD)** — `tests/test_logging_setup.py` (new file):
  - **Test isolation note**: Two `autouse` fixtures are required at the top of `tests/test_logging_setup.py` (they apply to both Task 2.1 and Task 2.2 tests):
    1. Logger isolation fixture — saves `logger.handlers[:]`, `logger.level`, AND `logger.propagate` before each test, then restores all three after each test (removing and closing any handlers added during the test). `propagate` must be saved and restored because `configure_logging()` mutates it, and failure to reset it between tests would cause false positives or false negatives in propagate-related assertions.
    2. ContextVar reset fixture — calls `correlation_id.set(None)` before each test (using a token to reset after), ensuring no ContextVar state leaks between tests.
  - Unit: `test_filter_sets_correlation_id_when_present` — set ContextVar to `"abc123"` via `.set()`; call `filter(record)`; assert `record.correlation_id == "abc123"`
  - Unit: `test_filter_omits_correlation_id_when_absent` — ContextVar has its default value (never set in this context); call `filter(record)`; assert `hasattr(record, "correlation_id") is False`. Distinct condition from `test_filter_does_not_set_none`: this tests the default value path.
  - Unit: `test_filter_always_returns_true` — both with and without ContextVar set, `filter()` returns `True`
  - Unit: `test_filter_does_not_set_none` — ContextVar was explicitly reset to `None` via `correlation_id.set(None)`; call `filter(record)`; assert `not hasattr(record, "correlation_id")`. Distinct condition from `test_filter_omits_correlation_id_when_absent`: this tests the explicit `set(None)` path, because `ContextVar.get()` with a default vs. explicit `set(None)` may behave differently. Both must confirm `hasattr(record, 'correlation_id') is False`.
  - Checkpoint: `uv run pytest tests/test_logging_setup.py::test_filter_sets_correlation_id_when_present tests/test_logging_setup.py::test_filter_omits_correlation_id_when_absent tests/test_logging_setup.py::test_filter_always_returns_true tests/test_logging_setup.py::test_filter_does_not_set_none -v`

#### Task 2.2 — `configure_logging()`
- [x] **File**: `archon_search/logging_setup.py`
- **Depends on**: Task 1.1 (needs `config.log_format`, `config.backup_count`), Task 2.1 (`CorrelationIdFilter`)
- **Description**:
  - Add `python-json-logger>=2.0,<3` to `pyproject.toml` `[project.dependencies]` (hard dependency — always available)
  - Implement `configure_logging(config: SearchConfig) -> None` in `archon_search/logging_setup.py`:
    1. Obtain `logger = logging.getLogger("archon_search")`
    1a. Remove all existing handlers from the logger and close them, then remove each handler from the logger. Reset `logger.propagate = True`. (This reset ensures idempotency: step 10 will override it back to `False` if a file handler is successfully attached. Without this reset, a second call with `log_file = ""` would leave `propagate = False`, silently discarding all logs.)
    2. Set `logger.setLevel(config.level)` — applies the configured level to the root `archon_search` logger hierarchy
    3. If `config.log_file == ""` (empty string), return immediately — stderr-only mode, no handler attached
    4. Expand the path: `log_path = Path(config.log_file).expanduser()`
    5. Try `log_path.parent.mkdir(parents=True, exist_ok=True)`. On `OSError`, call `logging.warning(...)` and return without attaching a handler. Then wrap `TimedRotatingFileHandler(...)` construction in a `try/except OSError`: on failure, call `logging.warning(...)` and return without attaching a handler. (`PermissionError` is a subclass of `OSError` — catching `OSError` alone is sufficient.) This handles cases where the directory exists but is not writable (e.g., containerised deployments with read-only `/var/log/`).
    6. Build `handler = TimedRotatingFileHandler(log_path, when="midnight", utc=True, backupCount=config.backup_count, encoding="utf-8")`
    7. Attach `CorrelationIdFilter` to the handler: `handler.addFilter(CorrelationIdFilter())`
    8. Build and attach the formatter; set `formatter.converter = time.gmtime` on both text and JSON formatters to ensure the `Z` suffix in `datefmt` is truthful (by default `logging.Formatter` uses `time.localtime()`):
       - `log_format == "text"`: `logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")`
       - `log_format == "json"`: `JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", rename_fields={"levelname": "level", "name": "logger", "asctime": "timestamp"}, datefmt="%Y-%m-%dT%H:%M:%SZ")`. Note: without an explicit format string, `JsonFormatter` defaults to `%(message)s` only — `rename_fields` has nothing to rename and the `timestamp`/`level`/`logger` fields are absent from output.
    9. `logger.addHandler(handler)`
    10. Set `logger.propagate = False` — prevents duplicate stderr output caused by uvicorn's internal `logging.config.dictConfig()` call. When `log_file = ""`, propagate is left unchanged.
  - Imports needed: `from logging.handlers import TimedRotatingFileHandler`, `from pathlib import Path`, `import time`, `from archon_search.config import SearchConfig`; since `python-json-logger` is a hard dependency, import `JsonFormatter` at module level: `from pythonjsonlogger.jsonlogger import JsonFormatter` (v2.x). Do NOT use a conditional import.
  - Note on import path: the pin `>=2.0,<3` resolves to v2.x — use `from pythonjsonlogger.jsonlogger import JsonFormatter`. If upgrading to v3.x, change pin to `>=3.0,<4` and use `from pythonjsonlogger.json import JsonFormatter`.
  - Note on text format and `correlation_id`: the text format string `%(asctime)s %(levelname)s %(name)s %(message)s` does not include `%(correlation_id)s`, so `correlation_id` is never exposed in text output. The filter still runs and sets the attribute, but the format string does not reference it. This is intentional — operators needing request tracing must use `log_format = "json"`.
- **Releasable**: `configure_logging(config)` is callable and produces a working file handler when `log_file` is set.
- **Tests (TDD)** — `tests/test_logging_setup.py`:
  - Note: the two `autouse` fixtures defined in Task 2.1 (logger isolation + ContextVar reset) apply here as well. The logger isolation fixture saves/restores `logger.handlers[:]`, `logger.level`, AND `logger.propagate`.
  - Unit: `test_configure_logging_attaches_handler_when_log_file_set(tmp_path)` — call `configure_logging()` with a valid `log_file` in `tmp_path`; assert `logging.getLogger("archon_search")` has at least one `TimedRotatingFileHandler`; also assert `handler.baseFilename` matches (or contains) the expected file path derived from `tmp_path`
  - Unit: `test_configure_logging_no_handler_when_log_file_empty` — first attach a dummy `logging.StreamHandler` to `logging.getLogger("archon_search")` to prove the logger is not blank; call `configure_logging()` with `config.log_file = ""`; assert no `TimedRotatingFileHandler` is attached (step 1a removes ALL handlers, so the pre-attached `StreamHandler` will also be gone — assert zero handlers total, or at minimum assert no TRFH)
  - Unit: `test_configure_logging_sets_level(tmp_path)` — set `config.level = "WARNING"`; call `configure_logging()`; assert `logging.getLogger("archon_search").level == logging.WARNING`
  - Unit: `test_configure_logging_json_format_produces_valid_json(tmp_path)` — `log_format = "json"`, emit a log message, read the file, parse as JSON, assert fields `timestamp`, `level`, `logger`, `message` are present. Also assert that the old key names `asctime`, `levelname`, and `name` are NOT present in the parsed JSON object (a buggy rename that copies rather than renames would otherwise still pass).
  - Unit: `test_configure_logging_text_format_is_not_json(tmp_path)` — `log_format = "text"`, emit a log message, read the file, assert the line cannot be parsed as JSON
  - Unit: `test_configure_logging_json_includes_correlation_id(tmp_path)` — `log_format = "json"`, set ContextVar to `"req-xyz"`, emit a log, read file, parse JSON, assert `"correlation_id": "req-xyz"`
  - Unit: `test_configure_logging_json_omits_correlation_id_when_absent(tmp_path)` — `log_format = "json"`, do not set ContextVar, emit a log, parse JSON, assert `"correlation_id"` key is absent
  - Unit: `test_configure_logging_text_format_no_correlation_id_in_output(tmp_path)` — `log_format = "text"`, set ContextVar to `"req-xyz"`, emit a log, read the file line; assert `"req-xyz"` does NOT appear in the text output (the filter sets the attribute but the text format string does not include `%(correlation_id)s`)
  - Unit: `test_configure_logging_directory_created(tmp_path)` — `log_file` path has a non-existent subdirectory; after `configure_logging()`, assert directory was created
  - Unit: `test_configure_logging_directory_failure_does_not_crash(tmp_path, monkeypatch)` — patch `Path.mkdir` to raise `OSError`; assert `configure_logging()` completes without raising; assert no handler is attached to root `archon_search` logger
  - Unit: `test_configure_logging_handler_construction_failure_does_not_crash(tmp_path, monkeypatch)` — monkeypatch `logging.handlers.TimedRotatingFileHandler.__init__` to raise `PermissionError`; assert `configure_logging()` completes without raising and no `TimedRotatingFileHandler` is attached to the logger. (Do not rely on filesystem `chmod` tricks — they are unreliable on macOS/containers.)
  - Unit: `test_configure_logging_filter_on_handler_not_root_logger(tmp_path)` — after `configure_logging()`, assert the `CorrelationIdFilter` is in `handler.filters`, not in `logging.getLogger("archon_search").filters`
  - Unit: `test_configure_logging_idempotent_no_duplicate_handlers(tmp_path)` — call `configure_logging()` twice with the same config; assert exactly one `TimedRotatingFileHandler` is attached to the logger (also covers C-1 idempotency requirement)
  - Unit: `test_configure_logging_handler_parameters(tmp_path)` — after `configure_logging()`, get the `TimedRotatingFileHandler` from the logger; assert `handler.when == "midnight"`, `handler.utc is True`, `handler.backupCount == config.backup_count`, `handler.encoding == "utf-8"`, and `str(tmp_path / "test.log") in handler.baseFilename`
  - Unit: `test_configure_logging_formatter_uses_utc(tmp_path)` — run this test twice: once with `config.log_format = "text"` and once with `config.log_format = "json"`. In both cases, after calling `configure_logging()`, get the handler from the logger and assert `handler.formatter.converter is time.gmtime`. Alternatively, parameterize with `@pytest.mark.parametrize("log_format", ["text", "json"])` so both formatters are verified in a single test definition. (The `configure_logging()` spec requires both the text and JSON formatters to set `converter = time.gmtime`; a single-format test would leave one path uncovered.)
  - Unit: `test_json_formatter_importable` — assert `from pythonjsonlogger.jsonlogger import JsonFormatter` does not raise `ImportError`
  - Unit: `test_configure_logging_sets_propagate_false_when_file_handler_attached(tmp_path)` — configure with a non-empty `log_file` in `tmp_path`; assert `logging.getLogger("archon_search").propagate is False`
  - Unit: `test_configure_logging_propagate_true_when_log_file_empty` — configure with `log_file = ""`; assert `logging.getLogger("archon_search").propagate is True`
  - Unit: `test_configure_logging_level_filters_messages(tmp_path)` — configure with `config.level = "WARNING"` and a `log_file` in `tmp_path`; emit one message at DEBUG level and one at WARNING level via `logging.getLogger("archon_search.test_filter_child")`; flush and read the file; assert the WARNING message appears in the file and the DEBUG message does NOT appear
  - Unit: `test_configure_logging_idempotent_closes_old_handler(tmp_path)` — call `configure_logging()` once; capture the handler reference from `logging.getLogger("archon_search").handlers`; call `configure_logging()` again with the same config; assert the captured old handler's stream is closed (`old_handler.stream.closed is True`)
  - Unit: `test_configure_logging_transition_file_to_empty(tmp_path)` — call `configure_logging()` with a valid `log_file` in `tmp_path` (handler is attached, `propagate` is `False`); then call `configure_logging()` again with `config.log_file = ""`; assert: no `TimedRotatingFileHandler` is attached to the logger; `logging.getLogger("archon_search").propagate is True`; the old handler's stream is closed (`old_handler.stream.closed is True`). (This scenario is the primary motivation for the `propagate` reset in step 1a — without it, a second call with `log_file = ""` would leave `propagate = False`, silently discarding all logs.)
  - Unit: `test_configure_logging_handler_backup_count_zero(tmp_path)` — configure with `config.backup_count = 0`; assert the attached `TimedRotatingFileHandler`'s `backupCount == 0`.
  - Unit: `test_configure_logging_tilde_path_expanded(tmp_path, monkeypatch)` — monkeypatch `Path.home()` to return `tmp_path` (to avoid writing to the real home directory); set `config.log_file = "~/test-archon.log"`; call `configure_logging()`; assert the attached handler's `baseFilename` does NOT contain `~` and starts with `str(tmp_path)`. (If `expanduser()` is omitted, the handler would try to create a literal `~` directory and this assertion would fail.)
  - Unit: `test_configure_logging_json_exception_is_valid_json(tmp_path)` — configure with `config.log_format = "json"` and a `log_file` in `tmp_path`; inside an `except` block catching a `ValueError("test")`, emit a log with `exc_info=True`; flush the handler; read the file; parse each line as JSON; assert the parsed object is valid JSON and contains `"message"`. Also assert the exception information appears as a field (note: the exact field name — `"exc_info"`, `"exception"`, or similar — should be verified against the resolved `python-json-logger` version at implementation time; if the version produces multiline traceback text rather than a JSON-compatible value, the test should assert that the entire line is still parseable as a single JSON object, i.e., no embedded unescaped newlines break the JSON structure).
  - **Note on handler flushing in file-reading tests**: in all tests that emit a log message and then read the log file, call `handler.flush()` (e.g., `logging.getLogger("archon_search").handlers[0].flush()`) immediately after emitting the log and before opening the file. Although `StreamHandler.emit()` calls `flush()` by default, explicitly flushing prevents latent flakiness on systems where OS-level buffering delays visibility of written bytes.
  - Checkpoint: `uv run pytest tests/test_logging_setup.py -v`

#### Task 2.3 — Wire `configure_logging()` into `run_server()`
- [x] **File**: `archon_search/server/app.py`
- **Depends on**: Task 2.2
- **Description**:
  - Add `from archon_search.logging_setup import configure_logging` import
  - In `run_server(config: SearchConfig) -> None`, add `configure_logging(config)` as the first line before `job_store = JobStore()`
  - No other changes to `run_server()` or `create_app()`
- **Releasable**: a running server now writes structured logs to file.
- **Note on uvicorn dictConfig interference**: uvicorn's `run()` internally calls `logging.config.dictConfig()` with `disable_existing_loggers: False`. This does not remove the `archon_search` handler, but log records propagate to root, causing duplicate output on stderr. This is mitigated by `configure_logging()` setting `logger.propagate = False` when a file handler is attached (see Task 2.2, step 10).
- **Tests (TDD)** — `tests/test_app.py` (extend existing):
  - Integration: `test_run_server_calls_configure_logging(monkeypatch)` — monkeypatch `archon_search.server.app.configure_logging` to a spy (the function as imported into the `app` module, not the original `logging_setup` module location); call `run_server()` (also patching `uvicorn.run` to no-op); assert the spy was called once with the config object
  - Checkpoint: `uv run pytest tests/test_app.py -v`

---

### Phase 3 — Logger name normalisation + CI guard

> **Releasable**: after Task 3.1 — the `archon_search.*` logger hierarchy is coherent; level propagation and structured JSON `logger` field work correctly; the CI guard prevents regression.

#### Task 3.1 — Normalise all `getLogger()` calls + add CI guard (single commit)
- [x] **Files**: 33 source files in `archon_search/` (see table in Architecture section); `tests/test_logger_names.py` (new)
- **Depends on**: Task 2.2 (so `logging_setup.py` exists and its intentional `"archon_search"` root call is in place before the guard is written)
- **Description**:
  - In each of the 33 files listed in the Architecture table, change `logging.getLogger("<hardcoded>")` to `logging.getLogger(__name__)`. The module-level variable name (`logger`, `_logger`, `log`, `_log`) is unchanged.
  - Exception: in `archon_search/logging_setup.py`, the call `logging.getLogger("archon_search")` is intentional (it targets the root logger). The CI guard must allow this one explicit string.
  - **In the same commit**, add `tests/test_logger_names.py` with the following structure (mirror `test_no_fstring_sql.py` pattern):
    - `_BAD_NAMES` regex: matches `getLogger(` followed by a quoted string literal (single or double quoted) that is NOT `__name__` and does NOT start with `archon_search` (covers `"archon"`, `"archon.search"`, `"archon-search"`, `'archon'`, and any other non-conforming string). The regex must match both single and double quoted logger name strings. Example pattern (conceptual): `getLogger\s*\(\s*['\"](?!archon_search)[^'\"]+['\"]`. The scanner must skip lines that are comments (lines where the first non-whitespace character is `#`). Strip comment-only lines before applying the regex, or use a regex that does not match patterns preceded only by `#` on the same line. This prevents false positives when a developer adds a comment explaining a name change.
    - Meta-test `test_guard_detects_bad_name`: assert the regex matches a string like `logging.getLogger("archon")`
    - Meta-test `test_guard_detects_single_quoted_bad_name`: assert the regex matches `logging.getLogger('archon')` (single quotes)
    - Meta-test `test_guard_ignores_dunder_name`: assert `logging.getLogger(__name__)` does not match
    - Meta-test `test_guard_ignores_archon_search_prefix`: assert `logging.getLogger("archon_search")` and `logging.getLogger("archon_search.server.app")` do not match
    - Real guard `test_no_bad_logger_names`: scan all `.py` files under `archon_search/`; assert zero matches; on failure, report file path + line number + offending string
  - The CI guard must be included in the same commit as the normalisation — adding it before the normalisation is complete will fail the build.
- **Releasable**: all `archon_search.*` loggers are in the correct hierarchy; CI prevents regression.
- **Tests (TDD)** — `tests/test_logger_names.py` (all tests in this file are the acceptance gate):
  - Unit: `test_guard_detects_bad_name` — meta-test: regex fires on `logging.getLogger("archon")`
  - Unit: `test_guard_detects_single_quoted_bad_name` — meta-test: regex fires on `logging.getLogger('archon')` (single quotes)
  - Unit: `test_guard_detects_archon_dot_search` — meta-test: regex fires on `logging.getLogger("archon.search")`
  - Unit: `test_guard_detects_archon_dash_search` — meta-test: regex fires on `logging.getLogger("archon-search")`
  - Unit: `test_guard_ignores_dunder_name` — meta-test: `logging.getLogger(__name__)` does not match
  - Unit: `test_guard_ignores_archon_search_root` — meta-test: `logging.getLogger("archon_search")` does not match (intentional root logger in `logging_setup.py`)
  - Unit: `test_guard_ignores_archon_search_dot_prefix` — meta-test: `logging.getLogger("archon_search.server.app")` does not match
  - Unit: `test_guard_ignores_commented_lines` — meta-test: assert the regex does NOT fire on `# logging.getLogger("archon")` (a commented-out example line)
  - Real guard: `test_no_bad_logger_names_in_archon_search` — scans all `.py` under `archon_search/`; zero violations; failure message shows file + line + offending call
  - Checkpoint: `uv run pytest tests/test_logger_names.py -v`

---

### Phase 4 — Configuration artifacts & verification

> **Releasable**: after Task 4.2 — the feature is fully verified, documented, and ready to ship.

#### Task 4.1 — Update `archon-search.toml.example`
- [x] **File**: `archon-search.toml.example`
- **Depends on**: Task 1.1
- **Description**:
  - In the `[logging]` section, add two commented-out keys with descriptions below the existing `log_file` line:
    ```toml
    # Log format: "text" (default) or "json" (structured JSON for log aggregators).
    # format = "text"
    
    # Number of rotated log files to retain (daily rotation at UTC midnight).
    # 0 = never delete rotated files (they accumulate indefinitely).
    # 1 = keep only 1 rotated file in addition to the current log file.
    # backup_count = 7
    ```
  - Add a comment above `log_file` noting the upgrade migration behaviour:
    ```toml
    # Set to "" to disable file logging (stderr only — useful in containers).
    # Upgrade note: the non-empty default activates file logging automatically;
    # set log_file = "" explicitly to opt out.
    log_file = "~/.archon-search/logs/archon-search.log"
    ```
- **Releasable**: the example TOML documents all four `[logging]` keys.
- **Tests (TDD)**: N/A — documentation file; verified manually in Task 4.2.
- **Checkpoint**: N/A

#### Task 4.2 — Final verification & documentation update
- [x] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, architecture docs, user guides, configuration references) and update every file whose content is affected by the changes delivered in this plan. Files to update at minimum:
    - `Documentation/UserManual/02_configuration.md` — update `[logging]` section with all four keys, their defaults, validation rules, `backup_count = 0` behaviour, upgrade migration note, and multi-worker constraint
    - `Documentation/Architecture/010_engineering_principles_and_constraints.md` — if it documents logging constraints, update
    - `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md` — update logging/observability section
    - `README.md` or `contributing.md` — if either mentions the logging config
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - **File handler attachment**: server emits log lines to `log_file` when it is non-empty; no file is created when `log_file = ""`
  - **Log level enforcement**: messages below the configured level do not appear in the log file; messages at or above do
  - **JSON format — valid JSON**: each line in the log file is parseable as JSON and contains the keys `timestamp`, `level`, `logger`, `message` when `log_format = "json"`
  - **JSON field mapping**: `python-json-logger`'s `rename_fields` produces `level` (not `levelname`), `logger` (not `name`), `timestamp` (not `asctime`)
  - **Text format**: log lines match `"%(asctime)s %(levelname)s %(name)s %(message)s"` format when `log_format = "text"`; no JSON
  - **`correlation_id` present in JSON**: when a request context is active, the JSON line contains `"correlation_id"` matching the request's ID
  - **`correlation_id` absent in JSON**: when no request context is active (startup logs, background tasks), the JSON line has no `"correlation_id"` key — not `null`, absent
  - **`level` validation**: an invalid `level` string in TOML raises `ConfigError` at load time; `"info"` is accepted (case-insensitive) and stored as `"INFO"`
  - **`format` validation**: a value other than `"text"` or `"json"` raises `ConfigError` at load time
  - **`backup_count` validation**: a negative value raises `ConfigError`; `0` is accepted
  - **Log directory auto-created**: the parent directory of `log_file` is created on startup if it does not exist
  - **Directory creation failure — graceful**: server starts successfully and emits a warning when the directory cannot be created; no file handler is attached
  - **Logger hierarchy coherent**: `logging.getLogger("archon_search").setLevel("DEBUG")` propagates to all `archon_search.*` loggers; no logger emits to a logger outside the hierarchy
  - **CI guard passes**: `uv run pytest tests/test_logger_names.py` passes with zero violations
  - **Full test suite passes**: `uv run pytest` (default run, excluding `live`, `eval`, `benchmark`, `integration` markers) passes with >= 85% coverage
- **Tests (TDD)**: N/A — verification task.
- **Checkpoint**: `uv run pytest` (full default suite); manually confirm all acceptance criteria above are checked.
