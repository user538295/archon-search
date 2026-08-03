**Purpose**: Configure `archon-search` application logs — file rotation, log level, text vs JSON output — and ship structured logs to an aggregation pipeline.
**Audience**: SREs and sysadmins operating `archon-search` in production.
**Status**: Draft
**Last reviewed**: 2026-07-29
**Next review**: 2027-07-29

# Logging

`archon-search` writes its own application logs (distinct from uvicorn's access log) through the `archon_search` logger hierarchy. The `[logging]` config section controls where those logs go, at what level, and in what format. The wiring lives in `archon_search/logging_setup.py` (`configure_logging()`), called as the first action of the server on startup.

By default the server writes a rotating text log file under the data directory. Set the format to `json` to feed a log aggregator (ELK, Loki, Datadog); set `log_file = ""` to fall back to stderr only.

> Only the server (`archon-search serve` / `start`) writes to the configured log file. CLI subcommands (`ingest`, `sync`, `collection …`) do not route through the server's `configure_logging()` and log to **stderr only**.

## The `[logging]` config

All four keys live in the `[logging]` table of `~/.archon-search/archon-search.toml`. Values are validated at config load — an invalid value raises `ConfigError` and the server refuses to start.

| Key | Python field | Default | Accepted values |
| --- | --- | --- | --- |
| `level` | `level` | `"INFO"` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive; `WARN` is normalised to `WARNING`) |
| `log_file` | `log_file` | `"~/.archon-search/logs/archon-search.log"` | any path, or `""` to disable file logging |
| `format` | `log_format` | `"text"` | `"text"` or `"json"` |
| `backup_count` | `backup_count` | `7` | integer `>= 0` |

Notes verified against `archon_search/config.py`:

- **`format` is the one key whose TOML name differs from its Python attribute** (`log_format`). Write `format = "json"` in the TOML, not `log_format`.
- Any `level` string outside the accepted set raises `ConfigError`; the same holds for a `format` other than `text`/`json` and for a negative `backup_count`.
- `~` in `log_file` is expanded at use time (`Path(...).expanduser()`), consistent with `db_path` and `telemetry.log_dir`.

### Worked config snippet

```toml
[logging]
level = "INFO"

# Set to "" to disable file logging (stderr only — useful in containers).
# Upgrade note: the non-empty default activates file logging automatically;
# set log_file = "" explicitly to opt out.
log_file = "~/.archon-search/logs/archon-search.log"

# Log format: "text" (default) or "json" (structured JSON for log aggregators).
format = "json"

# Number of rotated log files to retain (daily rotation at UTC midnight).
# 0 = never delete rotated files (they accumulate indefinitely).
# 1 = keep only 1 rotated file in addition to the current log file.
backup_count = 14
```

`archon-search config show` displays all four keys; `archon-search wizard` and `archon-search install --log-format {text,json} --log-level LEVEL` set them non-interactively.

## Enforced log level

The configured `level` is applied to the root `archon_search` logger (`logger.setLevel(config.level)`), which propagates to every module logger in the `archon_search.*` hierarchy (e.g. `archon_search.pipeline`, `archon_search.router`). Records below the level are dropped before reaching the file. This is a genuine change from earlier versions, where `[logging].level` was parsed but never applied.

## Daily rotation and backup count

File logging uses a `TimedRotatingFileHandler` with `when="midnight"`, `utc=True`, `encoding="utf-8"`, and `backupCount=<backup_count>`:

- Rotation happens at **UTC midnight**. Rotated files are renamed with a UTC-date suffix, e.g. `archon-search.log.2026-07-29`. Operators in non-UTC timezones: "yesterday's log" is the UTC day boundary, not local midnight.
- `backup_count = 7` (default) keeps 7 rotated files plus the current one; older ones are deleted by the handler.
- `backup_count = 1` keeps only one rotated file in addition to the current log.
- **`backup_count = 0` disables deletion** — rotated files accumulate indefinitely (stdlib behaviour). Watch disk usage if you set this.

The parent directory of `log_file` is created automatically on startup. If it cannot be created or the file cannot be opened (e.g. a read-only `/var/log`), the server logs a warning and continues **without** a file handler — it never crashes on a logging failure.

### Single-process only

`TimedRotatingFileHandler` is **not** multi-process safe. File logging is supported only when `archon-search` runs as a single process (the default — `uvicorn.run()` uses one worker). If you run multiple workers, set `log_file = ""` and rely on an external log aggregator scraping stderr/stdout instead.

## Text vs JSON format

**Text** (`format = "text"`, default) uses `"%(asctime)s %(levelname)s %(name)s %(message)s"` with ISO 8601 UTC timestamps (the `Z` suffix is truthful — the formatter's converter is `time.gmtime`). The text format string does **not** include `correlation_id`; request tracing requires JSON.

**JSON** (`format = "json"`) uses `python-json-logger`'s `JsonFormatter` with field renaming. Each line is a single JSON object:

```json
{"timestamp": "2026-07-29T14:03:11Z", "level": "INFO", "logger": "archon_search.pipeline", "message": "ingest complete", "correlation_id": "9f2c1ab34d..."}
```

Field mapping (do not expect the stdlib names): `asctime → timestamp`, `levelname → level`, `name → logger`. At minimum every line carries `timestamp`, `level`, `logger`, `message`. Exceptions logged with `exc_info=True` remain valid single-line JSON.

### Request correlation (B1 fields)

The `correlation_id` field is injected by a `logging.Filter` attached to the file handler (and the container stderr handler), reading the `correlation_id` ContextVar set by `RequestContextMiddleware`:

- When a request is in flight, `correlation_id` is present and **equals the request's `X-Request-ID` response header** (header name configurable via `[observability].request_id_header`, default `X-Request-ID`). This lets you pivot from a log line straight to the request — and to its per-stage latency telemetry (`stage_timings_ms`, also part of B1).
- Outside a request (startup, background jobs), the field is **omitted entirely** — it is never emitted as `null`.

Per-stage latency itself is surfaced through the telemetry/observability surface, not the log line — see [Monitoring and alerts](20_monitoring_and_alerts.md) for how to read it and tie it back via `correlation_id`.

## Container mode

When `ARCHON_SEARCH_CONTAINER=1`, `configure_logging()` attaches a `StreamHandler(sys.stderr)` — using the same format and correlation-id filter as the file handler — **in addition to** any file handler. This guarantees `docker logs` captures output even when `log_file = ""`. It is why the Docker image can run with file logging disabled and still emit structured logs to stdout/stderr for the container runtime to collect.

Outside container mode, an empty `log_file` produces a startup warning that file logging is off. `load_config` emits it at config-load time, and the serve path always runs `load_config` first, so operators typically see it once from there. `configure_logging` also emits it (via the root logger, not gated by `[logging].level`), which adds coverage only when logging is configured from a programmatically-built config that bypassed `load_config` — in the serve path this means the warning may appear twice. In this disabled state no `archon_search` handler is attached, so absent any configured root handler, the warning reaches stderr only via Python's last-resort handler — it is not written to a log file, since file logging is disabled. Set `ARCHON_SEARCH_CONTAINER=1` to silence it when stderr-only is intentional. See [Running with Docker](../UserManual/140_running_with_docker.md).

## Shipping JSON logs to ELK / Loki / Datadog

`archon-search` does **not** ship logs itself (no remote transport in v1, consistent with telemetry's local-only stance). Instead, emit JSON and let a standard agent forward the file or stderr stream:

1. Set `format = "json"` in `[logging]`.
2. Choose the ingestion path:
   - **File tailing** — point Filebeat / Promtail / the Datadog Agent at `~/.archon-search/logs/archon-search.log`. Each line is already a JSON object; configure the agent's JSON/`decode_json_fields` parser and it maps `timestamp`, `level`, `logger`, `message`, `correlation_id` directly.
   - **Container stdout** — run with `ARCHON_SEARCH_CONTAINER=1` and (optionally) `log_file = ""`, then let the container runtime's log driver (Docker json-file, Fluent Bit, Loki's Docker driver, Datadog's container agent) collect stderr.
3. Index on `correlation_id` to correlate multi-line request traces, and on `logger` for module-level filtering.

Notes:
- uvicorn's own access logs use a separate logger hierarchy and are **not** reformatted by this handler. If you want them structured too, configure uvicorn's logging separately.
- Rotated file suffixes are UTC dates; make sure your file-tailing agent follows renamed files (all of the above do by default).

## Upgrade behaviour

The default `log_file` is **non-empty**, so file logging activates automatically on upgrade for any operator who has not set the key. If you deliberately want stderr-only logging, set `log_file = ""` explicitly after upgrading. See [Upgrading](100_upgrading.md).

## Related documents

- [Operator Guide index](00_index.md)
- [Monitoring and alerts](20_monitoring_and_alerts.md) — endpoints, stage-latency telemetry, alert rules
- [Incident runbook](90_incident_runbook.md) — using logs during an incident
- [Configuration reference](../UserManual/30_configuration.md) — full `[logging]` and `[observability]` key reference
- [Running with Docker](../UserManual/140_running_with_docker.md) — container mode and `ARCHON_SEARCH_CONTAINER`
- [Operational readiness](../Architecture/160_operational_readiness_monitoring_and_reliability.md) — observability architecture
