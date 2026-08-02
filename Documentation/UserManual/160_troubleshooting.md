**Purpose**: Diagnose common runtime failures.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Troubleshooting

## Principles

1. **Start at `/health` and `/ready`, then `/status`.** They are the cheapest probes. `GET /health` tells you the process is alive; `GET /ready` tells you whether storage (and, informationally, the models) are ready; `GET /status` tells you what the indexer, jobs, and optional features are doing. All three are auth-exempt for `/health` and `/ready`; `/status` needs a Bearer token.
2. **Logs first, code second.** `~/.archon-search/logs/archon-search.log` captures most failures; check it before reading the source.
3. **Config errors are surfaced loudly.** `ConfigError` from `archon_search/config.py` (and the startup dependency checks in `server/app.py`) will refuse to start the server — read the message; it names the offending key or missing package.
4. **Most empty-result issues are configuration, not retrieval.** Check the collection exists, has been indexed, and that routing is letting it through before suspecting the model.

## Where to look

| Location | Contents |
| --- | --- |
| `~/.archon-search/logs/archon-search.log` | Server log (level from `[logging].level`). |
| `~/.archon-search/.search.env` | API key (mode `600`). |
| `~/.archon-search/.secrets.env` | HyDE / RAG-Fusion provider keys (mode `600`; managed service only). |
| `~/.archon-search/search/` | LanceDB data. Deleting forces a full rebuild on next sync. |
| `~/.archon-search/search-logs/` | Telemetry JSONL — only present when telemetry is enabled. |
| `~/.archon-search/archon-search.toml` | Config. Default location; overridable via `ARCHON_SEARCH_CONFIG`. |

The whole tree relocates with `ARCHON_SEARCH_DATA_DIR` — substitute that root if you set it. See [`30_configuration.md`](./30_configuration.md).

## Symptom: server will not start

Symptoms: `archon-search start` prints `Error: …` and exits 1.

1. Run `archon-search config show` — does the printed TOML look correct?
2. Force the loader to use the default config path (`~/.archon-search/archon-search.toml`) by clearing `ARCHON_SEARCH_CONFIG` for one invocation. An empty value is treated as unset, so this loads the default file rather than skipping config:
   ```bash
   ARCHON_SEARCH_CONFIG= archon-search start
   ```
3. Look for `ConfigError: …` lines in the log — the message names the bad key (e.g. `port must be between 1 and 65535`, `routing_confidence_threshold must be in [0.0, 1.0]`).

## Symptom: 401 on every request

The client is not presenting a valid Bearer token, or it is presenting one the server does not recognise.

1. Confirm the key the server is using:
   - `ARCHON_SEARCH_API_KEY` env var if set (takes priority; the env var itself holds the key value).
   - Otherwise the file at the path named by `ARCHON_SEARCH_KEY_FILE` (the env var holds a **path**, not the key), or the default `~/.archon-search/.search.env`.
2. The file format is `ARCHON_SEARCH_API_KEY=<hex>` on a single line; the value must be lowercase hex. An **invalid file** is logged at ERROR and then **overwritten with a freshly generated key** — invalid file content silently rotates the key.
3. **To rotate the default key (no restart required)**: `archon-search key rotate` (add `--grace <duration>` to let in-flight requests drain). Returns `409` if `ARCHON_SEARCH_API_KEY` is set — unset it first. **Legacy path**: delete `.search.env` and restart.

See [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md) for key precedence and rotation detail.

## Symptom: empty results from `/search`

In order of likelihood:

1. **Wrong collection name.** Run `archon-search collection list` and compare to the `collection` field in your request. Names are derived from paths, not the path itself.
2. **Collection not yet indexed.** Hit `GET /status` or `GET /indexing-state`. If the collection's status is not `done`, indexing is still running (or failed). Look at `processed_files / total_files` and `error_count`.
3. **Collection lives in a different namespace.** `POST /search` returns `404` (not empty results) for cross-namespace access; `GET /collections/` filters silently, so a missing collection there points at a namespace mismatch.
4. **Routing thresholds too strict.** If you reach `/search` via a route call, `[routing].routing_confidence_threshold` may be filtering all candidates. The default is `0.30`; lowering it temporarily (e.g. `0.10`) is a common diagnostic. #Unverified (exact value is operational judgement)
5. **HTTP 200 with `results: []`** means the pipeline completed successfully but found nothing — not a failure signal.

## Symptom: reindex stuck

Symptoms: `archon-search collection list` shows no growth in `chunks=`, `GET /status` shows `IN_PROGRESS` indefinitely.

1. `GET /jobs/{job_id}` — for a REST-triggered reindex, this shows current status and any error.
2. `GET /indexing-state` — per-collection `processed_files`, `total_files`, `error_count`, and the last `error` message.
3. A stale `IN_PROGRESS` on startup is treated as a crash on the next restart and will trigger a reindex. If you suspect a wedged run, restart the server.
4. Last resort: `archon-search collection reindex <name>` clears the state and rebuilds from scratch. See [`50_ingestion_and_collections.md`](./50_ingestion_and_collections.md).

## Symptom: 503 from `/search`

`POST /search` returns 503 (`code: metadata_store_error`, `service unavailable: metadata store could not be reached`) when the collection-metadata lookup raises. LanceDB unreachable is one cause; metadata-row deserialization errors or a transient store init failure produce the same 503. Check disk space and inspect the log for the logged exception — it is the authoritative cause.

## Symptom: 504 from `/route`

`/route` has a hard timeout (~30 s). A 504 means routing did not complete in time — typically model load on the first request after start, or contention from concurrent ingest. Retry; if persistent, check CPU pressure. #Unverified (tuning guidance is operational)

## Symptom: install hangs at health check

`archon-search install` polls `GET /health` for 60 seconds. On timeout it prints `Warning: service did not become ready within 60s` and exits 1. Causes:

- Model weights still downloading on first run — check the log for download progress.
- Port already in use — change `[server].port` (or set `ARCHON_SEARCH_PORT`). #Unverified
- Write failure on `db_path` or `log_file` parent — the installer's `mkdir(parents=True, exist_ok=True)` does not raise if a parent already exists under another user; the failure surfaces later when the server tries to write inside a directory it cannot access. Verify the running user can write to any custom location.

## Symptom: graph search returns 422 — communities not built

`POST /search` with `graph_mode=local` or `graph_mode=global` returns HTTP **422** with `{"detail": {"code": "graph_communities_not_built", "message": "<collection>"}}`. The pipeline raised `GraphCommunitiesNotBuiltError`: the collection's Leiden communities have not been built, and `local`/`global` retrieval requires them.

1. **Build communities** for the collection, then retry:
   ```bash
   archon-search graph build-communities <collection> --wait
   ```
   This proxies `POST /graph/{collection}/rebuild-communities` (an async job).
2. `naive` and `ppr` graph modes do **not** require communities — use one of them if you do not need community retrieval.

See [`65_graph_search.md`](./65_graph_search.md).

## Symptom: `graph_mode=ppr` silently behaves like normal hybrid search

`graph_mode=ppr` is not an error path — when no query entity matches the collection graph, the Personalised PageRank walk cannot seed itself and the pipeline **falls back to hybrid search** transparently (HTTP 200, results returned). The `ppr_entities_matched` field on the response is `0` in this case (and also when no graph store / PPR walker is configured, or when PPR chunks are all ACL-filtered).

1. If you expected graph-aware ranking, confirm the graph is populated: entities are extracted only after ingest with `[graph].enabled = true`, and **existing collections do not retroactively gain graph edges** — re-ingest is the only path.
2. Check `ppr_entities_matched` in the response to distinguish a real PPR walk (`> 0`) from a hybrid fallback (`0`).

## Symptom: 422 — `scope_filter is not supported with graph_mode`

`POST /search` returns HTTP **422** with `{"detail": "scope_filter is not supported with graph_mode"}` when both `scope_filter` and `graph_mode` are set. Graph-mode paths bypass the scope SQL predicate entirely, so the two are mutually exclusive by design. Send one or the other.

Related 422s from the same guard block: `graph_mode requires [graph] enabled=true in server config` (graph disabled), and `graph_mode is not supported with multi-collection fanout; use a single collection` (`graph_mode=ppr` with `collections[]`).

## Symptom: server refuses to start — `graph.enabled=true but spacy is not installed`

When `[graph].enabled = true` but the `spacy` package is missing, `create_app()`'s `_check_graph_deps` raises a `ConfigError` at startup:

```
graph.enabled=true but spacy is not installed; run: pip install archon-search[graph]
```

1. **Install the extra**: `pip install archon-search[graph]`, or
2. **Disable graphing**: set `[graph].enabled = false`.

Note: `leidenalg`/`igraph` (community clustering) and code-parser extras (`archon-search[code]`) are **not** checked at startup — a missing Leiden install fails only a `build-communities` job; missing code parsers log a WARNING and surface a per-file warning in `IngestResult.warnings`, but the server still starts and prose graphing still works.

## Symptom: HyDE or RAG Fusion not working — "expansion failed" in response

`POST /search` with `hyde=true` or `rag_fusion=true` returns a non-null `expansion_warning` (and `expansion_used: false` — expansion fell back to the original query embedding).

1. **Check `GET /status`** — with `[hyde] enabled = true` or `[rag_fusion] enabled = true`, the response includes `hyde.key_available` / `rag_fusion.key_available`. A `false` value means the provider's required key is unset: `ANTHROPIC_API_KEY` for `provider = "anthropic"` (default), `OPENAI_API_KEY` for `provider = "openai"`. `ollama` and `claude_cli` report `key_available: true` (no API key) but that does **not** confirm Ollama is reachable or the `claude` CLI is logged in.
2. **Managed service**: put the key in `~/.archon-search/.secrets.env` (`ANTHROPIC_API_KEY=sk-...` or `OPENAI_API_KEY=sk-...`), then restart (`archon-search stop && archon-search start`). The wizard creates this file (mode 600) when HyDE/RAG-Fusion is enabled.
3. **`archon-search serve` (container mode)**: pass the key as `-e ANTHROPIC_API_KEY=sk-...` / `-e OPENAI_API_KEY=sk-...`.
4. **`archon-search status`** warns on stderr when the feature is enabled with a key-needing provider but the key is absent.
5. Key set but still warned? The provider call may be timing out — raise `timeout_seconds` in `[hyde]`/`[rag_fusion]` (default `10.0`). For `ollama`, verify the server at `ollama_base_url`; for `claude_cli`, confirm `claude` is on PATH and logged in.

## Symptom: server refuses to start — "the 'anthropic' package is not installed"

The server exits with a `ConfigError` naming `[hyde]` or `[rag_fusion]`, e.g. `[hyde] enabled=true with provider='anthropic' but the 'anthropic' package is not installed; run: pip install archon-search[hyde]`. The feature is enabled with a provider whose package is missing.

1. **Install the package**: `pip install archon-search[hyde]` (or `[rag_fusion]` — both pull `anthropic`). For `provider = "openai"`: `pip install archon-search[openai-provider]`; for `provider = "ollama"`: `pip install archon-search[ollama]`; `claude_cli` needs no package.
2. **Or disable**: set `[hyde].enabled = false` / `[rag_fusion].enabled = false`.
3. This fires only when the feature is enabled — a default install (feature off) never requires `anthropic`.

## Symptom: `/ready` returns 503 (not ready)

`GET /ready` returns **503** only when the **storage** check fails (`store.ping()` — LanceDB unreachable / not initialised). The `models` check is **informational**: it never flips `ready` to false. In the JSON body:

- `checks.storage` — `ok` / `fail`. `fail` → HTTP 503; fix the datastore (disk, permissions, `db_path`).
- `checks.models` — `ok` / `warn` / `fail` / `pending`. `pending` means the background model probe has not finished yet (normal right after start); `warn`/`fail` do not block readiness but flag a model/provider issue — see below.

## Symptom: provider / model validation failures in `/ready` and `/status`

A background probe (`model_validation.py`) loads the embedder and reranker and records the result on `app.state.model_validation`. It never blocks startup and never raises. Surfaces:

- `GET /ready` → `checks.models`: `fail` (a model could not load), `warn` (both loaded but a provider fallback warning was emitted), `ok`, or `pending`.
- `GET /status` → `model_validation` sub-object: `embedder_ok`, `reranker_ok`, and `provider_warnings` (e.g. `validation timed out after 60s`, `validation failed unexpectedly: …`).

1. `fail` / `embedder_ok=false` — the embedding model failed to load: check the log for download errors, disk space, and that `[database].embedding_model` names a valid model.
2. A timeout warning — raise `[database].validation_timeout_seconds` (default 60) if first-run downloads are slow.
3. `reranker_model = ""` disables the reranker; the probe then reports `reranker_ok=true` with nothing to load — expected, not a fault.

## Symptom: import fails — schema-version or model mismatch, corrupt lines

`archon-search import <collection> <path>` (proxying `POST /collections/{name}/import`) ends the job `FAILED`, or the CLI prints an error before submitting.

1. **`schema_version mismatch: archive has X, server expects Y; use ignore_schema_version=true to bypass`** — the archive was produced by a different schema version. Retry with `--ignore-schema-version` **only if** you accept the risk of a format drift.
2. **`collection '<name>' already exists. Use --force-overwrite to overwrite.`** (HTTP `409`) — pass `--force-overwrite` to drop-and-recreate the target.
3. **Corrupt / unparseable lines in the archive** — by default `--on-error=fail` aborts the whole import on the first bad line. Use `--on-error=skip` to skip corrupt lines and import the rest.
4. Add `--wait` to poll the import job and see progress and the terminal status.

See [`90_export_import.md`](./90_export_import.md) for the full export/import workflow.

## Symptom: FAILED_EXPIRED ingest jobs

`GET /status` reports `failed_expired_ingest_count > 0`, or `archon-search status` shows a count with a re-ingest hint. A `FAILED_EXPIRED` job failed and either aged past `retry_max_age_hours` or exhausted `retry_max_attempts`; it is never retried automatically.

1. **List them**: `GET /jobs?status=FAILED_EXPIRED`. Each job's `error` is the failure message; `source_path` is the original file. (`result` is `null` for failed jobs.)
2. **Re-ingest** after fixing the root cause: `archon-search ingest <path>` (or `POST /ingest`).
3. **Tune retry policy**: raise `[maintenance].retry_max_age_hours` (default 72) or `retry_max_attempts` (default 3). Changes take effect on the next maintenance pass.

## Symptom: ACL sidecar skipped — ingest returns warnings

`IngestResult.warnings` (or `GET /jobs/{id}` result) contains a message about an ACL sidecar. A `.acl` sidecar next to the document was skipped for exceeding the 64 KB limit; the document was ingested **without** ACL enforcement (all authenticated namespaces can access it).

1. **Check `archon-search ingest <path>` stderr** — sidecar warnings are printed there.
2. **Check `GET /jobs/{id}`** — the job `result` includes a `warnings` list naming the oversized file.
3. **Resolution**: shrink the sidecar (64 KB ≈ 2500 entries at 25 chars each), or use front-matter `_acl` in the document for small ACL lists. See [`../SecurityGuide/03_authorization_and_acl.md`](../SecurityGuide/03_authorization_and_acl.md).

## Related documents

- [`00_index.md`](./00_index.md) — UserManual table of contents.
- [`30_configuration.md`](./30_configuration.md) — every key the loader validates.
- [`40_running_the_server.md`](./40_running_the_server.md) — `start`/`stop`/`status`/`serve` semantics.
- [`50_ingestion_and_collections.md`](./50_ingestion_and_collections.md) — reindex triggers and collection lifecycle.
- [`65_graph_search.md`](./65_graph_search.md) — graph modes and building communities.
- [`80_explain_and_debugging.md`](./80_explain_and_debugging.md) — `POST /explain` for per-result diagnostics.
- [`90_export_import.md`](./90_export_import.md) — export/import workflow and flags.
- [`../OperatorGuide/90_incident_runbook.md`](../OperatorGuide/90_incident_runbook.md) — operator-facing incident response.
- [`../Architecture/140_error_handling_strategy.md`](../Architecture/140_error_handling_strategy.md) — status-code conventions.
