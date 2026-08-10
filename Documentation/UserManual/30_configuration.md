**Purpose**: Configure `archon-search` via `archon-search.toml` and environment variables.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Configuration

## Principles

1. **One file, one process.** All server config lives in a single TOML file. The default is `~/.archon-search/archon-search.toml`; the `ARCHON_SEARCH_CONFIG` environment variable can redirect to any other path.
2. **Defaults are safe.** Every key has a default in `archon_search/config.py:SearchConfig`; a missing file or missing section uses defaults rather than failing.
3. **The auth key is a separate concern.** API keys live in `~/.archon-search/.search.env` (mode `600`), not in the TOML — see [Authentication](#authentication).
4. **Validation is strict where it matters.** Numeric ranges (port, thresholds, retention) raise `ConfigError` on bad values, and malformed TOML fails loudly at load time. String fields are coerced permissively via `str()`, so e.g. `host = 123` becomes `"123"` rather than raising.

The fully annotated reference is `archon-search.toml.example` at the repo root — every key, default, and validation rule is commented there. This page is the task-oriented summary.

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

`config show` prints the *effective* config: the file's contents plus every `[server]`, `[database]`, `[routing]`, `[collections]`, and `[logging]` key the file omits, filled in with its `SearchConfig` default. The install wizard deliberately writes only the choices you actually made — a default it never asked about (a `--non-interactive` run without the matching flag) is left out — so the file on disk is normally shorter than what `config show` prints. Values present in the file are echoed verbatim, and sections the defaults do not cover (`[hyde]`, `[telemetry]`, …) are passed through unchanged.

`config set` coerces the value in this order: bool (case-insensitive `"true"`/`"false"`) → int → float → string. Keys must be in `section.field` form. `config set` writes to the TOML file **without** validating against `SearchConfig` — unknown sections or fields silently succeed and are only caught on the next `load_config` call.

## Sections

Each section below maps 1:1 to a dataclass on `SearchConfig`. Tables list the notable keys; open `archon-search.toml.example` for the exhaustive list plus validation bounds.

### `[server]`

Bind address and port for the HTTP API (MCP mounts on the same port).

| Key | Default | Meaning |
| --- | --- | --- |
| `host` | `127.0.0.1` | Bind address. `archon-search serve` flips the default to `0.0.0.0`. Override at runtime with `ARCHON_SEARCH_HOST`. |
| `port` | `8765` | Must be in `[1, 65535]`. Override at runtime with `ARCHON_SEARCH_PORT`. There is **no `--port` CLI flag** at runtime. |

### `[database]`

Vector store location, models, chunking, and provider/GPU wiring.

| Key | Default | Meaning |
| --- | --- | --- |
| `db_path` | `~/.archon-search/search` | LanceDB on-disk location. Tilde is expanded. |
| `embedding_model` | `BAAI/bge-small-en-v1.5` | Dense-embedding model id. |
| `reranker_model` | `Xenova/ms-marco-MiniLM-L-6-v2` | Cross-encoder for second-stage scoring. |
| `chunk_size` | `512` | Target chunk size in tokens (>0). |
| `auto_reindex_on_chunk_size_change` | `true` | Reindex affected collections on startup when `chunk_size` changes. |
| `providers` | `[]` | ONNX Runtime execution providers (`[]` = CPU; `["CUDAExecutionProvider"]`; `["CoreMLExecutionProvider"]`). |
| `reranker_providers` | absent | When present, overrides `providers` for the reranker only (`[]` forces CPU). Written by the wizard in split-provider mode. |
| `top_k_retrieve` | `15` | First-stage candidate pool size (>0). |
| `top_k_return` | `5` | Results returned per query (>0). |
| `validation_timeout_seconds` | `60` | Timeout for the background model-validation probe; values ≤0 warn and fall back to 60. |
| `centroid_recompute_threshold` | `10000` | Full centroid recompute after this many mutations (≥1). |
| `profile` | `""` | Install profile (`minimal`/`balanced`/`max`), set by `install --profile`. |
| `multilingual` | `false` | Per-document language detection (requires `archon-search[multilingual]` + `lid.176.ftz`). |
| `language_detection_confidence_threshold` | `0.7` | Minimum fasttext confidence in `(0.0, 1.0]`; below it → `language="unknown"`. |
| `embedder_cache_size` | `3` | LRU cache of embedder instances (≥1). |
| `eager_load_embedders` | `false` | Pre-warm all embedding models at startup to remove first-query latency. |

### `[search]`

Multi-collection fan-out execution bounds and the top-k ceiling.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_fanout` | `8` | Max collections accepted in one multi-collection request (≥1). |
| `fanout_leg_trim` | `40` | Per-leg candidate pool kept by local RRF score before the global merge (≥1). |
| `fanout_timeout_seconds` | `30.0` | Whole-fan-out timeout; exceeding it returns HTTP 504 (>0). |
| `top_k_max` | `100` | Operator ceiling on `top_k` accepted by `POST /search` and `POST /explain` (>0). |

### `[routing]`

How the centroid router selects which collections to search.

| Key | Default | Meaning |
| --- | --- | --- |
| `routing_shortlist_size` | `8` | Collections considered before parallel search (>0). |
| `routing_confidence_threshold` | `0.30` | Minimum centroid confidence to dispatch to a collection, in `[0.0, 1.0]`. |
| `routing_strategy` | `centroid` | `centroid` (pure vector) or `hybrid` (blends description-embedding cosine). |
| `routing_description_weight` | `0.3` | Weight for description cosine in hybrid mode, in `[0.0, 1.0]`; ignored when `centroid`. |

Routing tuning is covered in more depth in [`60_searching.md`](./60_searching.md).

### `[collections]`

Static collection definitions and the watcher.

| Key | Default | Meaning |
| --- | --- | --- |
| `pinned_collections` | `[]` | Paths always included in every search, regardless of routing. |
| `collections` | `[]` | Static collection paths (managed via `collection add/remove` or directly). |
| `watch` | `false` | Watch source directories (watchdog) and reindex on change. |

### `[jobs]`

Concurrency and checkpointing for bulk export/import/migration jobs.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_concurrent_bulk` | `1` | Max concurrent export/import/migration jobs (>0). Ingest/reindex/delete dispatch immediately regardless. |
| `checkpoint_interval` | `100` | Documents written between progress checkpoints (>0). |

### `[logging]`

| Key | Default | Meaning |
| --- | --- | --- |
| `level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (case-insensitive). |
| `log_file` | `~/.archon-search/logs/archon-search.log` | Rotating log path. Set to `""` to disable file logging (recommended for containers / multi-worker). |
| `format` | `text` | `text` or `json` (structured, for Loki/Datadog/ELK). |
| `backup_count` | `7` | Rotated files to keep (≥0; `0` = never delete). |

File logging uses a `TimedRotatingFileHandler` (UTC-midnight rotation) which is **not** multi-process safe — set `log_file = ""` behind a multi-worker process manager. When `log_file` is non-empty, output goes only to the file (stderr propagation is disabled). Details and log shipping live in [`../OperatorGuide/30_logging.md`](../OperatorGuide/30_logging.md).

### `[observability]`

Correlation-ID propagation and per-stage latency recording.

| Key | Default | Meaning |
| --- | --- | --- |
| `stage_timings_enabled` | `true` | Record per-stage wall times; surfaced as `stage_timings_ms` in logs and in `POST /explain`. Set `false` to suppress. |
| `request_id_header` | `X-Request-ID` | Header carrying the correlation ID (read inbound, echoed on every response). Must be non-empty. |

### `[mcp]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Mount the MCP endpoint at `/mcp` on the REST port. When `false`, no mount is created and `GET /status`/`GET /health` report `mcp: null`. |

### `[auth]`

| Key | Default | Meaning |
| --- | --- | --- |
| `rotate_grace_seconds` | `0` | Grace window (seconds) after a key rotation before the old key is rejected; `0` = immediate revocation. The `POST /keys/rotate` body's `grace_seconds` overrides this per call. |

Full key lifecycle and rotation are in [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md).

### `[ingest]`

| Key | Default | Meaning |
| --- | --- | --- |
| `max_file_mb` | `0` | Per-file size guard. `0` = unlimited. Positive values reject any file whose size **strictly exceeds** the limit (a file exactly equal is accepted); directory ingest skips oversized files per-file and continues. Negative raises `ConfigError`. |

### `[hyde]` and `[rag_fusion]`

Optional LLM-backed recall boosters, both **disabled by default**. They share the same provider/model/rate-limit shape; RAG Fusion adds `num_queries`. Enable a request-side booster via the `hyde` / `rag_fusion` flags on `POST /search`.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch. When off, the matching request flag is silently ignored (`hyde_applied`/`rag_fusion_applied: false`). |
| `provider` | `anthropic` | One of `anthropic`, `ollama`, `openai`, `claude_cli`, `llama_cpp`. Use `ollama` or `llama_cpp` for air-gapped deployments (query text stays local). |
| `model` | `claude-haiku-4-5-20251001` | Generation model. Required for `ollama`/`openai`/`llama_cpp`; a model tag/id for those providers. |
| `ollama_base_url` | `http://localhost:11434` | Ollama server URL; used only when `provider = "ollama"`. |
| `llama_cpp_base_url` | `http://localhost:8080` | llama-server URL; used only when `provider = "llama_cpp"`. Use a small, direct-response instruct model — a reasoning model burns the whole `max_tokens` budget on hidden chain-of-thought and leaves `hyde_applied`/`rag_fusion_applied` silently `false`. |
| `timeout_seconds` | `10.0` | Per-request LLM timeout; on timeout the server falls back silently to the plain query (>0). |
| `max_requests_per_minute` | `60` | Per-process rate limit (≥1); **not** enforced for `ollama`/`claude_cli`/`llama_cpp`. |
| `num_queries` *(rag_fusion only)* | `2` | LLM-generated query variants, range `1–5`. Total searches = `num_queries + 1`. |

> **Privacy warning:** with `provider = "anthropic"`, `"openai"`, or `"claude_cli"`, enabling these features sends raw query text to the provider on every boosted request. `provider = "ollama"` or `"llama_cpp"` keeps query text on the local host (zero-transmission). Enabling either feature causes the wizard to create `~/.archon-search/.secrets.env` (mode 0600) for provider API keys — not created for `ollama`/`claude_cli`/`llama_cpp`, which need none. See [`60_searching.md`](./60_searching.md) for worked examples and the residency caveats.

### `[maintenance]`

In-process maintenance passes (FTS optimize, orphan cleanup, failed-ingest retry, expired-chunk pruning, graph GC). **Disabled by default** (`interval_hours = 0`), but `POST /maintenance/trigger` always works.

| Key | Default | Meaning |
| --- | --- | --- |
| `interval_hours` | `0` | Hours between scheduled passes; `0` = no scheduled pass (≥0). |
| `fts_optimize` | `true` | Optimize the FTS index each pass. |
| `orphan_cleanup` | `true` | Remove chunks whose `source_path` no longer exists (URL-sourced chunks skipped). |
| `failed_ingest_retry` | `true` | Re-enqueue failed `IngestJob`s within the retry limits. |
| `retry_max_attempts` | `3` | Max retry attempts per file per collection (≥1). |
| `retry_max_age_hours` | `72` | Only retry failures newer than this (≥0; `0` warns — immediate churn). |
| `prune_expired_chunks` | `true` | Prune chunks whose `expires_at < now`. |
| `graph_gc` | `true` | Garbage-collect orphaned graph nodes/edges after document deletion. |
| `exclude` | `[]` | Collections to skip (bare name = all namespaces; `{ns}/{col}` = one namespace). |

Operating maintenance and jobs is covered in [`../OperatorGuide/50_maintenance_and_jobs.md`](../OperatorGuide/50_maintenance_and_jobs.md).

### `[backup]`

Scheduled per-collection backup via the in-process `BackupLoop`. **Disabled by default.**

| Key | Default | Meaning |
| --- | --- | --- |
| `interval_hours` | `0` | Hours between backup ticks; `0` (or negative) disables scheduled backups (≥0). |
| `keep` | `7` | Archives retained per collection; `0` = never rotate (warns when paired with `interval_hours > 0`). |
| `exclude` | `[]` | Collections to skip (same matching as `[maintenance].exclude`). |
| `output_dir` | `""` | Archive root; empty resolves to `<data-dir>/backups`. Paths with fewer than 3 components fall back to the default and log an ERROR. |

Backup and restore procedures live in [`../OperatorGuide/40_backup_restore_disaster_recovery.md`](../OperatorGuide/40_backup_restore_disaster_recovery.md).

### `[graph]`

GraphRAG entity extraction, communities, synonyms, and PageRank. **Disabled by default**; requires the `archon-search[graph]` extra. Enabling and **re-ingesting** is required to populate graph tables — existing chunks are not auto-extracted.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch for the graph subsystem (entity extraction, PPR, communities, community/graph routes). `true` with `spacy` absent raises `ConfigError` at startup. LLM-backed enrichment (below) additionally needs `provider` set — both gates must be open (`enabled=true` AND `provider` set) for community summaries/typed relationship labels to be produced. |
| `provider` | `null` | Enrichment provider for community summaries and typed relationship labels: one of `anthropic`, `openai`, `ollama`, `llama_cpp`. `null` (default) disables enrichment entirely — no LLM call, preserving the air-gap guarantee; unlike `[hyde]`/`[rag_fusion]`, this field IS the enrichment enable gate (there is no separate `[graph].enrichment_enabled`). `claude_cli` is a valid provider name elsewhere but has no v1 enrichment client (deferred; the factory logs a WARNING and skips enrichment). |
| `extraction_model` | `null` | Bare model name for the configured `provider` (e.g. `"claude-haiku-4-5-20251001"`, an Ollama tag, or a llama-server `/v1/models` id) — never a `"provider:model"` string. Logs a WARNING once at build time if `provider` is set but this is empty. |
| `llama_cpp_base_url` | `http://localhost:8080` | llama-server URL for graph enrichment; used only when `provider = "llama_cpp"`. |
| `ollama_base_url` | `http://localhost:11434` | Ollama server URL for graph enrichment; used only when `provider = "ollama"`. |
| `extraction_timeout_seconds` | `30.0` | Per-request timeout (seconds) for enrichment LLM calls. |
| `extraction_rate_limit_rpm` | `60` | Per-minute rate limit for enrichment LLM calls; ignored by `llama_cpp` (local inference, unthrottled). |
| `extraction_token_budget` | `1024` | Max output tokens requested per enrichment LLM call. |
| `backend_threshold_edges` | `10000` | Edge count above which in-memory traversal becomes latency-noticeable; crossing it logs a WARNING (≥1). |
| `leiden_resolution` | `1.0` | Leiden resolution; higher → more, smaller communities (>0). |
| `max_community_size` | `10` | Max entities per community before splitting (≥1). |
| `community_summary_chunks` | `3` | MMR-selected representative chunks per community (≥1). |
| `max_global_candidates` | `100` | Cap on community chunks fed to the reranker in `global` mode (≥1). |
| `max_inspection_nodes` / `max_inspection_edges` | `5000` / `25000` | Ceilings on nodes/edges returned by `GET /graph/{collection}` (≥1). |
| `gc_rebuild_communities` | `true` | Rebuild communities after graph GC removes nodes. |
| `gc_rebuild_cpu_priority` | `low` | Rebuild-thread CPU priority: `low`/`normal`/`high` (per-thread effect is Linux-only). |
| `synonym_threshold` | `0.85` | Cosine threshold for automatic synonym edges, in `(0.0, 1.0]`. |
| `alias_file` | `null` | TOML file of manual aliases (`"K8s" = "Kubernetes"`); missing/unreadable → WARNING, treated as unset. |
| `enrichment_auto` | `true` | Run synonym enrichment automatically after each ingest. |
| `ppr_damping` | `0.85` | Personalised PageRank damping, strictly in `(0.0, 1.0)`. |
| `ppr_top_entities` | `20` | Top-ranked entities returned by PPR before chunk lookup (≥1). |
| `naive_max_expansion_terms` | `20` | Cap on terms added by naive graph-mode query expansion (≥1). |

Graph search modes (`naive`/`local`/`global`/`ppr`) are documented in [`65_graph_search.md`](./65_graph_search.md); operator-side graph management in [`../OperatorGuide/60_graph_operations.md`](../OperatorGuide/60_graph_operations.md).

### `[openai_shim]`

OpenAI-compatible API shim (G9). **Disabled by default** — when off, no `/v1` routes are registered.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch for `POST /v1/chat/completions` and `GET /v1/models`. |
| `inject_citations` | `true` | Append `[Source: {source_path}]` per chunk in the assistant reply. |
| `top_k` | `5` | Accepted and validated but **inert** — not forwarded to the pipeline (reserved for a future runtime `top_k`). |

See [`85_openai_compatible_api.md`](./85_openai_compatible_api.md) for usage.

### `[telemetry]`

Opt-in local query telemetry. **Disabled by default.** Raw query text is never written to disk (structural guarantee).

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch. |
| `retention_days` | `30` | Files older than this are pruned at startup and every 24h (≥1). |
| `log_dir` | `~/.archon-search/search-logs` | Output directory (non-empty). |
| `hash_doc_ids` | `false` | HMAC-SHA256 `result_doc_ids` before write, severing the mapping to filesystem paths in shared logs. |
| `export_enabled` | `false` | Reserved. **Setting `true` is silently coerced to `false` with a warning** — remote export is not implemented in v1. |

Full telemetry surface: [`120_telemetry.md`](./120_telemetry.md) and [`../SecurityGuide/04_telemetry_privacy.md`](../SecurityGuide/04_telemetry_privacy.md).

### `[namespaces]`

Optional `string = string` mapping of raw token → namespace. Entries must be string key / string value pairs or `ConfigError` is raised. See [`150_multi_instance_setup.md`](./150_multi_instance_setup.md) and [`../SecurityGuide/03_authorization_and_acl.md`](../SecurityGuide/03_authorization_and_acl.md) for namespace semantics.

## Authentication

The API key is **not** stored in the TOML. The key manager (`archon_search/key_manager.py:load_or_generate_key`) resolves in this order:

1. **`ARCHON_SEARCH_API_KEY`** (highest priority) — a non-empty lowercase hex string; invalid values are logged and ignored, falling through to the next step.
2. **`ARCHON_SEARCH_KEY_FILE`** if set, otherwise `<data-dir>/.search.env` (`~/.archon-search/.search.env` by default). The first `ARCHON_SEARCH_API_KEY=` line is used; the loader forces the file mode to `600`.
3. **Auto-generation** — on first start with no env var and no file, a 64-char hex token is generated, written atomically (mode `600`), and used.

`archon-search key rotate` (or `POST /keys/rotate`) rotates the default key live without a restart; it returns `409` when `ARCHON_SEARCH_API_KEY` is set. Full key management, rotation, and multi-key workflows are in [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md) and [`../OperatorGuide/70_key_management_and_rotation.md`](../OperatorGuide/70_key_management_and_rotation.md).

## Environment variables

Read at config load, key resolution, and by every lazy path accessor. All overrides take effect on the next `load_config` call.

| Variable | Effect | Default |
| --- | --- | --- |
| `ARCHON_SEARCH_CONFIG` | Path to `archon-search.toml`. | `~/.archon-search/archon-search.toml` |
| `ARCHON_SEARCH_API_KEY` | Bearer token; highest priority, bypasses the key file. | unset → key file or auto-generated |
| `ARCHON_SEARCH_KEY_FILE` | Path to the key file (absolute). Takes precedence over `ARCHON_SEARCH_DATA_DIR` for the key file. | unset → `<data-dir>/.search.env` |
| `ARCHON_SEARCH_HOST` | Bind-address override (env > TOML > default). Empty = not set. | unset → `[server].host` |
| `ARCHON_SEARCH_PORT` | Bind-port override; int `1–65535` or raises `ConfigError`. Empty = not set. | unset → `[server].port` |
| `ARCHON_SEARCH_DATA_DIR` | Relocate the whole runtime tree (DB, logs, telemetry, key file, jobs file, models). Must be absolute; empty raises `ConfigError`. Docker sets `/data`. | unset → `~/.archon-search/` |
| `ARCHON_SEARCH_CONTAINER` | `"1"` attaches a stderr handler so `docker logs` captures output. Docker sets `1`. | unset → no stderr handler |
| `FASTEMBED_CACHE_PATH` | fastembed's own weight-cache directory. Docker sets `/data/fastembed-cache`. | fastembed default (`~/.cache/fastembed`) |

`ARCHON_SEARCH_CONFIG` and `ARCHON_SEARCH_DATA_DIR` are **independent**: the TOML config file is not relocated by `ARCHON_SEARCH_DATA_DIR`. To also move the TOML onto a mounted volume, set `ARCHON_SEARCH_CONFIG` explicitly. `archon-search serve` warns at startup if `ARCHON_SEARCH_DATA_DIR` is set without `ARCHON_SEARCH_CONFIG`.

## Related documents

- [`00_index.md`](./00_index.md) — UserManual table of contents.
- [`20_wizard.md`](./20_wizard.md) — the interactive setup wizard that writes this file.
- [`40_running_the_server.md`](./40_running_the_server.md) — applying config changes.
- [`60_searching.md`](./60_searching.md) — HyDE, RAG Fusion, and routing tuning.
- [`65_graph_search.md`](./65_graph_search.md) — graph search modes and `[graph]` tuning.
- [`120_telemetry.md`](./120_telemetry.md) — telemetry in detail.
- [`140_running_with_docker.md`](./140_running_with_docker.md) — Docker env-var matrix and persistence layout.
- [`160_troubleshooting.md`](./160_troubleshooting.md) — config load errors.
- [`../OperatorGuide/50_maintenance_and_jobs.md`](../OperatorGuide/50_maintenance_and_jobs.md) — running maintenance passes.
- [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md) — key lifecycle and rotation.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — REST + MCP + CLI reference (`GET /openapi.json` is authoritative for HTTP).
