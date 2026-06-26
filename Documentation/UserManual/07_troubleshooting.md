**Purpose**: Diagnose common runtime failures.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Troubleshooting

## Principles

1. **Start at `/health` and `/status`.** They are the cheapest probes and tell you whether the process is alive and what the indexer is doing.
2. **Logs first, code second.** `~/.archon-search/logs/archon-search.log` captures most failures; check it before reading the source.
3. **Config errors are surfaced loudly.** `ConfigError` from `archon_search/config.py` will refuse to start the server — read the message; it names the offending key.
4. **Most empty-result issues are configuration, not retrieval.** Check the collection exists, has been indexed, and that routing is letting it through before suspecting the model.

## Where to look

| Location | Contents |
| --- | --- |
| `~/.archon-search/logs/archon-search.log` | Server log (level from `[logging].level`). |
| `~/.archon-search/.search.env` | API key (mode `600`). |
| `~/.archon-search/search/` | LanceDB data. Deleting forces a full rebuild on next sync. |
| `~/.archon-search/search-logs/` | Telemetry JSONL — only present when telemetry is enabled. |
| `~/.archon-search/archon-search.toml` | Config. Default location; overridable via `ARCHON_SEARCH_CONFIG`. |

## Symptom: server will not start

Symptoms: `archon-search start` prints `Error: …` and exits 1.

1. Run `archon-search config show` — does the printed TOML look correct?
2. Force the loader to use the default config path (`~/.archon-search/archon-search.toml`) by clearing `ARCHON_SEARCH_CONFIG` for one invocation. An empty value is treated as unset by `get_default_config_path()` in `archon_search/config.py`, so this does not skip config loading — it loads the default file:
   ```bash
   ARCHON_SEARCH_CONFIG= archon-search start
   ```
3. Look for `ConfigError: …` lines in the log — the message names the bad key (e.g. `port must be between 1 and 65535`, `routing_confidence_threshold must be in [0.0, 1.0]`).

## Symptom: 401 on every request

The client is not presenting a valid Bearer token, or it is presenting one the server does not recognise.

1. Confirm the key the server is using:
   - `ARCHON_SEARCH_API_KEY` env var if set (takes priority; the env var itself holds the key value).
   - Otherwise the file at the path named by `ARCHON_SEARCH_KEY_FILE` (the env var holds a **path**, not the key), or the default `~/.archon-search/.search.env`. The loader then reads `ARCHON_SEARCH_API_KEY=<hex>` from that file.
2. The file format is `ARCHON_SEARCH_API_KEY=<hex>` on a single line; the value must be lowercase hex (`archon_search/key_manager.py:_validate_key`). Behaviour on invalid input differs by source:
   - **Env var invalid:** logged at WARNING and ignored; loader falls back to the file (`_load_from_env`).
   - **File invalid:** logged at ERROR and `_load_from_file` returns `None`; `load_or_generate_key` then falls through to `_generate_and_write`, which **overwrites the existing key file with a freshly generated key** via the durable helper `_durable_io.atomic_write_bytes` (mode `0600` at creation, fsync file → `os.replace` → fsync parent dir). There is no "next source" after the file — invalid file content silently triggers key rotation.
3. The loader tries to tighten file permissions to `600` via `_chmod_600`, but a chmod failure is swallowed (`try`/`except OSError` in `_load_from_file`) and reading proceeds. The load only aborts if the subsequent `read_text` itself raises `OSError`. So `600` is enforced best-effort, not a hard precondition for loading.
4. **To rotate the default key (no restart required, D7)**: `archon-search key rotate`. The old key is revoked and a new token is written to `.search.env`. Use `--grace <duration>` if in-flight requests need time to drain. Returns `409` if `ARCHON_SEARCH_API_KEY` env var is set (unset it first).
   **Legacy (requires restart)**: delete `~/.archon-search/.search.env` and restart — a fresh key is generated.

## Symptom: empty results from `/search`

In order of likelihood:

1. **Wrong collection name.** Run `archon-search collection list` and compare to the `collection` field in your request. Names are derived from paths via `path_to_collection_name`; they are **not** the path itself.
2. **Collection not yet indexed.** Hit `GET /status` or `GET /indexing-state`. If the collection's status is not `done`, indexing is still running (or failed). Look at `processed_files / total_files` and `error_count`.
3. **Collection lives in a different namespace.** `POST /search` returns `404` (not empty results) for cross-namespace access — but `GET /collections/` filters silently, so a missing collection there points at namespace mismatch.
4. **Routing thresholds too strict.** If you reach `/search` via a route call, `[routing].routing_confidence_threshold` may be filtering all candidates. The default is `0.30` (valid range `[0.0, 1.0]`); lowering it temporarily (e.g. `0.10`) is a common diagnostic, though the exact value to try is operational judgement. #Unverified
5. **Pipeline raised internally.** `POST /search` returns HTTP 500 on pipeline stage exceptions (embedder, store, reranker); the log will contain the traceback. A 504 means the pipeline timed out (~30 s). HTTP 200 with `results: []` means the pipeline completed successfully but found no matching documents — it is not a failure signal.

## Symptom: reindex stuck

Symptoms: `archon-search collection list` shows no growth in `chunks=`, `GET /status` shows `IN_PROGRESS` indefinitely.

1. `GET /jobs/{job_id}` — if you triggered the reindex via REST, this shows the current status and any error.
2. `GET /indexing-state` — per-collection `processed_files`, `total_files`, `error_count`, and the last `error` message.
3. The "in-progress on startup means crashed mid-index" rule (`archon_search/server/mcp.py:_needs_install_trigger`) means a stale `IN_PROGRESS` is treated as a crash on the next restart and will trigger a reindex. If you suspect a wedged run, restart the server.
4. Last resort: `archon-search collection reindex <name>` clears the state and rebuilds from scratch.

## Symptom: 503 from `/search`

The route returns 503 when *any* exception is raised by the collection-metadata lookup (`routes_search.py:86-90`). LanceDB unreachable is one possible cause, but the same 503 can be produced by metadata-row deserialization errors, a transient store init failure, or any other exception in `pipeline.get_collection_meta(...)`. Check disk space and inspect the log for `meta lookup failed for collection …` — the logged exception is the authoritative cause. #Unverified (specific failure modes beyond what the code line catches are operational, not enumerated in source)

## Symptom: 504 from `/route`

`/route` has a 30-second hard timeout (`routes_route.py:94-101`). A 504 means routing did not complete in time — typically due to model load on the first request after start, or contention from concurrent ingest. Retry; if persistent, raise `routing_shortlist_size` or check CPU pressure. #Unverified (the tuning effect of `routing_shortlist_size` on the 30s timeout is operational guidance, not proven by source)

## Symptom: install hangs at health check

`archon-search install` polls `GET /health` for 60 seconds (`install_cmd.py:_HEALTH_TIMEOUT`). On timeout it prints `Warning: service did not become ready within 60s` and exits 1. Causes:

- Model weights still downloading on first run — check the log for `fastembed` / `cross-encoder` download progress. #Unverified (exact log strings emitted by those libraries were not confirmed against source)
- Port already in use — change `[server].port`. #Unverified (a port collision causing the install-time health-check to time out specifically, vs. the service failing to start visibly, was not traced end-to-end)
- Write failure on `db_path` or `log_file` parent — the installer creates these with `mkdir(parents=True, exist_ok=True)` (`install_cmd.py:96-99`), which does **not** raise if a parent already exists under another user. The failure surfaces later, when the server tries to create files inside a directory it cannot write to. If your `db_path` or `log_file` points at a custom location, verify the running user can write there.

## Symptom: HyDE or RAG Fusion not working — "HyDE expansion failed" in response

`POST /search` with `hyde=true` or `rag_fusion=true` returns a non-null `expansion_warning` (and `expansion_used: false`, because expansion fell back to the original query embedding).

1. **Check `GET /status`** — if `[hyde] enabled = true` or `[rag_fusion] enabled = true` in your config, the response includes `hyde.key_available` or `rag_fusion.key_available`. A `false` value means `ANTHROPIC_API_KEY` is not set in the server process environment.
2. **For the managed service**: the key must be in `~/.archon-search/.secrets.env` (one line: `ANTHROPIC_API_KEY=sk-...`). The wizard creates this file automatically (mode 600, empty) when HyDE or RAG Fusion is enabled — if you skipped the wizard, create it manually. The managed service sources this file at start time via the wrapper script (macOS) or `EnvironmentFile=` (Linux systemd). After editing the file, restart the service (`archon-search stop && archon-search start`).
3. **For `archon-search serve` (container mode)**: pass the key as an environment variable to the container: `-e ANTHROPIC_API_KEY=sk-...`.
4. **Run `archon-search status`** — it warns on stderr when HyDE or RAG Fusion is enabled but the key is absent: `Warning: HyDE enabled but ANTHROPIC_API_KEY is not set — expansion will fall back to plain search.`
5. If the key is set but you still see `expansion_warning`, the Anthropic API call may be timing out. Raise `timeout_seconds` in the `[hyde]` or `[rag_fusion]` TOML section (default is `10.0` seconds).

## Symptom: FAILED_EXPIRED ingest jobs

`GET /status` reports `failed_expired_ingest_count > 0`, or `archon-search status` shows a count with a re-ingest hint.

A `FAILED_EXPIRED` job is an ingest job that failed and was not successfully retried before the `retry_max_age_hours` window closed, **or** that exhausted all `retry_max_attempts` retries. The job will not be retried again automatically.

1. **List affected jobs**: `GET /jobs?status=FAILED_EXPIRED` lists all expired jobs in your namespace. Each job's `error` field contains the failure message; `source_path` contains the original file path. (The `result` field is `null` for failed jobs — it is only populated for successfully completed ingest jobs.)
2. **Re-ingest**: use `POST /ingest` with the path from the failed job, or `archon-search ingest <path>`. Fix the underlying cause (missing file, permission error, oversized sidecar) before re-ingesting.
3. **Tune retry policy**: raise `[maintenance].retry_max_age_hours` (default 72) or `retry_max_attempts` (default 3) in `archon-search.toml` if transient failures are common in your environment. Changes take effect on the next maintenance pass.
4. Jobs transition to `FAILED_EXPIRED` on the next maintenance pass after the cutoff — not at the exact expiry moment.

## Symptom: ACL sidecar skipped — ingest returns warnings

`IngestResult.warnings` (or `GET /jobs/{id}` result) contains a message about an ACL sidecar.

An ACL sidecar file (`.acl` file next to the ingested document) was skipped because it exceeded the 64 KB size limit. The document was ingested without ACL enforcement — all authenticated namespaces can access it.

1. **Check `archon-search ingest <path>` stderr** — warnings are printed there when a sidecar is skipped.
2. **Check `GET /jobs/{id}`** — the job `result` dict includes a `warnings` list with the message naming the oversized file.
3. **Resolution**: reduce the sidecar size (64 KB allows ~2500 namespace entries at 25 chars each). Alternatively, use front-matter `_acl` in the document file itself for small ACL lists — there is no *ACL-specific* size limit on front-matter ACL (document-level parsing limits may still apply).

## Related documents

- [`02_configuration.md`](./02_configuration.md) — every key the loader validates.
- [`03_running_the_server.md`](./03_running_the_server.md) — `start`/`stop`/`status` semantics.
- [`04_ingestion_and_collections.md`](./04_ingestion_and_collections.md) — reindex triggers.
- [`../Architecture/140_error_handling_strategy.md`](../Architecture/140_error_handling_strategy.md) — status-code conventions.
