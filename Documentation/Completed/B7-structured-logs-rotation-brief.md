# Feature Brief: B7 — Structured Logs + Log Rotation

## Problem
Operators cannot capture `archon-search` application logs to a file or ship them to log aggregation pipelines. The `[logging]` config section exists and is parsed, but is never wired up — no file handler is attached, log level is not enforced, and the log format cannot be changed. All output goes to uvicorn's stderr today.

## Goal
Application logs are written to a configurable file path with daily rotation, the configured log level is enforced, and an optional JSON format allows operators to route logs to structured aggregation pipelines (ELK, Loki, Datadog).

## Users & Context
Operators deploying archon-search as a background service who need: (a) persistent log files for debugging and incident response, (b) structured JSON output for log aggregation pipelines, or (c) both. They configure this once in `archon-search.toml` and expect it to work across restarts.

## Core Flow

1. Operator sets `log_file`, `level`, `format`, and optionally `backup_count` in `[logging]` section of `archon-search.toml`.
2. On server startup, archon-search creates the log directory if it does not exist.
3. A `TimedRotatingFileHandler` (daily rotation at midnight) is attached to the `archon_search` root logger with the configured level and format.
4. If `format = "json"` (i.e., `log_format = "json"`), a `python-json-logger` `JsonFormatter` is applied to the handler with field renaming and ISO 8601 timestamps; otherwise the text format `"%(asctime)s %(levelname)s %(name)s %(message)s"` is used (there is no pre-existing format to inherit — logs currently have no explicit formatter).
5. Each log record written to the file includes: `timestamp`, `level`, `logger`, `message`, and `correlation_id` if present in the request context.
6. On each midnight rotation, the previous day's file is renamed to `archon-search.log.YYYY-MM-DD` and a new file is opened. Files older than `backup_count` days are deleted by the handler.
7. If `log_file` is set to an empty string, no file handler is attached — output remains on stderr only. Note: the current default is `"~/.archon-search/logs/archon-search.log"` (non-empty), so file logging is active by default on upgrade unless the operator explicitly sets `log_file = ""`.

## In Scope

- Normalize all logger names to the `archon_search.*` hierarchy by replacing hardcoded string literals (`"archon"`, `"archon-search"`, `"archon.search"`, `"archon_search"`) with `logging.getLogger(__name__)`. Using `__name__` naturally produces hierarchical names matching the import path (e.g., `archon_search.server.app`, `archon_search.pipeline`) and is the only approach that makes future per-logger overrides viable.
- Add a CI test (`tests/test_logger_names.py`) that greps `archon_search/` for `getLogger(` calls with hardcoded string arguments that are not `archon_search.*` prefixed — analogous to `test_no_fstring_sql.py`. This prevents regression of the normalization. **The CI guard and logger name normalization must ship in the same commit.** Adding the guard before completing the normalization will fail the build.
- Wire `config.level` to the root `archon_search` logger at startup.
- Wire `config.log_file` to a `TimedRotatingFileHandler` (daily, `when="midnight"`, `utc=True`).
- Add `log_format` config field (TOML key: `format`): `"text"` (default) or `"json"`. Python field named `log_format` to avoid shadowing the `format()` builtin. In `load_config()`, read TOML key `"format"` from the `[logging]` section and assign to `config.log_format`. This is the only `[logging]` field where the TOML key and Python field name differ.
- Add `backup_count` config field (default: `7`) to `SearchConfig` alongside the existing `level` and `log_file` fields. Parse from TOML key `backup_count` in the `[logging]` section in `load_config()`. (TOML key and Python field name are both `backup_count`.)
- Add `python-json-logger>=2.0,<3` as a dependency; use its `JsonFormatter` when `log_format = "json"`. Import as `from pythonjsonlogger.jsonlogger import JsonFormatter` (v2.x API). Verify the import path matches the resolved version. Configure `JsonFormatter` with `rename_fields={"levelname": "level", "name": "logger", "asctime": "timestamp"}` and `datefmt="%Y-%m-%dT%H:%M:%SZ"` (ISO 8601 UTC).
- Inject `correlation_id` into log records via a `logging.Filter` attached to the **file handler** (not the root logger), to avoid adding the field to stderr output. The filter sets `record.correlation_id` to the current ContextVar value when present; when the ContextVar has no value, the filter must **not set** `record.correlation_id` at all (leaving the attribute absent from the record, ensuring `python-json-logger` omits the field rather than emitting `null`). The `correlation_id` ContextVar is **defined in `archon_search.observability`** and must be imported from there in the Filter implementation.
- Create `log_file` parent directory on startup (same pattern as telemetry `log_dir`).
- Apply `Path(config.log_file).expanduser()` at use-site (when creating the handler and creating the directory), consistent with the codebase pattern for `db_path` and `telemetry.log_dir`. Do NOT expand at config load time.
- Validate `level` config value at load time; raise `ValueError` for unknown strings. Accepted values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
- Configure logging (attach handlers, set levels) at the **top of `run_server()`**, as the first action before `create_app()` and `uvicorn.run()`. Extract this into a `configure_logging(config: SearchConfig) -> None` function for testability.
- Update `archon-search.toml.example` with the new keys.
- Update `Documentation/UserManual/02_configuration.md` logging section.

## Out of Scope

- **Telemetry JSONL rotation** — existing date-based rotation + retention-day pruning is sufficient; no changes needed.
- **structlog** — would require changing every log call site; the JSON formatter approach achieves the same output with zero call-site changes.
- **External log shipping / remote transport** — local-only in v1, consistent with telemetry's `export_enabled = false` stance.
- **Size-based rotation (RotatingFileHandler)** — daily rotation is simpler to reason about and consistent with telemetry's date pattern.
- **Changing log message content or log levels at existing call sites** — only formatter and handler are touched; existing messages are unchanged.
- **File logging for CLI subcommands** — `archon-search ingest`, `archon-search sync`, and other CLI commands do not go through `run_server()` and will not write to the configured log file. Their output goes to stderr only. This may be addressed in a future iteration by calling `configure_logging()` from all CLI entry points.

## Key Decisions

- **TimedRotatingFileHandler (daily) over RotatingFileHandler (size-based)**: Consistent with telemetry's date-based file pattern; simpler to reason about in incident response ("yesterday's log" is always one file). Rotation uses `utc=True`, so rotation occurs at UTC midnight and rotated file suffixes are UTC dates. For operators in non-UTC timezones, "yesterday's log" refers to the UTC day boundary, not local midnight.
- **python-json-logger over a custom formatter**: Well-tested library, handles edge cases (non-serializable extras, exception formatting), adds one small dependency rather than 40 lines of fragile custom code.
- **Opt-in JSON (text default)**: Plain text remains the default for local dev ergonomics; operators who need structured logs explicitly set `format = "json"` in `archon-search.toml` (Python field: `log_format = "json"`).
- **Normalize logger names to `archon_search.*`**: Makes hierarchical level filtering work (`logging.getLogger("archon_search").setLevel(...)` affects all module loggers). Underscore form matches the import name.
- **`correlation_id` via `logging.Filter` + `contextvars.ContextVar`**: Zero changes to existing log call sites; the filter injects the field from request context automatically. The `correlation_id` ContextVar is defined in `archon_search.observability` and populated by `RequestContextMiddleware` in `middleware_context.py`. The filter is attached to the **file handler only** (not the root logger) to avoid injecting the field into stderr output.

## Edge Cases & Constraints

- **`log_file` not set or empty**: Skip file handler entirely; log only to stderr. Do not raise an error — this is valid for container deployments that capture stdout/stderr directly. **Migration note**: the current `config.py` default is `log_file = "~/.archon-search/logs/archon-search.log"` (non-empty), meaning file logging will activate automatically on upgrade for any operator who has not set this key explicitly. Operators who want no file logging must set `log_file = ""` in `archon-search.toml`. This should be documented in the upgrade notes for the release that ships B7.
- **Log directory creation failure**: Log a warning and continue without a file handler; do not crash the server.
- **TimedRotatingFileHandler on Windows**: File rename on rotation can fail if the service manager holds the file open. Document as a known limitation; recommend log-shipping agents as an alternative on Windows.
- **`backup_count = 0`**: In Python's `TimedRotatingFileHandler`, `backupCount=0` disables deletion of rotated files — they accumulate indefinitely. Operators who set this should be aware that disk space is unbounded. To keep only the current file, set `backup_count = 1`. Document both behaviors.
- **`backup_count` invalid value**: Reject negative values at config load time with `ValueError`. Zero is allowed (see above). No upper bound.
- **`log_format` invalid value**: Reject values other than `"text"` and `"json"` for the `log_format` config field (TOML key: `format`) at config load time with `ValueError`.
- **`level` validation**: Reject unknown level strings at config load time (same validation style as existing config keys). Accepted values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
- **Multi-worker deployment**: `TimedRotatingFileHandler` is not multi-process safe. File logging is only supported when archon-search runs as a single process (the current default — `uvicorn.run()` in `run_server()` uses a single worker). Operators who run multiple workers must disable file logging (`log_file = ""`) and use an external log aggregator instead.
- **JSON format and uvicorn's own logs**: uvicorn logs to its own logger hierarchy and will not be affected by archon-search's handler. This is acceptable — operators targeting a log aggregator will typically configure uvicorn's log format separately.
- **Correlation ID absent**: The filter must omit the `correlation_id` field (not emit `null`) when no request context is active (e.g., background tasks, startup logs). The filter must **not set** `record.correlation_id` to `None` — it must leave the attribute unset entirely.

## Acceptance Criteria

- **File handler attachment**: File handler is attached to the `archon_search` logger when `log_file` is non-empty; no file handler is attached when `log_file = ""`.
- **Log level enforcement**: Messages below the configured level are not written to the file handler; messages at or above are written.
- **JSON format — valid JSON**: When `log_format = "json"`, each line written to the log file is valid JSON. Use `python-json-logger`'s `rename_fields` parameter to map: `levelname` → `level`, `name` → `logger`, `asctime` → `timestamp`. ISO 8601 format for `timestamp`. The JSON object must contain at minimum: `timestamp`, `level`, `logger`, `message`. `correlation_id` is also present when a request context is active.
- **Text format**: When `log_format = "text"`, log lines follow the format `"%(asctime)s %(levelname)s %(name)s %(message)s"` — not JSON.
- **`correlation_id` present**: When a request context is active (i.e., `RequestContextMiddleware` has set the ContextVar), `correlation_id` appears as a field in the log record.
- **`correlation_id` absent**: When no request context is active (e.g., startup logs, background tasks), the `correlation_id` field is omitted from the log record — not present as `null`. The Filter implementation must leave the `record.correlation_id` attribute **unset** when no ContextVar value is present (do NOT set it to `None`).
- **`level` validation**: An invalid `level` string in config raises `ValueError` at config load time; the server does not start.
- **`log_format` validation**: A value other than `"text"` or `"json"` for `log_format` (TOML key: `format`) raises `ValueError` at config load time.
- **`backup_count` validation**: A negative `backup_count` value raises `ValueError` at config load time; zero is accepted.
- **Log directory creation**: The `log_file` parent directory is created automatically on startup if it does not exist.
- **Log directory creation failure**: The server starts successfully (no crash or unhandled exception) when the log directory cannot be created (e.g., permission error); a warning is emitted and the file handler is skipped.
- **Logger name normalization — CI guard**: `tests/test_logger_names.py` asserts that no `getLogger(` call in `archon_search/` uses a hardcoded string argument that is not an `archon_search.*`-prefixed name — analogous to `test_no_fstring_sql.py`. This test must pass as part of the default `pytest` run.

## Open Questions

- Should the JSON formatter include uvicorn access log lines, or only application logger output? (Likely out of scope — leave for a follow-up once structuring archon-search's own logs proves useful.)

## Future Iterations

- **Log level per-logger override** — e.g. `[logging.overrides] archon_search.router = "DEBUG"` for targeted debugging without making the whole service verbose.
- **Structured log fields at pipeline stages** — add `stage`, `collection`, `doc_count` as structured extras to pipeline-layer log records, tying application logs to telemetry entries via `correlation_id`.
- **External log shipping** — once the format is JSON and rotation is stable, `export_enabled` in telemetry and a future `[logging] export_url` could share the same forwarding infrastructure.

## Recommendation

B7 is the right item to close now. The `[logging]` section being dead code is a credibility problem — operators set `log_file = ...` and get nothing, which erodes trust in the config system. The fix is well-bounded: wire the existing config, normalize logger names, add `TimedRotatingFileHandler`, and drop in `python-json-logger` behind a config flag. The hardest part is the logger name normalization — it touches many files but each change is a one-liner. The `correlation_id` injection is the only genuinely novel mechanism, and it's ~20 lines with a `logging.Filter`. Do not compromise on the logger name normalization — without it, level filtering and JSON field `logger` are both unreliable.
