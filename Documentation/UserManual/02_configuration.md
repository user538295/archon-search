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
| `host` | string | `127.0.0.1` | Bind address. Set to `0.0.0.0` only if you intend to expose the server. |
| `port` | int | `8765` | Must be in `[1, 65535]`; out-of-range values raise `ConfigError`. |

### `[database]`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `db_path` | string | `~/.archon-search/search` | LanceDB on-disk location. Tilde is expanded. |
| `embedding_model` | string | `BAAI/bge-small-en-v1.5` | Sentence-transformers model id. |
| `reranker_model` | string | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder used for second-stage scoring. |
| `chunk_size` | int (>0) | `512` | Target chunk size in tokens. |
| `auto_reindex_on_chunk_size_change` | bool | `true` | If `chunk_size` changes between starts, affected collections are reindexed automatically. #Unverified (reindex behaviour lives outside `config.py`). |
| `providers` | list[string] | `[]` | ONNX Runtime execution providers. See [`01_installation.md`](./01_installation.md). |
| `top_k_retrieve` | int (>0) | `15` | First-stage candidate pool size. |
| `top_k_return` | int (>0) | `5` | Number of results returned by `/search` (per-request `top_k` is ignored — see `BREAKING.md`). |

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

| Key | Type | Default |
| --- | --- | --- |
| `level` | string | `INFO` |
| `log_file` | string | `~/.archon-search/logs/archon-search.log` |

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

## Authentication

The API key is **not** stored in the TOML. The key manager (`archon_search/key_manager.py:load_or_generate_key`) resolves in this order:

1. **`ARCHON_SEARCH_API_KEY` environment variable** (highest priority). Must be a non-empty lowercase hex string (any length — no length constraint is enforced for env-var-supplied keys); invalid values are logged and ignored, falling through to the file/auto-generate steps.
2. **`ARCHON_SEARCH_KEY_FILE`** if set, otherwise `~/.archon-search/.search.env`. The loader scans the file line by line and uses the first line starting with `ARCHON_SEARCH_API_KEY=` (trailing whitespace stripped); additional lines are ignored. If the file's permissions are not exactly `600`, the loader forces them to `600` (this can both tighten *and* loosen the mode — e.g. `400` would be widened to `600`).
3. **Auto-generation**. On first start with no env var and no file, a 64-char hex token (`secrets.token_hex(32)`) is generated, written atomically with mode `600`, and used.

To rotate, delete `~/.archon-search/.search.env` and restart the server. To use a static key (Docker, CI), set `ARCHON_SEARCH_API_KEY` and skip the file entirely.

## Related documents

- [`03_running_the_server.md`](./03_running_the_server.md) — applying config changes.
- [`06_telemetry.md`](./06_telemetry.md) — telemetry section in detail.
- [`07_troubleshooting.md`](./07_troubleshooting.md) — config load errors.
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — auth and namespace model.
