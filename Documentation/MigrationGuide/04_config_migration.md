**Purpose**: Document how `<data-dir>/archon-search.toml` evolves across releases — which keys are stable, which have quirks, and how to validate a config file.
**Audience**: Operators editing config across upgrades; maintainers changing the config schema in `archon_search/config.py`.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-05-20

# Configuration Migration

`<data-dir>/archon-search.toml` (data dir = `ARCHON_SEARCH_DATA_DIR`, default `~/.archon-search/`; `ARCHON_SEARCH_CONFIG` overrides) is the single config file consumed by the server. It is loaded by `archon_search/config.py` `load_config()` and validated against the `SearchConfig` dataclass. Defaults are baked into the dataclass; an absent file is equivalent to an all-defaults config (`load_config` returns the default `SearchConfig`).

## Default config path follows `ARCHON_SEARCH_DATA_DIR` (NR-17)

With `ARCHON_SEARCH_CONFIG` unset, the default config is now `<data-dir>/archon-search.toml` (`archon_search/config.py` `get_default_config_path()`) instead of always `~/.archon-search/archon-search.toml`; default-data-dir installs are unaffected. With a custom `ARCHON_SEARCH_DATA_DIR` and the config still at `~/.archon-search/archon-search.toml`, do one of these before re-running `wizard` or `install`:

1. Move it to `<data-dir>/archon-search.toml` (in Docker: `/data/.archon-search/archon-search.toml` → `/data/archon-search.toml`).
2. Set `ARCHON_SEARCH_CONFIG` to the file's current path.

A legacy-config WARNING flags installs that still need this. Affected commands, the invalid-`ARCHON_SEARCH_DATA_DIR` exit and what happens if you skip the move: [`/BREAKING.md` → NR-17](../../BREAKING.md).

## Principles

1. **All keys are optional.** A missing key falls back to the default in `SearchConfig` / `TelemetryConfig`. An entirely missing file is valid.
2. **Unknown keys are silently ignored, with one exception.** `load_config` reads each section with `doc.get(section, {})` and only consumes known keys, so typos do not raise. The exception is the three removed ONNX-provider keys `[database].providers`, `[database].reranker_providers` and `[graph].providers`: `serve`, `start` and `install` exit `1` on a file that holds one (see [PyTorch runtime keys](#pytorch-runtime-keys-removed-and-added)).
3. **Type errors raise `ConfigError`.** Wrong type (including a TOML boolean in a number key, `inf`/`nan` in a float key, or a fractional value in an integer key), out-of-range port, negative `chunk_size`, etc. fail loudly at load time. See `_coerce_int` / `_coerce_float` / `_coerce_bool` in `config.py`.
4. **Range validation is enforced where it exists.** `port` must be `1..65535`, `routing_confidence_threshold` must be in `[0.0, 1.0]`, several integer fields must be `> 0`.

## Stable keys

The following sections and keys are part of the documented config surface and have been stable since v1. The reference example file is [`archon-search.toml.example`](../../archon-search.toml.example) at the repo root; defaults below mirror that file and `SearchConfig` / `TelemetryConfig`.

| Section | Key | Type | Default | Notes |
| --- | --- | --- | --- | --- |
| `[server]` | `host` | string | `"127.0.0.1"` | Bind address. Native `serve` binds it; with `ARCHON_SEARCH_CONTAINER=1` (the Docker image) the default is `0.0.0.0`. `ARCHON_SEARCH_HOST` overrides. |
| `[server]` | `port` | int | `8765` | Validated `1..65535`. `ARCHON_SEARCH_PORT` overrides. |
| `[database]` | `db_path` | string | `"~/.archon-search/search"` | LanceDB root. |
| `[database]` | `embedding_model` | string | `"BAAI/bge-small-en-v1.5"` | One of the 19 supported embedders (`archon_search/model_specs.py`); any other name is `422` on `POST /collections/`. |
| `[database]` | `reranker_model` | string | `"Xenova/ms-marco-MiniLM-L-6-v2"` | One of the 5 supported rerankers; empty disables the reranker. |
| `[database]` | `chunk_size` | int | `512` | Must be `> 0`. |
| `[database]` | `auto_reindex_on_chunk_size_change` | bool | `true` | Reindex on chunk-size diff at start. |
| `[database]` | `device` | string | `"auto"` | `auto`, `cpu`, `mps` or `cuda` for the embedder and the reranker (replaces `providers` / `reranker_providers`). |
| `[database]` | `top_k_retrieve` | int | `15` | Pipeline shortlist size, must be `> 0`. |
| `[database]` | `top_k_return` | int | `5` | Final returned cut-off, must be `> 0`. |
| `[routing]` | `routing_shortlist_size` | int | `8` | Must be `> 0`. |
| `[routing]` | `routing_confidence_threshold` | float | `0.30` | In `[0.0, 1.0]`. |
| `[collections]` | `pinned_collections` | list[string] | `[]` | Always searched, regardless of routing. |
| `[collections]` | `collections` | list | `[]` | Static collection list; empty means manage over HTTP. |
| `[collections]` | `watch` | bool | `false` | Enable filesystem watcher. |
| `[logging]` | `level` | string | `"INFO"` | Python logging level. |
| `[logging]` | `log_file` | string | `"~/.archon-search/logs/archon-search.log"` | Log destination. |
| `[telemetry]` | `enabled` | bool | `false` | Opt-in switch. |
| `[telemetry]` | `retention_days` | int | `30` | Must be `>= 1`. |
| `[telemetry]` | `log_dir` | string | `"~/.archon-search/search-logs"` | Non-empty. |
| `[namespaces]` | `<key> = <value>` | string = string | `{}` | Both sides must be strings; otherwise `ConfigError`. Note: `load_config` only checks the string-type constraint — the namespace-identifier regex (`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`) and reserved-name rules in `constants.py::_validate_namespace` are enforced by callers that use the namespace, not at config load. |

## Keys added by the 2026-10-25 security and operations batch

Every key is optional and sits in the section shown. Defaults mirror [`archon-search.toml.example`](../../archon-search.toml.example) and `archon_search/constants.py`. Detail and migration: the entries NR-43 … NR-48 in [`03_breaking_changes_index.md`](./03_breaking_changes_index.md) and [`/BREAKING.md`](../../BREAKING.md).

| Section | Key | Type | Default | Notes |
| --- | --- | --- | --- | --- |
| `[server]` | `max_request_body_mb` | int | `32` | Must be `>= 1`. A larger body is `413` before authentication. |
| `[server]` | `cors_origins` | list[string] | `[]` | Explicit `scheme://host[:port]` origins; empty sends no CORS headers; `*` is a `ConfigError`. |
| `[server]` | `docs_enabled` | bool | `false` | `true` serves `/docs`, `/redoc` and `/openapi.json` without authentication; `false` removes `/docs` and `/redoc` and authenticates `/openapi.json`. |
| `[server]` | `graceful_shutdown_seconds` | int | `25` | Must be `>= 1`; keep below the 30 s of `docker stop`. |
| `[ingest]` | `max_file_mb` | int | `100` | Was `0` (no limit); `0` still disables it; applies to files and to one inline document. |
| `[ingest]` | `allowed_roots` | list[string] | `[]` | Absolute directories a client-supplied path must lie under. Empty adds no scope limit, and a non-admin key may then not send paths at all. |
| `[ingest]` | `parse_timeout_seconds` | float | `300` | Must be `> 0`; bound on one PDF or image conversion. |
| `[ingest]` | `parse_timeout_seconds_per_mb` | float | `10` | Must be `>= 0`; added per MiB of the file (`0` keeps the bound flat). |
| `[ingest]` | `max_zip_entries` | int | `10000` | Must be `>= 1`; `.docx`, `.pptx`, `.xlsx`, `.epub`. |
| `[ingest]` | `max_zip_uncompressed_mb` | int | `512` | Must be `>= 1`. |
| `[ingest]` | `max_discovered_files` | int | `100000` | Must be `>= 1`; a larger directory tree is refused. |
| `[ingest]` | `max_concurrent_tasks` | int | `4` | Must be `>= 1`; more is `503` `ingest_busy`. |
| `[graph]` | `max_cross_collection_collections` | int | `8` | Must be `>= 2`. |
| `[maintenance]` | `version_retention_hours` | int | `24` | Must be `>= 1`; passes need `interval_hours > 0` or `POST /maintenance/trigger`. |

`[database].embedder_cache_size` (default `3`) no longer counts the default `embedding_model`, which is pinned. The keys and tokens that authenticate are not config values only: `ARCHON_SEARCH_API_KEY` and the key file must be lowercase hex of at least 32 characters, and a `[namespaces]` token printable ASCII of at least 32 characters, or the server refuses to start (`Error: …`, exit `1`).

## PyTorch runtime keys removed and added

| Section | Key | Change |
| --- | --- | --- |
| `[database]` | `providers`, `reranker_providers` | Removed; replaced by `[database].device` (default `"auto"`). |
| `[graph]` | `providers` | Removed; replaced by `[graph].device` (default `"cpu"`). |

`serve`, `start` and `install` exit `1` with `Error: [database].providers was replaced by [database].device (auto|cpu|mps|cuda); see BREAKING.md` (likewise for the other two). `archon-search wizard` loads such a file, prints `Dropping removed config keys …`, drops them and writes `device`. `archon-search config show` prints the file and warns on stderr; `config set` validates with the loader's rules, so it refuses to set a removed key but `config set database.device auto` works on a legacy file. The environment variable `FASTEMBED_CACHE_PATH` is no longer read. The tested manual sequence is in the PyTorch entry of [`/BREAKING.md`](../../BREAKING.md).

Cross-link to [`Architecture/510_release_and_environment_strategy.md`](../Architecture/510_release_and_environment_strategy.md) "Configuration" for the same surface from the release-engineering angle, and to [`Architecture/130_data_architecture_and_persistence.md`](../Architecture/130_data_architecture_and_persistence.md) for how `db_path` is laid out on disk.

## Quirks and known mismatches

### `[telemetry].export_enabled` — silent-coerce (TEL-1)

This is the **only** key whose behavior diverges from its surrounding documentation.

- **Schema**: `TelemetryConfig.export_enabled: bool = False`.
- **Loader behavior**: when set to `true`, `load_config` logs a warning (`"telemetry: export_enabled is reserved for a future release and will be ignored"`) and stores `False`. When set to `false`, it stores `False`. See the `export_enabled` branch in `archon_search/config.py` (`load_config`, `[telemetry]` section).
- **Example file is consistent with the loader**: the comment in [`archon-search.toml.example`](../../archon-search.toml.example) correctly describes the silent-coerce behavior ("the config loader logs a warning and silently coerces this to false. No external transmission occurs. Tracked as TEL-1…"). There is no mismatch between the example file and the loader.
- **CLAUDE.md / ADR-05** are reported to describe a stricter contract ("rejected at config load (no external transmission in v1)") that does not match the implementation. #Unverified — not re-checked against those source files in this revision; verify before relying on this claim.

This is tracked as **TEL-1** in [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md). Until it is resolved, treat `export_enabled` as effectively immutable at `false` — there is no v1 export pipeline, and setting the key has no observable effect beyond a log line at startup.

**Migration guidance**: do **not** rely on a `ConfigError` to catch a misconfigured `export_enabled`. If you need to assert this in a deploy check, grep the startup logs for the warning line, or assert that `/telemetry/stats` does not surface any external export activity (it cannot, since no exporter exists in v1).

### Unknown keys do not raise

`load_config` ignores keys it does not recognise. This means:

- Renaming a key in a future release will silently fall back to the default for callers who do not update their TOML.
- Typos (e.g. `to_k_return` instead of `top_k_return`) are not caught by config loading; the symptom is a value that quietly stays at default.

Until a strict-mode loader exists, treat each upgrade as an opportunity to diff your live TOML against the bundled example.

## Validating a config file

There is no standalone `config validate` subcommand. The supported ways to exercise the loader are:

```bash
# 1. Show the file with defaults filled in (a raw TOML view; it does not run the server's loader).
archon-search config show

# 2. Start the server; ConfigError causes start to fail loudly (`Error: …`, exit 1).
archon-search start
```

`archon-search config show` prints the config with defaults filled in (so a value the loader would reject can still print) and warns on stderr when removed keys remain; `config set` refuses a document the loader would reject. The authoritative check is starting the server (check 2). Read individual keys with `archon-search config get <key>`; write them with `archon-search config set <key> <value>` (see `archon_search/cli/config_cmd.py`).

## Upgrade checklist for config

1. Diff your live `<data-dir>/archon-search.toml` (or the file `ARCHON_SEARCH_CONFIG` names) against [`archon-search.toml.example`](../../archon-search.toml.example) for the version you are about to install.
2. For any key listed in `[next release]` of [`/BREAKING.md`](../../BREAKING.md) under a config surface, apply the migration step before restarting. Unreleased entries are headed `### [next release] — <title> (<date>)`.
3. Run `archon-search config show` against the new wheel and confirm:
   - No `ConfigError` is raised.
   - No "export_enabled is reserved" warning appears unless you explicitly opted into that key.
4. Restart the server (see [`02_upgrade_procedure.md`](./02_upgrade_procedure.md)).

## Related documents

- [`02_upgrade_procedure.md`](./02_upgrade_procedure.md) — where config validation fits in the upgrade flow.
- [`05_data_migration.md`](./05_data_migration.md) — `db_path` and on-disk layout.
- [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — TEL-1, the `export_enabled` mismatch.
- [`Architecture/510_release_and_environment_strategy.md`](../Architecture/510_release_and_environment_strategy.md) — release-engineering view of the config surface.
- [`Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — telemetry privacy invariants.
