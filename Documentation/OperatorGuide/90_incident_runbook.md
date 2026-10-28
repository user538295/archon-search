**Purpose**: Triage steps for the failures that actually occur in production `archon-search` deployments, using only the existing endpoints, logs, and CLI.
**Audience**: SREs and sysadmins responding to an `archon-search` incident.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-07-29

# Incident Runbook

This runbook lists the failure modes the codebase produces today, each paired with concrete triage steps. The signal surface is narrow (see [20_monitoring_and_alerts.md](20_monitoring_and_alerts.md)), so many incidents resolve to "read the log, restart the service".

## Principles

1. **Start with `/health`, then `/status`, then the log.** Three commands cover the first decision branch in nearly every incident.
2. **Restart is a legitimate first step.** It clears LanceDB lock-file remnants and re-loads models. The `/route` path builds a fresh router per request, so router-cache staleness is not a reason to restart.
3. **Search pipeline errors surface as 5xx.** `POST /search` returns HTTP 500 on pipeline failure, HTTP 504 on timeout, and HTTP 503 when collection metadata is unreachable (see below — this was formalised in A3). HTTP 200 with `results: []` means the pipeline succeeded but matched nothing — not a failure signal.
4. **Re-ingest is the rollback.** There is no transactional repair.

## First-five-minutes checklist

```bash
# 1. Liveness (auth-exempt)
curl -fsS http://127.0.0.1:8765/health || echo "process down"

# 2. Readiness — storage, model warm-up, model validation, default models, and startup-sync status (auth-exempt)
curl -fsS http://127.0.0.1:8765/ready | jq

# 3. Status — per-collection progress, error counts, ETA (Bearer required)
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     http://127.0.0.1:8765/status | jq

# 4. Logs
tail -n 200 ~/.archon-search/logs/archon-search.log     # macOS
journalctl --user -u archon-search -n 200 --no-pager    # Linux
```

If `/health` is up and `/status` shows no rising `error_count`, look at telemetry (`GET /telemetry/stats`, admin key) and recent ingest activity. Logging locations and formats are documented in [30_logging.md](30_logging.md).

## Failure modes

### Stuck job (ingest, reindex or sync)

**Symptoms**: a collection sits at `status: "indexing"`, `processed_files` stops advancing, `eta_seconds` grows unboundedly, no log entries for that job for several minutes.

**Triage**:

1. `GET /jobs/{job_id}` for the suspect job. Job IDs surface in the response of `POST /ingest`, `POST /collections/`, `POST /collections/{name}/reindex`, `POST /sync`, and the `--wait` output of the matching CLI proxies.
2. If `status` is `RUNNING` or not yet started (`status: PENDING`) but log activity has stopped, `DELETE /jobs/{job_id}`. `202` with `status: "CANCELLING"` means the cancel was recorded: a not-yet-started ingest or reindex job ends `CANCELLED` without running once its task starts (a reindex whose embedding model is loading, once the load completes), while a `RUNNING` job is not interrupted and settles only when its work finishes — if that work is itself stuck, see step 5. An ingest job never stays not-yet-started without a task: one the maintenance loop re-enqueued after a failure (`source: "maintenance"`) gets its task when it is created — but that task first waits, without a time limit, for the collection's ingest lock, so cancelling such a retry while another job holds the lock leaves it `CANCELLING` until the lock frees, then it ends `CANCELLED` without running — and one left over from before a restart is rewritten `FAILED` `"never started; the server restarted before it ran"` when the server starts. A not-yet-started reindex left over from before a restart is rewritten the same way, `FAILED` `"never started; the server restarted before it ran"`: re-run `collection reindex` to apply it. Do not cancel a metadata-reindex or community-rebuild job an older server left not yet started (`status: PENDING`): no task runs it, so it stays `CANCELLING` — which blocks `DELETE /collections/{name}` of its collection — until the next restart completes the cancel (`CANCELLED`; `FAILED` `"process_restart"` before the cancelled-before-start entry of `BREAKING.md`) (tech-debt CON-11). A `QUEUED` export, import or migration job is not cancellable this way (`500` `"Unknown job status: QUEUED"`). A community-rebuild, metadata-reindex or sync job an older server left `QUEUED` is rewritten `FAILED` `"never started; the server restarted before it ran"` when the server starts, so `DELETE` answers the idempotent `200`.
3. Read the `DELETE` status code: `200` means the job was already terminal — inspect the final status. `409` `{"detail": "a sync cannot be cancelled"}` means the job is a sync (the startup sync or a `POST /sync`), which can never be cancelled — the job did **not** terminate; see the sync bullet below.
4. Re-issue the ingest. Terminal jobs are evicted after 7 days by the job store.
5. A process restart marks any in-flight (`RUNNING`/`CANCELLING`) job as failed via the crash-recovery path — except a job cancelled before it started (`status: PENDING` when the `DELETE` landed), which never ran and is completed as `CANCELLED` instead (no `process_restart`, no crash-loop guard). Use this if the asyncio task itself is unresponsive — `DELETE` only flips the stored status, so an unresponsive task will not honour cancellation and a restart is the actual recovery.
6. **Stuck sync** (`kind: "sync"`): `DELETE` answers `409`, so restart the server. A graceful stop (SIGTERM) cancels the sync and records it `CANCELLED`, and the next boot runs the startup sync again (unless it is already suppressed). If the process has to be killed instead (`kill -9`, OOM, a supervisor's SIGKILL), the `RUNNING` sync row reloads as `FAILED` `"process_restart"` — an ingest-family crash marker that **suppresses the startup sync on every boot** (`checks.sync: "warn"`, `sync_result: "suppressed"`) until a clean `POST /sync` / `archon-search sync`. See [140 — JobStore persist failures and `POST /sync`](../Architecture/140_error_handling_strategy.md#jobstore-persist-failures-and-post-sync) and the `checks.sync: "warn"` alert in [20_monitoring_and_alerts.md](20_monitoring_and_alerts.md#suggested-alert-rules).

Underlying causes typically logged in `archon-search.log`: parser failure on a specific document, model-load timeout, or disk-full on `db_path`. See [50_maintenance_and_jobs.md](50_maintenance_and_jobs.md) for the job lifecycle and the `FAILED`/`FAILED_EXPIRED` distinction (below).

### Failed-ingest retry exhaustion (`FAILED_EXPIRED`)

**Symptoms**: a job shows `status: "FAILED_EXPIRED"` in `GET /jobs?status=FAILED_EXPIRED` or in `/status`; it is never retried.

**Triage**: this is terminal, not a fault to clear. The `MaintenanceLoop` retries `FAILED` ingest jobs until either they exhaust `[maintenance].retry_max_attempts` (default 3) or age past `[maintenance].retry_max_age_hours` (default 72, counted from the original request), at which point they transition to `FAILED_EXPIRED` and are never re-enqueued (`jobs/maintenance_loop.py`). Three kinds are expired without any retry: an ingest job saved by an older server (it carries no stored request, so a retry could drop its scopes), one that failed after a client cancelled it (`DELETE /jobs/{id}`), and one that failed `process_restart` (it was running when the server died — check whether that file crashed the server before re-ingesting it). Read the original failure from the job record, fix the root cause (bad document, disk, model), and re-issue the ingest manually. Retry knobs live in [50_maintenance_and_jobs.md](50_maintenance_and_jobs.md).

### Watcher loop / churn

**Symptoms**: high CPU when no one is searching; `error_count` rising on a collection without an explicit ingest call; log spam from the watcher.

**Triage**:

1. Identify the watched paths from the `[collections]` block in `~/.archon-search/archon-search.toml`.
2. Check whether an external tool is rewriting files in those paths (editor autosave, indexer, syncthing).
3. Mitigate: set `[collections].watch = false` and restart, then trigger ingest manually via `archon-search sync`. There is no event-rate limiter today.
4. If the watcher is stuck (no events despite filesystem changes), restart the service. `watcher.py` exposes no health flag.

### LanceDB lock contention or "table busy"

**Symptoms**: `/search` surfaces a LanceDB lock/IO condition as either a `503` (metadata-lookup branch) or a `500` (pipeline path); or `archon-search start` fails to connect to the store; or two processes are running against the same `db_path`. Check the log for LanceDB lock/IO messages — do not rely on the HTTP status alone.

**Not a fault**: `POST /collections/` returning `503 {"detail": "store busy; retry in N seconds"}` with a `Retry-After` header, or `POST /ingest` returning `503 {"detail": "collection is busy with another write; retry after Retry-After seconds", "code": "store_busy"}` with `Retry-After: 30`, is the intentional signal that another write (an ingest, a delete, a reindex, a job being created) holds the per-collection lock. `POST /collections/{name}/reindex`, `POST /collections/{name}/reindex-metadata` and `POST /graph/{collection}/rebuild-communities` return that same `store_busy` body when they cannot take the collection lock in time (no job is created). `DELETE /collections/{name}` returns the same `store_busy` body on a lock timeout, and a `503` with `"code": "store_busy"` and a job-in-progress detail (`"job <id> is in progress; retry after it completes"`, or `"a job is in progress on this collection; retry after it completes"` for another namespace's job) while a job may still be writing to the collection: an ingest `RUNNING`/`CANCELLING`, a reindex not yet started (`status: PENDING`), `RUNNING` or `CANCELLING`, or an import, migration, metadata reindex (not a `--dry-run` one: it only reads) or community rebuild `RUNNING`/`CANCELLING`. The same `503` with the detail "an MCP ingest or a collection sync is in progress on this collection; retry after it completes" (checked after the job check) means an MCP `ingest_file` / `ingest_directory` call or a collection sync (the startup sync, `POST /sync` or a filesystem-watcher sync; a sync is registered for the one collection it is writing, not for its whole run) — writes that create no job, so `GET /jobs` does not list them — is running on the collection; it clears when that call returns, however long an `ingest_directory` of a large directory takes (a cancelled or failed call releases it too), so look for the MCP client or the watched directory's change burst, not for a stuck job. Such a job stuck `RUNNING`/`CANCELLING` blocks `DELETE` until it finishes or the server restarts — on load the job store rewrites `RUNNING`/`CANCELLING` jobs to `FAILED` (`error: "process_restart"`; a job cancelled before it started (`status: PENDING` when the `DELETE` landed) becomes `CANCELLED` instead), and a reindex left not yet started (`status: PENDING`) by a restart to `FAILED` (`error: "never started; the server restarted before it ran"`), so after a restart none of them blocks `DELETE`. A server ERROR `JobStore: job <id> has the unclassified kind <Kind>; treating it as writing collection '<name>' until it is terminal` means that job's kind is missing from the `DELETE` guard's classification (`jobs/store.py`, a bug to report): `DELETE` of that collection refuses until the job ends. Honour `Retry-After` and retry; ingest to a *different* collection is unaffected. An import with `--force-overwrite` applies the same check when its job runs: instead of a `503` it ends `FAILED` with `force_overwrite refused: job <id> is in progress; retry after it completes` (or `…: a job is in progress on this collection; retry after it completes`, or `…: collection is busy with another write; retry after it completes` on a lock timeout), drops nothing, and is resumed with `POST /jobs/{id}/resume` once the other job is terminal. Treat a sustained store-busy window like a stuck reindex (above).

**Triage**:

1. Confirm only one server is running: `pgrep -af archon-search`. Two processes against one `db_path` is a hard fault — stop the older one.
2. Check disk and inotify headroom: `df -h` on the partition holding `db_path`.
3. A stale lock file after an unclean shutdown is cleared by a restart. There is no manual lock-clearing tool.
4. As a last resort, restore `search/` from backup — see [40_backup_restore_disaster_recovery.md](40_backup_restore_disaster_recovery.md).

### Search returns HTTP 500 / 503 / 504

**Symptoms**: `POST /search` returns 500, 503, or 504 instead of results — or returns nothing at all and the client times out.

**Diagnosis** (log strings and telemetry statuses verified against `server/routes_search.py`):

- **HTTP 500** — a pipeline stage failed (embedder, store query, or reranker). The server logs at ERROR with `event_type="search_pipeline_failure"` and message `search pipeline failed: <ExceptionClass>` (full traceback attached). Telemetry entry: `endpoint="search"`, `status="internal_error"`. The body is plain-text `Internal Server Error`, not JSON.
- **HTTP 500 with `{"detail": "search failed"}`** — a `hyde=true` request whose HyDE generation failed for a reason other than the missing provider package (that one is a `422`), e.g. embedding the hypothesis. The server logs it like a pipeline failure — `search pipeline failed: <ExceptionClass>` at ERROR with `event_type="search_pipeline_failure"` and the traceback; telemetry entry `status="internal_error"`. `POST /explain` answers the same failure with `{"detail": "explain failed"}` and logs `explain failed for <collection>` (`multi-collection explain failed` on a fan-out). Check the HyDE provider (`[hyde]` config, the LLM server it calls) and the embedder; retrying with `hyde=false` confirms the cause.
- **HTTP 504** — the pipeline call timed out (>30 s; with `hyde=true` the response can arrive ~60 s after the request, as HyDE's own ~30 s cap (S582) runs before the search budget). ERROR record with `event_type="search_timeout"`, message `search pipeline timed out`. Telemetry entry: `status="timeout"`. A cold cross-encoder is **not** a cause: since S184 the handler warms the reranker before the timer starts, so a 504 means the search itself was slow (store contention, an oversized candidate pool, CPU pressure) rather than a one-off model load.
- **No response at all** (client-side timeout, "connection failed", `status 0`) — not in the 500/503/504 family, and the most likely cold-start symptom. The warm-up above sits *outside* the timer, so a request that pays an unbounded model load returns nothing until the build completes instead of degrading to a `504`. Since **S281** an ingest job waits for warm-up before reporting `DONE`, so `collection add --wait` / `ingest --wait` no longer hand back a server that hangs on the next search; since **S288 (import)** an import job does too (warming the imported collection's embedder), so `import --wait` no longer hands back a collection whose first search silently runs FTS-only. **On `POST /search` (and the MCP `search` tool and the `/v1` shim, single-collection and fan-out alike) both warm-up legs are now bounded** — the cross-encoder at 0.5 s (**S286**/**S288**) and the query embedder at 0.5 s (**S290**) — so a cold model degrades the *ranking* instead of the *response*: the log carries `warm-up: reranker still cold after waiting 0.5s; this search skips reranking` (results keep `reranker_score: null`, ordered by the fused vector+FTS ranking) and/or `warm-up: embedder still cold after waiting 0.5s; this search runs FTS-only` (results ranked by the FTS leg alone, no vector leg; RAG Fusion, `graph_mode` **and `hyde`** are skipped for that request — a `hyde=true` search comes back `hyde_applied: false` with `expansion_warning: "HyDE expansion failed"`, because generating the hypothesis would use the same cold embedder), both at WARNING, and both builds continue in their worker threads so the next requests get the full pipeline back. **S299** bounds the stage before those two the same way: resolving the request's per-collection embedder from the embedder cache (`POST /search` only) waits at most 0.5 s and then degrades to `fts_only` as well, logging `search: embedder '<model>' unavailable after 0.5s (cache load still in flight) — serving this request FTS-only while it completes` — previously that resolution waited 30 s and then returned a `503 embedder_not_ready`. This one is *not* a model load (the cache hands back a lazy backend; the build is the two warm-up legs above), so a burst of this line means the cache handoff itself is slow — the request is queued behind a saturated thread-pool executor, or parked as a deduplicated waiter behind another caller's in-flight load. The load is shielded, so it finishes and populates the cache for later requests; only a load wedged past the cache's own 120 s dedup wait still produces the `503`. Search results during that window are *correct but weaker* — treat a burst of those WARNINGs right after a restart as a relevance blip, not an outage. **S582** closes a second, unrelated source of the same symptom: with `[hyde] enabled = true`, the provider call itself is bounded only by `[hyde].timeout_seconds`, which is accepted unclamped, so an LLM server that accepts the connection but never answers (a llama-server still loading its model) used to hold the request open for that whole duration; on every HyDE surface — `POST /search`, `POST /explain` and the MCP `search`, `search_with_context` and `explain` tools — it is now bounded by HyDE's own ~30 s cap (applied before the search budget on the two search surfaces; `search_with_context` and both `explain` surfaces have no search budget) and degrades to `hyde_applied: false` (on the search surfaces with `expansion_warning: "HyDE expansion failed"`), logging `HyDE generation exceeded the 30.0s search budget; searching without it` at WARNING, and a cold embedder skips HyDE on `POST /search` and the MCP `search` tool, the surfaces that degrade to FTS-only (on the others HyDE is not skipped: their retrieval embeds on the cold model anyway, unbounded, so the build is waited on once). The exposure that remains is on endpoints with no warm-up bound of their own — most notably `POST /explain`, whose retrieval neither warms the models nor runs under the ~30 s budget — issued without waiting for `GET /ready` to return `200`. Confirm with `GET /ready` (`checks.models: "pending"`) and the absence of any `search pipeline failed` / `search pipeline timed out` record; the fix is to gate clients on `/ready`, not to raise the client timeout.
- **HTTP 503** — collection metadata could not be reached (body `{"detail": "service unavailable: metadata store could not be reached", "code": "metadata_store_error"}`). ERROR message `search: meta lookup failed for collection ...` (`multi-collection search: metadata lookup failed` for a `collections` fan-out), with the traceback. **No telemetry entry is emitted** — triage as a store connectivity issue (see LanceDB lock contention above).
- **HTTP 200 + `results: []`** — success, no matches. Not a failure.

**Triage**:

1. Confirm the collection is in the caller's namespace: `GET /collections/`.
2. `GET /status` and `GET /indexing-state` — look for `status: "failed"` or a non-zero `error_count`.
3. Grep the log for `search pipeline failed`, `meta lookup failed` and `HyDE resolution failed` (`search:` / `explain:` prefix).
4. If telemetry is on, `GET /telemetry/entries` and filter `endpoint="search"` with `status="internal_error"` or `status="timeout"`.
5. Verify the store is reachable and the embedding model is loaded (`GET /ready` → `checks.storage` / `checks.models`). Also check `checks.sync`: `"pending"` means the lifespan's startup collection sync is still (re)building the index — `/ready` answers 503 and search results may be incomplete until it flips to `"ok"`. This is expected on first boot against a large corpus and is not itself a fault. `checks.sync: "fail"` means the startup sync either raised an exception or completed but recorded per-collection errors in `SyncResult.errors` (e.g. a missing collection path or an ingest failure) — unlike `"pending"`, this does NOT make `/ready` answer 503 (a corrupted collection must not wedge readiness forever), so it is easy to miss; grep the log for `"startup sync failed"` or `"startup sync completed with"` and re-run `POST /sync` (or `archon-search sync`) once the underlying cause (missing path, bad config) is fixed. A third case reads `checks.sync: "warn"` — also a 200, also easy to miss: when the previous process died mid-ingest (OOM, `kill -9`, machine reset — including inside the previous boot's own startup sync, which runs under a `SyncJob` so it leaves the same trace) the job store marks that job `FAILED / "process_restart"` and the lifespan's crash-loop guard **suppresses** the automatic startup sync so the server cannot re-enter the workload that killed it on every restart. Suppression is **sticky**: it persists across process restarts (a sentinel file, `archon-search-sync-suppressed.json`, under the data dir, survives the reboot) so it keeps firing on every boot after the crash, not just the first one — the index is whatever the crash left behind. `GET /status` reports `sync_result: "suppressed"` and the log carries `"startup sync SUPPRESSED"` at WARNING. The crash marker itself is **namespace-independent** — `crashed_ingest_on_load` scans every loaded job across every namespace — while `GET /jobs` is **namespace-filtered**, so a crashed tenant ingest in, say, namespace `acme` suppresses the operator's `default` sync too, and an operator who only checks `GET /jobs` in their own namespace finds nothing there that explains the `"warn"`. Look across **all** namespaces for the `FAILED / "process_restart"` job before concluding the cause is a mystery — query `GET /jobs` with each namespace's API key, or inspect the on-disk job ledger (`~/.archon-search/archon-search-jobs.json`, which is not namespace-scoped) directly. This matters more now that suppression is sticky, since the offending job may be from several boots ago. Fix the underlying cause first, then resume with `POST /sync` / `archon-search sync` — a clean run moves `sync_result` to `"done"` and `checks.sync` back to `"ok"`.
6. Restart if pipeline stages are in a bad state.

**Escalation**: if 500s or 504s persist after a restart, file an issue with the full ERROR record, the `GET /status` snapshot, and `archon-search --version`.

### Provider / model validation failing (`/ready`, `/status`)

**Symptoms**: `GET /ready` reports `checks.models: "fail"` or `"warn"`; `GET /status.model_validation` shows `embedder_ok: false` or `reranker_ok: false`, or a populated `provider_warnings` list.

**Triage** (D6, verified against `server/routes_ready.py`):

- The model probe runs in the background at startup and **never blocks boot or raises** — the server accepts requests while the probe is `pending`, though `GET /ready` answers `503` until model warm-up and the probe both finish (the probe is bounded as listed under [`validation_timeout_seconds`](../UserManual/30_configuration.md#database); a timeout reports `checks.models: "fail"`). A failed warm-up reports `checks.models: "fail"` at once and ends the wait even while the probe still runs; `/ready` then stays `503` only while the default embedder or a configured reranker is still cold, and a background retry (5 s doubling to 300 s, log line `model warm-up recovered: the default models are warm`) brings it back to `200` on its own once a transient cause is gone.
- `checks.models` priority is strict: model warm-up **PENDING** > model warm-up **FAILED** > `model_validation` **FAIL** (an embedder or reranker model could not load) > **WARN** (both loaded, but a provider fallback warning was emitted) > **OK**. `pending` means model warm-up is still running or the probe has not produced a result yet. Only `pending`, and a failed warm-up that left the default embedder or a configured reranker cold, change the HTTP status (`503`); every other `fail` and every `warn` still answers `200`. In a container the Docker `HEALTHCHECK` probes `/ready`, so a replica stuck in that state turns `unhealthy` once its retries run out (after the 600 s `start-period`).
- **FAIL from `model_validation`** → the configured `embedding_model` / `reranker_model` cannot be loaded (unsupported name, weights missing and no network or `HF_HUB_OFFLINE` set, damaged files; see [Model weights missing, offline or damaged](#model-weights-missing-offline-or-damaged)). `provider_warnings` reads `embedder probe failed; see the log for details` and/or `reranker probe failed; see the log for details`; the server log has the cause. Fix the model name in `[database]` or restore the weights, then restart. A **timeout** also reads FAIL even though the models may be fine: `provider_warnings` carries `validation timed out after <N>s` with both `embedder_ok` and `reranker_ok` false. On a merely slow host (first model download, CPU-only), raise `[database].validation_timeout_seconds` (default 60) and restart. Until a model loads, search answers `200` but degraded: FTS-only while the embedder is unloadable, unreranked fused ranking while the cross-encoder is.
- **FAIL from a terminally-failed model warm-up** (warm-up runs on every startup, whatever `eager_load_embedders` is set to) — the task raised, timed out, or finished with a model that could not load/warm; the WARNING `"model warm-up failed: <models>"` names the failed model(s) — on a timeout it reads `model warm-up failed: timed out after 600.0s`, suffixed `; still cold: <models>` when any covered model is still cold. Reported *before* `model_validation` is even consulted, so `checks.models: "fail"` can appear even when `model_validation` itself is clean. Search does not 500: every search retries the cold model with a short bounded wait and degrades meanwhile (FTS-only without the embedder, no reranking without the cross-encoder). A transient cause (download error, network timeout) heals on a later query or by the background retry; a deterministic one (bad model name, disk full, `HF_HUB_OFFLINE=1` without cached weights) keeps every search degraded, and `/ready` at `503` when the cold model is the default embedder or a configured reranker, until fixed — check whether the named model also appears in a `model_validation` FAIL. A model-download failure also logs `model download failed for '<repo>': <ExcClass>: <message>` at WARNING (URL credentials, query strings and tokens are scrubbed from the message), which names the real cause (a 401, a proxy error, a full disk, a read-only cache).
- **WARN** → both models loaded, but `provider_warnings` is non-empty. Read the list — it has four sources, and they need different responses. (a) A **device step-down warning** (`"device unavailable: <requested>; models loaded on cpu"`): `[database].device` names `mps` or `cuda` but this host has no such device (a CPU-only PyTorch build, a missing NVIDIA driver, a different machine than the wizard ran on), so the embedder and reranker loaded on CPU — search works, on the slower device, and the server log carries one `device '<requested>' is not available on this host; using cpu` WARNING per process (`torch_runtime.py`). `auto` never produces this warning: it silently picks the best device present. Set `[database].device = "auto"` (or `"cpu"` to silence it), or fix the PyTorch/driver install and re-run `archon-search wizard`, then restart. (b) A **graph engine warning** when `[graph].enabled = true` — `"graph prose entity extraction is disabled: gliner is not installed. Install archon-search[graph] to use graph prose entity extraction"`; the exact install command for this install method is logged at WARNING in the server log (`graph prose entity extraction is disabled: gliner is not installed. Install the graph extras: <install command>`). This one is defensive rather than routine: `create_app()`'s `_check_graph_deps` already refuses to boot in that state, so a *running* server rarely shows it. (c) `"graph NER model presence could not be determined; see the log for details"` — the probe itself failed unexpectedly; it never raises, so a surprise reads as "cannot determine". (d) An **extraction-model probe warning**, only when `[graph].extraction_model` is set — either a timeout (the "this looks like a reasoning model" signal) or a plain misconfiguration; see [60_graph_operations.md](60_graph_operations.md#opt-in-llm-enrichment). Every graph cause here degrades prose entity/relation extraction at ingest and **never** affects search. Fix (b) with the install command from that log line (`pip install 'archon-search[graph]'` in a pip install) or `archon-search wizard` — [60_graph_operations.md](60_graph_operations.md#provisioning-the-prose-extraction-model). HyDE / RAG-Fusion provider reachability never appears here; read the `hyde` / `rag_fusion` sub-objects on `/status` instead.

- **Two graph conditions deliberately do NOT appear in `provider_warnings`.** (1) A **failed checkpoint load** — the model artifact is fetched lazily on first ingest, and a load failure latches per process and degrades each document; the startup probe checks package importability only, so grep the server log for `"prose extraction backend failed to load"` instead. (2) A **device step-down** — the wizard's two-stage device probe settles `[graph].device` against the pre-warmed model at *provision* time; when an accelerator was requested (see [Step 8 of the wizard guide](../UserManual/20_wizard.md#step-8--model-download) for what counts as requested) but the pre-warm landed the model on CPU, or the graph pre-warm produced no device at all (it failed, or the pre-warm time limit skipped it), it logs `"Graph accelerator not validated (<reason>) — writing [graph].device = 'cpu'."` at WARNING and writes `device = "cpu"` without prompting. At load time an explicit `mps`/`cuda` this host lacks only steps down to CPU with a server-log WARNING (`device '<requested>' is not available on this host; using cpu`, `torch_runtime.py::resolve_device`), so a step-down that first appears later (driver removed, PyTorch build swapped) shows up only in that log line until you re-run `archon-search wizard`. There is no `provider_notes` field any more, and nothing replaced it: every entry in `provider_warnings` is operator-actionable by construction, because `checks.models` grades that field alone.

### Model weights missing, offline or damaged

**Symptoms**: `checks.models` is `fail` (see above), ingest jobs go `FAILED`, or searches degrade to FTS-only / unreranked, and the server log names a model: `could not fetch model '<org>/<name>': <reason>` with one of `offline mode is on and the model is not in the local cache`, `download failed; check the network connection and free disk space`, or `the downloaded snapshot is missing required files` (`model_store.py`).

**Cause**: embedder and reranker weights are PyTorch files fetched from Hugging Face at a pinned revision into `<data dir>/models` (hub layout, `models--<org>--<name>/snapshots/<revision>/`) on first use or by the wizard's pre-warm. A snapshot counts as present only when `model.safetensors`, `config.json` and `tokenizer.json` are all there; otherwise the next load downloads again. With `HF_HUB_OFFLINE=1` in the server's environment a missing model is an error instead of a download.

**Triage**:

- Offline host or `HF_HUB_OFFLINE` set: copy the `models--<org>--<name>` directory for each configured model from a host that has it into `<data dir>/models`, or unset `HF_HUB_OFFLINE` and let the server download.
- Download failed: check network access to Hugging Face and free disk space under `<data dir>/models`, then restart or re-run `archon-search wizard` to pre-warm.
- Damaged or half-copied files: delete that model's `models--<org>--<name>` directory and restart so it is downloaded again.
- `unsupported model '<name>'; supported: ...`: the configured `embedding_model` / `reranker_model` is not in the supported registry (`archon_search/model_specs.py`; the parity lane's `tests/fixtures/runtime_parity/README.md` lists what was verified). The server still starts (`GET /health` is `200`) but `GET /ready` is `503` with `checks.models: "fail"`, and the log carries this text as a WARNING at start and again when warm-up fails. On the API an unsupported collection embedding model is rejected with `422`. Pick a name from the `supported:` list and restart.
- Upgraded from a fastembed-based release: the old `models--Qdrant--*` / `models--Xenova--*` ONNX directories in `<data dir>/models` are no longer used and can be deleted; every model is downloaded again in the new format (see [Upgrading](100_upgrading.md#pytorch-runtime-upgrade)).

### Graph search failures

Graph search (`graph_mode`) requires `[graph].enabled = true`. See [60_graph_operations.md](60_graph_operations.md) for building and maintaining graphs.

- **`local` / `global` before communities are built** → `POST /search` returns **422** with `{"code": "graph_communities_not_built", ...}` (`GraphCommunitiesNotBuiltError`). Build communities first: `POST /graph/{collection}/rebuild-communities` or `archon-search graph build-communities <collection> --wait`.
- **`ppr` with no matching entities** → not an error. The PPR walk falls back to a normal hybrid search and returns `ppr_entities_matched: 0` in the response (`pipeline._search_ppr_mode`). If you expected graph-weighted results, the query terms did not match any extracted entity — re-ingest to populate the graph, or broaden the query.
- **Community rebuild job ends `FAILED` with `community detection requires the archon-search[graph] extra; see the server log for the install command`** → the Leiden libraries are optional and imported lazily inside `community_builder.py`; a missing install surfaces only when a rebuild runs (it never blocks startup). The job record carries only that fixed text; the server log has a WARNING `_community_rebuild_task: job <id> failed: leidenalg is required for community detection. Install it with: <command>`. Install the graph extra with that logged command: `pip install 'archon-search[graph]'`, or in a `uv tool` install a `uv tool install` command that keeps the recorded extras (a plain `uv tool install 'archon-search[graph]'` with a `#` note to repeat the original install options when the receipt cannot be reproduced). Then re-run the rebuild. If the extras disappeared after `uv tool upgrade archon-search`, see [Optional extras](../UserManual/10_installation.md#optional-extras).
- **Graph has code symbols but no prose entities; ingests succeed with a warning** → the prose extraction checkpoint could not be loaded. Ingest degrades rather than failing: chunks embed and persist, code-symbol nodes are written, prose extraction is skipped, and `IngestResult.warnings` carries one of three sanitized notices (`graph_extractor.py`) — which one tells you what to do:
  - `"graph prose extraction model is unavailable; prose entity extraction is disabled for this ingest ..."` — the checkpoint failed to load. Usual cause on a fresh install is no network reachability to HuggingFace with a cold cache. This **latches for the process lifetime**, so restart after fixing it. The server logs two WARNING lines, each **once per process** (not once per file): `Failed to load graph NER model '<name>': <reason>` (no traceback, never repeated) and `GraphExtractor: prose extraction backend failed to load` (with the traceback); later documents log only `GraphExtractor: prose extraction backend is unavailable (latched load failure)` at DEBUG — so grep the log from process start. Exception: a non-`Exception` failure such as `SystemExit` is not latched, so it is retried and logs the `GraphExtractor` WARNING on every prose document.
  - `"graph prose extraction model is still loading for another document; ..."` — a concurrent ingest was waiting on someone else's first load and gave up. Transient and **not** latched: the next document retries. If you see it repeatedly, the first load itself is slow (cold ~1.2 GB download) — pre-warm with `archon-search wizard`.
  - `"graph prose entity/relation extraction failed; ..."` — the model loaded but the forward pass raised. Check the log for the traceback; this is not a provisioning problem.

  `GET /status` → `model_validation.provider_warnings` will **not** show any of these — the startup probe checks `gliner` importability only, never the checkpoint. Remedy for the first: restore network access and restart, or seed the cache by hand for an air-gapped host ([60_graph_operations.md](60_graph_operations.md#provisioning-the-prose-extraction-model)). Re-ingest afterwards — degraded documents are not retroactively extracted.
- **Startup WARNING listing orphan graph tables** → after upgrading, pre-namespacing `_archon_graph_{col}_*` tables (single-underscore separator) are orphaned by the namespaced `_archon_graph_{ns}__{col}_*` scheme. The startup scan logs a WARNING naming them (`check_and_warn_legacy_graph_tables`, never raises). Delete them manually once you have confirmed the collections re-graphed correctly.

### Telemetry log explosion

**Symptoms**: disk usage growing under `~/.archon-search/search-logs/`; many JSONL files older than `retention_days`.

**Triage**:

1. Confirm telemetry is on: `GET /telemetry/stats` (admin key) returns numbers, not `{"enabled": false}`.
2. Confirm `[telemetry].retention_days` (default 30).
3. The pruner runs once immediately when its background task starts, then every 24 h. Restarting triggers another prune. Today's file is intentionally exempt from deletion.
4. To free space immediately: `archon-search stop`, delete the oldest JSONL files by hand (you must exclude today's file yourself — the pruner exemption does not protect manual deletion), then `archon-search start`.
5. If `skipped_lines > 0` on `/telemetry/stats`, a line failed schema validation — inspect the day's file (malformed lines are logged at WARNING).

To turn telemetry off without losing data: set `[telemetry].enabled = false` and restart. Existing JSONL files remain; reader endpoints then return `{"enabled": false}`.

### `export_enabled = true` quietly does nothing

**Symptoms**: operator sets `[telemetry].export_enabled = true` expecting remote shipping; nothing happens.

**Triage**: this is a known gap, not a bug. The config loader logs a warning at startup and coerces the value to `false` (`config.py`); no external transmission occurs. Remove the line or accept the no-op.

### Key file lost or compromised

Key rotation is owned by [70_key_management_and_rotation.md](70_key_management_and_rotation.md) — the summary below is the incident-response path.

**Symptoms**: clients receive `401 Unauthorized`; the key file is gone; or a key has been published.

**Rotate (live, no restart)**:

```bash
archon-search key rotate --grace 60s   # duration string; POST /keys/rotate takes integer grace_seconds
```

1. The new raw token prints to stdout once — record it immediately.
2. **In-flight 401s are expected during rotation.** With `[auth].rotate_grace_seconds > 0` (or a `--grace` window), the old key keeps working until the grace window expires, so clients can be updated without a hard cutover; with grace `0` the old key is rejected at once. Set `[auth].rotate_grace_seconds` to cover your slowest client's update cadence.
3. Update every client (CLI users, MCP integrations, monitoring scraper) before the grace window closes.

**When `ARCHON_SEARCH_API_KEY` env var is set**: `POST /keys/rotate` returns **409** (`Cannot rotate: ARCHON_SEARCH_API_KEY env var is set...`) because the env var always overrides `.search.env` in the running process. Rotate by changing the env var and restarting instead.

`POST /keys/rotate` requires a JSON body (`KeyRotateRequest`; the integer `grace_seconds` field is optional). FastAPI validates the body **before** the env-var check runs, so a plain `curl` with no body returns **422** (`{"loc": ["body"], "msg": "Field required"}`), not the 409 above — send an empty body and `Content-Type: application/json` to reach the env-var check:

```bash
curl -i -X POST http://127.0.0.1:8765/keys/rotate \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

**Key file lost entirely**: on next start, `key_manager.py` mints a fresh key and **all existing clients break**. For emergency restoration set `ARCHON_SEARCH_API_KEY=<known-value>` in the service environment and restart — the env var takes precedence over the file. The value must be lowercase hex of at least 32 characters, or the server refuses to start (see [Server exits at startup with `ConfigError`](#server-exits-at-startup-with-configerror-about-a-key)). This first-start recreation is the only key path that requires a restart.

### Migration stuck or schema-migration job failing

**Symptoms**: `POST /collections/{name}/migrate` job sits in `RUNNING`, or `GET /collections/{name}/migrations/pending` reports pending migrations after an upgrade.

**Triage**: schema migrations that are not applied at startup must be run explicitly after upgrading (`POST /collections/{name}/migrate`, 202 job — or `archon-search collection migrate`). A stuck migration job is triaged exactly like any stuck job (above): inspect `GET /jobs/{id}`, cancel if hung, fix the underlying cause, re-run. The full upgrade and migration procedure — including which migrations are startup-applied vs operator-run — lives in [100_upgrading.md](100_upgrading.md) and the [../MigrationGuide/](../MigrationGuide/05_data_migration.md).

### Service will not start

- **macOS**: `launchctl load ~/Library/LaunchAgents/com.archon.search.plist`, then read `~/.archon-search/logs/archon-search.log`.
- **Linux**: `systemctl --user status archon-search`, then `journalctl --user -u archon-search -e`.
- **Port already in use**: `lsof -i :8765`; stop the conflicting process or set `[server].port` with `archon-search config set server.port N` (a fresh wizard install does not write the key; there is no `--port` run flag — use the config key or `ARCHON_SEARCH_PORT`).
- **Exit status after a stop**: a server stopped with `SIGTERM` (`docker stop`, `systemctl stop`, `launchctl stop`) ends by the signal, so a shell or `docker inspect` shows exit code **143** (`-15` in a parent that reads the raw wait status). That is a clean stop, not a crash; it takes well under a second, and a SIGINT during warm-up takes up to about 3 s while the in-flight model load finishes. See [10_deployment_topologies.md](10_deployment_topologies.md#process-supervision-options).

### Server exits at startup with `ConfigError` about a key

**Symptoms**: `archon-search serve|start|install` exits 1 with one line `Error: …` (no traceback), or the service crash-loops.

- `ARCHON_SEARCH_API_KEY is not a valid API key`: the env var is set but is not lowercase hex of at least 32 characters. Unset it, or set a value from `python -c "import secrets; print(secrets.token_hex(32))"`. It no longer falls back to the key file.
- `<key file> holds an invalid ARCHON_SEARCH_API_KEY: …`: the key file has a short or non-hex value (a hand-written or old key), cannot be read, or is not valid UTF-8 text (the message then ends `it is not valid UTF-8 text`; the file's bytes are never echoed). The file is left untouched; fix its value, encoding or permissions, or delete it to have the server generate a new key (clients must then be rekeyed).
- `[namespaces]` token problems (shorter than 32 printable ASCII characters, whitespace, or equal to the default key): the error names the namespace, never the token. Replace the token in `archon-search.toml`.
- `[database].providers was replaced by [database].device` and similar removed-key errors after an upgrade: see [100_upgrading.md](100_upgrading.md#pytorch-runtime-upgrade).

### Clients refused: `403`, `413`, `503 ingest_busy`, `path is unsafe`

- **`403 this operation requires an admin API key`** — the caller used a TOML `[namespaces]` token or a non-admin managed key on `/keys/rotate`, `/sync`, `/backup/trigger`, `/maintenance/trigger`, `/telemetry/*` or `POST /collections/`. Use the default key, or a managed key created with `admin`. `archon-search sync|backup|maintenance` read the default key from the key file.
- **`413` `request_body_too_large`** — the body exceeds `[server].max_request_body_mb` (default 32). Send smaller batches of inline documents, or raise the setting (and any proxy limit).
- **`503` `ingest_busy` with `Retry-After`** — `[ingest].max_concurrent_tasks` (default 4) ingest tasks are already running; the request was not queued. Retry after the header's delay, or raise the cap if the host can take it. It is not a fault.
- **`400 path is unsafe: denied_location` / `outside_allowed_roots` / `allowed_roots_required`** — the ingest path is a credential or server-state location (data directory, `.ssh`, `.env`, `*.pem`, `/etc/shadow`, …), is outside `[ingest].allowed_roots`, or a non-admin key sent a path while `allowed_roots` is empty. Add the directory to `[ingest].allowed_roots`, ingest it with the admin key, or send inline `documents`.
- **A foreign collection name**: an ingest job `FAILED` (REST) or `{"code": "collection_unavailable"}` (MCP) with `collection name is not available to this namespace` means another namespace owns that name. Pick another name.
- **`400 outside_allowed_dirs` on import / `422 archive_namespace_mismatch`** — the archive is not in the caller's own `exports/<namespace>/` or `<backup>/<namespace>/`, or its manifest names another namespace. See [40_backup_restore_disaster_recovery.md](40_backup_restore_disaster_recovery.md#single-collection-from-a-targz-archive).

### Process killed (OOM) or memory growth

**Symptoms**: the process or container disappears with exit 137 / `Killed`, or RSS climbs. **Triage**: check the startup log for `estimated model memory … exceeds the cgroup memory limit` (the server's own estimate of fp32 weights × 1.5 + 1536 MB against the container or host limit), then choose a smaller profile or `embedding_model`/`reranker_model`, lower `[database].embedder_cache_size`, or raise the limit. Peak startup memory and steady-state figures are in [80_capacity_and_performance.md](80_capacity_and_performance.md#memory-threads-and-the-accelerator). A crash mid-ingest makes the next start suppress the startup sync (`checks.sync: "warn"`): fix the cause, then `POST /sync`.

## Escalation

There is no on-call rotation for a local service. Escalation is reproduction plus an issue containing: the relevant log excerpt, the `GET /status` snapshot, `archon-search --version`, and the reproduction steps.

## Related documents

- [00_index.md](00_index.md) — OperatorGuide table of contents.
- [20_monitoring_and_alerts.md](20_monitoring_and_alerts.md) — what to alert on so these runbooks fire predictably.
- [30_logging.md](30_logging.md) — log locations, formats, and levels.
- [40_backup_restore_disaster_recovery.md](40_backup_restore_disaster_recovery.md) — restore steps referenced from LanceDB lock contention.
- [50_maintenance_and_jobs.md](50_maintenance_and_jobs.md) — job lifecycle, retry knobs, `FAILED_EXPIRED`.
- [60_graph_operations.md](60_graph_operations.md) — building and maintaining graphs and communities.
- [70_key_management_and_rotation.md](70_key_management_and_rotation.md) — full key issuance and rotation procedures.
- [100_upgrading.md](100_upgrading.md) — upgrade and schema-migration procedure.
- [../UserManual/160_troubleshooting.md](../UserManual/160_troubleshooting.md) — user-side troubleshooting overlap.
