**Purpose**: Configure `archon-search` via `archon-search.toml` and environment variables.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Configuration

## Principles

1. **One file, one process.** All server config lives in a single TOML file. The default is `~/.archon-search/archon-search.toml`; the `ARCHON_SEARCH_CONFIG` environment variable can redirect to any other path.
2. **Defaults are safe.** Every key has a default in `archon_search/config.py:SearchConfig`; a missing file or missing section uses defaults rather than failing.
3. **The auth key is a separate concern.** API keys live in `~/.archon-search/.search.env` (mode `600`), not in the TOML.
4. **Validation is strict where it matters.** Numeric ranges (port, threshold, retention) raise `ConfigError` on bad values, and malformed TOML fails loudly at load time. Type validation is partial: bool/int/float fields raise on the wrong type, but string fields (`host`, `db_path`, `embedding_model`, `reranker_model`, `level`, `log_file`) are coerced permissively via `str()`, so e.g. `host = 123` becomes `"123"` rather than raising.

## Config file location

The loader (`archon_search.config.get_default_config_path`) resolves the path as follows:

1. If `ARCHON_SEARCH_CONFIG` is set, use it. Tilde and relative paths are expanded; relative paths resolve against the current working directory, not `$HOME`.
2. Otherwise use `~/.archon-search/archon-search.toml`.

If the file does not exist, the loader returns an all-defaults `SearchConfig` — the server still starts.

You can inspect or edit the live config from the CLI:

```bash
archon-search config show
archon-search config get server.port
archon-search config set server.port 9000
```

`config set` tries to coerce the value in this order: bool (case-insensitive `"true"`/`"false"`) → int → float → string. Keys must be in `section.field` form. Note that `config set` writes to the TOML file without validating against `SearchConfig` — unknown sections or fields (e.g. `config set foo.bar baz`) silently succeed and are only caught on the next `load_config` call.

## Sections

The annotated reference is `archon-search.toml.example`. The sections below match `archon_search/config.py:SearchConfig` exactly.

### `[server]`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `host` | string | `127.0.0.1` (or `0.0.0.0` when invoked via `archon-search serve`) | Bind address. Set to `0.0.0.0` only if you intend to expose the server. Overridable at runtime via `ARCHON_SEARCH_HOST` env var (env > TOML > default). |
| `port` | int | `8765` | Must be in `[1, 65535]`; out-of-range values raise `ConfigError`. Overridable at runtime via `ARCHON_SEARCH_PORT` env var; non-integer or out-of-range env values raise `ConfigError`. |

### `[database]`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `db_path` | string | `~/.archon-search/search` | LanceDB on-disk location. Tilde is expanded. |
| `embedding_model` | string | `BAAI/bge-small-en-v1.5` | Sentence-transformers model id. |
| `reranker_model` | string | `Xenova/ms-marco-MiniLM-L-6-v2` | Cross-encoder used for second-stage scoring. |
| `chunk_size` | int (>0) | `512` | Target chunk size in tokens. |
| `auto_reindex_on_chunk_size_change` | bool | `true` | If `chunk_size` changes between starts, affected collections are reindexed automatically. #Unverified (reindex behaviour lives outside `config.py`). |
| `providers` | list[string] | `[]` | ONNX Runtime execution providers. See [`01_installation.md`](./01_installation.md). |
| `top_k_retrieve` | int (>0) | `15` | First-stage candidate pool size. |
| `top_k_return` | int (>0) | `5` | Number of results returned by `/search` (per-request `top_k` is ignored — see `BREAKING.md`). |
| `multilingual` | bool | `false` | **C2** — Enable per-document language detection using the fasttext `lid.176.ftz` model. Requires `pip install archon-search[multilingual]` and `lid.176.ftz` present at `~/.archon-search/models/`. Server startup fails with a clear error if either prerequisite is missing. When `true`, ingested documents receive a language tag on all chunks; the `language=<code>` search filter becomes active. |
| `language_detection_confidence_threshold` | float `(0.0, 1.0]` | `0.7` | **C2** — Minimum fasttext confidence for a language prediction to be accepted. Predictions below this threshold produce `language="unknown"` on all chunks. Out-of-range values (≤ 0.0 or > 1.0) raise `ConfigError`. |

### `[routing]`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `routing_shortlist_size` | int (>0) | `8` | Collections considered by the router before parallel search. |
| `routing_confidence_threshold` | float `[0.0, 1.0]` | `0.30` | Minimum centroid-similarity confidence to dispatch a query to a collection. |
| `max_parallel_collections` | int (>0) | `3` | Hard cap on concurrent per-collection searches per query. |

### `[collections]`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `pinned_collections` | list[string] | `[]` | Paths always included in every search, regardless of routing. |
| `collections` | list[string] | `[]` | Static collection paths. Managed via `archon-search collection add/remove` or directly in the file. |
| `watch` | bool | `false` | When `true`, the source directories are watched (watchdog) and reindexed on change. |

### `[logging]`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `level` | string | `INFO` | Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive). Invalid values raise `ConfigError`. |
| `log_file` | string | `~/.archon-search/logs/archon-search.log` | Path to the rotating log file. Tilde is expanded. Set to `""` to disable file logging entirely (recommended for containers and multi-worker deployments — see note below). |
| `format` | string | `text` | Log line format. Valid values: `text` (human-readable) or `json` (structured JSON). Use `json` when shipping logs to ELK, Loki, or Datadog. Invalid values raise `ConfigError`. |
| `backup_count` | int (>=0) | `7` | Number of rotated log files to keep alongside the active file. `0` means rotated files are never deleted and accumulate indefinitely. Invalid values (negative or non-integer) raise `ConfigError`. |

File logging uses a `TimedRotatingFileHandler` that rotates at UTC midnight. When `format = "json"`, each log record is a JSON object; records emitted during an active HTTP request include a `correlation_id` field populated from the request's `X-Request-ID` context.

**Upgrade migration note**: if you upgrade from a version that did not default `log_file`, the new non-empty default activates file logging automatically on next start. Set `log_file = ""` to restore the previous behaviour of logging to stderr only.

**File-only output**: when `log_file` is non-empty, `configure_logging()` sets `logger.propagate = False` to prevent duplicate output. Log output goes **only** to the file — not to stderr. Operators who need both file and stderr output simultaneously must implement a separate log-forwarding solution. macOS users running under launchd should note that `StandardErrorPath` output will be empty while `log_file` is configured, because stderr is suppressed by the propagation flag.

**Multi-worker constraint**: `TimedRotatingFileHandler` is not multi-process safe. If you run `archon-search` behind a multi-worker process manager (e.g. `gunicorn -w N`), set `log_file = ""` in your config to avoid interleaved writes and lost rotation events.

### `[telemetry]`

Opt-in only. See [`06_telemetry.md`](./06_telemetry.md) for the full surface.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch. |
| `retention_days` | int (>=1) | `30` | Files older than this are pruned at startup and every 24h. |
| `log_dir` | string | `~/.archon-search/search-logs` | Must be non-empty. |
| `export_enabled` | bool | `false` | Reserved. **Setting to `true` is silently coerced to `false` with a warning log** (see `archon_search/config.py:209-217`). `archon-search.toml.example` documents this same behaviour. No external transmission occurs in v1. |

### `[namespaces]`

Optional `string = string` mapping (`archon_search/config.py:225-233`). Entries must be string key / string value pairs or `ConfigError` is raised. See `Architecture/150_security_and_privacy_architecture.md` for namespace semantics.

### `[backup]`

Scheduled backup of every collection in every namespace via the in-process `BackupLoop`. Disabled by default. When enabled, the server periodically exports each collection (excluded patterns aside) to a `.tar.gz` archive and rotates older archives. Backup jobs are queued behind any user-initiated export/import jobs.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `interval_hours` | int | `0` | Hours between automatic backup ticks. `0` (or any non-positive value) disables periodic backups entirely; the completion loop still runs to drain any in-flight jobs from a prior session. |
| `keep` | int | `7` | Number of archives to retain per collection. `0` = never rotate (archives accumulate; the config loader emits a WARNING when paired with `interval_hours > 0`). |
| `exclude` | list[str] | `[]` | Collection patterns to skip. Bare `{col}` matches the collection across all namespaces; `{ns}/{col}` matches one namespace only. |
| `output_dir` | string | `""` | Root directory for archives (resolved to `get_data_dir() / "backups"` when empty). Archives land at `{output_dir}/{namespace}/{collection}.backup.{timestamp}.tar.gz`. Paths with fewer than three components fall back to the default and log an ERROR. |

Operator commands:

- `archon-search backup --now` — call `POST /backup/trigger` and print the queued job IDs and any skipped collections (with reason: `excluded`, `already_active`, or `already_queued`). Add `--wait` to poll each job to a terminal state (exits `1` on FAILED).
- `archon-search backup status` — offline-capable summary. Reads `~/.archon-search/.backup-state.json` and counts archive files on disk; merges `last_tick_at` and `next_run_at` from `GET /status` when the server is reachable. `--json` emits a `BackupStatusDetail` payload.

See `Architecture/120_services_and_integration_architecture.md` for the trigger / completion loop design and `Architecture/600_api_reference_or_public_interface.md` for the full schema of `POST /backup/trigger` and the `backup` extension of `GET /status`.

## Authentication

The API key is **not** stored in the TOML. The key manager (`archon_search/key_manager.py:load_or_generate_key`) resolves in this order:

1. **`ARCHON_SEARCH_API_KEY` environment variable** (highest priority). Must be a non-empty lowercase hex string (any length — no length constraint is enforced for env-var-supplied keys); invalid values are logged and ignored, falling through to the file/auto-generate steps.
2. **`ARCHON_SEARCH_KEY_FILE`** if set, otherwise `get_data_dir() / ".search.env"` (`~/.archon-search/.search.env` by default, or `$ARCHON_SEARCH_DATA_DIR/.search.env` when `ARCHON_SEARCH_DATA_DIR` is set — the Docker image sets this to `/data` so the key file lands on the mounted volume at `/data/.search.env`). The loader scans the file line by line and uses the first line starting with `ARCHON_SEARCH_API_KEY=` (trailing whitespace stripped); additional lines are ignored. If the file's permissions are not exactly `600`, the loader forces them to `600` (this can both tighten *and* loosen the mode — e.g. `400` would be widened to `600`).
3. **Auto-generation**. On first start with no env var and no file, a 64-char hex token (`secrets.token_hex(32)`) is generated, written atomically with mode `600`, and used.

To rotate, delete the key file (`~/.archon-search/.search.env` or wherever `get_key_file()` resolves to under `$ARCHON_SEARCH_DATA_DIR`) and restart the server. To use a static key (Docker, CI), set `ARCHON_SEARCH_API_KEY` and skip the file entirely.

## Environment variables

These env vars are read at config load (`archon_search/config.py::load_config`), at key resolution (`archon_search/key_manager.py::get_key_file`), and by every lazy path accessor (`paths.get_data_dir`, `jobs.get_jobs_file`, `language_detector.get_fasttext_models_dir`, `cli/ingest.py`). All overrides take effect on the next `load_config` call.

| Variable | Effect | Default |
| --- | --- | --- |
| `ARCHON_SEARCH_CONFIG` | Path to `archon-search.toml`. | `~/.archon-search/archon-search.toml` |
| `ARCHON_SEARCH_API_KEY` | Bearer token. Highest priority for auth; bypasses the key file entirely. | unset → falls through to key file or auto-generated |
| `ARCHON_SEARCH_KEY_FILE` | Path to the key file. Takes precedence over `ARCHON_SEARCH_DATA_DIR` for the key file. Must be an absolute path; empty / whitespace-only falls through to the DATA_DIR-derived default; tilde with HOME unset raises `ValueError`. | unset → `get_data_dir() / ".search.env"` |
| `ARCHON_SEARCH_HOST` | Bind address override. Empty string is treated as "not set" (no override). Env > TOML > default. | unset → TOML `[server].host` or `127.0.0.1` (or `0.0.0.0` under `archon-search serve`) |
| `ARCHON_SEARCH_PORT` | Bind port override. Must parse to int 1–65535; non-integer or out-of-range raises `ConfigError`. Empty string is treated as "not set". | unset → TOML `[server].port` or `8765` |
| `ARCHON_SEARCH_DATA_DIR` | Relocate the entire runtime tree (LanceDB index, logs, telemetry, key file, jobs file, fastembed models, ingest history) under a single root. Must be an absolute path; empty / whitespace-only raises `ConfigError`. The Docker image sets this to `/data`. | unset → `~/.archon-search/` |
| `ARCHON_SEARCH_CONTAINER` | Attach a `StreamHandler(sys.stderr)` to the `archon_search` logger when set to `"1"` (so `docker logs` captures output). The Docker image sets this to `1`. | unset → no stderr handler |
| `FASTEMBED_CACHE_PATH` | fastembed's own env var for the embedding-model weight cache. The Docker image sets this to `/data/fastembed-cache` so weights persist on the mounted volume. | unset → fastembed default (`~/.cache/fastembed`) |

`ARCHON_SEARCH_CONFIG` and `ARCHON_SEARCH_DATA_DIR` are independent: the TOML config file is **not** relocated by `ARCHON_SEARCH_DATA_DIR`. To put the TOML on the mounted volume too (e.g. for `archon-search collection add/remove` to work inside the container), set `ARCHON_SEARCH_CONFIG=/data/archon-search.toml` explicitly. `archon-search serve` logs a warning at startup if `ARCHON_SEARCH_DATA_DIR` is set without `ARCHON_SEARCH_CONFIG`.

## Related documents

- [`03_running_the_server.md`](./03_running_the_server.md) — applying config changes.
- [`06_telemetry.md`](./06_telemetry.md) — telemetry section in detail.
- [`07_troubleshooting.md`](./07_troubleshooting.md) — config load errors.
- [`08_running_with_docker.md`](./08_running_with_docker.md) — Docker-specific env-var matrix and persistence layout.
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — auth and namespace model.
