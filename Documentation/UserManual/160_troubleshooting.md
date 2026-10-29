**Purpose**: Diagnose common runtime failures.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-10-20 / **Next review**: 2027-07-29

# Troubleshooting

## Principles

1. **Start at `/health` and `/ready`, then `/status`.** They are the cheapest probes. `GET /health` tells you the process is alive; `GET /ready` tells you whether storage is connected, the startup model warm-up (run on every start) and the background model validation have finished (or warm-up failed), and the startup collection sync has finished (the model-*validation* outcome it also reports is informational); `GET /status` tells you what the indexer, jobs, and optional features are doing. `/health` and `/ready` need no token (`/health` returns only `{"status": "running"}`); `/status` needs a Bearer token.
2. **Logs first, code second.** `~/.archon-search/logs/archon-search.log` captures most failures; check it before reading the source.
3. **Config errors are surfaced loudly.** `ConfigError` from `archon_search/config.py` (and the startup dependency checks in `server/app.py`) will refuse to start the server — read the message; it names the offending key or missing package.
4. **Most empty-result issues are configuration, not retrieval.** Check the collection exists, has been indexed, and that routing is letting it through before suspecting the model.

## Where to look

Paths are shown for the default data dir `~/.archon-search/`; `ARCHON_SEARCH_DATA_DIR` relocates every entry, the config included; `ARCHON_SEARCH_CONFIG`, when set, overrides the config location (the managed service bakes the config path in at install time: the `--config` path, else `ARCHON_SEARCH_CONFIG`, else `<data-dir>/archon-search.toml`), and `ARCHON_SEARCH_KEY_FILE`, when set, overrides the `.search.env` location. See [`30_configuration.md`](./30_configuration.md).

| Location | Contents |
| --- | --- |
| `~/.archon-search/logs/archon-search.log` | Server log (level from `[logging].level`). |
| `~/.archon-search/.search.env` | API key (mode `600`). |
| `~/.archon-search/.secrets.env` | HyDE / RAG-Fusion provider keys (mode `600`; managed service only). |
| `~/.archon-search/search/` | LanceDB data. Deleting forces a full rebuild on next sync. |
| `~/.archon-search/search-logs/` | Telemetry JSONL — only present when telemetry is enabled. |
| `~/.archon-search/archon-search.toml` | Config. Default `<data-dir>/archon-search.toml`; overridable via `ARCHON_SEARCH_CONFIG`. |

## Symptom: server will not start

Symptoms: `archon-search start` prints `Error: …` and exits 1.

1. Run `archon-search config show` — does the printed TOML look correct?
2. Force the loader to use the default config path (`<data-dir>/archon-search.toml`, default `~/.archon-search/archon-search.toml`) by clearing `ARCHON_SEARCH_CONFIG` for one invocation. An empty value is treated as unset, so this loads the default file rather than skipping config:
   ```bash
   ARCHON_SEARCH_CONFIG= archon-search start
   ```
3. Look for `ConfigError: …` lines in the log — the message names the bad key (e.g. `port must be between 1 and 65535`, `routing_confidence_threshold must be in [0.0, 1.0]`).

## Symptom: 401 on every request

The client is not presenting a valid Bearer token, or it is presenting one the server does not recognise.

1. Confirm the key the server is using:
   - `ARCHON_SEARCH_API_KEY` env var if set (takes priority; the env var itself holds the key value).
   - Otherwise the file at the path named by `ARCHON_SEARCH_KEY_FILE` (the env var holds a **path**, not the key), or the default `~/.archon-search/.search.env`.
2. The file format is `ARCHON_SEARCH_API_KEY=<hex>` on a single line; the value must be lowercase hex of at least 32 characters. An **invalid, unreadable or non-UTF-8 key file** (or an invalid `ARCHON_SEARCH_API_KEY`) refuses startup with a `ConfigError` that names the rule and the file and leaves the file untouched — fix the value, or delete the file to have the server generate a new key. A `[namespaces]` token must be at least 32 printable ASCII characters; an unusable `Authorization` token (non-ASCII, empty) is a plain `401`.
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
2. `GET /indexing-state` — per-collection `processed_files`, `total_files`, `error_count`, and the last `error` message. A failed sync reads `sync of collection '<name>' failed; see the server log for details`; the cause is in the server log under `Sync of collection '<name>' failed`. A sync that skipped a collection because another write held its lock reads `sync of collection '<name>' skipped: the collection is busy with another write; the next sync retries it` in the `POST /sync` job's `result["errors"]` (nothing was written; `POST /sync` again), and one that did not remove a collection gone from the config because a job or an MCP ingest may still write it reads `collection '<name>' is no longer configured but was not removed: it is busy; the next sync retries` (the server log, at WARNING, names the job or writer; the next sync removes it once that has ended).
3. A stale `IN_PROGRESS` on startup is treated as a crash on the next restart and will trigger a reindex. If you suspect a wedged run, restart the server.
4. Last resort: `archon-search collection reindex <name>` clears the state and rebuilds from scratch. See [`50_ingestion_and_collections.md`](./50_ingestion_and_collections.md).

## Symptom: 503 from `/search`

`POST /search` returns 503 (`code: metadata_store_error`, `service unavailable: metadata store could not be reached`) when the collection-metadata lookup raises. LanceDB unreachable is one cause; metadata-row deserialization errors or a transient store init failure produce the same 503. Check disk space and inspect the log for the logged exception — it is the authoritative cause.

## Symptom: 504 from `/route`

`/route` has a hard timeout (~30 s). A 504 means routing did not complete in time — typically model load on the first request after start, or contention from concurrent ingest. Retry; if persistent, check CPU pressure. #Unverified (tuning guidance is operational)

## Symptom: install hangs at health check

`archon-search wizard` (or `install`) polls `GET /health` for up to 60 seconds by default (longer when eager embedder loading is enabled — see the readiness-timeout note in the [Wizard guide Step 8](20_wizard.md)). On timeout it prints `Warning: Search service did not become ready within N seconds.` and exits 1. Causes:

- Model weights still downloading on first run (from Hugging Face, into `<data-dir>/models`) — check the log for download progress. A failed download logs `could not fetch model '<repo>': download failed; check the network connection and free disk space`.
- Port already in use — set `[server].port` (`archon-search config set server.port N`; a fresh wizard install does not write the key) or set `ARCHON_SEARCH_PORT`. #Unverified
- Write failure on `db_path` or `log_file` parent — the installer's `mkdir(parents=True, exist_ok=True)` does not raise if a parent already exists under another user; the failure surfaces later when the server tries to write inside a directory it cannot access. Verify the running user can write to any custom location (a custom `db_path` or `log_file` is used only while `ARCHON_SEARCH_DATA_DIR` is unset; with it set, both live under the data directory).

## Symptom: Linux install pulls `nvidia-*`/`cuda-*` packages on a CPU-only host

A plain `pip install archon-search` or `uv tool install archon-search` on Linux installs PyPI's CUDA build of PyTorch, which pulls in `triton` and 18 `nvidia-*`/`cuda-*` packages, several GB of disk. A host without an NVIDIA GPU never uses those packages. Install (or switch to) the CPU-only build as described in [Linux: install the CPU-only PyTorch build](./10_installation.md#linux-install-the-cpu-only-pytorch-build).

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

## Symptom: server refuses to start — `graph.enabled=true but gliner is not installed`

When `[graph].enabled = true` but the `gliner` package is missing, `create_app()`'s `_check_graph_deps` — delegating to `ensure_graph_engine_importable` in `archon_search/graph_extractor.py` — raises a `ConfigError` at startup:

```
graph.enabled=true but gliner is not installed. Install the graph extras:
pip install 'archon-search[graph]'
```

The install command in the message matches how archon-search is installed. In a `uv tool` install it is a `uv tool install 'archon-search[...]'` command that repeats the extras and options recorded in the tool's receipt and adds `graph`; when the receipt cannot be reproduced, it is a plain `uv tool install 'archon-search[graph]'` with a `#` note to repeat the options of your original install. Elsewhere it is `pip install`.

1. **Install the extra**: run the command from the message (see [Optional extras](./10_installation.md#optional-extras)), or
2. **Disable graphing**: set `[graph].enabled = false`.

Note: the prose extraction *checkpoint*, `leidenalg`/`igraph` (community clustering), and code-parser extras (`archon-search[code]`) are **not** checked at startup — a checkpoint that will not load degrades prose extraction (see the `graph prose extraction model is unavailable` symptom below); a missing Leiden install fails only a `build-communities` job; missing code parsers log a WARNING and surface a per-file warning in `IngestResult.warnings`, but the server still starts and prose graphing still works.

## Symptom: extras missing after `uv tool upgrade archon-search`

After `uv tool upgrade archon-search`, the server refuses to start with `gliner is not installed`, `fasttext-wheel is not installed` or a missing provider package. Code files also log missing tree-sitter parsers. The extras were installed into the uv tool environment without being recorded in its receipt: by an older wizard or by `uv pip install`. `uv tool upgrade` removes anything the receipt does not list. Re-install with every extra you use, repeating the options of your original install, e.g. `uv tool install "archon-search[graph,code,multilingual]"`; on Linux with a `+cpu` or `+cu126` torch build, add `--torch-backend cpu` or `--torch-backend cu126`. uv then records them and later upgrades keep them. See [Optional extras](./10_installation.md#optional-extras).

## Symptom: graph has no prose entities — `graph prose extraction model is unavailable`

The graph fills with `code_symbol` entities from code files but no `person` / `concept` / `system` / `event` entities from prose, and ingests succeed carrying this warning in `IngestResult.warnings`:

```
graph prose extraction model is unavailable; prose entity extraction is disabled
for this ingest (code-symbol extraction is unaffected).
```

The `archon-search[graph]` extra installs the `gliner` library; its checkpoint (`knowledgator/gliner-relex-multi-v1.0`, pinned by revision in `archon_search/paths.py`) is a separate artifact fetched from Hugging Face into `<data-dir>/models/graph/` — pre-warmed by `archon-search wizard`, or downloaded lazily by the server on first graph ingest. This warning means that fetch or load did not succeed. **It is not an ingest failure** — chunks embed and persist, search works, and only prose extraction is skipped.

**The failure latches for the life of the process.** Once a load fails, every subsequent ingest in that server process degrades the same way without retrying, so fixing the cause requires a restart to take effect. `GET /status` does **not** show this: `model_validation.provider_warnings` reports a missing `gliner` package, not a checkpoint that failed to load. The server log and the ingest warnings are the only surfaces.

1. **Check egress.** The usual cause is that the process cannot reach Hugging Face. The server log carries the underlying reason from `ProseExtractionBackend.load()`.
2. **Pre-warm on a host that can reach it**: `archon-search wizard`, then make `<data-dir>/models/graph/` available to the server — for containers, bind-mount the host `models/` directory (see [`140_running_with_docker.md`](./140_running_with_docker.md#graph-extraction-in-a-container)).
3. **Restart the server**, because of the latch.
4. **Re-ingest** afterwards. Documents indexed while degraded are not retroactively extracted.

### The variants: extraction failed, and still loading

Two other wordings mean different things and neither is fixed by provisioning. If the warning reads:

```
graph prose entity/relation extraction failed; prose entity extraction was
skipped for this document (code-symbol extraction is unaffected).
```

then the model loaded fine and the extraction call itself failed for that one document. The rest of the ingest is unaffected and the next document is retried normally; check the server log for the cause.

If it reads:

```
graph prose extraction model is still loading for another document; prose entity
extraction is disabled for this ingest (code-symbol extraction is unaffected).
```

then this document waited on another document's in-flight model load and gave up rather than stalling behind it. This is bounded concurrency working as designed, not a fault: the load continues, and later documents in the same run get the loaded model. It is deliberately never surfaced as a 503. If you see it on every document, the load is failing rather than being slow — treat it as the first symptom above.

All three wordings are sanitized constants in `archon_search/graph_extractor.py` and never carry exception text; the cause is always in the server log, not in the warning.

## Symptom: HyDE or RAG Fusion not working — "expansion failed" in response

`POST /search` with `hyde=true` or `rag_fusion=true` returns a non-null `expansion_warning` (and `expansion_used: false` — expansion fell back to the original query embedding).

1. **Check `GET /status`** — with `[hyde] enabled = true` or `[rag_fusion] enabled = true`, the response includes `hyde.key_available` / `rag_fusion.key_available`. A `false` value means the provider's required key is unset: `ANTHROPIC_API_KEY` for `provider = "anthropic"` (default), `OPENAI_API_KEY` for `provider = "openai"`. `ollama` and `claude_cli` report `key_available: true` (no API key) but that does **not** confirm Ollama is reachable or the `claude` CLI is logged in.
2. **Managed service**: put the key in `<data-dir>/.secrets.env` (`~/.archon-search/.secrets.env` by default; `ANTHROPIC_API_KEY=sk-...` or `OPENAI_API_KEY=sk-...`), then restart (`archon-search stop && archon-search start`). The wizard creates this file (mode 600) when HyDE/RAG-Fusion is enabled.
3. **`archon-search serve` (container mode)**: pass the key as `-e ANTHROPIC_API_KEY=sk-...` / `-e OPENAI_API_KEY=sk-...`.
4. **`archon-search status`** warns on stderr when the feature is enabled with a key-needing provider but the key is absent.
5. Key set but still warned? The provider call may be timing out — raise `timeout_seconds` in `[hyde]`/`[rag_fusion]` (default `10.0`). For `ollama`, verify the server at `ollama_base_url`; for `claude_cli`, confirm `claude` is on PATH and logged in.

## Symptom: server refuses to start — "the 'anthropic' package is not installed"

The server exits with a `ConfigError` naming `[hyde]` or `[rag_fusion]`, e.g. `[hyde] enabled=true with provider='anthropic' but the 'anthropic' package is not installed; run: pip install 'archon-search[hyde]'`. The feature is enabled with a provider whose package is missing. The command after `run:` matches how archon-search is installed: in a `uv tool` install it is the `uv tool install` command that keeps the extras already recorded in the receipt.

1. **Install the package**: run the command from the message. The extras are `hyde` (or `rag_fusion`; both pull `anthropic`), `openai-provider` for `provider = "openai"` and `ollama` for `provider = "ollama"`; `claude_cli` needs no package. See [Optional extras](./10_installation.md#optional-extras) for the command per install method.
2. **Or disable**: set `[hyde].enabled = false` / `[rag_fusion].enabled = false`.
3. This fires only when the feature is enabled — a default install (feature off) never requires `anthropic`.

## Symptom: `/ready` returns 503 (not ready)

`GET /ready` returns **503** in four cases: the **storage** check fails (`store.ping()` — LanceDB unreachable / not initialised), the startup model warm-up (run on every start) or the background model validation (bounded as listed under [`validation_timeout_seconds`](30_configuration.md#database)) is still running (`checks.models: "pending"`; a failed warm-up reports `checks.models: "fail"` at once and ends the models gate even while validation runs), the server's own startup collection sync (run automatically at boot over all configured collections) has not finished yet, or (new on 2026-10-25) the warm-up failed and the **default embedder or a configured reranker is still not loaded** (`checks.models: "fail"`): a replica that cannot embed must not take traffic, and the server retries the load in the background (5 s, doubling to a 300 s cap), so `/ready` returns 200 by itself once the weights load — fix the cause (network, disk, wrong model name, `HF_HUB_OFFLINE`) and wait; no restart is needed. A `warn`/`fail` result that comes only from the validation probe is **informational** and does not flip `ready` to false. In the JSON body:

- `checks.storage` — `ok` / `fail`. `fail` → HTTP 503; fix the datastore (disk, permissions, `db_path`).
- `checks.models` — `ok` / `warn` / `fail` / `pending`. `pending` means either the background model probe has not finished yet, or the startup model warm-up is still loading models — either way it returns HTTP 503, and is normal right after every start, on every config (warm-up runs unconditionally; `[database].eager_load_embedders` only widens which embedders it covers). Just wait and retry. `warn` never blocks readiness, and a probe-only `fail` does not either, but both flag a model/device issue — see below; a `fail` from a failed warm-up that left the default embedder or a configured reranker cold does block it (503, self-healing). `fail` has two distinct causes, checked in this order: a **terminally failed model warm-up** (the warm-up task raised, exceeded its 600s ceiling, or a model it covered could not load — the log line `"model warm-up failed: <models>"` names them; a timeout logs `model warm-up failed: timed out after 600.0s`, suffixed `; still cold: <models>` when any covered model is still cold), or the separate background `model_validation` probe reporting the configured embedding/reranker model could not load at all. Either way search still answers, degraded: FTS-only (keyword) results while the embedding model is not loaded, unreranked results while the reranker is not. Each search retries the load, so a transient cause (download hiccup) clears itself; a wrong model name or full disk does not until you fix it.
- `checks.sync` — `ok` / `pending` / `warn` / `fail`. `warn` means the automatic startup sync was **suppressed**: the previous run died mid-ingest (OOM, `kill -9`), so the crash-loop guard refused to re-enter the same workload. This suppression is **sticky** — it survives a process restart (a sentinel file under the data dir keeps it armed) and keeps firing on every subsequent boot, not just the one right after the crash. `/ready` still answers 200 (`ready: true`) and `GET /status` reports `sync_result: "suppressed"`; the index stays as it was until you fix the cause and run `POST /sync` (or `archon-search sync`), which clears both back to `ok` / `"done"`. `pending` means the server's own startup collection sync (run automatically at boot over all configured collections) has not finished yet — normal on a first boot against a large corpus. Check the log for `"startup sync"` lines to confirm it is progressing rather than stuck; `POST /sync` (and `archon-search sync`) also returns `409` for the same window. `fail` means the startup sync either raised an exception or completed but recorded per-collection errors (missing path, ingest failure) — check the log for a `"startup sync failed"` or `"startup sync completed with"` WARNING, then investigate and retry with `POST /sync` (or `archon-search sync`). Unlike `pending`, `fail` does NOT make `/ready` answer 503: the server still reports `ready: true` so a corrupted collection cannot wedge the pod's readiness forever.

## Symptom: provider / model validation failures in `/ready` and `/status`

A background probe (`model_validation.py`) loads the embedder and reranker and records the result on `app.state.model_validation`. It never blocks startup and never raises. Surfaces:

- `GET /ready` → `checks.models`: `fail` (a model could not load), `warn` (both loaded but a device fallback warning was emitted), `ok`, or `pending`.
- `GET /status` → `model_validation` sub-object: `embedder_ok`, `reranker_ok`, `llama_cpp_ok`, `provider_warnings` (e.g. `validation timed out after 60s`, `model validation failed unexpectedly; see the log for details`, or `validation task failed unexpectedly; see the log for details` when the validation task itself crashed — both with `embedder_ok` and `reranker_ok` false; the server log has the cause), and `validated_at`. While validation is pending the object is still present, with the `*_ok` flags and `validated_at` `null` and `provider_warnings` empty.

1. `fail` / `embedder_ok=false` — the embedding model failed to load: check the log for download errors (`could not fetch model '<repo>': …`), disk space, and that `[database].embedding_model` names a supported model. A name outside the registry is logged as `unsupported model '<name>'; supported: …` (the same text a collection with an unsupported `embedding_model` gets as a `422`); the supported names are listed in `archon_search/model_specs.py`. With `HF_HUB_OFFLINE=1` a model missing from `<data-dir>/models` fails with `offline mode is on and the model is not in the local cache`.
2. A timeout warning — raise `[database].validation_timeout_seconds` (default 60) if first-run downloads are slow.
3. `reranker_model = ""` disables the reranker; the probe then reports `reranker_ok=true` with nothing to load — expected, not a fault.
4. `warn` with `provider_warnings` containing `device unavailable: <requested>; models loaded on cpu` (`embedder_ok=true`, `reranker_ok=true`) — `[database].device` is set to `mps` or `cuda` explicitly, but this host (or the installed PyTorch build) has no such device. Not a load failure: the models loaded on CPU, and the server log carries one `device '<requested>' is not available on this host; using cpu` WARNING per process. Search works, on the slower device. To clear it, either install a PyTorch build that supports the device (for CUDA, see [NVIDIA GPU: run embedding and reranking on CUDA](./10_installation.md#nvidia-gpu-run-embedding-and-reranking-on-cuda)), or set `[database].device` to `auto` or `cpu` (re-running the wizard with `--disable-gpu` writes `cpu`), then restart.
5. `embedder probe failed; see the log for details` or `reranker probe failed; see the log for details` in `provider_warnings` — that model could not be loaded or run on the configured device; the log line `embedder probe failed for <model> on device <device>: <cause>` (or `reranker probe failed …`) names the cause.

## Symptom: import fails — schema-version or model mismatch, corrupt lines

`archon-search import <collection> <path>` (proxying `POST /collections/{name}/import`) ends the job `FAILED`, or the CLI prints an error before submitting.

1. **`schema_version mismatch: archive has X, server expects Y; use ignore_schema_version=true to bypass`** — the archive was produced by a different schema version. Retry with `--ignore-schema-version` **only if** you accept the risk of a format drift.
2. **`collection '<name>' already exists. Use --force-overwrite to overwrite.`** (HTTP `409`) — pass `--force-overwrite` to drop-and-recreate the target. If the job then ends `FAILED` with `force_overwrite refused: …`, another job (an ingest, reindex, import, migration, metadata reindex or community rebuild) was still writing the collection, or another write held its lock too long: nothing was dropped. Wait until that job is terminal (`archon-search jobs status <job_id>`), then resume the failed import or run it again.
3. **Corrupt / unparseable lines in the archive** (`Corrupt line N: not valid UTF-8 JSON`, `Corrupt line N: not a JSON object`, `corrupt doc (vector) at line N: …`, `malformed doc at line N: …`) — by default `--on-error=fail` aborts the whole import on the first bad line. Use `--on-error=skip` to skip corrupt lines and import the rest.
4. Add `--wait` to poll the import job and see progress and the terminal status.

See [`90_export_import.md`](./90_export_import.md) for the full export/import workflow.

## Symptom: FAILED_EXPIRED ingest jobs

`GET /status` reports `failed_expired_ingest_count > 0`, or `archon-search status` shows a count with a re-ingest hint. A `FAILED_EXPIRED` job failed and either aged past `retry_max_age_hours` (counted from the original request, not from each retry) or exhausted `retry_max_attempts`, or was never eligible for a retry — saved by an older server (no stored request), failed after you cancelled it, or failed `process_restart` (running when the server died); it is never retried automatically. Re-submit the ingests you still need.

1. **List them**: `GET /jobs?status=FAILED_EXPIRED`. Each job's `error` is a short failure summary — an unexpected failure reads `ingest failed; see the server log for details`, so look up the job id in the server log for the cause; `source_path` is the original file. (`result` is `null` for failed jobs.)
2. **Re-ingest** after fixing the root cause: `archon-search ingest <path>` (or `POST /ingest`).
3. **Tune retry policy**: raise `[maintenance].retry_max_age_hours` (default 72) or `retry_max_attempts` (default 3). The `[maintenance]` section is read once at startup, so restart the server for a change to take effect. Raising a limit never revives a `FAILED_EXPIRED` job — it applies only to jobs still `FAILED`; re-submit the expired ones (step 2).

## Symptom: 403, 413 or 503 `ingest_busy` from a call that used to work

These came with the 2026-10-25 hardening (see `BREAKING.md`):

- **`403 {"detail": "this operation requires an admin API key"}`** — `POST /keys/rotate`, `POST /sync`, `POST /backup/trigger`, `POST /maintenance/trigger`, `POST /collections/`, `GET /telemetry/*` and the MCP `rotate_key` tool need the default key (admin). A namespace key or `[namespaces]` token gets `403`. `a non-admin API key can only manage keys in its own namespace` means a key call named another namespace or asked for `admin`.
- **`400 path is unsafe: path-based ingest by this API key needs the operator to set [ingest].allowed_roots; send the content as inline documents instead`** — a non-admin key cannot ingest by path until the operator sets `[ingest].allowed_roots`; see [`50_ingestion_and_collections.md`](./50_ingestion_and_collections.md#letting-a-team-key-ingest-by-path). `outside_allowed_roots`, `denied_location` and `symlink_path` (the `path` itself is a symlink: send the real path) are the other reasons.
- **`413 request_body_too_large`** — the body exceeds `[server].max_request_body_mb` (default 32), refused before authentication. A file over `[ingest].max_file_mb` (default 100) is a `413 file_too_large`.
- **`503 ingest_busy` with `Retry-After`** — `[ingest].max_concurrent_tasks` (default 4) ingest jobs are already running; retry after the delay.
- **`collection name is not available to this namespace`** — the collection name belongs to another namespace; pick another name (admin included).
- **`/docs` or `/redoc` is `404`, `/openapi.json` is `401`** — set `[server].docs_enabled = true` to serve them without authentication, or send a Bearer token to `/openapi.json`.
- **Files under `node_modules`, `venv`, `site-packages` or a hidden directory are no longer indexed** by directory ingest.

## Symptom: ACL sidecar skipped — ingest returns warnings

`IngestResult.warnings` (or `GET /jobs/{id}` result) contains a message about an ACL sidecar. A `.acl` sidecar next to the document was skipped for exceeding the 64 KB limit (or is a symlink, is not UTF-8, or cannot be read); since 2026-10-25 the document is ingested **deny-all** (`acl: []`, no namespace can see it), not open — a declared ACL that cannot be used fails closed. Fix the sidecar and re-ingest to make the document visible.

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
