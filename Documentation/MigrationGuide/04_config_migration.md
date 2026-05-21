**Purpose**: Document how `~/.archon-search/archon-search.toml` evolves across releases — which keys are stable, which have quirks, and how to validate a config file.
**Audience**: Operators editing config across upgrades; maintainers changing the config schema in `archon_search/config.py`.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Configuration Migration

`~/.archon-search/archon-search.toml` is the single config file consumed by the server. It is loaded by `archon_search/config.py` `load_config()` and validated against the `SearchConfig` dataclass. Defaults are baked into the dataclass; an absent file is equivalent to an all-defaults config (`load_config` returns the default `SearchConfig`).

## Principles

1. **All keys are optional.** A missing key falls back to the default in `SearchConfig` / `TelemetryConfig`. An entirely missing file is valid.
2. **Unknown keys are silently ignored.** `load_config` reads each section with `doc.get(section, {})` and only consumes known keys. There is no "strict mode" today — typos do not raise.
3. **Type errors raise `ConfigError`.** Wrong type, out-of-range port, negative `chunk_size`, etc. fail loudly at load time. See `_coerce_int` / `_coerce_float` / `_coerce_bool` in `config.py`.
4. **Range validation is enforced where it exists.** `port` must be `1..65535`, `routing_confidence_threshold` must be in `[0.0, 1.0]`, several integer fields must be `> 0`.

## Stable keys

The following sections and keys are part of the documented config surface and have been stable since v1. The reference example file is [`archon-search.toml.example`](../../archon-search.toml.example) at the repo root; defaults below mirror that file and `SearchConfig` / `TelemetryConfig`.

| Section | Key | Type | Default | Notes |
| --- | --- | --- | --- | --- |
| `[server]` | `host` | string | `"127.0.0.1"` | Bind address. |
| `[server]` | `port` | int | `8765` | Validated `1..65535`. |
| `[database]` | `db_path` | string | `"~/.archon-search/search"` | LanceDB root. |
| `[database]` | `embedding_model` | string | `"BAAI/bge-small-en-v1.5"` | fastembed model id. |
| `[database]` | `reranker_model` | string | `"cross-encoder/ms-marco-MiniLM-L-6-v2"` | Cross-encoder id. |
| `[database]` | `chunk_size` | int | `512` | Must be `> 0`. |
| `[database]` | `auto_reindex_on_chunk_size_change` | bool | `true` | Reindex on chunk-size diff at start. |
| `[database]` | `providers` | list[string] | `[]` | ONNX execution providers. |
| `[database]` | `top_k_retrieve` | int | `15` | Pipeline shortlist size, must be `> 0`. |
| `[database]` | `top_k_return` | int | `5` | Final returned cut-off, must be `> 0`. |
| `[routing]` | `routing_shortlist_size` | int | `8` | Must be `> 0`. |
| `[routing]` | `routing_confidence_threshold` | float | `0.30` | In `[0.0, 1.0]`. |
| `[routing]` | `max_parallel_collections` | int | `3` | Must be `> 0`. |
| `[collections]` | `pinned_collections` | list[string] | `[]` | Always searched, regardless of routing. |
| `[collections]` | `collections` | list | `[]` | Static collection list; empty means manage over HTTP. |
| `[collections]` | `watch` | bool | `false` | Enable filesystem watcher. |
| `[logging]` | `level` | string | `"INFO"` | Python logging level. |
| `[logging]` | `log_file` | string | `"~/.archon-search/logs/archon-search.log"` | Log destination. |
| `[telemetry]` | `enabled` | bool | `false` | Opt-in switch. |
| `[telemetry]` | `retention_days` | int | `30` | Must be `>= 1`. |
| `[telemetry]` | `log_dir` | string | `"~/.archon-search/search-logs"` | Non-empty. |
| `[namespaces]` | `<key> = <value>` | string = string | `{}` | Both sides must be strings; otherwise `ConfigError`. Note: `load_config` only checks the string-type constraint — the namespace-identifier regex (`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`) and reserved-name rules in `constants.py::_validate_namespace` are enforced by callers that use the namespace, not at config load. |

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

There is no standalone `config validate` subcommand. The two supported ways to exercise the loader are:

```bash
# 1. The CLI shows the loaded, merged config — runs the same load_config() the server uses.
archon-search config show

# 2. Start the server; ConfigError causes start to fail loudly.
archon-search start
```

`archon-search config show` is the fastest non-destructive check: it prints the parsed config and surfaces any range or type errors via `ConfigError`. Read individual keys with `archon-search config get <key>`; write them with `archon-search config set <key> <value>` (see `archon_search/cli/config_cmd.py`).

## Upgrade checklist for config

1. Diff your live `~/.archon-search/archon-search.toml` against [`archon-search.toml.example`](../../archon-search.toml.example) for the version you are about to install.
2. For any key listed in `[next release]` of [`/BREAKING.md`](../../BREAKING.md) under a config surface, apply the migration step before restarting. #Unverified — the exact `[next release]` section heading in `BREAKING.md` has not been confirmed; consult the file's current structure when migrating.
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
