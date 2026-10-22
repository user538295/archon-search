# BREAKING CHANGES

## Compatibility Policy

`archon-search` uses CalVer (`YY.M.<commit-count>`). CalVer segments encode **time only** — they do not signal compatibility. This file IS the compatibility contract.

**Rule**: every release that removes or changes an existing API contract MUST add an entry here describing: what changed, the migration path, and from which release the deprecated form was announced. Consumers should subscribe to changes in this file, not interpret CalVer segments.

## Changelog

### [next release] — `DELETE /collections/{name}` returns `503 store_busy` while any job may still write the collection, not only an ingest; the refusal's `detail` is reworded; reindex jobs report their `collection` (2026-10-21)

**Surface**: `DELETE /collections/{name}` status and body (a config-only collection included); the `collection` of a reindex job in `GET /jobs`, `GET /jobs/{id}` and the `POST /collections/{name}/reindex` `202` body; the `status` / `error` of a reindex job found `PENDING` when the server starts; `POST /collections/{name}/reindex`, `POST /collections/{name}/reindex-metadata` and `POST /graph/{collection}/rebuild-communities` (new `503` `store_busy`, `404` when a `DELETE` drops the collection meanwhile, and a `409` duplicate-job check that now also catches a concurrent submission); `POST /collections/{name}/reindex` on a server that is not ready (`503` before any `404` or `409`), and `POST /collections/{name}/reindex-metadata` and `POST /graph/{collection}/rebuild-communities` on a server without a search store (`503` where a `500`).

**Behaviour-only change — no schema field was added, removed or retyped.** A metadata-reindex job's row in the job store file gains one key, `dry_run`; `GET /jobs` does not serve it. OpenAPI's `503` description for `DELETE /collections/{name}` now names the job kinds, that of `POST /collections/{name}/reindex` adds the store-busy case, and `POST /collections/{name}/reindex-metadata` and `POST /graph/{collection}/rebuild-communities` now declare a `503` (`ErrorDetail`) covering both the store-busy and the service-not-ready case.

**What changed:**

- **More jobs block `DELETE`**: the `503` `store_busy` refusal covered only a base ingest job `RUNNING`/`CANCELLING` on the collection; a `DELETE` racing any other job dropped the collection, and the job's later writes re-created its table or meta row. It now also refuses while, on the collection in any namespace, a reindex job is `PENDING`/`RUNNING`/`CANCELLING` (a `PENDING` reindex is loading its embedding model, holding no lock), or an import, migration, community-rebuild or non-dry-run metadata-reindex job is `RUNNING`/`CANCELLING` (`COLLECTION_WRITING_JOB_STATUSES`, checked by `JobStore.find_active_collection_job` in `archon_search/jobs/store.py`). Never blocking: a dry-run metadata reindex (`"dry_run": true`; it only reads, so its job never refuses a `DELETE` — but its scan holds the collection lock, which a `DELETE` waits for and answers the lock-timeout `503` if the scan outlasts `INGEST_LOCK_TIMEOUT_S`), a `QUEUED` import or migration, a `PENDING` base ingest (the collection lock already covers it), export jobs, sync jobs and legacy delete jobs (`NEVER_BLOCKING_JOB_KINDS`). A job kind in neither list blocks while it is not terminal (logged at ERROR), so a future kind refuses the `DELETE` rather than lose its collection.
- **Config-only collections are guarded too**: `DELETE` of a collection that is registered in the config but has no meta row yet (its first ingest has not written one) removed the config entry without taking the collection lock or checking jobs, so a running first ingest then re-created the table and meta row. It now takes the lock (when a search store exists), reads the meta row again under it and applies the same job check first: it can answer the lock-timeout or job-in-progress `503` `store_busy` where it always answered `200`. When a first ingest wrote the meta row while the `DELETE` waited for the lock, the collection is now dropped like any other (table and meta row) where only its config entry was removed; when another namespace's meta row now hides the name, it answers `404`. It also ends the collection's `FAILED` and `PENDING` base ingest jobs `CANCELLED` (as the drop of a collection with a table does, see the failed-ingest retry entry below), so a maintenance retry of a failed first ingest no longer creates the deleted collection.
- **Reworded `detail`**: the refusal's `detail` no longer says "ingest": `job <id> is in progress; retry after it completes` when the job belongs to the caller's namespace (was `"ingest job <id> is in progress; …"`), else `a job is in progress on this collection; retry after it completes` (was `"an ingest job is in progress on this collection; …"`). `code` `store_busy` and `Retry-After: 30` are unchanged. The server's WARNING log line now names the job's kind (e.g. `ReindexJob`).
- **Reindex jobs name their collection**: a reindex job's `collection` was always `""`; it is now the reindexed collection's name, in `GET /jobs`, `GET /jobs/{id}`, the `202` body of `POST /collections/{name}/reindex`, and the CLI's `jobs list` `COLLECTION` column. A reindex job saved by an older server keeps `""` and never blocks `DELETE` — nor can it be active any more: one still `PENDING` is failed at startup (next bullet), and one `RUNNING`/`CANCELLING` was already rewritten `FAILED` `"process_restart"`.
- **A reindex job `PENDING` at startup is failed.** Only `POST /collections/{name}/reindex` starts a reindex job's task, and a shutdown cancels that task while it loads its embedding model, leaving the job `PENDING` with no task to run it. Loading the job store now marks it `FAILED` with `error` `never started; the server restarted before it ran` (`NOT_STARTED_BEFORE_RESTART_DETAIL`, as for an ingest job), where it stayed `PENDING` for good — and so would have blocked `DELETE` of its collection, and kept `POST /collections/{name}/reindex`, a `PATCH /collections/{name}` embedding-model change and a rewrite migration answering `409`. Nothing ran, so this does not suppress the startup sync; a reindex is never retried.
- **Job routes racing a `DELETE`**: `POST /collections/{name}/reindex`, `POST /collections/{name}/reindex-metadata` and `POST /graph/{collection}/rebuild-communities` read the collection's meta row, created their job, then recorded its id on the meta row — so a request that read the row just before a `DELETE` dropped the collection created a job `DELETE` never saw and re-inserted the dropped collection's meta row (a reindex's task also re-created its table). They now create the job, and record its id on the meta row, under the collection lock, from the meta row re-read under it (`create_collection_job`, `archon_search/server/_collection_job.py`): such a request answers `404` (same body as for an unknown collection), and a `DELETE` that takes the lock afterwards sees the job and refuses with `503`. Waiting for that lock can time out: they answer `503` `{"detail": "reindex in progress; retry after Retry-After seconds", "code": "store_busy"}` with `Retry-After: 30` (the body `POST /ingest` answers), creating no job. Their `409` duplicate-job check still answers at once when the collection's meta pointer names an active job of the route's kind — it runs on the meta row read before the lock, so a duplicate never waits for a lock another write holds (a metadata reindex holds it for its whole run) — and runs again on the row re-read under the lock: of two concurrent submissions for the same collection, the second now answers `409` where both created a job — except two dry-run metadata reindexes: a dry-run records no meta pointer, so the second waits for the collection lock, which the first holds for its whole scan (`SearchStore.reindex_metadata`), and answers `202` once it is free, or `503` if that wait times out. A dry-run is refused like any duplicate, with an instant `409`, while a non-dry-run metadata reindex is active. Only a request that has to wait for the lock can time out with that `503`: one with no active job of its kind recorded on the collection while another write holds the lock — e.g. a reindex while a metadata reindex runs, or a dry-run metadata reindex while another dry-run runs. `POST /collections/{name}/reindex` now checks readiness first: while the embedder cache, pipeline or search store is not initialised it answers `503` `{"detail": "service not ready"}` even for an unknown collection (was `404`) or while a reindex is in progress (was `409`), and without a search store a configured collection gets that `503` where the request failed with a `500`. `POST /collections/{name}/reindex-metadata` and `POST /graph/{collection}/rebuild-communities` likewise answer `503` `{"detail": "service not ready"}`, creating no job, when the server has no search store — after their checks that need none (`404` for a name absent from the config; the `422` namespace and graph checks) — where the request failed with a plain-text `500` `Internal Server Error`.

**Migration:** treat every `503` with `code == "store_busy"` from `DELETE /collections/{name}` as "retry later", whatever its `detail`, and honour `Retry-After`; stop matching the word "ingest" in it. A script that deletes a collection right after starting a reindex, import, migration, metadata reindex (not a `dry_run` one) or community rebuild must now wait for that job to reach a terminal status (`GET /jobs/{id}`), or cancel it first (`DELETE /jobs/{id}`; a cancelled job blocks until it settles). Do not cancel a metadata-reindex or community-rebuild job left `PENDING` by an older server: no task runs it, so it does not block `DELETE`, but a cancel moves it to `CANCELLING`, which does — until the next restart rewrites it `FAILED` `"process_restart"` (tech-debt CON-11 in `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`). Treat a `503` `store_busy` from the reindex, reindex-metadata and rebuild-communities routes the same way (no job was created; retry after `Retry-After`); a client that fires the same one of these requests twice concurrently must expect one `202` and one `409` — except two dry-run metadata reindexes, which both answer `202` (the second once the first's scan has released the collection lock) unless the second's wait times out with `503` `store_busy`. A `503` `"service not ready"` from `POST /collections/{name}/reindex` no longer implies that the collection exists: retry it once the server is ready, then handle a `404`; the same `503` from reindex-metadata or rebuild-communities is retryable too (no job was created). A reindex left `PENDING` by a server restart is `FAILED` `"never started; the server restarted before it ran"` after the restart and no longer blocks anything: re-run `POST /collections/{name}/reindex` to apply it.

**Announced in**: this release.

---

### [next release] — maintenance failed-ingest retries now run; which `FAILED` ingest jobs are retried changed; an ingest job left `PENDING` by a restart is failed (2026-10-21)

**Surface**: ingest jobs `MaintenanceLoop` re-enqueues (`[maintenance].failed_ingest_retry`, `source: "maintenance"` in `GET /jobs`) — their `status` / `error` / `retry_count`, and the chunks they write; which `FAILED` ingest jobs a maintenance pass retries or expires; `DELETE /collections/{name}`'s effect on the collection's ingest jobs; the `status` / `error` of an ingest job found `PENDING` when the server starts; the job store file (`archon-search-jobs.json`) on a downgrade.

**Behaviour-only change — no schema field was added, removed or retyped.** A path ingest's row in the job store file gains one key, `ingest_options`, and a job `DELETE /jobs/{id}` cancelled gains `cancel_requested`; `GET /jobs` serves neither.

**What changed:**

- **Retry jobs run.** A maintenance pass re-enqueued each eligible `FAILED` ingest job as a new `PENDING` job, but nothing ran it: it stayed `PENDING` (a cancel left it `CANCELLING` until a restart rewrote it `FAILED` `"process_restart"`), and the file was never re-ingested. The pass now starts each retry job through the same ingest task as `POST /ingest` (`make_retry_dispatcher`, `archon_search/server/routes_jobs.py`), so it goes `PENDING` → `RUNNING` → `DONE` / `FAILED` / `CANCELLED` like any ingest job, re-ingests the file, and a `FAILED` retry is itself retried on a later pass while the per-file attempt and age limits allow. Its `retry_count` in `GET /jobs` is the attempt it is (`1`, `2`, …), where it stayed `0`. Like `POST /ingest`, a retry takes the collection's ingest lock before it starts; unlike it, the retry waits for a busy collection without a time limit, so contention never fails it or uses up an attempt, and a server shutdown while it waits leaves it `PENDING` (the next start marks it `FAILED` `never started; the server restarted before it ran`, and it is retried as the next attempt). A retry job whose task cannot be started is marked `FAILED` with `error` `maintenance retry could not be started; see the server log for details` (`RETRY_DISPATCH_FAILED_DETAIL`, `archon_search/jobs/maintenance_loop.py`; best-effort; when that write fails too, the collection's `last_error` in `GET /status` shows the same text), the cause logged.
- **A retry re-ingests with the original request's options — and only a job that has them is retried.** `POST /ingest` now stores the request of a path ingest on the job (`ingest_options`: its `path` as submitted, `X-Ingested-By` value, `chunk_ttl_seconds` and `chunk_scopes`), and a retry re-applies them: retried chunks keep their scopes (an unscoped chunk matches every `scope_filter`), TTL and `ingested_by`, and a retry that finds no file reports the path as the caller submitted it. A `FAILED` ingest job without stored options — every one an older server saved, so its original request is unknown — is never retried: the next pass moves it to `FAILED_EXPIRED` (WARNING log line naming the job), as a retry with the request defaults would drop its scopes. Inline-`documents` ingests (no `source_path`) are never retried, as before.
- **An ingest that was running when the server died is never retried.** A `FAILED` `"process_restart"` ingest job — `RUNNING` or `CANCELLING` when the previous process died, the crash marker that suppresses the startup sync on every boot until a clean `POST /sync` — was retried like any `FAILED` ingest, re-entering the workload that may have killed the process. The next pass now moves it to `FAILED_EXPIRED`.
- **Superseded jobs are not retried.** A `FAILED` ingest job is retried only when it is the newest job for its `{namespace}/{collection}/{source_path}`. An older one — whose path has a newer `PENDING` / `RUNNING` (outstanding), `DONE` or `FAILED` job, or a newer job a client cancelled with `DELETE /jobs/{id}` — stays `FAILED` until the job store evicts it (7 days after its last update) or the path's retries end; a newer `FAILED_EXPIRED` job, or one `CANCELLED` by a server shutdown, does not supersede it.
- **Cancelling a retry ends the path's retries.** `DELETE /jobs/{id}` on an active (`PENDING` / `RUNNING`) ingest job of a path — a retry, or a newer submission of the path — stops the maintenance loop from starting another retry of that path — while the cancel settles (`CANCELLING`) and after (`CANCELLED`); a job that ends `FAILED` after its cancel was requested is expired, not retried. Only an active job can be cancelled: `DELETE /jobs/{id}` on a `FAILED` job changes nothing (it answers with the job as it is), so the path's retries go on. Before, the cancelled retry superseded nothing, so the next pass re-enqueued the path again. A server shutdown is no client's cancel: a retry it interrupts while `RUNNING` ends `CANCELLED` as before, and the path is retried again on a later pass (the interrupted retry still uses its attempt). A client re-submitting the path (`POST /ingest`) starts over. Before, such a job was re-enqueued on each pass until the path's attempts reached `retry_max_attempts`, then expired; now that retries run, without this rule the file would be re-ingested on every pass — and expiring the original after a successful retry would raise `failed_expired_ingest_count` for a file that is ingested.
- **Attempts are counted per submission.** Every retry the maintenance loop creates for a submission uses one attempt however it ended — `FAILED`, `CANCELLED` by a server shutdown, or never started before a restart: the next retry's `retry_count` is the highest `retry_count` among the submission's retries plus one, and none starts once that reaches `retry_max_attempts`, a hard bound per submission. A client re-submitting a path whose retries were used up gets the full number again — before, a per-path count in `.maintenance-state.json` (`retry_counts`, now gone from that file) reset only when an ingest of the path succeeded, so the re-submission was expired with no retry. A re-enqueue that cannot be created (job store write failure) no longer uses up an attempt.
- **Ending a path's retries expires all its `FAILED` jobs at once.** When the newest `FAILED` job of a path ages out, reaches `retry_max_attempts`, or is one of the never-retried jobs above, every `FAILED` ingest job of that path moves to `FAILED_EXPIRED` in the same pass, where one job expired per pass.
- **`retry_max_age_hours` bounds the whole retry chain.** The age is measured from the request the retries descend from — the path's newest job not created by maintenance, among its `PENDING` / `RUNNING` / `DONE` / `FAILED` jobs and those a client cancelled with `DELETE /jobs/{id}` (a request `CANCELLED` by a server shutdown does not restart the window; a client-cancelled one does, but it also supersedes the path's older failures, so no retry follows it) — not from the newest failed retry, so the window no longer restarts with each retry.
- **`[maintenance].exclude` applies to retries.** A `FAILED` ingest job on an excluded collection is no longer retried (nor expired); it stays `FAILED`.
- **`DELETE /collections/{name}` ends the collection's retries.** Once the table is dropped (or, since the `DELETE /collections/{name}` "any job may still write the collection" entry above, once a config-only collection's config entry is removed), the collection's `FAILED` and `PENDING` base ingest jobs (a `PENDING` one is a retry waiting for the collection lock) end `CANCELLED` — in every namespace, since the dropped table is shared by name — so no retry re-creates the deleted collection, and none counts toward `failed_expired_ingest_count`. Best-effort: a failed write is logged at WARNING and the `DELETE` still succeeds.
- **An ingest job `PENDING` at startup is failed.** Every ingest job's task is started when the job is created, so one still `PENDING` when the server loads its job store lost that task with the previous process. Loading now marks it `FAILED` with `error` `never started; the server restarted before it ran` (`NOT_STARTED_BEFORE_RESTART_DETAIL`, `archon_search/jobs/store.py`), where it stayed `PENDING` for good; nothing ran, so unlike `"process_restart"` this does not suppress the startup sync. A path ingest failed this way is retried by the next maintenance pass when it has stored options (expired otherwise, as above). Other job kinds keep a saved `PENDING` status as before — except a reindex job, failed the same way since the `DELETE /collections/{name}` "any job may still write the collection" entry above.

**Migration:** expect `source: "maintenance"` ingest jobs to reach a terminal status, to write chunks and to report their attempt in `retry_count`; a client that waits on one, or cancels one, now sees it settle, and cancelling one stops that path's retries (re-submit the path to try again). After the upgrade, every `FAILED` ingest saved by the older server moves to `FAILED_EXPIRED` on the first maintenance pass (raising `failed_expired_ingest_count`): re-submit the ingests you still need. Expect `CANCELLED` for the failed and pending ingests of a collection you delete, and `FAILED` `never started; the server restarted before it ran` instead of a lasting `PENDING` after a restart. **Downgrade:** an older server cannot read the `ingest_options` or `cancel_requested` key, treats the job store file as corrupt and starts with an empty job list (`JobStore: corrupt jobs file … — resetting`, ERROR) — all job history, including `FAILED` jobs and the crash marker the startup-sync guard reads, is lost; back up `archon-search-jobs.json` before downgrading if you need it.

**Announced in**: this release.

---

### [next release] — an ingest or reindex job cancelled before it starts ends `CANCELLED` instead of running (2026-10-21)

**Surface**: `GET /jobs/{id}` `status` / `error` of an ingest job (`POST /ingest`, `POST /collections/`) or reindex job (`POST /collections/{name}/reindex`) after `DELETE /jobs/{id}`; the collection's embedding model after such a reindex.

**Behaviour-only change — no schema field was added, removed or retyped.**

**What changed** (the change marked *On a failed write* happens only when the job store cannot write its file — disk full, read-only data dir):

- **Cancelled while `PENDING`**: `DELETE /jobs/{id}` answered `202` `CANCELLING`, but the job's task then overwrote `CANCELLING` with `RUNNING` and ran the work to `DONE` — an ingest wrote its chunks, and a reindex cancelled while its embedding model loaded re-embedded the collection and promoted its target model. The task now claims `RUNNING` only from `PENDING`, so such a job ends `CANCELLED` with nothing written; a reindex leaves the collection's embedding model unchanged (`pending_embedding_model` and `needs_reindex` kept) — also when the embedding-model load the cancel landed during then fails (`CANCELLED`, not `FAILED`). Every reindex end clears `reindex_job_id` (while it still names this job) before it writes `CANCELLED` or `FAILED`, so a client that sees `CANCELLED` can start a new reindex. Unchanged: a `PENDING` job that no task runs — a job other than an ingest or a reindex (a reindex since the `DELETE /collections/{name}` "any job may still write the collection" entry) left `PENDING` across a restart — stays `CANCELLING` after a cancel until the next restart rewrites it `FAILED` `"process_restart"`. An ingest `MaintenanceLoop` re-enqueued after a failure (`source: "maintenance"`) now has a task too, so it ends `CANCELLED` like any other, and an ingest left `PENDING` by a restart is marked `FAILED` when the server starts (see the failed-ingest retry entry above).
- **Reindex cancelled while its target model is promoted**: a `DELETE /jobs/{id}` that landed after a `RUNNING` reindex's ingest finished, while the reindex promoted its target model, was overwritten with `DONE`; the job now ends `CANCELLED`, with the model already promoted (the vectors were re-embedded with it). The rest of a `RUNNING` cancel is unchanged: the job is not interrupted and ends `CANCELLED` (or `FAILED`) once its work finishes, and a reindex cancelled during its ingest ends `CANCELLED` without promoting its target model (`FAILED` if the ingest fails).
- **Reindex already `CANCELLING` when its task starts**: the job stayed `CANCELLING` — never evicted — until a restart rewrote it `FAILED` `"process_restart"`; it now ends `CANCELLED`.
- **Reindex claim**: *On a failed write*: a failed write of the reindex job's `RUNNING` status was logged and the reindex ran anyway; now nothing runs: `reindex_job_id` is cleared while it still names this job, then the job is marked `FAILED` with `error` `"reindex failed; see the server log for details"` — or `CANCELLED` when a `DELETE /jobs/{id}` landed meanwhile.

**Migration:** clients that cancel a job they just submitted must expect `CANCELLED` (the CLI's `--wait` exits `1`) where they used to see `DONE`, and resubmit to get the work done. After a reindex cancelled this way, wait until `GET /jobs/{id}` reports `CANCELLED`, then re-run `POST /collections/{name}/reindex` (`archon-search collection reindex`) to apply a pending embedding-model change. The reindex `409` guard already accepts a new reindex while the cancelled one is `CANCELLING`, but a reindex cancelled while `RUNNING` keeps ingesting until its work finishes, so the two would run side by side. Clients that cancel a `RUNNING` reindex must not read `CANCELLED` as "model unchanged": read `active_embedding_model` from `GET /collections/{name}` to see whether the target model was promoted.

**Announced in**: this release.

---

### [next release] — `DELETE /collections/{name}` also returns `503 store_busy` while another namespace's ingest job writes to the collection; the ingest check runs after the lock wait (2026-10-21)

**Surface**: `DELETE /collections/{name}` status, body and timing.

**Behaviour-only change — no schema field was added, removed or retyped.** OpenAPI now describes the `503`.

**Since amended** by the `DELETE /collections/{name}` "any job may still write the collection" entry above: the refusal now covers reindex, import, migration, metadata-reindex and community-rebuild jobs too, and both `detail` texts quoted below were reworded.

**What changed:**

- **Any namespace**: `DELETE /collections/{name}` returned `503` `store_busy` only while a base ingest job of the caller's own namespace was `RUNNING`/`CANCELLING` on the collection. It now also returns it while such a job from **another** namespace is — the store keeps one table per collection name, so that ingest writes into the very table `DELETE` drops. The body is `{"detail": "an ingest job is in progress on this collection; retry after it completes", "code": "store_busy"}` (no job id); a job of the caller's own namespace still yields `"ingest job <id> is in progress; retry after it completes"`, and is preferred when both exist. `Retry-After: 30` as before.
- **Check after the lock wait**: the active-ingest check now runs after the per-collection lock is acquired, not before the wait. A `DELETE` that waits longer than `INGEST_LOCK_TIMEOUT_S` (30 s) for the lock now gets the lock-timeout body `{"detail": "reindex in progress; retry after Retry-After seconds", "code": "store_busy"}` instead of the ingest-in-progress one; a `DELETE` that gets the lock now also catches an ingest that turned `RUNNING` while it waited.

**Migration:** treat every `503` with `code == "store_busy"` from `DELETE /collections/{name}` as "retry later", whatever its `detail`, and honour `Retry-After`. Clients that read the job id out of the `detail` must handle the id-less detail.

**Announced in**: this release.

---

### [next release] — `install` / `wizard` on Linux and macOS refuse service paths the systemd unit or launchd plist cannot carry: outer whitespace, an undetermined interpreter, unrepresentable characters (2026-10-20)

**Surface**: CLI `archon-search install` and `archon-search wizard` (`--dry-run` included) exit code and stderr on Linux and macOS (and, for the early refusal, Windows); the systemd user unit (`~/.config/systemd/user/archon-search.service`) and the launchd plist / `<data-dir>/run-server.sh` wrapper they write.

**Behaviour-only change — no CLI flag, REST or MCP field was added, removed or retyped.**

**What changed:** The interpreter, data-dir, config and secrets-file paths were formatted into the unit, plist and wrapper unescaped and unchecked, so some values produced a service that could not start (`ExecStart="" -m archon_search.server` for an empty `sys.executable`; an unparseable plist for a path containing `&` or `<`) or that ran on a different path than the one installed (the server strips `ARCHON_SEARCH_DATA_DIR` / `ARCHON_SEARCH_CONFIG`). Every value is now escaped for its directive, and `validate_registration()` (`archon_search/platform/linux.py`, `archon_search/platform/macos.py`, shared checks in `archon_search/platform/service.py`) refuses the values that have no correct encoding:

- **Both platforms**: an empty or missing interpreter path (Python could not determine it), and a data-dir or config path that still has leading or trailing whitespace once resolved — the env vars are already stripped, so this is e.g. `ARCHON_SEARCH_DATA_DIR='/srv/data /'` (the trailing `/` is dropped, leaving `/srv/data `) or a `--config` value with outer whitespace.
- **Linux**: an interpreter path containing `"`, `'`, `\`, `$`, `*`, `?` or `[`; any interpreter, data-dir, config or secrets-file path containing a control character or bytes that are not valid UTF-8; a `WorkingDirectory=` or `EnvironmentFile=` path ending in an odd number of `\`, starting or ending in whitespace, or still containing `..` after resolution.
- **macOS**: a data-dir or config path that does not read back unchanged from the plist (a C0 control character other than tab and newline, `\r` included; U+FFFE or U+FFFF; bytes that are not valid UTF-8), and an interpreter or secrets-file path containing NUL or bytes that are not valid UTF-8.
- **Windows**: service management is still unsupported, but `wizard` now refuses it up front (`Windows service management not yet supported — run archon-search start manually`) and exits `1` before any download or config write, instead of failing only when it registers the service.

On a refused value, `wizard` (before any download or config write, `--dry-run` too) and `install` (before stopping the running service) write nothing and exit `1` with one stderr line: `Error during install:`, the reason, then the offending value (as a quoted Python repr). Before, both went on to write a unit or plist the service manager could not run correctly (a `None` interpreter on Linux crashed with a `TypeError` traceback instead). Paths free of these characters are unaffected; in the unit, values are now double-quoted (`ExecStart=`, `Environment=`) with `%` doubled and `\` / `"` escaped, so spaces and `%` no longer break it; the plist XML-escapes `&`, `<` and `>`, and the wrapper `shlex.quote`s its paths, so quotes, `$` and backticks stay literal. The full refused-value lists live in `Documentation/OperatorGuide/10_deployment_topologies.md`.

**Migration:** if `install` or `wizard` now exits `1` with `Error during install:`, fix the value the line names — run the CLI from an interpreter whose `sys.executable` resolves, remove whitespace before a trailing `/` in `ARCHON_SEARCH_DATA_DIR` / `ARCHON_SEARCH_CONFIG` and outer whitespace from `--config`, or move the data dir, config file or interpreter to a path without the refused characters — then re-run it. An existing unit or plist is left as it is until the service is registered again.

**Announced in**: this release.

---

### [next release] — export/import: imported chunks carry the metadata marker `_imported`, and maintenance orphan cleanup no longer deletes them (2026-10-20)

**Surface**: the `metadata` of every chunk an import writes (`POST /collections/{name}/import`, `archon-search import`, MCP `import_collection`) — returned by `POST /explain` always, by `POST /search` and the MCP `search` / `search_with_context` tools when `include_metadata` is set, and written into later export archives; `MaintenanceLoop` orphan cleanup (`[maintenance].orphan_cleanup`) and its `orphans_removed_last_run`; the per-document refusals of an import job (the 50-field metadata limit).

**No schema field was added, removed or retyped (`metadata` stays `dict[str, str]`), so `GET /openapi.json` needs no regeneration.**

**What changed:**

- **Every imported chunk is marked.** `_decode_chunk` (`archon_search/server/routes_export.py`) adds `"_imported": "true"` (`IMPORTED_CHUNK_METADATA_KEY`, `archon_search/constants.py`) to the archived metadata of each chunk, overwriting an archived `_imported` of any other value. A client reading `metadata` sees the extra key on imported chunks; an export of an imported collection carries it, so importing that archive keeps it.
- **Orphan cleanup never deletes imported chunks.** The archive keeps the exporting host's `source_path`, so on any other host the sweep read every file-backed imported chunk as a deleted file and removed it — with `orphan_cleanup` on by default, the first maintenance pass (scheduled when `[maintenance].interval_hours` > 0, or triggered by `POST /maintenance/trigger` / `archon-search maintenance run`) after importing another machine's archive deleted nearly the whole collection. The sweep now never deletes a chunk whose metadata holds `_imported` = `"true"` exactly, as for `_inline` = `"true"` (S293) — the markers of chunks not ingested from a file on this host (`NOT_DISK_INGESTED_CHUNK_MARKERS`, `archon_search/constants.py`). A `source_path` with no marked chunk is swept as before; at a missing path with a marked chunk, at most the unmarked rows under the `doc_id` derived from the path as resolved here are deleted — full rules in [Orphan cleanup rules](Documentation/OperatorGuide/50_maintenance_and_jobs.md#orphan-cleanup-rules). Consequence on the exporting host itself: the sweep keeps an imported document after its source file is deleted (a collection sync that tracked the file still deletes it).
- **Re-ingesting clears the marker only in one case.** Ingesting a path from disk replaces its imported rows with unmarked ones only when the path resolves to the archived `doc_id` (typically a same-host restore) and that ingest writes chunks: one that yields no chunks, or stops before its write (for example a fatal graph-extraction error), leaves the imported rows (`archon_search/pipeline.py`, `_ingest_text`). Collection sync and the watcher re-ingest only files that are new or changed since their recorded sync while their recorded sync state survives and the collection's embedding model and chunk size are unchanged (otherwise sync re-ingests every file), so after a same-host restore that kept that state an unchanged file keeps its imported rows until you ingest it explicitly: `POST /ingest` with its `path` (or the MCP `ingest_file` / `ingest_directory` tools), or `POST /collections/{name}/reindex` for a collection with a configured source directory — these ingest every file regardless of mtime. Re-ingesting under other paths leaves the imported rows beside the new ones, still marked: drop the imported collection (`DELETE /collections/{name}` removes the whole collection, re-ingested documents included) before re-ingesting into a fresh one, or delete each imported document by its `doc_id` with the MCP `delete_document` tool.
- **The marker needs room among the 50 metadata fields.** An archived document whose `metadata` holds 50 fields other than `_imported` is now refused as `` malformed doc at line N: metadata has no room for the `_imported` marker (at most 50 fields) `` (the job fails, or skips it under `on_error="skip"`); before, it imported. One holding 50 fields including `_imported` still imports: the marker overwrites it and adds no key; one holding more than 50 is refused as before, with the generic `` malformed doc at line N: a required field is missing or has the wrong type ``. The cap is `META_MAX_FIELDS` (`archon_search/store.py`). No ingest path stores caller-supplied metadata, so only a hand-built archive reaches the limit.
- **Imports resumed across the upgrade.** An import job that failed before this release and is resumed after it keeps the rows it wrote before the upgrade unmarked, so the sweep still deletes them when their paths are missing here, and the resume itself can be refused or restart from scratch when the archive's first document hits the field limit above. Re-import such a collection with `force_overwrite=true` instead of resuming it.

**Migration**: clients that compare an imported chunk's `metadata` exactly or count its keys must allow `_imported`. Collections imported before this release carry no marker and are still deleted by orphan cleanup when their paths are missing here: import them again with `force_overwrite=true` to mark them — if the original archive is gone, export the collection on this host and re-import that export with `force_overwrite=true`, which works only while the sweep has not yet deleted the rows — or list them in `[maintenance].exclude`, which skips every per-collection maintenance policy for them (FTS optimize, orphan cleanup, expired-chunk pruning, graph GC), not just orphan cleanup. No schema migration is needed (`STORE_SCHEMA_VERSION` is unchanged).

**Announced in**: this release.

---

### [next release] — a job whose status write fails keeps its old status; `POST /jobs/{id}/resume` returns a JSON `500`; route-run jobs are created `RUNNING` (2026-10-20)

**Surface**: `POST /jobs/{job_id}/resume` status and body; `POST /collections/{name}/migrate` (rewrite), `POST /collections/{name}/reindex-metadata`, `POST /graph/{collection}/rebuild-communities` status and body, and the created job's persisted `status`; `DELETE /jobs/{job_id}` status and body (on a sync job too), and the job status after a `500`; the bulk-job scheduler, and the `error` of a job it fails to dispatch; `GET /jobs/{id}` `status` / `error` of a community rebuild that fails unexpectedly; the startup sync job; `POST /sync` status and body, the created job's persisted `status`, the sync lock after a `500`, its `409` while the startup sync waits for the lock, and legacy `QUEUED`/`PENDING` sync job rows; the `MaintenanceLoop` failed-ingest expiry.

**Behaviour-only change — no schema field was added, removed or retyped.** OpenAPI now declares the `500` (`ErrorDetail`) of resume, reindex-metadata, rebuild-communities, `POST /sync` and `DELETE /jobs/{job_id}`.

**What changed** (a change marked *On a failed write* happens only when the job store cannot write its file — disk full, read-only data dir — and one marked *On a failed write or task start* also when the sync task cannot be started; every unmarked change always applies):

- **`POST /jobs/{id}/resume`**: *On a failed write*: a failed `FAILED → QUEUED` write returned a plain-text `500 Internal Server Error` and left the job `QUEUED` in memory, so the scheduler could run it. It now returns `500` `{"detail": "internal error"}` and the job stays `FAILED` (unless its `updated_at` is more than 7 days old: the store's corrective rewrite of its file then evicts it, as any job-store write evicts terminal jobs of that age).
- **Migrate (rewrite), reindex-metadata, rebuild-communities**: the job was created `QUEUED` and then claimed `QUEUED → RUNNING`; it is now created `RUNNING` in one write — the file never holds it `QUEUED`. The `202` body is unchanged (`RUNNING`). *On a failed write*: a failed claim returned a plain-text `500` and left the job `RUNNING` in memory with no task behind it (and a migration `QUEUED` on disk, for the scheduler to run after a restart); now a failed write returns `500` `{"detail": "internal error"}` (for any failure, not only an `OSError`), leaves no job (unless the store's best-effort corrective rewrite of its file fails too, or a crash right after it leaves the earlier file version: the row then stays on disk and reloads after a restart as `FAILED` `process_restart`), and starts nothing.
- **Reindex-metadata and rebuild-communities `409`**: a collection whose `metadata_reindex_job_id` / `community_rebuild_job_id` points at a `QUEUED` or `PENDING` job (only an older server wrote those, and nothing ever runs them) is no longer "in progress": the id is cleared, the old job is marked `FAILED` (best-effort) with `error` `"never started; superseded by a newer request"` — so it becomes terminal: evicted after 7 days like any terminal job, and `DELETE /jobs/{id}` answers `200` for it (a `QUEUED` one answered `500`) — and the request proceeds (`202`). A `CANCELLING` job now counts as in progress (`409`) — `DELETE /jobs/{id}` does not stop the running task.
- **`DELETE /jobs/{id}`**: any non-terminal `SyncJob` — the startup sync's or a manual `POST /sync` one, legacy `QUEUED`/`PENDING` rows included — now answers `409` `{"detail": "a sync cannot be cancelled"}`. Previously only the startup sync's job answered `409`, with `{"detail": "the startup sync cannot be cancelled"}`; a manual sync job answered `202` `CANCELLING` although nothing interrupted the sync, whose `DONE` then overwrote `CANCELLING`; a legacy `QUEUED` one answered `500` and a legacy `PENDING` one `202` `CANCELLING`. A terminal `SyncJob` still answers the idempotent `200`. The `500` for a job in a status DELETE does not handle (e.g. a `QUEUED` export) now names the bare status — `{"detail": "Unknown job status: QUEUED"}` instead of `"Unknown job status: JobStatus.QUEUED"`. *On a failed write*: the `500` on a failed cancel no longer leaves the job `CANCELLING` in memory; it keeps its old status. Any failure to save the cancel (not only an `OSError`) now returns `500` `{"detail": "internal error"}` instead of a plain-text `500`.
- **Scheduler**: *On a failed write*: a failed `QUEUED → RUNNING` write escaped `JobScheduler.run()`, ending the scheduler loop (no queued export/import/migration job ran until a restart) with the job `RUNNING` in memory. It is now logged, the job stays `QUEUED`, and the scheduler keeps ticking.
- **`POST /sync`**: the job is created `RUNNING` in one write instead of `QUEUED` then claimed (the `202` body is unchanged); it answers `409` instead of waiting when the startup sync is already queued for the lock; and it marks every legacy `QUEUED`/`PENDING` `SyncJob` (nothing runs them) `FAILED` with `error` `"never started; superseded by a newer request"` (best-effort), so `DELETE /jobs/{id}` then answers `200` for it. A manual sync now ends `DONE` or `FAILED` (`CANCELLED` only on server shutdown), since `DELETE` no longer cancels it. *On a failed write or task start*: a plain-text `500` that could leave the sync lock held (every later `POST /sync` answered `409` until a restart), or a `500` that left the job `RUNNING`, is now `500` `{"detail": "internal error"}` with the lock released; a job whose task could not start is marked `FAILED` `"sync failed"` (best-effort); and a failed terminal status write no longer turns a successful sync `FAILED`. Details: [140 § JobStore persist failures and `POST /sync`](Documentation/Architecture/140_error_handling_strategy.md#jobstore-persist-failures-and-post-sync).
- **Failed-ingest expiry (`MaintenanceLoop`)**: *On a failed write*: a failed `FAILED → FAILED_EXPIRED` write now leaves the job `FAILED` in memory (previously `FAILED_EXPIRED` in memory, `FAILED` on disk); the next maintenance pass retries the expiry.
- **Startup sync job**: the lifespan's startup sync job is now created `RUNNING` in one write instead of `QUEUED` then claimed, so the `FAILED` job with `error` `"startup sync job could not be marked running"` (written when that claim was rejected) no longer appears.
- **Community rebuild, unexpected failure**: an exception from the rebuild other than `ValueError` / `ImportError` / `RuntimeError` left the job `RUNNING` and the collection's `community_rebuild_job_id` set, so every later `POST /graph/{collection}/rebuild-communities` answered `409` until a restart. The job is now `FAILED` with the fixed `error` `"community rebuild failed; see the server log for details"` (the exception is logged server-side) and the id is cleared.
- **Scheduler dispatch failure**: a job the scheduler fails to start reports the fixed `error` `"dispatch_failed; see the server log for details"` (`DISPATCH_FAILED_DETAIL`) instead of `dispatch_failed: <exception text>`, which could embed server paths and library internals. The exception is logged server-side with its traceback.

**Migration:** treat any `500` from these routes as "not done" and retry once the data dir is writable. Clients matching `dispatch_failed:` in a job's `error` must match the new fixed text; the cause is in the server log. Clients matching the startup sync's `"startup sync job could not be marked running"` error can drop that match. Clients matching `JobStatus.` in the `detail` of a `DELETE /jobs/{id}` `500` must match the bare status. Clients that cancel sync jobs must treat a `409` from `DELETE /jobs/{id}` as "not cancellable" (match `"a sync cannot be cancelled"`, no longer `"the startup sync cannot be cancelled"`) and wait for a terminal status (`DONE`, `FAILED`, or `CANCELLED` when server shutdown stops the sync; a legacy `QUEUED`/`PENDING` sync row ends only when the next `POST /sync` marks it `FAILED`); treat a `POST /sync` `409` as "retry later".

**Announced in**: this release.

---

### [next release] — collection-sync errors, ingest parse errors, model-validation `provider_warnings` and def/ref ingest warnings report fixed text, not exception text (2026-10-20)

**Surface**: `GET /jobs/{id}` → `result["errors"]` of a `POST /sync` job; `GET /status` → `collections[].error` and `GET /indexing-state` → `collections.<name>.error`; MCP `ingest_file` / `ingest_directory` → `error` of a result with `code` `parse_error`, and of a file whose size could not be read; `GET /status` → `model_validation.provider_warnings` and `GET /ready` → `checks.models`; MCP `ingest_file` / `ingest_directory` → `warnings` and the ingest job's `result["warnings"]` (`GET /jobs/{id}`). CLI: `archon-search jobs show` (the job's `result`: a `POST /sync` job's `errors`, an ingest job's `warnings`) and `archon-search maintenance status` (`provider_warnings`) print these fields.

**Schema changes in `GET /openapi.json`: `POST /explain` now declares a `500` response with the `ErrorDetail` body (`{"detail": str, "code": str | null}`; this route never sets `code`), covering the pipeline-stage failure (`<stage> error: <ExceptionType>`) and the `explain failed` answer to a failed HyDE resolution or pipeline call it already returned undeclared; any other failure (e.g. loading the collection's embedder failing for a reason other than the embedder not being ready yet, which is a JSON `503`) is still the undeclared plain-text `500`. `POST /ingest` now declares its `413` as `CodedErrorDetail` (`{"detail": {"code": str, "message": str}}`), the body it has always sent for an oversized single-file path or inline document, instead of `ErrorDetail` (`{"detail": str, "code": str | null}`), and declares the `500` `ErrorDetail` (`{"detail": "internal error"}`) it answers when the job cannot be recorded. `POST /search` and `POST /explain` now declare `400` (`CodedErrorDetail`, code `invalid_scope_filter`), `404` and `504` (`ErrorDetail`), and declare `422` as `anyOf` `HTTPValidationError`, `CodedErrorDetail` (code `graph_communities_not_built`) and `ErrorDetail` (the string `detail` of a request the route rejects itself, e.g. `graph_mode requires [graph] enabled=true in server config` or a missing HyDE / RAG Fusion package) instead of `HTTPValidationError` alone — all bodies the routes already sent undeclared. No field was added, removed or retyped on the wire; everything else is behaviour only.**

**What changed:** these fields echoed the caught exception's message, which can embed server paths and library internals (LanceDB, ONNX, docling / RapidOCR, fastembed, tree-sitter) — or, for the missing-`[graph]`-extra `provider_warnings` entry, an install command rebuilt from the `uv tool` receipt, which can embed index credentials and local paths. They now carry fixed text; the exception is logged server-side with its traceback, and the install command at WARNING. This completes "[next release] — MCP tool errors, background-job `error` and maintenance `last_error` report fixed text, not exception text (2026-10-17)" below for the modules outside `server/` and `jobs/` that feed the wire.

- Collection sync: a collection whose sync raised now reports `sync of collection '<name>' failed; see the server log for details` instead of the exception text, both in the `POST /sync` job's `result["errors"]` and as the collection's `error` on `GET /status` / `GET /indexing-state` (`collection_sync_failed_detail`, `sync.py`). The server log carries `Sync of collection '<name>' failed during ingest` or `… failed while applying file changes` at ERROR with the traceback. The other `result["errors"]` entries (`path does not exist: <path>`, `Collection '<name>' in manifest but not in LanceDB; skipping drop`) are unchanged; they are curated text and still echo the sync's own source path or collection name.
- Ingest parse failure (`code` `parse_error`): `Failed to parse <absolute path>: <library message>` → `Failed to parse <absolute path>: the document could not be parsed; see the server log for details` (`PARSE_FAILED_TEMPLATE` with `PARSE_FAILED_DETAIL`, `parser.py`). The message still names the file by the absolute path the server resolved, so the files of an `ingest_directory` result stay distinguishable; only the library message is gone. A lost parse worker keeps its curated text after the path (`The document parser worker was lost while converting this file (it may have been killed by the OS, e.g. out of memory); retry the ingest.`, unchanged). A missing markitdown now reads `markitdown is not installed; reinstall archon-search to restore it` instead of `markitdown is not installed; run: pip install markitdown` — markitdown is a core dependency, so its absence means a broken install, and a `pip` command is wrong for a `uv tool` install. The server log carries `Failed to parse <path>` at WARNING with the traceback.
- `provider_warnings`: `could not query ONNX providers: <exception text>` → `could not query ONNX providers; see the log for details`, `embedder probe failed: <exception text>` → `embedder probe failed; see the log for details`, `reranker probe failed: <exception text>` → `reranker probe failed; see the log for details` (`model_validation.py`). They say "the log" because the install wizard runs the same probes with no server and logs these warnings itself. The exception is logged at WARNING with the provider list the probe ran under and its message (`could not query ONNX providers for <providers>: <exception text>`, `embedder probe failed for <model> under providers <providers>: <exception text>`, `reranker probe failed for <model> under providers <providers>: <exception text>`, where `<providers>` is the configured `providers` or `reranker_providers` list) and at DEBUG with its traceback — a failed probe is an expected outcome (e.g. a CoreML/CUDA fallback), so neither the wizard nor every boot prints a traceback for it. When both probe passes of a split config publish the same fixed warning (e.g. onnxruntime cannot be queried in either), `provider_warnings` carries it once; the log keeps one WARNING per pass, each naming its provider list. For the same reason the two unexpected-failure texts now say "the log" too: `model validation failed unexpectedly; see server logs for details` → `model validation failed unexpectedly; see the log for details` and `reranker split-provider validation failed unexpectedly; see server logs for details` → `reranker split-provider validation failed unexpectedly; see the log for details`. Two texts that pointed nowhere now point at the log: `validation task failed unexpectedly` → `validation task failed unexpectedly; see the log for details` and `graph NER model presence could not be determined` → `graph NER model presence could not be determined; see the log for details`. With `[database].reranker_providers` set, the reranker is now probed only under `reranker_providers`; before, it was also probed under `providers`, and a failure there (the very case the split config exists for, e.g. a reranker CoreML cannot run) left a `reranker probe failed: <exception text>` warning — `GET /ready` `checks.models: "warn"` — even when the reranker then loaded under `reranker_providers`. Such a host now reports no warning and `checks.models: "ok"`. The `checks.models` grading rules and the meaning of `embedder_ok` / `reranker_ok` are unchanged.
- Missing `[graph]` extra (`[graph].enabled = true` with `gliner` not importable), `GET /status` → `model_validation.provider_warnings`: `graph prose entity extraction is disabled: gliner is not installed. Install the graph extras: <install command>` → `graph prose entity extraction is disabled: gliner is not installed. Install archon-search[graph] to use graph prose entity extraction` (`GLINER_NOT_INSTALLED_WARNING`, `model_validation.py`). The entry no longer carries the install command, as for the missing-extra errors of "[next release] — MCP tool errors, background-job `error` and maintenance `last_error` report fixed text, not exception text (2026-10-17)" below. In a `uv tool` install the command is rebuilt from the tool receipt and can carry `--index-url` credentials, `--find-links` and local source paths; `graph_ner_status` now logs it at WARNING instead (`graph prose entity extraction is disabled: gliner is not installed. Install the graph extras: <install command>`). The `ConfigError` for the same state (`graph.enabled=true but gliner is not installed. Install the graph extras: <install command>`), raised by `create_app` at startup and by the `create_pipeline` factory, is unchanged: it reaches the operator or the calling code, never an HTTP client.
- Ingest warnings: `def/ref extraction failed to parse '<path>': <exception text>` → `def/ref extraction failed to parse '<path>'; see the server log for details` (`defref_extractor.py`); the warning still names the ingested file's path.
- Ingest size guard: a file whose size cannot be read for the `[ingest].max_file_mb` check (`code` `null`) now reads `Cannot determine file size for <path>` with the full path the ingest was given instead of only the file name (`FILE_SIZE_UNKNOWN_TEMPLATE`, `pipeline.py`), so the files of an `ingest_directory` result stay distinguishable, as for `parse_error`.
- Persisted state written before the upgrade keeps its old text: a collection's `FAILED` entry in the indexing state (`GET /status`, `GET /indexing-state`) until that collection syncs again, and a finished `POST /sync` job's `result["errors"]` until the job store evicts the finished job (7 days after its last update).

**Migration**: clients and monitors that parsed exception text from these fields must match the fixed wording and read the cause from the server log. Operators and tools that read the `[graph]` install command from `provider_warnings` (e.g. via `archon-search maintenance status`) must take it from the server log or the startup error. OpenAPI-generated clients gain a typed `ErrorDetail` for `POST /explain` `500`, and a client generated from the old contract must be regenerated to read `POST /ingest` `413` as `CodedErrorDetail` (`detail.code`, `detail.message`) instead of a string `detail`, and the `POST /search` / `POST /explain` `422` as the three-way union rather than `HTTPValidationError` (a generated client that decoded every `422` as `HTTPValidationError` failed on the other two bodies). Monitors that alerted on `checks.models: "warn"` for a split-provider host whose reranker fails only under `providers` stop firing.

**Announced in**: this release.

---

### [next release] — ingest/reindex jobs report fixed `error` text; `DELETE /collections/{name}` returns `500 collection_drop_failed` when the table drop fails; a `503` creates no job (2026-10-18)

**Surface**: `GET /jobs` / `GET /jobs/{id}` → `error` of ingest jobs (`POST /ingest`, `POST /collections/`, `archon-search collection add --wait`, `archon-search ingest --wait`) and reindex jobs (`POST /collections/{name}/reindex`, `archon-search collection reindex --wait`); `DELETE /collections/{name}` status and body; the job list after a `503` from `POST /ingest` or `POST /collections/{name}/reindex`.

**Behaviour-only change — no schema field was added, removed or retyped.**

**What changed:**

- **Unexpected ingest or reindex failure**: the job's `error` is now the fixed `"ingest failed; see the server log for details"` / `"reindex failed; see the server log for details"` instead of the exception's own text, which could embed server paths and library internals — in every unexpected-failure case, including a reindex job that `POST /collections/{name}/reindex` marks `FAILED` because recording it on the collection meta failed (its `error` used to be `null`). The exception is logged server-side with its traceback. Unchanged: `path does not exist or is not a file/directory: <path>` for an ingest path that does not exist, and `service unavailable: embedding model still loading`.
- **`DELETE /collections/{name}`**: a `RuntimeError` from the table drop (store not connected, LanceDB failure) was treated as "no table" — the meta row was deleted, `200` returned, and the table orphaned. It is now a failed drop: `500` `{"detail": "could not remove the collection from the store; see the server log for details", "code": "collection_drop_failed"}`, the config change rolled back, the meta row kept. Only a missing table is still ignored. Any other drop failure, or a failed meta-row delete after the drop, also returns this `500` instead of a plain-text `500 Internal Server Error`.
- **`503` responses create no job**: `POST /ingest` acquires the per-collection lock before creating its job, so a lock-timeout `503` no longer leaves a `PENDING` job that never runs; `POST /collections/{name}/reindex` answers `503 service not ready` before creating a job (it used to leave a `FAILED` reindex job and point the collection's `reindex_job_id` at it).

**Migration:** clients that parsed exception text out of a failed ingest or reindex job's `error` should read the server log instead. `DELETE /collections/{name}` callers should treat `500` with `code == "collection_drop_failed"` as "not removed" (after a failed meta-row delete, the table and config entry are already gone; retry the `DELETE`). Nothing to do for the `503` change.

**Announced in**: this release.

---

### [next release] — MCP tool errors, background-job `error` and maintenance `last_error` report fixed text, not exception text (2026-10-17)

**Surface**: MCP tools `search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`, `update_collection`, `export_collection`, `import_collection`, `create_key`, `revoke_key`, `rotate_key`, `get_graph`, `get_graph_cross_collection`, `graph_impact` → `error`, and the error text of any exception a tool does not catch; `GET /jobs/{id}` / `GET /jobs` → `error` of ingest, reindex, migration, metadata-reindex, community-rebuild and scheduler-dispatched jobs; `GET /status` → `collection_health[*].last_error`; `POST /search` / `POST /explain` with `hyde=true` or `rag_fusion=true` (`422` / `500` `detail`); `POST /keys/rotate` → `500` body, and `422` for a `grace_seconds` above the new maximum; REST and `/mcp` → which default key authenticates after an MCP `rotate_key`, and the key it retires; `POST /keys` and `DELETE /keys/{key_id}` → `500` when `keys.json` cannot be read; `[auth].rotate_grace_seconds` (config load); `GET /graph/{collection}` and `GET /graph/cross-collection` with `format=graphml`. CLI: `archon-search jobs show` / `jobs status` (`error`), every command that waits on a job (`Job FAILED: <error>`, e.g. `ingest`, `sync`, `jobs show --wait`), `archon-search maintenance status` (`last_error`) and `archon-search key rotate` / `key create` / `key revoke` (the `500` body) print these fields.

**Schema changes in `GET /openapi.json`: `KeyRotateRequest.grace_seconds` gains `maximum: 3153600000` (`MAX_ROTATE_GRACE_SECONDS`), and `POST /keys/rotate`, `POST /keys`, `DELETE /keys/{key_id}` and `POST /search` now declare a `500` response with the `ErrorDetail` body (`{"detail": str}`) — on `POST /keys` and `DELETE /keys/{key_id}` it covers only the unreadable-`keys.json` refusal, on `POST /search` only the HyDE-failure `500`; other failures there are still the undeclared plain-text `500`. No field was added, removed or retyped; everything else is behaviour only.**

**What changed:** these fields echoed the caught exception's message, which can embed server paths and library internals. They now carry fixed text; the exception is logged server-side with its traceback.

- MCP `internal_error`: `internal error; see the server log for details`, except `search` / `search_with_context` (`search failed`) and `explain` (`explain failed`, its existing text). MCP `schema_validation_error`: `response schema validation failed; see the server log for details`, except `search` / `search_with_context` (`search response failed schema validation`) and `explain` (`explain response failed schema validation`). An invalid `search` / `search_with_context` filter argument answers `validation_error` with `invalid search filters` instead of the Pydantic text, which echoed the rejected value. MCP `rotate_key` write failure (an unusable `ARCHON_SEARCH_KEY_FILE`, creating the key file's directory or writing `.search.env`): `Failed to write .search.env — rotation aborted; keys.json unchanged.` (the REST `POST /keys/rotate` text). `create_key`'s catch-all changes from `Failed to create key` to `key creation failed; see the server log for details` and is now logged with the traceback.
- Key rotation: `POST /keys/rotate` and MCP `rotate_key` failing to update `keys.json` after `.search.env` was written now answer `Rotation incomplete: .search.env holds the new key but keys.json was not updated; see the server log for details` — REST as a JSON `500` instead of the plain-text `Internal Server Error`, MCP as `internal_error`. REST failing to create the key file's directory, or finding `ARCHON_SEARCH_KEY_FILE` unusable (relative, or `~` with `HOME` unset), now answers the JSON `500` `Failed to write .search.env — rotation aborted; keys.json unchanged.` instead of the plain-text one (MCP: that text as `internal_error` instead of the escaped exception text). A recorded token that differs from the one written (a defensive bug check) answers the incomplete-rotation text instead of a plain-text `500` / escaped `RuntimeError`. Invalid rotation input (`KeyRotationInputError`) is now rejected before `.search.env` is written, so nothing has changed when it is reported. `grace_seconds` is now capped at `MAX_ROTATE_GRACE_SECONDS` (100 years, `3153600000`; `constants.py`) — a larger value overflowed `datetime` after `.search.env` was written: REST `POST /keys/rotate` answers FastAPI's standard `422` (list `detail`, `less_than_equal`) for a body `grace_seconds` above it, as it already did for a negative one; MCP `rotate_key` answers `validation_error` with `grace_seconds must be >= 0, got <n>` or `grace_seconds must be <= 3153600000, got <n>` (the negative-value text was `grace_seconds must be >= 0`); config load rejects `[auth].rotate_grace_seconds` above it with `ConfigError`. A blank current key raises `BlankCurrentKeyError` (a separate `ValueError`, not a `KeyRotationInputError`), a defensive server-fault check that the server's current default key (`app.state.api_key`, from `load_or_generate_key` or a previous rotation) cannot trigger: REST `500` with `current_token must not be empty` as `detail`, MCP `rotate_key` `internal_error` with the same text (`{"error": "current_token must not be empty", "code": "internal_error"}`), while its `grace_seconds` errors stay `validation_error`. An unreadable `keys.json` is now refused before `.search.env` is written (next bullet); past that check every `keys.json` failure is the incomplete-rotation answer above, and no exception text (e.g. a pydantic `ValidationError` while loading `keys.json`) reaches the client. REST and MCP rotations now share one lock (`KeyStore.rotation_lock`), so concurrent rotations through the two transports are serialized within the server process.
- One current default key for REST and `/mcp`: MCP `rotate_key` now retires the server's current default key (`app.state.api_key`, the one `POST /keys/rotate` retires) and makes the new key current for both transports at once, and the mounted `/mcp` app checks the default key against that same `app.state.api_key`. Before, MCP `rotate_key` retired the key read from `.search.env` and never updated `app.state.api_key`: after an MCP rotation, a rotated-out default key with no revoked or expired `keys.json` record (an auto-generated or installer-written key rotated with no grace) kept authenticating REST and `/mcp` until restart, and a later `POST /keys/rotate` retired that old key instead of the MCP-issued one, which stayed active in `keys.json`. A `POST /keys/rotate` already took effect on `/mcp` at once, because the REST app's auth middleware also guards the `/mcp` mount; the documented limitation S24 (`/mcp` kept accepting the old key until restart) described only the inner `/mcp` check, which that outer check masked.
- A key rotation whose request is cancelled after the pre-write checks now completes instead of possibly stopping between the `.search.env` and `keys.json` writes; the caller gets no response, and the server logs the outcome (WARNING `rotate: cancelled mid-rotation, but it completed: …`, or ERROR `rotate: cancelled mid-rotation, and the rotation failed` / `rotate: cancelled mid-rotation and the rotation task was cancelled too; …`). An unexpected MCP `rotate_key` failure is logged as `rotate_key failed`.
- Ingest jobs: when the ingest succeeds but its `DONE` (or `CANCELLED`) status write fails, the job is no longer marked `FAILED`; the write failure is logged (`background ingest: could not persist DONE status for job <id>`) and the job keeps its last stored status.
- Unreadable `keys.json`: a key mutation finding `keys.json` present but unreadable or unparseable (empty, invalid or non-UTF-8 JSON, JSON the parser rejects for an integer over its 4300-digit limit or nesting past its recursion limit, not an array, an invalid record) now refuses instead of rewriting the file with only its own record — which deleted every managed key. `POST /keys`, `DELETE /keys/{key_id}` and `POST /keys/rotate` answer a JSON `500` and MCP `create_key`, `revoke_key` and `rotate_key` answer `internal_error`, all with `keys.json could not be read; no key changes were made; see the server log for details`; nothing is written (a rotation leaves `.search.env` untouched). Before, a key creation or rotation succeeded while rewriting the file without the stored keys (a revoke answered `404` `Key not found`), and startup rewrote it with only the TOML records; startup now logs an ERROR and leaves it unchanged. Authentication and `GET /keys` / `list_keys` still treat such a file as empty.
- Durable key writes: when the directory fsync fails after `.search.env` or `keys.json` was renamed into place (`DurabilityUnconfirmedError`), the write is now treated as done and logged as a WARNING — the rotation completes, `POST /keys` / `DELETE /keys/{key_id}` and MCP `create_key` / `revoke_key` succeed. Before, a rotation reported `rotation aborted; keys.json unchanged` although `.search.env` already held the new key (or, for `keys.json`, failed as a plain-text `500` / escaped MCP error after recording it), and create/revoke failed although the change was written.
- MCP failures that used to escape the tool — and reach the client as `Error calling tool '<name>': <exception text>` — now return `internal_error` with `internal error; see the server log for details`: `revoke_key` failing to write `keys.json`; `rotate_key` failing to read the current key (a `keys.json` update failure answers the incomplete-rotation text above, also as `internal_error`); `export_collection` / `import_collection` failing to look up the collection or to create the job.
- An ordinary exception that still escapes an MCP tool now reaches the client as `Error calling tool '<name>'`, without the exception text (FastMCP `mask_error_details=True`). FastMCP does not mask everything: argument-validation errors are unchanged, and a pydantic `ValidationError` raised inside a tool body or any `FastMCPError` (`ToolError`, …) is re-raised unmasked and sent as its own text — which is why the tools still catch their response-schema `ValidationError`s themselves.
- Job `error`: `ingest failed; …`, `reindex failed; …`, `migration failed; …`, `metadata reindex failed; …`, `community rebuild failed; …` (each ending `see the server log for details`); a job the scheduler fails to start reports `dispatch_failed; see the server log for details` instead of `dispatch_failed: <exception text>`. A community rebuild missing `leidenalg`/`igraph` reports `community detection requires the archon-search[graph] extra; see the server log for the install command` instead of the install hint (see the missing-extra bullet below). Unchanged: a community rebuild's `No entity graph nodes found …`, a resumed migration's `MigrationJob <id>: no pending REWRITE migration found …`, and an ingest or `collection add` job whose `path` does not exist keeps `path does not exist or is not a file/directory: <path>` — `<path>` is now the caller's `path` as submitted (before `~` expansion and symlink resolution), not the server's resolved path. The job's `source_path` (`POST /ingest` `202` `JobResponse` and `GET /jobs/{id}`) still holds the resolved path; it is `""` for a `collection add` job.
- Maintenance `last_error`: `fts_optimize failed; …`, `orphan_cleanup failed; …`, `expired_chunk_pruning failed; …`, `graph_gc failed; …`.
- Missing optional extra: the install command no longer reaches the client. In a `uv tool` install it is rebuilt from the tool receipt and can carry `--index-url` credentials, `--find-links` and local source paths, so it is logged server-side as a WARNING and the wire gets fixed text instead: HyDE (REST `POST /search` / `POST /explain` `422` `detail`, MCP `search` / `search_with_context` / `explain` `validation_error`) `Install archon-search[hyde] to use HyDE` instead of `Install archon-search[hyde] to use HyDE (<install command>)`; RAG Fusion (same surfaces) `Install archon-search[rag_fusion] to use RAG Fusion` instead of `Install archon-search[rag_fusion] to use RAG Fusion (<install command>)`; GraphML export (`500` `detail`) `GraphML export requires the archon-search[graph] extra; see the server log for the install command` instead of `GraphML export requires networkx; install archon-search[graph]`; community rebuild job `error` as above. Any other `ImportError` from GraphML export is a plain `500`.
- HyDE: any HyDE generation failure other than the missing provider package (e.g. the embedder) now answers `500` instead of `422` with the exception text — `POST /explain` with `{"detail": "explain failed"}`, `POST /search` with `{"detail": "search failed"}`, each with an error telemetry entry — and MCP `internal_error` instead of `validation_error`, with the matching REST text (`search` / `search_with_context`: `search failed`, `explain`: `explain failed`) and an error telemetry entry.

**Migration**: clients that parsed exception text from these fields must match the fixed wording; the cause is in the server log. Clients that read the install command from a missing-extra error must take it from the server log. Clients that treated any other HyDE failure as `422` must expect `500` / `internal_error`. Clients that relied on an invalid rotation being reported only after `.search.env` was rewritten get a `422` / `validation_error` (a blank server-held key: REST `500`, MCP `internal_error`) with nothing written. Keep `grace_seconds` / `[auth].rotate_grace_seconds` at or below `3153600000`. Clients that read a `POST /keys/rotate` `500` as plain text can now parse its JSON `detail`. Clients and operators that relied on key changes succeeding against a corrupt `keys.json` must restore or repair the file first (see `Documentation/OperatorGuide/70_key_management_and_rotation.md`). REST and MCP clients must switch to the new default key right after an MCP `rotate_key` (or within its grace window) instead of at the next server restart. Job monitors must not treat an ingest job whose status write failed as `FAILED`; the server log has the write error.

**Announced in**: this release.

---

### [next release] — `POST /search` and MCP `search`: any `graph_mode` now suppresses HyDE (2026-10-17)

**Surface**: REST `POST /search` and MCP tool `search` (single- and multi-collection) → `hyde_applied`, `expansion_used` and `expansion_warning` of a `hyde=true` call that also sets `graph_mode`.

**What changed:** Behaviour only, no schema change. A `hyde=true` request with any non-null `graph_mode` used to call the HyDE LLM (sending the query to the provider) and report `hyde_applied: true` even where the vector was dropped: the single-collection `"ppr"`, `"local"` and `"global"` paths never used it, `"naive"` dropped it whenever the graph expansion added terms, and the fan-out's `"naive"` always re-embedded. Any `graph_mode` now suppresses HyDE the way it already did on `POST /explain` and MCP `explain`: no LLM call, `hyde_applied: false`, no `expansion_warning`. The paths that did use the vector — a single-collection `"naive"` search whose expansion adds nothing, the fan-out `"local"` legs, and a fan-out `"global"` search that falls through to the standard path — now search with the plain query embedding instead.

**Migration**: clients that combined `hyde=true` with `graph_mode` must expect `hyde_applied: false`; send `graph_mode: null` to use HyDE.

**Announced in**: this release.

---

### [next release] — HyDE on MCP `explain` and REST `POST /explain` embeds with the explained collection's model and is bounded by the search budget; MCP auto-routing no longer routes on the HyDE vector (2026-10-17)

**Surface**: MCP tool `explain` and REST `POST /explain` → `hyde_applied`, the results of a `hyde=true` call, and (MCP) `routing` of a collectionless (auto-routed) `hyde=true` call; MCP `search_with_context` → `hyde_applied` of a slow-LLM `hyde=true` call.

**What changed:** Behaviour only, no schema change. On MCP `explain` the HyDE vector used to be embedded with the global model and then discarded whenever the explained collection was not served by the pipeline's own global embedder instance — which, with the embedder cache every shipped server runs, was every call — so `hyde_applied` came back `false` and the trace was the plain-query one. On REST `POST /explain` the global-model vector was used even for a collection pinned to another model, so it searched the wrong vector space (or failed on a dimension mismatch). On both, the hypothesis is now embedded with the explained collection's model (the global model on a `collections` fan-out, which excludes collections on another model) and used, so `hyde_applied` is `true` whenever HyDE produced a vector and the results reflect it; HyDE is also resolved only after the collection is found, so an unknown collection no longer triggers the LLM call. A collectionless MCP call now routes on the plain query embedding (as REST `POST /explain` always has) instead of the HyDE vector, so `routing.candidates` scores and the chosen collection can differ from before. HyDE on both `explain` surfaces and on MCP `search_with_context` is now bounded by HyDE's own ~30 s cap (S582; these surfaces have no search budget) and skipped (`hyde_applied: false`) when the LLM does not answer within it. It is not skipped while the serving embedder is still cold: these surfaces have no FTS-only degrade, so their retrieval embeds on that model regardless and the build is waited on once.

**Migration**: none required for clients that read `hyde_applied`; clients that compared `hyde=true` `explain` traces with plain ones must expect them to differ now, and an auto-routed MCP `hyde=true` call to pick the collection a plain call picks.

**Announced in**: this release.

---

### [next release] — MCP `search` / `search_with_context` / `explain` errors and the HyDE / RAG Fusion missing-package `422` no longer echo exception or input text (2026-10-17)

**Surface**: MCP tools `search` (single- and multi-collection) and `search_with_context` → the `error` field of `internal_error`, `validation_error` (filter arguments) and `schema_validation_error` responses; MCP tool `explain` → the `error` field of `schema_validation_error` responses; the HyDE and RAG Fusion missing-package errors on REST `POST /search` and `POST /explain` (`422` `detail`) and on the MCP `search`, `search_with_context` and `explain` tools (`validation_error`).

**What changed:** An unexpected pipeline failure returned `{"error": str(exc), "code": "internal_error"}`, leaking internals such as LanceDB's `Invalid input, No vector column found to match with the query vector dimension: 384`. It now returns the fixed `{"error": "search failed", "code": "internal_error"}`; the full exception is still logged server-side. A Pydantic `ValidationError` — whose text embeds the rejected input values — likewise no longer reaches the wire: an invalid filter argument returns `{"error": "invalid search filters", "code": "validation_error"}` and response-schema drift `{"error": "search response failed schema validation", "code": "schema_validation_error"}` (mirroring `explain`'s existing `"invalid explain request"`); MCP `explain`'s response-schema drift likewise answers `{"error": "explain response failed schema validation", "code": "schema_validation_error"}`. A missing HyDE provider package now answers the fixed `"Install archon-search[hyde] to use HyDE"`, and a missing RAG Fusion package the fixed `"Install archon-search[rag_fusion] to use RAG Fusion"`, without the parenthesised install command for the server's own environment, which goes to the server log; any other failure while resolving HyDE (e.g. the embedding model failing to load) is no longer reported as a `validation_error` carrying its raw text but falls to the generic sanitized failure (`"search failed"` / `"explain failed"` on MCP, `500` on REST).

**Migration**: clients that parsed the `error` text of these responses must key on `code` instead; the text carries no diagnostic detail any more — read the server log.

**Announced in**: this release.

---

### [next release] — export/import: an import resume continues only into the collection its job wrote, otherwise restarts from scratch; more bad documents are refused per document and skippable under `on_error="skip"` (2026-10-17)

**Surface**: `POST /jobs/{id}/resume` of a `FAILED` import job, and that job's `error`, `progress` and result in `GET /jobs/{id}` / `GET /jobs` (imports started by `POST /collections/{name}/import`, `archon-search import`, or the MCP `import_collection` tool); the per-document refusals of every import job — its `error` under `on_error="fail"`, its `skipped` count under `on_error="skip"`.

**Behaviour-only change — no schema field was added, removed or retyped, so `GET /openapi.json` needs no regeneration.**

**What changed:**

- **A resume continues only into the collection its job wrote.** Each import run claims its collection right before its first ingest write to it — after any `force_overwrite` drop — and that claim revokes the claim of every other import job of the same collection name — in any namespace, whatever its status. A resumed import now continues from its checkpoint only while (a) its own claim has not been revoked and (b) the collection still holds the archive's first valid document as archived — the same `chunk_id` and `indexed_at`. Otherwise it logs a WARNING and restarts from scratch: with nothing left under the name it imports the whole archive; with anything there it fails `collection '<name>' already exists and was not written by this import; start a new import with force_overwrite=true` — even when the job was started with `force_overwrite=true`: a resume of a job that claimed its collection or checkpointed documents never drops it. A resume of a job that failed before its claim with no checkpoint runs as a fresh import, as before: it refuses an existing collection, or drops it under `force_overwrite=true`. Before, a resume with a checkpoint skipped the collection-exists guard and appended into whatever collection existed under the name, or recreated a deleted one and imported only the archive's remainder, reaching `DONE`. Because `MaintenanceLoop` orphan cleanup (when enabled) deleted the rows of source files missing on this host, a resume of an archive exported from another machine was refused this way once an orphan-cleanup pass had run on the collection; the sweep now skips imported rows (see the `_imported` entry above), so this still happens only to rows imported before that marker existed.
- **A fresh import is unchanged**: it still fails `collection '<name>' already exists; use force_overwrite=true to overwrite`, or drops the collection under `force_overwrite=true`, checked before the archive is scanned, so its error precedence is the same as before.
- **No second copy of documents committed after the last checkpoint.** A continuing resume drops from its batches the chunks already stored in the collection, until the first batch none of whose chunks is stored; before, a batch committed just before a crash, ahead of its checkpoint, was appended a second time. Accepted limitation: a resume with a smaller `[jobs].checkpoint_interval` than the failed run, whose overlapping rows were deleted since, can still re-append part of one old batch.
- **A resume that fails before its first new checkpoint keeps the checkpoint** (e.g. the archive is rejected because the server's embedding model changed), unless it restarted from scratch — its claim then resets the checkpoint to the start of the archive. Before, it reset `progress.processed` to 0, so the next resume ran as a fresh import: it failed `already exists` without `force_overwrite=true`, and with it dropped the collection and imported the whole archive again.
- **A resume after a crash before the job's first checkpoint is a resume**, subject to the checks above. Before, it ran as a fresh import: it failed `already exists` without `force_overwrite=true`, and with it dropped the collection and imported the whole archive again.
- **More bad documents are refused per document** — and so skipped and counted in `skipped` under `on_error="skip"` instead of failing the whole job. The import decoder now runs `validate_chunk_record`, the per-chunk checks `ingest_chunks` applies, on each document:
  - an empty vector: now `corrupt doc (vector) at line N: missing or not base64-encoded float32`; before, it was not rejected as a corrupt vector;
  - a vector with a NaN component, which LanceDB refuses for its whole batch (infinities it stores): now `corrupt doc (vector) at line N: a component is NaN`; before, it failed the whole job, even under `on_error="skip"`, with `import failed; see the server log for details`;
  - a `doc_id`, `text`, `source_path`, `indexed_at`, `file_type`, `language` or `updated_at` that is neither a string nor null; a string in any of those fields, in `acl_source` or `acl_sidecar_path`, or an item of `acl`, `scopes` or `acl_warning`, holding a lone surrogate (JSON `"\ud800"`, which UTF-8 cannot encode; an `ingested_by` holding one still imports as `cli`, and a `metadata` holding one is stored JSON-escaped, both as before); a `metadata` that is not a JSON object of string values (or a string holding one), is JSON nested past the parser's recursion limit, or is an object outside the metadata limits; a `chunk_id` that is not a string of the form `<64 lowercase hex>-<6 ASCII digits>`; a `custom_score` that does not convert to a number; and an `expires_at` that is neither null nor an ISO-8601 timestamp string: `import failed; see the server log for details` → `malformed doc at line N: a required field is missing or has the wrong type`. `indexed_at` and `updated_at` are not parsed: a string is stored as archived, except that an empty or null `updated_at` is stored as the document's `indexed_at`.
- **A vector-dimension mismatch still fails the whole job, even under `on_error="skip"`**, now with its own message: a non-empty vector that decodes in full to another number of floats than the archive's first valid document's fails `import failed; see the server log for details` → `corrupt doc (vector) at line N: its dimension differs from the archive's first valid document`. An empty vector, or one whose byte length is not a multiple of 4, is refused as `missing or not base64-encoded float32` instead, and skipped under `on_error="skip"`.
- **A `chunk_id` must now match in full**: `ingest_chunks` now refuses one with a trailing newline or non-ASCII digits, which it accepted before — on every ingest path, not only imports, where it gets the `malformed doc` message above (archon-search's own chunk ids never take that form).
- **The embedding dimension now comes from the archive's first valid document** — the first that decodes in full. Before, the first document whose `vector` merely base64-decoded set it, as its byte length divided by 4 rounded down: an empty first vector, or one 1–3 bytes short, set a wrong dimension, and under `on_error="skip"` the import then failed with `import failed; see the server log for details` instead of skipping that document (a vector 1–3 bytes longer than a valid one still gave the right dimension, and only that document was refused). Now such a first document is refused or skipped like any other. A first vector that decodes in full to fewer or more floats still sets the dimension, and the job then fails at the first later document whose vector decodes in full to another number of floats, with the dimension message above.

**Migration**: a resumed import refused with `… already exists and was not written by this import; start a new import with force_overwrite=true` needs a new import with `force_overwrite=true`. Clients that matched `import failed; see the server log for details` for the per-document cases above must match the new `corrupt doc (vector)` (including `… a component is NaN`) / `malformed doc` messages; with `on_error="skip"` those documents now land in `skipped` and the job reaches `DONE`. A vector-dimension mismatch still fails the job, now with `corrupt doc (vector) at line N: its dimension differs from the archive's first valid document`. A document whose `chunk_id` ends in a newline or uses non-ASCII digits can no longer be imported or ingested; with `on_error="skip"` an import skips it. Nothing else is needed.

**Announced in**: this release.

---

### [next release] — CLI `--wait` exit codes: `import`, `export` and `backup --now` no longer exit `0` when a job ends `CANCELLED` (or, for `import`, `FAILED_EXPIRED`) (2026-10-17)

**Surface**: `archon-search import --wait`, `archon-search export --wait`, `archon-search backup --now --wait` → process exit code and stderr.

**What changed:** A terminal job status other than `DONE` or `FAILED` fell through to a success path (`archon_search/cli/export_cmd.py` `_poll_job` / `_poll_import_job`, `archon_search/cli/backup_cmd.py` `_wait_for_jobs`):

- `import --wait`: a job ending `FAILED_EXPIRED` or `CANCELLED` printed `Job ended with status: <STATUS>` and exited `0`. It now exits `1`, printing `Import FAILED: <error>` (for `FAILED_EXPIRED`, as for `FAILED`) or `Import CANCELLED: the collection may be partially or fully imported; re-run the same import command with --force-overwrite to restore a complete copy.` to stderr. `DONE` still exits `0`; `import` still has no `--timeout`.
- `export --wait`: a job ending `CANCELLED` printed `Job ended with status: CANCELLED` and exited `0`. It now exits `2` (as `FAILED` / `FAILED_EXPIRED` already did), printing `Export CANCELLED: no archive was produced.` to stderr. `DONE` and a `--timeout` still exit `0`; the timeout hint now reads `Timed out after <N>s waiting for export job to complete. Check status: 'archon-search jobs status <job_id>' with the same --api-url/--api-key.` instead of pointing at `GET /jobs/{job_id}`.
- `backup --now --wait`: when any job ended `CANCELLED` (and none `FAILED`), it printed `Backup finished (N collection(s) cancelled).` and exited `0`. It now prints `Backup incomplete: N collection(s) cancelled.` to stderr instead and exits `2`; the per-collection `{collection}: CANCELLED` line moved from stdout to stderr, joining `{collection}: FAILED — <error>`. When a job was cancelled before the `--timeout` elapsed, it now prints `Some backup jobs were cancelled before timeout. Pending: <collections>. Check status: 'archon-search jobs status <job_id>' with the same --api-url/--api-key (job IDs are listed under 'Queued jobs:').` (`failed or were cancelled` when jobs did both) and exits `2` instead of the timeout hint and exit `0`; the failed-before-timeout line now lists the pending collections and the same hint too. The plain timeout hint (exit `0`) now reads `Timed out after <N>s waiting for backup jobs to complete. Pending: <collections>. Check status: 'archon-search jobs status <job_id>' with the same --api-url/--api-key (job IDs are listed under 'Queued jobs:').` — the old hints pointed at `archon-search backup status`, which shows no job status. All `DONE`, or a timeout with nothing failed or cancelled, still exits `0`.

Exit `1` on connection, auth, or HTTP errors is unchanged for all three.

**Migration**: scripts that treated exit `0` from these commands as "finished, whatever the outcome" must handle the new codes: `1` from `import --wait`, `2` from `export --wait` and `backup --now --wait`. A cancelled export produces no archive, a cancelled backup skips that collection, and a cancelled import may leave a partial collection. Exit `2` from `export`/`backup` also means `FAILED`/`FAILED_EXPIRED`, so a bare `|| [ $? -eq 2 ]` would swallow failures too: a script that tolerates cancellation must confirm the job ended `CANCELLED` before accepting the non-zero code — from the per-collection `{collection}: CANCELLED` / `{collection}: FAILED — <error>` lines `backup --now --wait` prints to stderr, or with `archon-search jobs status <job_id>` passing the same `--api-url`/`--api-key` (job IDs are printed when the jobs are queued).

**Announced in**: this release.

---

### [next release] — MCP `search_with_context`: `context_window` must be in `[0, [search].context_window_max]` (default 10); `GET /status` `search` gains `context_window_max` (2026-10-17)

**Surface**: the MCP tool `search_with_context` → its `context_window` argument; the new config key `[search].context_window_max`; `GET /status` → `search` (`SearchStatusDetail`).

**The tool's input schema keeps `context_window` as `int`, default `1`, with no `minimum` / `maximum`; only its `description` now states the `[0, [search].context_window_max]` bound. In `GET /openapi.json`, `SearchStatusDetail` gains the required integer `context_window_max` (additive; the tool itself has no REST counterpart).**

**What changed:**

- Any `context_window` integer was accepted. A large value made the server build up to `2 * context_window` neighbour chunk ids per hit and query them all at once — an unbounded memory allocation and an equally oversized database query (the lower side is clamped at chunk `0`, so `10**9` still meant at least `10**9` ids); a negative value silently returned empty `context_before` / `context_after`. A value outside `[0, context_window_max]` now returns `{"error": "context_window must be in [0, N]; got M", "code": "validation_error"}` before any search runs, where `N` is the configured bound. `0` stays valid and returns no neighbours.
- The bound is the operator setting `[search].context_window_max` (integer, default `10`, must be `>= 1` so the tool's default window of `1` stays valid; a value below `1`, a boolean, a fractional or a non-numeric value is a `ConfigError` at load time).
- `GET /status` → `search` reports the live value as `context_window_max`, next to `max_fanout` and `top_k_max`.

**Migration**: clients passing `context_window` above the bound (`10` unless the operator set `[search].context_window_max`) must lower it, or the operator must raise the setting; clients passing a negative value must pass `0` for the same empty-context result. Clients with strict `StatusResponse` type stubs add `context_window_max: int` to `search`. Nothing else is needed.

**Announced in**: this release.

---

### [next release] — config: number keys reject booleans, `inf`/`nan` and fractional integers; `[database].validation_timeout_seconds` is a float (2026-10-17)

**Surface**: `archon-search.toml` loading (`load_config`, server startup, every CLI command that reads the config).

**What changed:**

- **A TOML boolean in a float key is a `ConfigError` at load time** (`Expected float for '<key>', got bool`). Before, `float(True)` silently became `1.0` for `[database].language_detection_confidence_threshold`, `[search].fanout_timeout_seconds`, `[routing].routing_confidence_threshold`, `[routing].routing_description_weight`, `[hyde].timeout_seconds`, `[rag_fusion].timeout_seconds` and `[graph].extraction_timeout_seconds`. `false` (`0.0`) loaded silently only for the two `[routing]` keys, whose range is `[0.0, 1.0]`; the other five already failed their range check with `0.0`. The `[graph]` keys `leiden_resolution`, `ppr_damping`, `synonym_threshold`, `ner_confidence` and `relation_confidence` already rejected booleans and are unchanged.
- **`inf`, `-inf` and `nan` in any float key are a `ConfigError`** (`Expected finite float for '<key>', got inf`), including the quoted string forms (`"inf"`). Before, a key whose only check was `> 0` loaded them — `inf` as a timeout (`fanout_timeout_seconds`, `[hyde]`/`[rag_fusion]`/`[graph]` timeouts) or `leiden_resolution`, and `nan` there too, since `nan <= 0` is false. A TOML integer too large for a float is also a `ConfigError` (`Expected finite float for '<key>', got an int beyond float range`) instead of an uncaught `OverflowError`.
- **A TOML boolean in a lenient integer key is a `ConfigError`** (`Expected integer for '<key>', got bool`). The lenient integer keys are the ones loaded through `_coerce_int`: `[server].port`; `[database]` `chunk_size`, `top_k_retrieve`, `top_k_return`, `centroid_recompute_threshold`, `embedder_cache_size`; `[search]` `max_fanout`, `fanout_leg_trim`, `top_k_max`; `[routing].routing_shortlist_size`; `[logging].backup_count`; `[telemetry].retention_days`; `[hyde].max_requests_per_minute`; `[rag_fusion]` `max_requests_per_minute`, `num_queries`; `[jobs]` `max_concurrent_bulk`, `checkpoint_interval`; `[backup]` `interval_hours`, `keep`; `[maintenance]` `interval_hours`, `retry_max_attempts`, `retry_max_age_hours`; `[auth].rotate_grace_seconds`; `[graph]` `extraction_rate_limit_rpm`, `extraction_token_budget`. Before, `int(True)` loaded every one of them as `1`. `false` (`0`) loaded silently only for the keys whose range admits `0` — `[logging].backup_count`, `[backup].interval_hours`, `[backup].keep`, `[maintenance].interval_hours`, `[maintenance].retry_max_age_hours` and `[auth].rotate_grace_seconds`; the others already failed their `> 0` / `>= 1` range check with `0`.
- **A fractional value in a lenient integer key is a `ConfigError`** (`Expected integer for '<key>', got Float`) instead of being truncated — `top_k_return = 5.9` used to load as `5`. `inf` there is the same `ConfigError` instead of an uncaught `OverflowError`; `nan` already raised that exact `ConfigError` and is unchanged. Integral floats such as `5.0` and numeric strings such as `"5"` still load as `5` in these keys.
- **The strict integer keys are unchanged.** `[ingest].max_file_mb`, `[ingest].max_tasks_per_child`, `[graph].backend_threshold_edges`, the `[graph]` keys `max_community_size`, `community_summary_chunks`, `max_global_candidates`, `max_inspection_nodes`, `max_inspection_edges`, `ppr_top_entities`, `naive_max_expansion_terms`, and `[openai_shim].top_k` already required a TOML integer: any boolean, float (`5.0` included) or string was, and still is, a `ConfigError`.
- **`[database].validation_timeout_seconds` is now a float** (`SearchConfig.validation_timeout_seconds: float = 60.0`). Fractional values such as `1.5` are honoured instead of being truncated by `int()`. `true`, which `int(True)` previously turned into a 1-second timeout, and `false`, which previously loaded as `0` and then warned and fell back to `60`, are now a `ConfigError`. `inf` (formerly an uncaught `OverflowError`) and `nan` are a `ConfigError` too; values `<= 0` still warn and fall back to `60.0`. The timeout warning in `provider_warnings` still reads `validation timed out after 60s` for whole-second values (`1.5` renders `1.5s`).

**Migration**: replace any `true`/`false` in a number key with a number (`true` was equivalent to `1`), replace `inf`/`nan` with a finite value, and write integer keys as whole numbers. Library callers reading `SearchConfig.validation_timeout_seconds` receive a `float`; an integer in the TOML file still loads (as `30.0` for `30`).

**Announced in**: this release.

---

### [next release] — Default config path follows `ARCHON_SEARCH_DATA_DIR`; `serve` no longer warns about an unset `ARCHON_SEARCH_CONFIG` (2026-10-17)

**What changed:** With `ARCHON_SEARCH_CONFIG` unset, the default config (`config.get_default_config_path()`, via `paths.get_data_dir()`) is now `<data-dir>/archon-search.toml` instead of always `~/.archon-search/archon-search.toml`, so it follows `ARCHON_SEARCH_DATA_DIR`; with the default data dir the path is unchanged. In the Docker image (`ARCHON_SEARCH_DATA_DIR=/data`, `HOME=/data`, no `ARCHON_SEARCH_CONFIG`) it moves from `/data/.archon-search/archon-search.toml` to `/data/archon-search.toml`. Without `--config`, the plist/unit written by `install` / `wizard` already pointed at `<data-dir>/archon-search.toml` while the wizard wrote its config to `~/.archon-search/archon-search.toml`; both now use the same default, and the plist/unit also honours an `ARCHON_SEARCH_CONFIG` set at install time (`platform/macos.py`, `platform/linux.py`). `archon-search serve` no longer logs the `ARCHON_SEARCH_DATA_DIR is set but ARCHON_SEARCH_CONFIG is not` warning.

**Affected commands:** every command that resolves the default config without `--config` — `serve`, `start`, `config show/get/set`, `wizard`, `install`, `uninstall --delete-db` — and the server's own config writes (`collection add/remove`). An invalid `ARCHON_SEARCH_DATA_DIR` (empty, relative) now fails default config path resolution with `ConfigError`: each of these commands prints `Error: ...` and exits `1`. `serve` and `start` already did (via `load_config`); `config` (which silently used `~/.archon-search/archon-search.toml`), `wizard`, `install` and `uninstall --delete-db` (which failed with a traceback) now exit cleanly, and `uninstall --delete-db` exits before stopping or unregistering the service.

**Legacy warning:** with `ARCHON_SEARCH_CONFIG` unset and no `--config`, `serve`, `config show/get/set`, `wizard`, `install` and `uninstall --delete-db` log a WARNING when `<data-dir>/archon-search.toml` does not exist but a legacy `~/.archon-search/archon-search.toml` does, telling you to move it into the data dir or set `ARCHON_SEARCH_CONFIG`. `serve` emits it once logging is configured, so it lands in the configured `[logging]` destination and format (the log file, or stderr in container mode).

**Migration** — installs with a custom `ARCHON_SEARCH_DATA_DIR`, no `ARCHON_SEARCH_CONFIG`, and a config at `~/.archon-search/archon-search.toml` (including Docker containers whose config sits at `/data/.archon-search/archon-search.toml`); default-data-dir installs need no change:

1. Before re-running `wizard` or `install`, move the file to `<data-dir>/archon-search.toml` (`/data/archon-search.toml` in Docker), or set `ARCHON_SEARCH_CONFIG` to its current path. Otherwise the server starts with built-in defaults and its collections are no longer configured, `config set` starts a new file in the data dir, `wizard` writes a fresh default config to `<data-dir>/archon-search.toml` that the legacy file's settings never reach, and `install` exits 1 with `No configuration found at <path>`.
2. Managed services keep the explicit `ARCHON_SEARCH_CONFIG` baked into their plist/unit until re-installed.
3. Drop any log alert keyed on the removed `serve` warning.

**Announced in**: this release.

---

### [next release] — Linux systemd unit: `EnvironmentFile=` follows `ARCHON_SEARCH_DATA_DIR` (2026-10-17)

**Surface**: the systemd user unit `archon-search install` writes on Linux (`~/.config/systemd/user/archon-search.service`) → `EnvironmentFile=`.

**What changed:** The unit loaded `EnvironmentFile=-%h/.archon-search/.secrets.env` regardless of `ARCHON_SEARCH_DATA_DIR`, while the wizard creates the file under the data dir — so with a custom data dir the service never saw the provider keys added there. The unit now loads `EnvironmentFile=-<data-dir>/.secrets.env` (`archon_search/platform/linux.py`, via `platform.service.get_secrets_env_file()`), the same file the wizard creates and the macOS launchd wrapper sources. Only Linux installs with a custom `ARCHON_SEARCH_DATA_DIR` are affected; with the default data dir the path is unchanged.

**Migration**: Linux installs with a custom `ARCHON_SEARCH_DATA_DIR` whose keys sit in `~/.archon-search/.secrets.env`: move those keys into `<data-dir>/.secrets.env`, then re-run `archon-search install` with the same `ARCHON_SEARCH_DATA_DIR`, passing `--config <path-to-your-archon-search.toml>` unless the config lives at `<data-dir>/archon-search.toml`, to regenerate the unit. Without `--config`, `install` points the unit at the default config path: `ARCHON_SEARCH_CONFIG` if set, else `<data-dir>/archon-search.toml` (see the default-config-path entry above). Default-data-dir installs: no change.

**Announced in**: this release.

---

### [next release] — S631: `GET /ready` stays `503` while the background model validation is pending (2026-10-12)

**Surface**: `GET /ready` → `ready` and the HTTP status.

**What changed:** `/ready` gated on storage, model warm-up and the startup sync only, so once warm-up finished it answered `200 {"ready": true, "checks": {"models": "pending", ...}}` while the separate background model validation (`app.py` `_run_model_validation`) was still running — reporting ready while its own models check was pending (S631). This reverses the D6 entry below, which promised `ready` would **not** be gated on `checks.models` so that load balancers keying on `ready` would be unaffected — they now are, while `checks.models` is `"pending"`. `ready` now gates on `checks.models != "pending"`, which covers both a running warm-up and an unfinished validation: `/ready` answers `503` with `checks.models: "pending"` until both finish, then `200`. A failed warm-up is the exception: it reports `checks.models: "fail"` at once, which ends the models gate even before validation finishes. The validation starts alongside warm-up and never raises; it runs its startup probes first — the llama-server reachability probe (3 s, only when `llama_cpp` is configured), the graph NER importability check (only when `[graph].enabled`), and the graph extraction-model probe (at most min(`[graph].extraction_timeout_seconds`, 75 s), only when configured — `[graph].enabled`, `[graph].extraction_model` set, a `[graph].provider` with a buildable enrichment client and, for `llama_cpp`, a reachable llama-server) — then the embedder/reranker probe, bounded by `[database].validation_timeout_seconds` (default 60 s). A timeout of that last probe yields `checks.models: "fail"` with both models reported not ok (only the reranker when its split-provider re-check, under `reranker_providers`, is what overran). `"warn"` and `"fail"` are unchanged: informational only, they do not gate `ready`.

**Migration**: probes that gate on `/ready` (Kubernetes readiness, Docker `HEALTHCHECK`, load balancers) may see `503` after warm-up until validation finishes. Validation starts with warm-up and takes up to its startup probes (3 s llama-server reachability, the graph NER importability check, up to 75 s extraction-model, each only when configured) plus `validation_timeout_seconds`; allow that in the startup grace period. Lowering `validation_timeout_seconds` shortens only the last part, and a validation that times out reports `checks.models: "fail"`, which trips a `models = "fail"` alert. Use `GET /health` for liveness.

**Announced in**: this release.

---

### [next release] — `GET /status`: `model_validation` is always an object, never `null` while validation is pending (2026-10-12)

**Surface**: `GET /status` → `model_validation` (`StatusResponse`, `ModelValidationStatus`); CLI `archon-search maintenance status` (text and `--json` output).

**What changed:** While the background model-validation task is still running, `model_validation` was `null`. It is now the documented sub-object with every probe field unknown: `{"embedder_ok": null, "reranker_ok": null, "llama_cpp_ok": null, "provider_warnings": [], "validated_at": null}` (`archon_search/server/routes_status.py`, `_build_model_validation_status`; S488), matching `Documentation/OperatorGuide/20_monitoring_and_alerts.md` ("unknown is distinct from failed"). The OpenAPI schema is unchanged (`ModelValidationStatus | null`, kept nullable like the sibling `search` sub-object); only the emitted value changed. Completed results are emitted exactly as before. CLI side effect: `archon-search maintenance status` now prints the "Model validation" block with `pending` values while validation runs (it was omitted), and `archon-search maintenance status --json` includes `model_validation` (`archon_search/cli/maintenance_cmd.py`).

**Migration**: clients that treated `model_validation == null` as "pending" must check `model_validation.validated_at == null` instead. A poll loop that waits for `model_validation` to become non-null now exits immediately on the pending object; wait for `validated_at` to become non-null.

**Announced in**: this release.

---

### [next release] — export/import: failed jobs, the import route's `invalid_manifest` detail and MCP `import_collection` errors report fixed text, not exception text; an import reaches `DONE` only after warming its embedder (2026-10-12, S288 (import))

**Surface**: `GET /jobs/{id}` / `GET /jobs` → `error` and terminal timing of export and import jobs (`POST /collections/{name}/export`, `POST /collections/{name}/import`, `archon-search export|import --wait`, the MCP `export_collection` / `import_collection` tools); the `422` `detail` of `POST /collections/{name}/import`; the `error` text of the MCP `import_collection` tool.

**Behaviour-only change — no schema field was added, removed or retyped, so `GET /openapi.json` needs no regeneration.**

**What changed:**

- **Unexpected failure** (a store, filesystem or other internal error) of a running export or import job: `error` is now the fixed `"export failed; see the server log for details"` / `"import failed; see the server log for details"` instead of the exception's own text, which could embed server paths and library internals. The original exception is logged server-side.
- **Import refusals** keep a specific `error`, now built only from archon-search's own wording, line numbers and field names — never a library's exception text. Archive-supplied values (model name, `schema_version`) are quoted truncated to 80 characters, here and in the import route's and MCP tool's mismatch messages. Changed wording:
  - a non-JSON line in `documents.jsonl`: `Corrupt line N: <JSON parser message>` → `Corrupt line N: not valid UTF-8 JSON`;
  - a corrupt vector: `corrupt doc (vector): <decoder message>` → `corrupt doc (vector) at line N: missing or not base64-encoded float32`;
  - a malformed document: `malformed doc: <exception message>` → `malformed doc at line N: a required field is missing or has the wrong type`;
  - a `documents.jsonl` line that is valid JSON but not an object (an array, a number, a string): `corrupt doc (vector): <exception message>` → `Corrupt line N: not a JSON object`;
  - a manifest that is not UTF-8 JSON: the JSON parser's or UTF-8 decoder's message → `manifest.json is not valid UTF-8 JSON`;
  - a manifest that is not a JSON object: `… got <class 'list'>` → `… got list`;
  - unchanged in the job `error`: the schema-version and embedding-model mismatch messages (apart from the truncation), `collection '<name>' already exists; use force_overwrite=true to overwrite`, `manifest.json is missing required keys: …`, the `unsafe_tar_member` reason code, and `service unavailable: embedding model still loading`.
- **Unparseable `documents.jsonl` line** — not UTF-8, or JSON the parser rejects for an integer over Python's 4300-digit limit or for nesting past its recursion limit: with `on_error="skip"` it is now skipped and counted in `skipped` like a non-JSON line; with `on_error="fail"` it fails with `Corrupt line N: not valid UTF-8 JSON`. Before, either mode failed the job with the decoder's or parser's message (`maximum recursion depth exceeded …` for deep nesting).
- **Unparseable manifest** (the same oversized-integer and deep-nesting cases): now the `manifest.json is not valid UTF-8 JSON` refusal everywhere. Before, an oversized integer reported the parser's message, and deep nesting escaped the handlers — a `500` from `POST /collections/{name}/import`, an unhandled error from the MCP tool.
- **`POST /collections/{name}/import` → `422` `invalid_manifest`**: `detail` is the refusal message above (e.g. `manifest.json is not valid UTF-8 JSON` instead of the JSON parser's message), or the fixed `manifest.json could not be read` for any other manifest read failure.
- **MCP `import_collection`**: an archive that cannot be opened now returns `archive could not be opened: invalid or corrupt tar.gz` (the REST route's existing detail) instead of `invalid archive: <tarfile/gzip message>`; a manifest failure stays `invalid manifest: <message>`, where `<message>` is the refusal message above or `manifest.json could not be read`, never a library's text. The mismatch errors now read exactly like the REST route's `detail`: `embedding_model_mismatch` changed from `embedding model mismatch: archive has '<model>', server is configured with '<model>'` to `archive uses model '<model>'; server is configured with '<model>'`, and `schema_version_mismatch` now ends `use ignore_schema_version=true to bypass` instead of `…=True…`.
- **Import `DONE` timing**: an import job now reaches `DONE` only after warming the embedder serving the imported collection plus the reranker (best-effort, bounded; the job never fails because of it), so the first single-collection search after `import --wait` runs the full hybrid ranking instead of silently falling back to FTS-only. On a server whose models are still building, the import's `DONE` therefore arrives later and its bulk-job slot is held for that time. A `DELETE /jobs/{id}` landing during that warm-up ends the job `CANCELLED` — but every document is already written, so the imported collection stays (as for an ingest cancelled during its warm-up), and importing the archive again needs `force_overwrite=true`, else `409` `collection_exists`. A server shutdown during the warm-up leaves the job to crash recovery, which marks it `FAILED` (`process_restart`) and `POST /jobs/{id}/resume` can continue it, unchanged.

A job the scheduler fails to start still reported `dispatch_failed: <exception text>` (unchanged) as of this entry. Superseded by "[next release] — MCP tool errors, background-job `error` and maintenance `last_error` report fixed text, not exception text (2026-10-17)" above: it now reports `dispatch_failed; see the server log for details`.

**Migration**: clients that parsed an unexpected export/import failure's `error`, the changed refusal messages listed above, the import route's `invalid_manifest` `detail`, or the MCP `invalid archive: …` and mismatch texts must match the new fixed wording; the cause of an unexpected failure is in the server log. Nothing else is needed.

**Announced in**: this release.

---

### [next release] — Docker `:gpu` image: CUDA 13 base, NVIDIA driver ≥ 580 required; no system CUDA 12.1 / cuDNN 8 libraries (2026-10-10)

**Surface**: the `ghcr.io/user538295/archon-search:gpu` and `:<version>-gpu` images — host driver requirement under `docker run --gpus`, and the base image that images built `FROM` them inherit.

**What changed:** The base moves from `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` to `nvidia/cuda:13.0.3-base-ubuntu22.04` (`Dockerfile`, `.github/workflows/archon-search-release.yml`). The image now installs `onnxruntime-gpu[cuda,cudnn]>=1.27` and takes the CUDA 13 / cuDNN 9 libraries from the pip `nvidia-*` wheels it shares with PyPI's CUDA 13 torch; the old base could not load the CUDA provider of the CUDA 13 `onnxruntime-gpu` it installed, so embedding and reranking ran on CPU. The entrypoint no longer installs the CPU `onnxruntime` into `/pip-packages`, where it shadowed `onnxruntime-gpu`; it logs `Leaving out onnxruntime==<resolved version>: the image's onnxruntime-gpu <image version> replaces it (same onnxruntime import package).` instead, and does not re-check the extras' requirements on `onnxruntime` against the GPU build.

- **Driver**: CUDA 13.x needs NVIDIA driver 580 or later ([CUDA 13.0 release notes](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html), Table 2). The image sets `NVIDIA_REQUIRE_CUDA=cuda>=13.0 …` (was `cuda>=12.1 …`; both read with `docker image inspect` from the images built on 2026-10-10, `Config.Env`), so with `--gpus` on an older driver the NVIDIA Container Toolkit refuses to start the container: `nvidia-container-cli: requirement error: unsatisfied condition: cuda>=13.0, please update your driver to a newer version, or use an earlier cuda container` (message format: [nvidia-container-toolkit#940](https://github.com/NVIDIA/nvidia-container-toolkit/issues/940)). The variable still admits data-center and professional brands (not GeForce or Titan) on the 535, 550, 565, 570 and 575 branches for CUDA forward compatibility (same `docker image inspect` output); that path is untested. Containers run without `--gpus` start as before and run on CPU.
- **Derived images**: the image no longer contains system CUDA 12.1 or cuDNN 8 libraries (`libcudnn8`); the base ships only the CUDA 13 runtime, and cuDNN 9 / cuBLAS 13 come from Python wheels under `dist-packages/nvidia/*/lib`, registered in `/etc/ld.so.conf.d/archon-search-cuda-wheels.conf`.

**Migration**: before pulling the new `:gpu` image, upgrade the host NVIDIA driver to 580 or later (check with `nvidia-smi`), or pin the last earlier `:<version>-gpu` tag. Keep the `/pip-packages` volume: the changed package list invalidates its stamp, so the first start reinstalls the extras and replaces its content, no manual step needed. An image built `FROM` `:gpu` that needs CUDA 12 or cuDNN 8 must bring its own, or build from the earlier base. Details: `Documentation/UserManual/140_running_with_docker.md` → "Running the GPU image". Real-GPU inference with the new image is not yet verified; the opt-in `tests/test_docker_gpu_image.py` tests check library resolution without a GPU.

**Announced in**: this release.

---

### [next release] — wizard: NVIDIA hosts write CUDA only after a CUDA load test passes; no `+cu126` torch swap when it does (2026-10-10)

**Surface**: `archon-search wizard` → `[database].providers` / `reranker_providers` in `~/.archon-search/archon-search.toml`, its stdout and summary, and the Linux x86_64 PyTorch reinstall.

**What changed:** On an NVIDIA host where you accept CUDA, the wizard now loads the embedder and reranker under `CUDAExecutionProvider` before writing it, the check Apple Silicon runs for CoreML (`archon_search/install/installer.py`, `_probe_and_configure_cuda`; ADR 16). It previously wrote `providers = ["CUDAExecutionProvider"]` unchecked.

- **Check fails** — every install where `onnxruntime-gpu` has not replaced the stock `onnxruntime`, which has no CUDA provider: the wizard writes `providers = []` instead of `["CUDAExecutionProvider"]`, the summary's `Providers:` reads `(CPU default)` instead of `CUDA`, and stdout carries a four-line block starting `Warning: CUDA validation failed — falling back to CPU.` On Linux x86_64 the `+cu126` torch/torchvision reinstall still runs.
- **Check passes** (`onnxruntime-gpu` installed): the wizard writes `providers = ["CUDAExecutionProvider"]`, removes `[database].reranker_providers`, and no longer reinstalls torch as `+cu126` on Linux x86_64; it prints `Keeping the installed PyTorch build: onnxruntime-gpu shares its CUDA libraries, which the CUDA 12.6 PyTorch build would replace.`
- `--dry-run` is unchanged: it narrates the CUDA write and the torch reinstall and runs no check.
- When the wizard adds an extra to a `uv tool` install whose receipt records `excludes`, it now keeps them (`--excludes -` with the names on stdin) instead of falling back to `uv pip install`.

**Migration**: embedding and reranking ran on CPU on these hosts before too, so nothing stops working. Scripts or checks that expect `["CUDAExecutionProvider"]` in a wizard-written config on NVIDIA must accept `[]`, or the host must first get `onnxruntime-gpu`: follow `Documentation/UserManual/10_installation.md` → "NVIDIA GPU: run embedding and reranking on CUDA", then re-run `archon-search wizard` and accept CUDA. An existing config keeps its CUDA entry until the wizard is re-run. If you relied on the wizard's `+cu126` torch alongside `onnxruntime-gpu`, use the CUDA 13 torch the guide installs (`--torch-backend cu130`).

**Announced in**: this release.

---

### [next release] — `/mcp`: idle Streamable HTTP sessions expire after 30 minutes (`404`); at most 10 000 concurrent sessions (`503`) (2026-10-10)

**Surface**: the MCP endpoint `/mcp` — Streamable HTTP session lifetime (`Mcp-Session-Id`) and its HTTP status codes.

**What changed:** `pyproject.toml` now requires `mcp>=1.30.0` (the MCP SDK that `fastmcp` serves `/mcp` with), to fix a per-request stream leak (`Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` → DEP-2). mcp 1.30.0 changes the `session_idle_timeout` default from `None` (no expiry) to 1800 s and adds a `max_sessions` parameter defaulting to 10 000 ([python-sdk#3426](https://github.com/modelcontextprotocol/python-sdk/pull/3426)). `fastmcp` does not pass either setting through — neither 3.4.5 (the version `uv.lock` pins) nor 3.4.8 (the latest) accepts them in `FastMCPStreamableHTTPSessionManager.__init__` or passes them where `fastmcp/server/http.py` constructs it — so archon runs with these defaults and cannot change them: (1) a stateful session with no request in flight and no open GET stream for 30 minutes is closed — the next request carrying its `Mcp-Session-Id` gets `404`; before, a session lived until the client sent `DELETE` or the server stopped. (2) While 10 000 sessions are open, a request that would open another gets `503`; existing sessions are unaffected. Installs from PyPI (`pip install archon-search`, `uv tool install`, the Dockerfile's `pip install .`) have resolved mcp 1.30.0 since its release on 2026-09-07; this release makes it the minimum, so the behaviour is certain on every install path, and only `uv sync` checkouts change now (from the locked 1.29.0 to 1.30.0).

**Migration**: a client that keeps an `Mcp-Session-Id` across idle periods longer than 30 minutes must treat `404` on that session as "initialize a new session and retry", as the MCP Streamable HTTP spec requires. Clients that keep the GET stream open (the MCP SDK clients do) are not affected. Treat `503` from `/mcp` as retryable with backoff. No config or data migration is required.

**Announced in**: this release.

---

### [next release] — wizard: fresh-install config files omit never-answered keys; HyDE/RAG Fusion `enabled` written only when answered (2026-10-09)

**Surface**: `archon-search wizard` → the generated `~/.archon-search/archon-search.toml` (fresh install, and `--force --delete-db`).

**What changed:** A fresh install no longer seeds the file with every default. With no optional answers it holds only `[server].host = "127.0.0.1"` (kept so `archon-search serve`, whose host default is `0.0.0.0`, stays on loopback; `--host` overrides it), the profile's `[database]` keys (`embedding_model`, `reranker_model`, `chunk_size`, `profile`, `multilingual`) and `[database].providers` when the GPU step writes it (a detected GPU, or `--disable-gpu`/a declined GPU prompt, which write `[]`). These keys are no longer written unless answered by a flag: `[server].port`, `[database].db_path`, `[database].auto_reindex_on_chunk_size_change`, `[routing].routing_shortlist_size`, `[routing].routing_confidence_threshold`, `[collections].collections`, `[collections].pinned_collections`, `[logging].level`, `[logging].log_file`, `[logging].backup_count`; no empty section headers (e.g. `[telemetry]`) are written either. `--force --delete-db` without an existing config writes the same minimal file; with one, it empties `collections`/`pinned_collections` only where present instead of adding an empty `[collections]` table. `[hyde].enabled` and `[rag_fusion].enabled` are now written only when answered — the interactive prompt writes both (`true` or `false`), `--enable-hyde` writes only `[hyde].enabled = true` and no longer writes `[rag_fusion].enabled = false` (and vice versa for `--enable-rag-fusion`), a non-interactive run without either flag writes neither, and a re-run that leaves them unanswered removes a previous `enabled` key instead of writing `false`. The effective configuration is unchanged (every omitted key resolves to its `SearchConfig` default, and HyDE/RAG Fusion stay disabled), and existing config files keep the keys they already have on a re-run. An absent `[hyde].enabled` or `[rag_fusion].enabled` means disabled (the default): `archon-search config get hyde.enabled` / `config get rag_fusion.enabled` print `False` and `archon-search config show` lists `enabled = false` for both (as it does for an absent `[telemetry].enabled`).

**Migration**: scripts that read or edit one of the omitted keys directly in the file must use `archon-search config get`/`config set` (or add the key themselves); `archon-search config show` still prints every key with its default filled in.

**Announced in**: this release.

---

### [next release] — S632: `/status` `warmup_result` reads `"failed"` and `/ready` `checks.models` `"fail"` when warm-up finishes with a model it could not load (2026-10-09)

**Surface**: `GET /status` → `warmup_result`; `GET /ready` → `checks.models`; the server log's warm-up lines.

**What changed:** The startup model warm-up logs and swallows a model it cannot load, so with an invalid `[database].embedding_model` or `reranker_model` — or, under `eager_load_embedders = true`, an unloadable per-collection `active_embedding_model` — it still finished as `warmup_result: "done"` and logged `INFO model warm-up complete (...)` (S632). It now checks each model's outcome once it finishes: any model that failed to load, or whose warm-up `embed()` raised, sets `warmup_result: "failed"`, which `/ready` reports as `checks.models: "fail"` even when `model_validation` is clean, and logs `WARNING model warm-up failed: <models>`, naming each failed model once. A model the embedder cache's LRU evicted *after* it warmed — `eager_load_embedders` selecting more distinct models than `embedder_cache_size` — is not a failure: warm-up still reports `"done"`, and the existing `eager_load_embedders: N distinct models exceed embedder_cache_size` WARNING flags the capacity problem. A warm-up that runs past `_WARMUP_TASK_TIMEOUT_SECONDS` (600 s) was already `"failed"`; its log line changes from `model warm-up failed: ` (empty reason) to `model warm-up failed: timed out after 600.0s`, with `; still cold: <models>` appended when any covered model is cold at that moment — a state probe, so on a full cache it can also name a model evicted after warming. The success line is now pluralized correctly: `model warm-up complete (1 embedding model + reranker)` (was `1 embedding models`). `ready` and the HTTP status are unchanged: this `"fail"` never gates readiness, so `/ready` still answers `200`.

**Migration**: alerting on `warmup_result == "failed"` or `checks.models == "fail"` can now fire on a deployment that previously reported healthy — fix or remove the model the WARNING names and restart. Log matchers keyed on the bare `model warm-up failed: ` timeout line must accept the `timed out after <N>s` reason and its optional `; still cold: <models>` suffix; matchers on `model warm-up complete (1 embedding models` must accept the singular `1 embedding model`.

**Announced in**: this release.

---

### [next release] — wizard: an embedder that cannot load now fails the install (exit 1); pre-warm failure prefix renamed (2026-10-09)

**Surface**: `archon-search wizard` exit code, stdout and stderr.

**What changed:** The wizard's model pre-warm only downloaded the embedder and reranker (fastembed `lazy_load=True`), so a model whose files downloaded but could not load passed it: the wizard exited `0` and started a service whose every ingest failed (S671). Pre-warm now also loads each model through the service's own backend, under the service's providers, and runs one inference (ADR 14). An embedder that fails to download or load exits `1`, does not start the service, and rolls the config back (a fresh install removes the config and `.bak` it wrote; a re-run restores the previous config from its `.bak`; a `--force --delete-db` reinstall keeps the new config). A reranker failure still only warns. In detail:

- **stderr prefix**: `Model download failed: <cause>` → `Model pre-warm failed: <cause>`.
- **Failure wording** names the phase: `Could not resolve or download the <role> model '<name>': <cause>` or `The <role> model '<name>' was downloaded but cannot be loaded: <cause>` (was `Failed to pre-warm embedder model '<name>': <cause>`, and `Failed to pre-warm reranker model '<name>': <cause> — the model will be downloaded on first search.` for the reranker). The timeout warning now reads `Model pre-warm timed out after <N>s with the embedder ready — skipping the <models>; …`, where `<models>` names only the models not yet started (`reranker model`, `graph NER model`, or `reranker and graph NER models`).
- **Progress label** (stdout): `[4/5] Downloading models...` → `[4/5] Downloading and loading models...`; with `--dry-run`, `[DRY RUN] Would download models (~<N> MB).` → `[DRY RUN] Would download and load models (~<N> MB).`.
- **Removed**: the post-download CoreML re-check and its `Warning: model validation failed under <provider> — the reranker will fall back to CPU.` line; pre-warm loads both models under the written providers instead.
- **A configured GPU provider the installed onnxruntime lacks loads on CPU, with a warning**: pre-warm and the service drop the provider and load both models on CPU (S635, ADR 15), so the install exits `0`, and the wizard's stderr carries one `configured ONNX providers not available: <provider> — loading models on CPUExecutionProvider instead …` warning; the service embeds on CPU where it previously failed every load (`Provider <provider> is not available`), and `GET /ready` reports `checks.models: "warn"`. The wizard itself no longer writes an unavailable CUDA provider: see the CUDA load-test entry above.
- **CoreML fallback writes CPU**: when both CoreML checks fail, the wizard now writes `[database].providers = []` instead of keeping a CoreML entry a previous run wrote.

**Migration**: scripts that match `Model download failed:` must match `Model pre-warm failed:`. A host whose onnxruntime lacks the configured GPU provider needs no action to install; to run on the GPU, replace `onnxruntime` with `onnxruntime-gpu` in the install's environment (NVIDIA, see `Documentation/UserManual/10_installation.md` → "NVIDIA GPU: run embedding and reranking on CUDA"), or re-run the wizard to write CPU and clear the `checks.models: "warn"`. No config or data migration is required.

**Announced in**: this release.

---

### [next release] — S293: the `documents` mode of `POST /ingest` now really ingests; `source_path` is validated, capped, and sandboxed (2026-09-14)

**Surface**: `POST /ingest` request body and `GET /jobs/{id}` result; `SearchPipeline.ingest_documents` (library); `IngestResult.code`.

**The same request now writes data where it wrote none.** `POST /ingest` with a `documents` payload previously reached a `hasattr(pipeline, "ingest_documents")` check that was always false: the route logged a warning, wrote nothing, and moved the job to `DONE`. The mode is now implemented. A client that has been sending `documents` and reading `202`/`DONE` as success was silently storing nothing and now stores chunks — re-check your collection sizes, your `max_file_mb`, and any de-duplication assumptions before upgrading. Consequently `GET /jobs/{id}` → `result.warnings` and `result.file_results` become non-empty for `documents` jobs, where they were always empty.

**Breaking for REST consumers** — three new rejections, all before the job is created:

1. **`documents[]` items are typed.** The field was `list[dict[str, Any]]` and accepted anything; it is now `list[IngestDocument]` with `text: str` and `source_path: str` both required. A missing, extra-typed, or wrongly-typed field returns `422`. `IngestDocument` is published in `GET /openapi.json`. This replaces a failure mode where a malformed item raised inside the job and the raw exception text was written to the job's `error` field.
2. **`documents[].source_path` gets the same path-safety rule as `body.path`.** It must be absolute, `..`-free, NUL-free and non-blank, or the route returns `400` with `detail = "documents[i].source_path is unsafe: <reason>"` naming the offending index. It is stored resolved, which is also what makes the document deletable: `doc_id` is `sha256(str(Path(source_path).resolve()))` — the single derivation shared with `ingest_file` and `delete_by_source_path`.
3. **`[ingest].max_file_mb` applies per inline document.** When `max_file_mb > 0`, a `documents[i].text` whose UTF-8 byte length exceeds the limit returns the same `413` (`{"detail": {"code": "file_too_large", "message": ...}}`) a single oversized file gets. Strictly greater-than, same as the file boundary. Unchanged at the default `max_file_mb = 0`.

**Behaviour changes with no new status code**:

- **No `.acl` sidecar is read for an inline document.** `source_path` names no file the client placed on the server, so probing `<source_path>.acl` would apply an unrelated file's ACL to the document and disclose whether an arbitrary server path exists. An inline document's ACL now comes from its front-matter `_acl` key or the collection default only. Path-mode ingest is unchanged.
- **The maintenance orphan sweep no longer deletes inline documents.** `MaintenanceLoop._run_orphan_cleanup` removes every chunk whose `source_path` is absent from disk, and `[maintenance].orphan_cleanup` defaults to `true` — so without this, the first `POST /maintenance/trigger` after an inline ingest destroyed it. Inline chunks are now marked in their `metadata` (key `_inline`) and skipped by the sweep. **The mark is per chunk and carries no schema change** (`STORE_SCHEMA_VERSION` is untouched, no migration to run); an absent key means disk-backed, which is correct for every row written before this release.

**Additive** (breaking only for strict-schema validators with `extra="forbid"`): `IngestResult.code` gains `"invalid_document"`, for an item missing `text` or `source_path`. Reachable only by direct callers of `SearchPipeline.ingest_documents` — over HTTP those items are rejected with `422`/`400`. Previously such items reported `"parse_error"`, which is now reserved for a genuine `ParseError` on a real document.

**Migration**: send `documents` items with both `text` and `source_path`; make `source_path` an absolute, `..`-free path (it still need not name a real file). Add `400`, `413` and `422` handling to any client that assumed `documents` requests always returned `202`. If you consume `IngestResult.code`, add `"invalid_document"` to your stub. No operator action and no data migration are required on upgrade.

**Announced in**: this release. No prior deprecation — the mode never worked, so there is no behaviour anyone could have depended on other than the silent no-op.

---

### [next release] — graph NER model unavailability no longer fails ingest; `provider_warnings` gains a graph-NER category (2026-08-19)

**Surface**: `POST /ingest` (and directory/MCP ingest); `GET /status` → `model_validation.provider_warnings`; `GET /ready` → `checks.models`.

**Behaviour change** (previously-failing ingests now succeed — non-breaking for well-behaved clients):
- With `[graph].enabled = true` and the `en_core_web_sm` spaCy model missing, `POST /ingest` (and directory/MCP ingest) previously returned `IngestResult.status = "error"` for every file that produced at least one plain-text chunk, with zero chunks persisted. It now returns `status = "ok"`: chunks embed and persist, code-symbol graph entities are unaffected, and prose entity extraction is skipped with a warning in `IngestResult.warnings`. Automation that branched on that `status == "error"` case for this specific cause must be aware the same input now succeeds.
- `provider_warnings` (`GET /status` → `model_validation`) can newly carry graph-NER entries when `[graph].enabled = true`: it reports either that the `[graph]` extra is missing (`gliner` is not importable) or that the probe could not determine it — it does **not** report the model checkpoint, which is fetched lazily on first ingest and degrades per-document on failure rather than surfacing here. On upgrade, any existing `[graph].enabled = true` deployment missing the `[graph]` extra gains one of these entries.
- A deployment missing the `[graph]` extra can flip `checks.models` (`GET /ready`) from `"ok"` to `"warn"` fleet-wide, purely from the upgrade — no config change on the operator's part. `provider_warnings` was already `list[str]` and `CheckStatus.WARN` already existed for `checks.models`, so this is not a schema break, but `Documentation/OperatorGuide/20_monitoring_and_alerts.md` documents alerting on exactly this field — operators with that alert wired up should expect new, non-search-affecting `"warn"` entries after upgrading and triage them separately from a GPU/provider misconfiguration (see that doc for the distinction).

**Breaking for REST/MCP consumers**: `GET /status` → `model_validation` **no longer carries `provider_notes`**. The field was `list[str]` and its only producer was the prose extractor's permanent, unactionable English-only disclosure, which disappears with the engine that caused it — the pinned multilingual GLiNER checkpoint has no corpus-language limitation to disclose. A permanently-empty wire field is dead surface, so it is removed rather than kept empty, and **nothing replaces it**: there is deliberately no successor channel for permanent, unactionable notes (`Documentation/ADRs/12_local_prose_relations_enrichment_narrowed.md` records that absence so a later reader restores it deliberately or not at all). `provider_warnings` is unchanged in shape and stays actionable-strings-only, because `GET /ready` → `checks.models` is graded on it. **Migration**: tolerant JSON consumers are unaffected. Strict-schema validators (`extra="forbid"`) that enumerate `ModelValidationStatus` fields must drop `provider_notes` from their stubs; regenerate from `GET /openapi.json`. MCP is unaffected — `provider_notes` was never exposed on any tool schema.

**Breaking for library embedders**: `archon_search.pipeline.create_pipeline()` — public API, also re-exported at `archon_search.install.create_pipeline` — now raises `ConfigError` at construction time when `[graph].enabled = true` and `gliner` — the prose extraction engine, checked with `importlib.util.find_spec` — is not importable, before it builds the store, embedder or reranker. It previously constructed successfully and failed per-ingest. The server was already guarded this way (`create_app` → `_check_graph_deps`); this closes the same hole for direct callers. **Migration for this case**: install the extra (`pip install 'archon-search[graph]'`) or set `[graph].enabled = false`.

**Migration** (REST/MCP consumers): the only wire-schema change is the `provider_notes` removal described above — drop it from any strict stub. Beyond that no action is required: no other field was added, removed or retyped, and no HTTP status code changed. Operators alerting on non-empty `provider_warnings` or `checks.models != "ok"` should update their runbook to distinguish the new graph-NER cause (fix: `pip install 'archon-search[graph]'`, or re-run `archon-search wizard`) from a GPU/provider probe failure; see `Documentation/OperatorGuide/60_graph_operations.md` and `20_monitoring_and_alerts.md`.

**Post-upgrade cleanup order (operators with `[graph].enabled = true`)**: the new engine emits a different distribution of `entity_type` values, and an entity's type is part of its node key — so a re-typed entity re-keys to a new node while the old one remains. The graph write path is a pure upsert and never deletes, so **re-ingest alone does not clean up**. Run all three, in this order:

1. **Re-ingest** every collection whose graph you want rebuilt (`POST /ingest`, `archon-search ingest`, or `POST /sync`). This writes the new nodes and edges; the stale ones are still there.
2. **Trigger the maintenance pass** (`POST /maintenance/trigger`, or `archon-search maintenance run`). Its graph GC sweep is what actually deletes the orphaned nodes and their unsupported edges.
3. **Rebuild communities** (`archon-search graph build-communities <collection>`), because community membership computed over the pre-sweep graph is stale once step 2 removes nodes.

**Two settings gate this silently, and neither reports an error if it is off**: `[maintenance].graph_gc` must be `true` or step 2's GC sweep never runs at all, and `[graph].gc_rebuild_communities` must be `true` for step 3 to happen automatically after the sweep (leave it `false` and you must run `graph build-communities` by hand). Both default to `true` (`MaintenanceConfig.graph_gc`, `GraphConfig.gc_rebuild_communities` in `config.py`). Check both before concluding the cleanup worked.

---

### [next release] — the startup collection sync is suppressed after an unclean, mid-ingest death; `StartupSyncResult` gains `"suppressed"` and `/ready` `checks.sync` gains `"warn"` (2026-08-19)

**What changed:** The lifespan previously created its background startup sync unconditionally whenever any collection was configured. It now consults the job store first: when loading `~/.archon-search/archon-search-jobs.json` rewrote an **ingest-family** job (`job_type` `ingest` or `sync`) to `FAILED` with `error="process_restart"` — i.e. the previous process died mid-ingest — no sync task is created. The lifespan logs `"startup sync SUPPRESSED for N collection(s): …"` at WARNING with the manual resume path, leaves `app.state._startup_sync_task` at `None`, and sets `app.state.sync_result` to the new `StartupSyncResult` member `"suppressed"`, which `GET /status` surfaces in `sync_result`. One marker is enough — there is no two-strike rule — and there is no config opt-out.

**The startup sync is now job-backed.** `_run_startup_sync` opens a `SyncJob` (namespace `default`, `source: "user"`, `kind: "sync"`) before it runs and drives it to `DONE` / `FAILED` on every exit path (`CANCELLED` when server shutdown cancels it). This is what makes the guard self-arming: `collection_sync.sync()` writes no job row of its own, so a process killed *during the unattended startup sync* previously left no marker and the next boot re-entered the identical workload — the guard would only ever have broken a loop whose first iteration was a client-submitted ingest. Consequence on the wire: `GET /jobs` (and `archon-search jobs list`) now shows one server-created sync job on every boot where the startup sync actually runs — not on a boot where the crash-loop guard suppresses it (no task is created in that case, so no `SyncJob` is written). A job-store write failure there logs a WARNING and the sync runs unmarked; it never fails startup.

Making the startup sync job-backed also exposed a pre-existing gap in the `GET /jobs?kind=` filter: `kind=sync` was missing from the server's kind→type map, so `GET /jobs?kind=sync` (and `archon-search jobs list --kind sync`) silently returned zero rows regardless of how many sync jobs existed. This is now fixed — `?kind=sync` returns sync jobs like every other `kind` value. Automation that filtered on `kind=sync` and treated the empty result as "no sync jobs" must be aware the query now returns real data.

This is degraded-but-ready, not an outage: no task exists, so `_startup_sync_pending()` is false and `GET /ready` answers `200`. `checks.sync` reads the new `"warn"` value in this state (`CheckStatus.WARN` was already part of the `checks` enum, used by `checks.models`) — informational only, it does not gate `ready` or the HTTP status, exactly like `checks.sync: "fail"`. Searches are served immediately; the index is simply not refreshed from disk until an operator runs `POST /sync` or `archon-search sync`. A run of that sync which completes with no exception and no per-collection errors moves `sync_result` to `"done"` from `"suppressed"` **or** `"failed"`, and clears `app.state._startup_sync_failed` (so `checks.sync` reads `"ok"` again) in the same step — the two are always cleared together, never one without the other, or `GET /ready` and `GET /status` would contradict each other. No other `POST /sync` outcome writes either field.

**Suppression is sticky — it survives a process restart, not just one boot.** A fresh process-restart marker (a `RUNNING`/`CANCELLING` job rewritten to `FAILED`/`"process_restart"` on load) only exists for exactly one boot: by the boot after that, the row is already terminal and says nothing. Without persisting the decision, a supervisor that restarts more than once (launchd `KeepAlive`, systemd `Restart=`) would see the guard suppress once and then re-arm the identical crash loop on the very next restart — crash / skip / crash / skip, not a broken loop. A new file, `archon-search-sync-suppressed.json`, appears under the data dir (`get_data_dir()`, same directory as `archon-search-jobs.json` and `keys.json`) whenever the guard suppresses: a small `{"suppressed_at": "<ISO timestamp>"}` sentinel, unrelated to and never sharing the shape of the jobs file. Suppression now persists across every subsequent boot until a clean `POST /sync` clears both `sync_result` and the sentinel file in the same step (see above). Fail-open: a sentinel write failure is logged at WARNING and never fails startup or the sync — suppression then reverts to lasting one boot, exactly like before this change.

**`readiness.jobs.running` (`GET /status` → `readiness.jobs`) is now ≥1 during every startup sync that actually runs**, because the sync is job-backed (see above) — a `SyncJob` counts toward the same `JobStatus.RUNNING` tally as any client-submitted job. `Documentation/OperatorGuide/20_monitoring_and_alerts.md` documents that counter as a queue-depth alerting signal; automation that pages on "running jobs > 0 at startup" now gets a false page for every boot where the startup sync runs (not on a suppressed boot, where no task exists).

**`DELETE /jobs/{id}` on the job backing the startup sync now returns `409`** with `{"detail": "the startup sync cannot be cancelled"}` instead of `202` + `CANCELLING`. Nothing actually cancels the lifespan task that runs the startup sync, so the previous behavior silently ignored the operator's cancel while the sync ran to completion and overwrote the row anyway; worse, `CANCELLING` is itself a crash status (`jobs/store.py` `_CRASH_STATUSES`), so leaving the row there would arm the (now-sticky) crash-loop guard on the next boot for an action that was never a crash. This is the initial contract of the job this same change introduced, not an alteration of `DELETE` semantics for any pre-existing job kind — every other job kind keeps its current `DELETE` behavior (idempotent `200` on terminal, `202` + `CANCELLING` on active) unchanged.

**Why:** With a service manager that auto-restarts (launchd `KeepAlive`, systemd `Restart=`), an ingest heavy enough to OOM-kill the machine was re-entered automatically ~1 minute after every reboot, with no operator action and no way to break the loop except unloading the service. The job store already recorded the unclean exit; the lifespan just ignored it. See `Documentation/Completed/2026-08-19-020-startup-sync-crash-loop-brief.md`.

**Migration:** Clients that decode `sync_result` into a closed enum must accept `"suppressed"` as a fourth value, and probes that treat any non-`"ok"` `checks.sync` as a failure must accept `"warn"` (it answers 200). Automation that assumed a restart always re-syncs the corpus must either poll `GET /status` for `sync_result == "suppressed"` and issue `POST /sync`, or ensure jobs are terminal before shutdown (a clean shutdown leaves no `RUNNING` job, so nothing is suppressed) — and must be aware that, once suppressed, the corpus stays stale across every subsequent restart until that `POST /sync` runs, not just the next one. Automation that reads `GET /jobs` must tolerate the startup `SyncJob` it did not submit, on any boot where the sync actually ran — filter on `kind`/`created_at` rather than assuming every job is client-originated. Automation that alerts on `readiness.jobs.running > 0` at startup as a queue-depth signal must special-case the startup sync's own `SyncJob`, e.g. by excluding `kind=sync` jobs younger than the process uptime. Clients that call `DELETE /jobs/{id}` unconditionally (rather than only on jobs they themselves submitted) must handle `409` for the one job they never submitted — the startup sync.

*Superseded in part:* the startup-sync-only `DELETE /jobs/{id}` `409` above now covers every non-terminal sync job, with `{"detail": "a sync cannot be cancelled"}` — see the [2026-10-20 `DELETE /jobs/{id}` entry](#next-release--a-job-whose-status-write-fails-keeps-its-old-status-post-jobsidresume-returns-a-json-500-route-run-jobs-are-created-running-2026-10-20).

### [next release] — a cold cross-encoder now degrades `/search`, MCP `search` and `/v1` to the RRF-fused ranking instead of blocking; fan-out gains a 30 s budget (2026-09-14, S286/S288)

**Surface**: `POST /search`, the MCP `search` tool, `POST /v1/chat/completions` — single-collection **and** fan-out branches of all three.

**Behaviour-only change — no schema field was added, removed or retyped, so `GET /openapi.json` needs no regeneration.** `reranker_score` was already `float | null` on the wire and `score` was already `float`; only the *circumstances* under which `reranker_score` is `null` have widened.

**What changed:** All six search call sites now warm the lazy ONNX models *before* entering the request budget, with the reranker leg bounded at 0.5 s (`server/_search_budget.RERANKER_WARMUP_WAIT_SECONDS`). When the cross-encoder is still building when that bound expires, the request is answered with `rerank=False` instead of waiting for the build.

Two consequences for clients of a **reranker-configured** deployment, during a cold window (typically the first searches after a restart, before `GET /ready` returns 200):

- **`reranker_score` can be `null` on every result.** It previously meant only "this deployment has no reranker". Clients that treated a non-null `reranker_score` as guaranteed on a reranker-configured deployment — or that branch on it to decide whether reranking is enabled at all — must now treat it as nullable unconditionally. The condition is transient: once the build lands, later requests get the full reranked pipeline back with no config change.
- **The result ordering differs.** Degraded responses are ordered by the fused RRF score, `(-rrf_score, chunk_id)` — deterministic, but not the cross-encoder ordering. `score` carries that RRF score. Anything that snapshots an exact result order (golden-file tests, cached top-k) may see a different order for the same query during that window.

This replaces the previous behaviour, which was strictly worse: the request blocked for the whole ONNX build (measured: 91 s for `BAAI/bge-reranker-base` on the `max` profile) *outside* any timeout budget, so a real client read-timed-out and recorded no response at all rather than a slow one.

**Also:** the fan-out (`collections` / the `archon-search` catch-all model) is now wrapped in the same overall ~30 s request budget the single-collection path already had, so an over-long fan-out returns `504` (`{"detail": "Search timed out"}` on REST, `{"error": "search timed out", "code": "timeout"}` on MCP, OpenAI-shaped `504 Request timeout.` on `/v1`) instead of running unbounded. `search_many`'s own fan-out timeout bounds only the per-collection leg gathers, so it never covered the meta lookup, the query embed or the global rerank pass. Conversely, a `TimeoutError` raised *inside* the pipeline (e.g. a socket `ETIMEDOUT` from the store) is no longer mis-mapped to `504` — it keeps its own `500`/`503` mapping.

**Migration:** tolerant clients need no change. Clients that assert `reranker_score is not None`, or that pin an exact result ordering, should either accept the nullable/RRF-ordered case or gate their traffic on `GET /ready` returning 200 (which already waits for the lifespan warm-up, S279). Clients that treat any `504` as "the store is down" should note the fan-out can now produce one.

---

### [next release] — `POST /search` no longer returns `503 embedder_not_ready` when the embedder-cache handoff is slow; it degrades to the FTS leg (2026-09-14, S299)

**Surface**: `POST /search` (single-collection branch only — the fan-out branch never resolved a per-collection embedder).

**Behaviour-only change — no schema field was added, removed or retyped, so `GET /openapi.json` needs no regeneration.** `503` remains a documented `/search` response; only the circumstances that produce it narrow.

**What changed:** the route's own bound on `embedder_cache.get_or_load()` (`_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS`) drops from 30s to `server/_search_budget.EMBEDDER_WARMUP_WAIT_SECONDS` = 0.5s, and expiring it no longer raises `EmbedderNotReadyError`. The request now runs `fts_only=True` and answers `200`, exactly as the S290 warm-up stage one step later already did — so the whole cold path is under one policy. Note this bound never covered an ONNX build: `make_embedder` returns a lazy backend, so what it covers is the cache handoff (a saturated `asyncio.to_thread` executor, or this request parked as a deduplicated waiter behind another caller's in-flight load). The load is shielded and keeps running, so it still populates the cache for later requests.

**Consequences for clients:** a request that previously got `503` + `Retry-After: 30` after 30s of waiting now gets a `200` in ~0.5s carrying the S290 degraded response — FTS-only ordering, `rag_fusion_applied` / `graph_expansion_applied` / `hyde_applied` `false`; see the S290 entry below for the full list. A `503` with `code: "embedder_not_ready"` on `/search` now means only a genuinely wedged load (past the cache's 120s dedup wait). No other surface changed: `/explain`, the MCP tools, the `/v1` shim and the job tasks keep the 120s cache-level bound and their existing `embedder_not_ready` mapping.

**Migration:** clients that treated a cold-start `/search` `503` as "retry shortly" need no change — they simply stop seeing it. Clients that relied on that `503` to detect a cold server must gate on `GET /ready` returning `200` (S279) instead, and should handle the degraded `200` per the S290 entry below.

### [next release] — a cold query embedder now degrades `/search`, MCP `search` and `/v1` to the FTS leg alone instead of blocking (2026-09-14, S290)

**Surface**: `POST /search`, the MCP `search` tool, `POST /v1/chat/completions` — single-collection **and** fan-out branches of all three.

**Behaviour-only change — no schema field was added, removed or retyped, so `GET /openapi.json` needs no regeneration.** Every affected response field (`score`, `rag_fusion_applied`, `graph_expansion_applied`, `hyde_applied`, `expansion_used`, `expansion_warning`) already had its current type; only the *circumstances* under which they take a given value have widened.

**What changed:** the embedder leg of the same pre-budget warm-up is now bounded too, at 0.5 s (`server/_search_budget.EMBEDDER_WARMUP_WAIT_SECONDS`), alongside the reranker leg above. When the embedder is still building when that bound expires, the request runs with no query vector at all: the store skips its vector leg and fuses the FTS leg alone. The previous behaviour rested on the premise that a search without a query vector cannot be answered — wrong for a hybrid store, which still has BM25.

Consequences for clients during a cold window (typically the first searches after a restart, before `GET /ready` returns 200):

- **The result ordering and the result set differ.** Degraded responses are ranked by the FTS leg alone, so a chunk that only ever matched semantically is absent, and the fused ranking behind `score` is computed over that one leg (the cross-encoder still reruns on top of it when it is warm). Anything that snapshots an exact result order or asserts a semantic-only match is transiently affected.
- **`rag_fusion_applied` and `graph_expansion_applied` come back `false` even when the request asked for them** (and `expansion_used` with them). Both features exist to produce something to embed, so they are skipped in that mode; a `graph_mode` that passes the route's `422` validation is accepted and then not applied.
- **`hyde: true` comes back `hyde_applied: false`** during the same window, with the usual `expansion_warning: "HyDE expansion failed"`. Generating a HyDE vector ends in embedding the hypothesis through the very embedder that is still cold, and that happens *before* the request enters any budget — so running it would park the connection for the whole build, which is the defect this entry is about. Only an already-computed `query_vector` cancels the degrade, since it needs no embedding at all — and none of these three surfaces accepts one on the wire, so during a cold window nothing a client can send avoids it.

All three conditions are transient: the build continues in its worker thread, so later requests get the full hybrid pipeline back with no config change. This replaces the previous behaviour, in which the request blocked for the whole cold ONNX build *outside* any timeout budget and the client recorded no response at all (`status 0`) rather than a slow one.

**Migration:** tolerant clients need no change. Clients that pin an exact result ordering, or that treat `rag_fusion_applied` / `graph_expansion_applied` / `hyde_applied` / `expansion_used` as an echo of what they requested rather than a report of what ran, should either accept the degraded case or gate their traffic on `GET /ready` returning 200 (S279).

---

### [next release] — `GET /ready` returns 503 while model warm-up or the startup collection sync is pending (2026-08-14, revised 2026-09-14 for S279)

**What changed:** `ready: bool` (and the HTTP status) on `GET /ready` was previously storage-only — see the D6 entry below, which explicitly promised it would stay that way. It now gates on **three** independent conditions: `SearchStore.ping()` succeeding, no model warm-up outstanding, and no lifespan startup collection sync still running.

- **Unconditionally** (regardless of `[database].eager_load_embedders`), the lifespan now starts a background model warm-up on every boot, and `/ready` answers `503` with `{"ready": false, "checks": {"storage": "ok", "models": "pending", "sync": "ok"}}` until it finishes. `eager_load_embedders` no longer decides *whether* warm-up runs — only its **scope**: `false` (the default) warms `[database].embedding_model` plus the reranker cross-encoder, `true` additionally warms every distinct per-collection `active_embedding_model`. The D6 model-*validation* result (`ok`/`warn`/`fail`) still never gates `ready`.
- **S279 — this is a change for default (`eager_load_embedders = false`) deployments.** They previously answered `200` immediately and paid the full cold ONNX build on the first `POST /search` (measured: 91 s for `BAAI/bge-reranker-base` on the `max` profile, which is past any normal client read timeout — the caller sees no response at all). They now report `503` for that window instead, and the first search is served from warm models.
- `checks.models` also reads `"fail"` once a **terminally failed** warm-up finishes (the task raised, ran past `_WARMUP_TASK_TIMEOUT_SECONDS` = 600s without finishing, or — since S632, see the 2026-10-09 warm-up entry — finished with a model that failed to load or warm) — checked before the D6 model-validation result, so it reports `"fail"` even if that separate, cheaper probe found the models resolvable. Unlike the `"pending"` case above, this does **not** gate `ready` or the HTTP status — `/ready` answers `200` with `{"storage": "ok", "models": "fail", "sync": "ok"}` — mirroring `checks.sync: "fail"` below (a wedged/crashed warm-up must not wedge readiness forever any more than a corrupted collection sync does).
- **Unconditionally** (regardless of `eager_load_embedders`), `/ready` also answers `503` while the lifespan's own backgrounded startup sync (`collection_sync.sync()` over all configured collections) has not finished. `checks` gains a new `sync: "pending"|"ok"|"fail"` field for this condition — a 503 from this cause alone reports `{"storage": "ok", "models": "ok", "sync": "pending"}` with `ready: false`.
- `checks.sync` also reads `"fail"` (`app.state._startup_sync_failed`) when the startup sync either raised an exception (logged `"startup sync failed: %s"`) or completed normally but recorded per-collection errors in `SyncResult.errors` — e.g. a missing collection path or an ingest failure (logged `"startup sync completed with %d error(s): %s"`). Unlike `"pending"`, `"fail"` does **not** gate `ready`: `/ready` answers `200` with `{"storage": "ok", "models": "ok", "sync": "fail"}` — a corrupted collection or bad sync config must not wedge the pod's readiness forever. Both cases log at WARNING; those log lines are currently the only other signal an operator has, since `checks.sync` is the only field that surfaces it.
- The same `app.state.sync_lock` that the startup sync holds for its whole run is also used by `POST /sync`: a `POST /sync` (and `archon-search sync`) submitted during that window now answers `409` instead of queueing.

**Why:** The server accepted traffic while the ONNX models were still being built, so the first `/search` blocked for minutes behind warm-up. A readiness probe that reports ready in that window is wrong: load balancers and the CLI's "is it up?" logic had no way to tell "starting" from "serving". The same problem exists for the startup sync — search results are incomplete while the index is still being (re)built — so it was folded into the same gate. The CLI now uses exactly this signal — `_server_connect_fail_msg(base_url)` prints "archon-search is starting up…" on a 503 with `checks.storage == "ok"` and either `checks.models == "pending"` or `checks.sync == "pending"`.

**Migration:** Operators who gate on `/ready` (Kubernetes readiness probes, the Docker `HEALTHCHECK`, load-balancer health checks) must allow enough startup grace for both model warm-up (**every** deployment, not only `eager_load_embedders = true` — see S279 above; bounded by `_WARMUP_TASK_TIMEOUT_SECONDS` = 600s, past which `checks.models` is force-marked `"fail"` and `/ready` recovers to `200`) **and** the startup collection sync to complete — the service reports unhealthy until then, which is the intended behaviour. This matters even with `eager_load_embedders = false`: the startup sync is bounded to `_STARTUP_SYNC_TIMEOUT_SECONDS = 1800s` (past which it is force-marked `checks.sync: "fail"` and `/ready` recovers to `200`), but that bound is well above the Docker image's default `start-period=600s` + `interval=15s --retries=3` grace (~645s), so a large first-boot corpus can still make the container report unhealthy well before the sync's own ceiling fires — an orchestrator that restarts unhealthy containers (`depends_on: service_healthy`, a Kubernetes/Swarm health-based restart policy) can restart it mid-sync in that ~645s–1800s window — see `Documentation/UserManual/140_running_with_docker.md` for mitigation and for why the sync's incremental nature still converges over successive restarts. Probes that must ignore both should use `GET /health` (liveness, never 503) instead. Automation that calls `POST /sync` immediately after starting the server should retry on `409` rather than treating it as an error.

### [next release] — `EmbedderNotReadyError` now maps to a uniform, sanitized 503 across every route surface (2026-08-14)

**What changed:** Previously only `POST /search` mapped a wedged/still-loading embedder-cache load (`EmbedderNotReadyError`, raised when a deduplicated waiter blocks past `_LOAD_WAIT_TIMEOUT_SECONDS`=120s) to a clean 503. Every other surface that resolves an embedder let the exception fall into its generic `except Exception` handling: `/explain` and `/v1/chat/completions` fell through to FastAPI's default handler and returned a plain-text `500 Internal Server Error`; `routes_jobs.py`'s ingest/reindex tasks and `routes_export.py`'s import task left the job `FAILED` with `error=str(exc)` (leaking the exception class name, the timeout value, and the model name); the 5 embedder-resolving MCP tools (`search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory`) returned `{"error": str(exc), "code": "internal_error"}`.

Now a single `@app.exception_handler(EmbedderNotReadyError)` in `create_app()` (`server/app.py`) covers every plain HTTP route without its own handling, returning `503` with `{"detail": "service unavailable: embedding model still loading", "code": "embedder_not_ready"}` and a `Retry-After: 30` header (or the OpenAI error envelope plus the same header under `/v1/*`) instead of a `500`. `routes_jobs.py`/`routes_export.py` job tasks now catch it explicitly and record the same sanitized `error` string instead of `str(exc)`, leaving the job `FAILED` (retryable — see the per-job-type retry rules in `Architecture/140_error_handling_strategy.md`). Each of the 5 MCP tools now catches it explicitly and returns `{"error": "service unavailable: embedding model still loading", "code": "embedder_not_ready"}` instead of `"internal_error"`. `routes_search.py`'s own local handler gained the same `Retry-After: 30` header and now also enqueues a telemetry entry (`from_error`, `status="internal_error"`, `error_kind="other"`) — previously this branch recorded nothing.

Also new: `POST /search` now bounds its own wait on `embedder_cache.get_or_load()` with `_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS` (`routes_search.py`), separately from the cache's own internal dedup-wait bound (`_LOAD_WAIT_TIMEOUT_SECONDS = 120s`, unchanged), so the request never holds the connection for up to 120s. **Superseded within this same unreleased window by the S299 entry above** — that bound is now `EMBEDDER_WARMUP_WAIT_SECONDS` = 0.5s and expiring it degrades the search to the FTS leg with a `200` rather than returning a 503. Only the cache-level `EmbedderNotReadyError` (a load wedged past the 120s dedup wait) still produces the sanitized 503 + `Retry-After: 30` on `/search`. No release ever shipped the intermediate 30s-then-503 behaviour, so there is nothing to migrate from it.

**Why:** A retryable, expected "still loading" condition was indistinguishable from a genuine server bug (`500`/`internal_error`) on every surface except `/search`, and the message body leaked internal implementation details (exception class name, timeout constant, model name) wherever it did surface. Neither is desirable for a condition that resolves itself once the model finishes loading. The 120s cache-level wait was also too long for one HTTP request to hold a connection open for.

**Migration:** Clients polling `/explain`, `/v1/chat/completions`, MCP tools, or job status (`GET /jobs/{id}`) for this condition should now check for `code == "embedder_not_ready"` (503 on REST, `McpErrorResponse.code` on MCP, `JobResponse.error` text on jobs) instead of treating it as an unrecoverable `500`/`internal_error`/generic `FAILED`, and may honor the new `Retry-After: 30` header. `POST /search` callers relying on the request blocking up to ~120s on a cold start will now see a 503 at 30s instead — retry per the header.

### [next release] — `DELETE /collections/{name}` returns 503 while an ingest job is active (2026-08-07)

**What changed:** `DELETE /collections/{name}` now returns HTTP 503 with `{"detail": "...", "code": "store_busy"}` and a `Retry-After: 30` header when a base `IngestJob` with status `RUNNING` or `CANCELLING` exists for that collection in the job store.  Previously the endpoint returned 200 and dropped the collection while the ingest was mid-flight, because the per-collection lock is released before actual disk writes complete.

**Why:** Deleting a collection under an active write produces a torn-write state: the ingest task holds no lock, yet continues calling `store.ingest_chunks`, which recreates the dropped table as a meta-less orphan.  The fix is a bug-fix for S399/S428/S430; the 503 response was always the declared and documented behaviour.

**Migration:** Clients or scripts that delete a collection immediately after submitting an ingest (e.g. during test teardown) must wait for the ingest job to reach a terminal status (`DONE`, `FAILED`, `CANCELLED`) before deleting.  The `Retry-After` header gives a poll hint.  The error code `"store_busy"` is machine-readable.

**Also changed:** the 503 body shape on `DELETE /collections/{name}` (and the pre-existing lock-timeout 503 in `_ingest_lock.py`) now follows the `ErrorDetail` schema — `{"detail": "…", "code": "store_busy"}` — instead of the previous non-schema `{"error": "store_busy", "detail": "…"}` shape.  Clients that parsed the `"error"` key must switch to `"code"`.

### [next release] — `[search].fanout_leg_trim` and `fanout_timeout_seconds` are now enforced on the server (2026-08-07)

**What changed:** `create_app` (`archon_search/server/app.py`) built its `SearchPipeline` without
passing `max_fanout`, `fanout_leg_trim`, or `fanout_timeout_seconds`, so the pipeline always used
the `SearchPipeline.__init__` defaults (8 / 40 / 30.0) no matter what `[search]` contained. All
three are now forwarded. `fanout_leg_trim` and `fanout_timeout_seconds` are genuinely read by the
pipeline, so operator values take effect for the first time. `max_fanout` is forwarded for parity
with the `create_pipeline` factory, but fan-out breadth remains enforced solely in the route and
MCP handlers (`routes_search.py`, `routes_explain.py`, `routes_openai_shim.py`, `mcp.py`) which
read `config.max_fanout` directly — `SearchPipeline._max_fanout` is not read anywhere, so nothing
changes for that key.

**Why:** The keys were parsed, validated, and documented in `archon-search.toml.example` and
`Documentation/Architecture/210_performance_and_scalability.md`, but were inert on the server path.
Operators tuning fan-out latency or recall saw no effect.

**Migration:** No action required for installations using the defaults. Installations that set
`[search].fanout_timeout_seconds` below their real fan-out latency will now receive `504
{"detail": "Search timed out"}` on multi-collection `POST /search` and `POST /explain` where they
previously received `200`; raise the value if the timeout was set aspirationally. Installations
that set `[search].fanout_leg_trim` below the default `40` will now get a smaller per-leg candidate
pool and therefore different (potentially fewer) results; `fanout_leg_trim` is a hard recall
ceiling applied before the ACL pass and rerank, so raise it if results regress.

### [next release] — `POST /explain` no longer 404s on a partially-unknown `collections` list (2026-08-07)

**What changed:** A multi-collection `POST /explain` (and the MCP `explain` tool) where *some*
requested collections exist and others do not now returns `200`, with the unknown names reported in
`excluded_collections` (`reason: "not_found"`) and the surviving legs fully explained. Previously a
single unknown name failed the whole request with `404 {"detail": "collection not found"}`. `404`
is now returned only when **every** requested name is absent from the caller's namespace (no leg
left to fan out over). Pinned single-`collection` requests are unchanged (still `404`), and
`POST /search` / MCP `search` are unchanged (still `404` on *any* unknown name).

**Why:** `/explain` is a debugging tool — a typo in one of eight collection names discarded the
trace for the seven valid ones, which is precisely when the trace is most needed. The fan-out
already had an `excluded_collections` channel for non-fatal per-leg exclusions
(`embedding_model_mismatch`); absent collections now use the same channel.

**Migration:** Clients that read a `404` as "at least one requested name was wrong" must now
inspect `excluded_collections[].reason == "not_found"` instead. Clients that only read `results`
are unaffected when at least one leg survives. A request where *every* requested collection ends
up excluded — e.g. one absent name plus one embedding-model mismatch — now returns `200` with
`results: []` and all names in `excluded_collections`, where it previously returned `404`.
Clients that treat empty `results` as "no matches" must inspect `excluded_collections` to
distinguish that from "no leg ran". To preserve the old strictness on any unknown name, check
`excluded_collections` for `reason == "not_found"` and treat it as an error client-side.
`excluded_collections` is a pre-existing field (added in B3), so there is no schema change —
only a new `reason` value on the `/explain` path.

### [next release] — Removed inert `[routing].max_parallel_collections` config key (2026-07-29)

**What changed:** The `[routing].max_parallel_collections` key is removed from `SearchConfig`, the
config loader, `archon-search config show` output, and `archon-search.toml.example`. It was parsed
and validated (`> 0`) but never consumed by any runtime search path — no code in `router.py`,
`pipeline.py`, or `server/` read it — so setting it never affected concurrency or latency.

**Why:** A documented knob that does nothing misleads operators. Fan-out breadth is already
governed by `[search].max_fanout`.

**Migration:** No action required. An existing `archon-search.toml` that still contains
`max_parallel_collections` continues to load — the key is ignored and a deprecation WARNING is
logged at startup (mirroring the `centroid_incremental_enabled` handling). Note that values that
were previously rejected at load (`<= 0`, non-integer) are no longer validated, since the key is
no longer read. Remove the line at your convenience.

### [next release] — `ErrorDetail` gains optional `code` field; 503 meta-lookup responses now include `code` (2026-07-21)

**What changed:** `ErrorDetail` (the Pydantic schema used by documented 4xx/5xx responses on
`POST /search`, `POST /explain`, and several other endpoints) gains an optional `code: string | null`
field. `POST /search` and `POST /explain` 503 responses for `MetadataLookupError` failures now
include `"code": "metadata_store_error"` and a more specific `detail` message. The MCP `search`
and `explain` tools similarly use `code: "metadata_store_error"` (previously `"internal_error"`)
for `MetadataLookupError` failures. The routing-failure 503 in the collectionless `POST /explain`
path uses `"code": "service_unavailable"`.

**Why:** Clients benefit from a machine-readable error code to distinguish error categories without
parsing the `detail` string.

**Migration:** This is additive — `code` is optional and absent on non-503 error responses.
Existing clients that don't read `code` are unaffected.

### [next release] — New `reranker_providers` TOML field for CoreML split-provider support (2026-07-20)

**What changed:** Added `reranker_providers` under `[database]` in `archon-search.toml`. When
present, it overrides `providers` for the reranker model only; the embedder continues to use
`providers`. When absent (the default), the reranker inherits `providers` — fully backward-compatible.

**Why:** On some Apple Silicon configurations CoreML works for the embedder but not the reranker.
The new field allows the wizard to write a split config (`providers = ["CoreMLExecutionProvider"]`,
`reranker_providers = []`) instead of falling back entirely to CPU.

**Migration:** No action required for existing configs. Existing `providers` settings continue
to apply to both models. Re-run `archon-search wizard` on Apple Silicon to apply the optimised
split configuration automatically.

### [next release] — Removed `--log-to-stderr` wizard flag (2026-07-18)

**What changed:** The `--log-to-stderr` flag and interactive wizard prompt have been removed.
`WizardFeatures.log_to_stderr` field is also removed.

**Why:** The flag's sole effect was setting `log_file = ""` in the generated TOML config,
which disabled file logging. This was a bug (brief 2026-07-15-160). After the bug fix,
the flag became an inert no-op, so it was removed rather than shipping a dead control.

**Migration:** For stderr-only logging in containers, set `ARCHON_SEARCH_CONTAINER=1` in
the service environment (already the canonical mechanism). For intentional file-logging
opt-out, set `[logging] log_file = ""` directly in `archon-search.toml`.

### [next release] — CSP120: `archon-search collection add`, `collection reindex`, and `sync` are now HTTP proxies (require server running)

**Surface**: `archon-search collection add`, `archon-search collection reindex`, and `archon-search sync` CLI commands.

**Breaking changes**:

**`collection add`:**

1. **Server must be running.** `archon-search collection add <path>` now proxies `POST /collections/` on the running server. Exits `1` with `"archon-search serve is not running. Start it first."` when the server is not reachable. Previously ran in-process without a server.
2. **Config is no longer written locally.** The CLI no longer writes `archon-search.toml`. The server writes the path to `archon-search.toml` server-side via `_maybe_save_config()` as part of `POST /collections/`. If the ingest job later fails asynchronously, the config entry remains (stale "registered but not yet populated"); recovery is via `collection reindex <name>`.
3. **`--config` option removed.** Use `--api-url` / `--api-key` instead.
4. **Output changed.** Previously printed nothing until ingest completed. Now prints `Add collection job submitted: <job_id>. Collection: '<name>'` immediately. Use `--wait` to block until ingest completes.
5. **Collection name comes from the server.** The displayed collection name is derived by the server's `path_to_collection_name(resolved_path)` (lowercased, non-alphanumeric runs replaced by `_`, leading/trailing `_` stripped). This is the same function the server used previously, but the CLI previously computed it locally — any discrepancy between client-side and server-side resolution is now eliminated.

**`collection reindex`:**

1. **Server must be running.** `archon-search collection reindex <name>` now proxies `POST /collections/{name}/reindex`. Exits `1` on `ConnectError`. Previously ran in-process.
2. **`--config` option removed.** Use `--api-url` / `--api-key` instead.
3. **Output changed.** Previously blocked until reindex completed. Now prints the job ID immediately. Use `--wait` to block.

**`sync`:**

1. **Server must be running.** `archon-search sync` now proxies `POST /sync`. Exits `1` on `ConnectError`. Previously ran `SearchCollectionSync` in-process.
2. **`--config` option removed.** Use `--api-url` / `--api-key` instead.
3. **Output changed.** Previously blocked until sync completed. Now prints the job ID immediately. Use `--wait` to block.
4. **Sync is namespace-blind in v1.** `POST /sync` syncs all server-configured collections regardless of the caller's namespace (see Known Limitations in the CSP120 team plan). Per-namespace filtering is deferred.

**`collection reindex-metadata`:**

1. **Server must be running.** `archon-search collection reindex-metadata <name>` now proxies `POST /collections/{name}/reindex-metadata`. Exits `1` on `ConnectError`. Previously ran in-process.
2. **`--config` option removed.** Use `--api-url` / `--api-key` instead.
3. **Output changed.** Previously blocked until reindex-metadata completed. Now prints the job ID immediately. Use `--wait` to block.

**Migration**: ensure `archon-search serve` is running before invoking any of these commands. Replace `--config` flags with `--api-url` / `--api-key`. Scripts that expect synchronous completion must add `--wait`.

---

### [next release] — DCS: `archon-search collection list` is now an HTTP proxy (requires server running)

**Surface**: `archon-search collection list` CLI command.

**Breaking changes**:

1. **`--config` option removed.** The `--config PATH` flag no longer exists. Use `--api-key` / `ARCHON_SEARCH_API_KEY` / `--api-url` for authentication.

2. **Server must be running.** `archon-search collection list` now proxies the request to `GET /collections/` on the running archon-search server (default `http://localhost:8765`). The command exits `1` when the server is not reachable. Previously the command read LanceDB directly without a server.

**Migration**: ensure `archon-search serve` is running before invoking `collection list`. Pass `--api-key` or set `ARCHON_SEARCH_API_KEY`.

---

### [next release] — FE-8: `archon-search collection remove` is now an HTTP proxy (requires server running)

**Surface**: `archon-search collection remove` CLI command.

**Breaking changes**:

1. **Positional argument changed from filesystem path to collection name.** Previously `collection remove /Users/me/docs` took the source directory path. Now it takes the collection name as derived by `path_to_collection_name` (e.g. `collection remove docs`). Use `archon-search collection list` to see the exact names.

2. **`--dry-run` option removed.** The `--dry-run` flag no longer exists.

3. **`--force` option removed.** The `--force` flag no longer exists. The server serialises concurrent writes automatically; no caller-side bypass is needed.

4. **`--config` option removed.** The `--config PATH` flag no longer exists. Authentication is now via `--api-key` / `ARCHON_SEARCH_API_KEY` env var / key file. The `--api-url` and `--api-key` options replace `--config`.

5. **Server must be running.** `archon-search collection remove` now proxies the request to `DELETE /collections/{name}` on the running archon-search server (default `http://localhost:8765`). The command exits `1` with `"archon-search serve is not running. Start it first."` when the server is not reachable. Previously the command ran in-process without a server.

**Migration**: replace `collection remove <path>` with `collection remove <name>` (get `<name>` from `collection list`). Remove any `--dry-run`, `--force`, or `--config` flags. Ensure `archon-search serve` is running before invoking `collection remove`.

---

### [next release] — FE-5: `archon-search ingest` is now an HTTP proxy (requires server running)

**Surface**: `archon-search ingest` CLI command.

**Breaking changes**:

1. **`--path` is now mandatory.** Previously, when `--path` was omitted the command defaulted to `~/.archon-search/history/sessions` and printed `No --path given, using default: <path>`. Now, omitting `--path` exits `1` with `Error: --path is required.` No default path fallback exists.

2. **Collection name derivation changed for single files.** Previously, a single-file path derived the collection name from `Path.stem` (e.g. `docs.md` → `"docs"`). Now, `path_to_collection_name` is applied to the full filename (e.g. `docs.md` → `"docs_md"`). To preserve the old name, pass `--collection docs` explicitly.

3. **Server must be running.** `archon-search ingest` now proxies the request to `POST /ingest` on the running archon-search server (default `http://localhost:8765`). The command exits `1` with `"archon-search serve is not running. Start it first."` when the server is not reachable. Previously the command ran in-process without a server.

4. **`--config` option removed.** The `--config PATH` flag no longer exists. Authentication is now via `--api-key` / `ARCHON_SEARCH_API_KEY` env var / key file. The `--api-url` and `--api-key` options replace `--config`.

5. **Output changed.** Previously: `Ingest complete: <ok> ingested, <errors> errors.` immediately on completion. Now: the job is submitted asynchronously and `Ingest job submitted: <job_id>. Collection: '<name>'` is printed. Use `--wait` to block until completion (prints `Ingest complete for '<name>'.`).

**Migration**: scripts that call `archon-search ingest` without `--path` must add `--path`. Scripts that pass `--config` must switch to `--api-key` / `--api-url`. Scripts that relied on the single-file `Path.stem` collection name must pass `--collection <stem>` explicitly. Ensure `archon-search serve` is running before invoking `archon-search ingest`.

---

### [next release] — G10: `HydeStatusDetail` and `RagFusionStatusDetail` gain `provider: str` field

**Surface**: `GET /status` response — `hyde: HydeStatusDetail | null` and `rag_fusion: RagFusionStatusDetail | null` sub-objects.

**Additive change** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

- `HydeStatusDetail` gains `provider: str` — the active LLM provider name (`"anthropic"`, `"ollama"`, or `"openai"`), mirroring `[hyde].provider` from config. Default value when no provider is set: `"anthropic"`.
- `RagFusionStatusDetail` gains `provider: str` — the active LLM provider name for RAG Fusion, mirroring `[rag_fusion].provider`. Default value: `"anthropic"`.
- `key_available` semantics updated: was Anthropic-only (`ANTHROPIC_API_KEY`); now provider-aware — `false` for `"openai"` when `OPENAI_API_KEY` is unset; always `true` for `"ollama"`.

**Migration**: tolerant JSON consumers are unaffected. Strict-schema validators must add `provider: str` to `HydeStatusDetail` and `RagFusionStatusDetail` type stubs. Regenerate from `GET /openapi.json`.

---

### [next release] — E2j BE-1: `GraphNodeResponse` gains required `entity_type: str` field

**Surface**: `GET /graph/{collection}` response (`GraphNodeResponse` items in `nodes[]`); `GET /graph/cross-collection` response (same `GraphNodeResponse` items).

**Change**: `entity_type: str` is added as a required (non-nullable, no default) field to `GraphNodeResponse`. The value is the string form of the entity's `EntityType` enum (e.g. `"person"`, `"concept"`, `"system"`, `"event"`, `"code_symbol"`). Strict-schema validators that enumerate allowed fields on `GraphNodeResponse` must add `entity_type: str` to their type stubs; consumers doing strict Pydantic deserialization with `extra="forbid"` must update their models.

**Migration**: tolerant JSON consumers (those that ignore unknown fields) are unaffected. Strict-schema validators must add `entity_type: str` to their `GraphNodeResponse` type stubs. Regenerate from `GET /openapi.json`.

---

### [next release] — E2h: `graph_mode` extended to `"ppr"`; `SearchResponse` and `ExplainResponse` gain `ppr_entities_matched` (all additive)

**Surface**: `POST /search` request and response; `POST /explain` request and response; MCP `search` and `explain` tools.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **`SearchRequest.graph_mode` and `ExplainRequest.graph_mode` extended to accept `"ppr"`** — the new literal `"ppr"` activates Personalised PageRank retrieval. Clients that already pass `graph_mode="naive"`, `"local"`, or `"global"`, or omit the field, are completely unaffected. Strict-schema validators that enumerate the allowed literals must add `"ppr"`. Mutually exclusive with `collections` (multi-collection fanout) and `scope_filter` — both combinations return `422`. Returns `422` when `[graph] enabled = false`.

2. **`SearchResponse` gains `ppr_entities_matched: int | null`** — always present (default `null`). Non-null only when `graph_mode="ppr"` was used; contains the count of seed entities with ≥1 mention row (`0` = no match, fell back to hybrid). Strict-validating clients must add this nullable integer field to their `SearchResponse` type stubs. Tolerant clients are unaffected.

3. **`ExplainResponse` gains `ppr_entities_matched: int | null`** — same semantics as (2) for the `/explain` path. Strict-validating clients must add this field to their `ExplainResponse` type stubs.

4. **MCP `search` and `explain` tools now accept `graph_mode: "ppr"`** — additive extension of the existing `graph_mode` parameter. `ppr_entities_matched: int | null` is added to both tool return shapes. Existing callers that pass other `graph_mode` values or omit the field are unaffected.

**Migration**: no action required for tolerant JSON consumers. Strict-schema validators should add `"ppr"` to the `graph_mode` literal union in `SearchRequest` and `ExplainRequest`, and add `ppr_entities_matched: int | null` to `SearchResponse` and `ExplainResponse` type stubs. Regenerate from `GET /openapi.json`. To use PPR mode: install `archon-search[graph]`, set `[graph] enabled = true`, and ensure entities and mention rows are populated via ingest with graph extraction enabled.

---

### [next release] — E2h BE-8: naive graph expansion is now capped at `naive_max_expansion_terms`

**Surface**: `POST /search` with `graph_mode="naive"` (via `GraphExpander`).

**Behaviour change**:

- **Naive query expansion now caps the neighbour-name list** before deduplication.  Previously, all first-degree neighbours of matched entities were appended to the query (no upper bound).  After E2h BE-8, at most `[graph].naive_max_expansion_terms` (default `20`) candidate names are passed to the expansion builder.  Deduplication (names already present in the query) runs after the cap, so the final number of added terms may be lower.

  Operators with very high-degree graph nodes (e.g. a "god" entity connected to hundreds of nodes) will observe shorter expanded queries.  This is intentional: unbounded expansion degrades FTS precision and increases query latency.

- **`[graph].naive_max_expansion_terms: int = 20`** — added in E2h BE-1 as a `GraphConfig` field; BE-8 wires it into `GraphExpander` and enforces it.  To restore previous (uncapped) behaviour, set a very large value (e.g. `naive_max_expansion_terms = 10000`) in `archon-search.toml`.

**Migration**: no action required for deployments where the highest-degree graph node has fewer than 20 neighbours.  For deployments with very high-degree nodes that rely on wide expansion, increase `[graph].naive_max_expansion_terms` in `archon-search.toml`.

---

### [next release] — E2d: graph table names are now namespace-scoped (`_archon_graph_{ns}__{col}_*`)

**Surface**: LanceDB internal graph tables (auxiliary storage); `archon-search graph build-communities` CLI; all `GET /graph/*` endpoints (read path).

**Breaking change**:

1. **All graph LanceDB tables renamed from `_archon_graph_{col}_*` to `_archon_graph_{ns}__{col}_*`** — the namespace is now embedded in the table name, separated from the collection name by a double underscore (`__`). Affected table suffixes: `_nodes`, `_edges`, `_communities` (E1b), `_mentions` (E2b). After upgrading to E2d, all existing graph tables (created under the old naming scheme) become **orphaned** — they are no longer referenced by the server and will not be read or written. The server does not automatically migrate or delete them.

2. **Namespace and collection name charset constraint** — collection names and namespace names must NOT contain consecutive underscores (`__`) and must NOT end with `_`. Names that violate these constraints are rejected with `ValueError` at ingest time. The constraint existed implicitly (the `__` separator requires it for unambiguous parsing); E2d makes it explicit and enforced at the store layer. Valid examples: `docs`, `tenant-a`, `ns_a`. Invalid examples: `docs_`, `my__col`, `tenant_`.

3. **Startup WARNING (BE-1b)** — after upgrade, the server will log a WARNING listing any LanceDB tables that match the old `_archon_graph_{col}_*` pattern (without a namespace prefix). These are orphaned tables from pre-E2d ingests.

**Impact**: all graph data (entities, edges, communities, mentions) is invisible after upgrade. Graph search modes (`naive`, `local`, `global`) return empty results until data is re-ingested.

**Remediation**:
1. After upgrading to E2d, delete old graph tables manually (use the `lancedb` Python client or LanceDB CLI to drop tables matching `_archon_graph_<collection>_nodes`, `_archon_graph_<collection>_edges`, etc.).
2. Re-ingest all collections with `[graph] enabled = true` to rebuild graph data under the new namespace-scoped table names.
3. If communities were built (`archon-search graph build-communities`), re-run that command after re-ingest.

**Deployments without `[graph] enabled = true`** are completely unaffected — no graph tables exist and no migration is required.

---

### [next release] — E2c: `GET /graph/{collection}` and `GET /graph/cross-collection` gain `salience_mode` field and namespace-scoped resolution

**Surface**: `GET /graph/{collection}` REST response (`GraphInspectionResponse`); `GET /graph/cross-collection` REST response (`CrossCollectionGraphInspectionResponse`); collection-resolution behaviour inside both handlers.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **`GraphInspectionResponse` gains `salience_mode: Literal["frequency", "tfidf"] = "frequency"`** — always present in the JSON response. The default value `"frequency"` preserves the previous implicit behaviour. Clients using strict JSON schema validation (`extra="forbid"`) may reject the response if they have not updated their type stubs. Lenient clients are completely unaffected.

   Schema: `GraphInspectionResponse.salience_mode: Literal["frequency", "tfidf"] = "frequency"`.

   To opt into TF×IDF salience: add `?salience=tfidf` to the query string.

2. **`CrossCollectionGraphInspectionResponse` gains `salience_mode: Literal["frequency", "tfidf"] = "frequency"`** — same semantics as (1) above, for the cross-collection endpoint. Schema: `CrossCollectionGraphInspectionResponse.salience_mode: Literal["frequency", "tfidf"] = "frequency"`. Lenient clients are completely unaffected; strict-schema validators must add the field to their `CrossCollectionGraphInspectionResponse` type stubs.

**Behaviour changes**:

3. **`GET /graph/{collection}` now resolves collection names within the authenticated namespace only** — previously the handler resolved the collection against `DEFAULT_NAMESPACE` regardless of the caller's bearer token. After E2c, the handler passes `namespace=request.state.namespace` to `get_collection_meta`, so each caller can only inspect collections in their own namespace. Callers in non-default namespaces that previously received data from `DEFAULT_NAMESPACE` collections will now receive `404 collection not found`. Callers using the default namespace (single-tenant deployments) are unaffected.

4. **`GET /graph/cross-collection` now resolves collection names within the authenticated namespace only** — the same namespace fix applies to the cross-collection handler. The `?collections=` parameter is now filtered against the authenticated namespace: collection names that exist in other namespaces return `404 collection not found`. Additionally, when `?salience=tfidf` is used, the IDF denominator is scoped strictly to the authenticated namespace (`pipeline.get_all_collections_meta(namespace)`), preventing cross-namespace IDF leakage. Callers using the default namespace (single-tenant deployments) are unaffected.

**Migration**: no action required for tolerant JSON consumers or single-tenant deployments. Multi-tenant operators: tokens in non-default namespaces must now call both graph endpoints with a token scoped to the namespace that owns the collection(s). Strict-schema validators should add `salience_mode: "frequency" | "tfidf"` to their `GraphInspectionResponse` and `CrossCollectionGraphInspectionResponse` type stubs; regenerate from `GET /openapi.json`.

---

### [next release] — E2a: TTL and scoping — additive chunk columns, new endpoints, scope_filter, maintenance fields

**Surface**: `POST /ingest` and `POST /ingest/directory` request bodies; `PATCH /collections/{name}` request body; new `GET /collections/{name}/expiring` endpoint; `POST /search` and `POST /explain` request bodies; `GET /status` maintenance sub-object; `GET /collections/{name}/documents` response items; MCP tools `ingest_file`, `ingest_directory`, `search`, `search_with_context`, `explain`; LanceDB chunk-table and meta-table schemas; `STORE_SCHEMA_VERSION`.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **`STORE_SCHEMA_VERSION` bumped `0` → `1`** — two new in-place migrations registered with `introduced_at=1`: `migrate_expires_at_and_scopes` (adds `expires_at: utf8 | null` and `scopes: list<utf8> | null` to every collection's chunk table) and `migrate_default_ttl_seconds` (adds `default_ttl_seconds: int64 | null` to `_archon_collection_meta`). These migrations are NOT triggered at server startup — operators must run `POST /collections/{name}/migrate` for each collection after upgrading to E2a before using TTL or scopes features. Until migrated, TTL and scope data is silently omitted (the store detects un-migrated columns and omits the keys from ingest rows entirely; the columns are absent, not stored as null). `GET /collections/{name}/migrations/pending` returns both specs for un-migrated collections; `GET /status` `collections_schema_behind` reflects the count.

2. **`POST /ingest` and `POST /ingest/directory` gain `chunk_ttl_seconds?: int | null` and `chunk_scopes?: list[str] | null`** — additive optional fields. `chunk_ttl_seconds` ∈ [1, 2^31-1]; `chunk_scopes` 0–100 items × 1–255 chars. Validation: `chunk_ttl_seconds=0` or negative → 422; scope string > 255 chars or > 100 items → 422. `chunk_scopes=[]` (explicit empty list) is normalized to `null` by the pipeline (no storage difference). Existing calls without these fields are completely unaffected.

3. **`PATCH /collections/{name}` gains `default_ttl_seconds?: int | null`** — additive optional field. Sets the collection-level TTL default; `null` clears it. When set, newly ingested chunks without a per-request `chunk_ttl_seconds` inherit `expires_at = ingest_time + default_ttl_seconds`. Forward-only: PATCH does NOT retroactively update existing chunks. Strict-validating clients on `PatchCollectionBody` must add this nullable field.

4. **New `GET /collections/{name}/expiring?within_hours={n}` endpoint** — cursor-paginated list of chunks expiring in the next `within_hours` hours (`within_hours` ∈ [1, 8760]); returns `ExpiringChunksResponse`. Additive — no existing endpoints modified.

5. **`POST /search` and `POST /explain` gain `scope_filter?: str | null`** — additive optional field. `null` (default) = no scope filtering; exact value = only chunks with that exact tag in `scopes`; trailing `*` = prefix wildcard match. Unscoped chunks (`scopes = null`) always match any `scope_filter`. Invalid patterns (bare `*`, leading `*`, mid-string `*`, multiple `*`) → 400. 400 body: `{"detail": {"code": "invalid_scope_filter", "message": "..."}}` (wrapped in a `detail` envelope, same pattern as `graph_communities_not_built`). `scope_filter` + any `graph_mode` value → 422. Strict-validating clients on `SearchRequest` and `ExplainRequest` must add this nullable field.

6. **`GET /status` maintenance sub-object gains `expired_chunk_count: int` and `last_expired_pruned_at: str | null`** — `expired_chunk_count` is always an integer (never null); reflects the live point-in-time count of chunks with `expires_at < now_utc` in the caller-visible collection tables (note: counts span all namespaces sharing a collection table — the namespace parameter is accepted for API symmetry only; all tenants in a shared-table collection contribute to the count). `last_expired_pruned_at` is null until the first prune pass. Strict-validating clients on `MaintenanceStatusDetail` must add both fields.

7. **`GET /collections/{name}/documents` items gain `scopes: list[str]`** — present after E2a migration (empty list when the document has no scoped chunks; absent on un-migrated collections where the `scopes` column has not been added yet). Strict-validating clients on `DocumentInfoItem` must add this field.

8. **MCP tools updated** — `ingest_file` and `ingest_directory` gain `chunk_ttl_seconds: int | null` and `chunk_scopes: list[str] | null`; `search`, `search_with_context`, and `explain` gain `scope_filter: str | null`. MCP tool-level validation mirrors the REST rules. All changes are additive — callers that do not pass these parameters are unaffected.

**Operator runbook for migration**: after upgrading to E2a, run `POST /collections/{name}/migrate` (or `archon-search collection migrate <name>`) for each collection before using TTL or scopes features. All five existing v0 startup migrations are unaffected. The schema bump adds two migration specs at `introduced_at=1`; these are NOT applied at server startup (unlike the five v0 migrations) — explicit per-collection migration is required.

**Migration**: no action required for existing callers that do not use TTL or scopes. Run `POST /collections/{name}/migrate` for each collection when you are ready to use E2a features. Strict-schema validators should add the new optional fields to their type stubs; regenerate from `GET /openapi.json`.

---

### [next release] — E2a BE-4: `PATCH /collections/{name}` `embedding_model` field is now optional

**Surface**: `PATCH /collections/{name}` request body (`PatchCollectionBody`).

**Change**: `embedding_model` was previously a required `string` field. It is now an optional `string | null` field (default `null`). Callers that always provide `embedding_model` are completely unaffected. Sending `embedding_model: null` or omitting the field entirely skips the embedding-model state machine — only the newly-added `default_ttl_seconds` field is applied when provided.

**Migration**: no action required for existing callers that always supply `embedding_model`.

---

### [next release] — E1b: `graph_mode` extended to `"local"` and `"global"`; `StatusCollectionEntry` gains community stats (all additive)

**Surface**: `POST /search` request; `GET /status` response (`StatusCollectionEntry`); `GET /openapi.json` schema; MCP `search` tool.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **`SearchRequest.graph_mode` extended from `"naive" | null` to `"naive" | "local" | "global" | null`** — the two new string literals `"local"` and `"global"` activate community-based retrieval modes. Clients that already pass `graph_mode="naive"` or omit the field are completely unaffected. Clients with strict-schema validators that enumerate the allowed literals must add `"local"` and `"global"`. When either new value is supplied and `[graph] enabled = false`, the server returns `422`; callers that always omit `graph_mode` are unaffected.

2. **`GET /status` `StatusCollectionEntry` gains `community_count: int` and `last_built_at: str | null`** — always present (zero and null when communities have not been built or when graph is disabled). Strict-validating clients must add these fields to their `StatusCollectionEntry` type stubs. Tolerant clients are unaffected.

3. **`GET /status` `GraphCollectionStats` gains `community_count: int` and `last_built_at: str | null`** — same semantics as (2), scoped to the per-collection entry inside `graph.collections`. Strict-validating clients must update their `GraphCollectionStats` stubs.

4. **New auxiliary LanceDB table** — when `[graph] enabled = true`, `CommunityBuilder.build()` creates a `_archon_graph_{col}_communities` table per collection (triggered via `archon-search graph build-communities <COLLECTION>`). This is auxiliary/internal, never exposed via REST or MCP pagination. Deployments without `graph.enabled = true` are completely unaffected.

**Migration**: no action required for tolerant JSON consumers. Strict-schema validators should add `"local"` and `"global"` to `SearchRequest.graph_mode`, `community_count: int` to `StatusCollectionEntry` and `GraphCollectionStats`, and `last_built_at: str | null` to both. Regenerate from `GET /openapi.json`. To use the new modes: install `archon-search[graph]`, set `[graph] enabled = true`, re-ingest, then run `archon-search graph build-communities`.

---

### [next release] — E1a: graph tables, `SearchRequest.graph_mode`, `SearchResponse.graph_expansion_applied` (all additive)

**Surface**: LanceDB auxiliary tables (internal); `POST /search` request and response; `GET /status` response; MCP `search` and `search_with_context` tools.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **Auxiliary LanceDB graph tables** — when `[graph] enabled = true`, two new LanceDB tables are created per collection after ingest: `_archon_graph_{col}_nodes` and `_archon_graph_{col}_edges`. These are auxiliary to the main chunk table and are never exposed via REST or MCP pagination. Deployments that do not set `[graph] enabled = true` are completely unaffected — no tables are created and no startup overhead occurs.

2. **`SearchRequest` gains `graph_mode: "naive" | null = null`** — additive optional field. Existing clients that omit the field continue to receive unmodified behaviour. Strict-schema validators that use `extra="forbid"` on `SearchRequest` must add this nullable field. When `"naive"` is supplied and `[graph] enabled = false`, the server returns `422`; callers that always omit `graph_mode` are unaffected.

3. **`SearchResponse` gains `graph_expansion_applied: bool`** — always present (default `false`). Strict-schema validators must add this boolean field to their `SearchResponse` type stubs. Tolerant clients are unaffected.

4. **`GET /status` gains `graph: GraphStatusDetail | null`** — `null` when `[graph] enabled = false`. When the graph is enabled, the field contains `{enabled: true, node_count: int, edge_count: int, collections: [{collection, node_count, edge_count}]}`. Strict-validating clients must add `graph` to their `StatusResponse` type stubs.

5. **MCP `search` tool gains `graph_mode: str | null = null`** — additive optional parameter. Existing callers are unaffected.

6. **MCP `search_with_context` returns `{error, code: "graph_mode_not_supported"}` when `graph_mode` is non-null** — guard behaviour; callers that do not pass `graph_mode` are unaffected.

**Migration**: no action required for tolerant JSON consumers or existing deployments without `[graph] enabled = true`. To opt in: install `archon-search[graph]`, set `[graph] enabled = true` in TOML, and re-ingest collections. Strict-schema validators should add `graph_mode: "naive" | null` to `SearchRequest`, `graph_expansion_applied: bool` to `SearchResponse`, and `graph: GraphStatusDetail | null` to `StatusResponse` type stubs; regenerate from `GET /openapi.json`.

---

### [next release] — E0e: `POST /search` multi-collection + filters now supported; `SearchResponse` gains `applied_filters`

**Surface**: `POST /search` REST endpoint.

**Behaviour change** (previously-rejected requests now succeed — non-breaking for well-behaved clients):
- `POST /search` with both `collections` and `filters` no longer returns `422 "filters are not supported for multi-collection search in v1"`. The filters are applied per-leg to the multi-collection fan-out. Clients that relied on this 422 as a guard must update their error-handling logic.

**Additive change** (non-breaking for tolerant JSON consumers):
- `SearchResponse` gains `applied_filters: SearchFilters | null` — echoes the parsed, normalised filters from the request; `null` when no filters were submitted. Present on both single-collection and multi-collection response paths. Strict-validating clients must add this nullable field to their response schemas.

---

### [next release] — E0d: `POST /ingest` gains 413 response; `IngestResult.code` field added; MCP `IngestResultSchema` gains `code` field

**Surface**: `POST /ingest` REST response; `IngestResult` domain dataclass; MCP `ingest_file` and `ingest_directory` tool return shapes (`IngestResultSchema`).

**Breaking changes**:

1. **`POST /ingest` now returns HTTP 413 when a single-file path exceeds `[ingest].max_file_mb`** — when `max_file_mb > 0` is configured and a single-file path in the request body exceeds the limit, the route returns `413 Request Entity Too Large` with `{"detail": {"code": "file_too_large", "message": "File size X MB exceeds the configured limit of Y MB (\`[ingest].max_file_mb\`). Raise the limit in \`archon-search.toml\` or split the file."}}` BEFORE any job is created. The `detail` is the structured `{code, message}` object used by the rest of the API's structured errors. Clients that assume `POST /ingest` always returns `202` or `400`/`503` must add handling for `413`. The 413 only fires when `max_file_mb > 0` (opt-in); the default (`max_file_mb = 0`) is unchanged — all ingest submissions return `202`. Directory paths are never checked at the route level (413 does not apply). `documents`-payload requests were not checked either when this entry was written; the S293 entry above supersedes that — they are now checked per document, in the same release.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

2. **`IngestResult` domain dataclass gains `code: Literal["file_too_large"] | None = None`** — `None` on success; `"file_too_large"` when the pipeline size guard fires. This field flows through `GET /jobs/{id}` result dicts (per-file results in directory ingest jobs) and the MCP tool return value. Clients that reconstruct `IngestResult` objects from raw dicts must add the `code` field or allow unknown fields.

3. **MCP `IngestResultSchema` gains `code: Literal["file_too_large"] | None = None`** — mapped from `IngestResult.code` by `from_result()`. Strictly-validating MCP clients with `extra="forbid"` on the schema must add the `code` field. Tolerant clients are unaffected. MCP `ingest_file` tool returns `{status: "error", code: "file_too_large", error: "<message>"}` when the size guard fires.

**Migration**:
- Item 1: If your client checks `POST /ingest` response codes, add a `413` case. The response body `detail` is a structured object `{"code": "file_too_large", "message": "<human-readable>"}` — read `detail.message` for display and `detail.code` for programmatic handling. (Note the `400` on the same route still returns a plain-string `detail`.) No migration needed if `max_file_mb` remains `0` (the default).
- Items 2 and 3: Purely additive if you tolerate unknown fields. For strict schemas, add `code: "file_too_large" | null` to your `IngestResult` / `IngestResultSchema` type stubs (`str | null` is acceptable for tolerant consumers).

---

### [next release] — E0c: `top_k` OpenAPI schema change; 422 envelope change for fanout and top_k validation; additive `search` sub-object on `GET /status`; new `GET /collections/{name}/documents` endpoint

**Surface**: `POST /search`, `POST /explain` request schemas; `GET /status` response; `GET /collections/{name}/documents` (new endpoint); MCP `list_documents` tool.

**Breaking changes**:

1. **`top_k` OpenAPI `maximum` constraint removed from the schema definition** — `SearchRequest.top_k` and `ExplainRequest.top_k` previously carried a static `le=100` Pydantic `Field` constraint that appeared in the generated OpenAPI schema as `"maximum": 100`. This constraint has been removed from the Pydantic model. The upper-bound check is now applied at runtime in the route handler body using `config.top_k_max` (default `100`). The schema in `GET /openapi.json` no longer carries `"maximum": 100` on the `top_k` field. Generated clients that relied on the static OpenAPI `maximum` annotation for client-side range validation must update their type stubs — the server still enforces the limit, but the schema no longer communicates it statically.

2. **422 error envelope change for fanout and `top_k` bound violations** — previously, `len(collections) > max_fanout` and `top_k > 100` were caught by Pydantic `@model_validator` and `Field(le=…)` respectively, producing a standard Pydantic validation-error list `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`. Both checks now run in the route handler body and return a `JSONResponse` with `status_code=422` and `{"detail": "..."}` body, producing a plain-string detail `{"detail": "collections length exceeds maximum of N"}` or `{"detail": "top_k N exceeds operator-configured maximum of M"}`. Clients that parse `response.json()["detail"]` as a list will see a different shape. Clients that only check `status_code == 422` and display the body verbatim are unaffected.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators):

3. **`GET /status` gains `search: SearchStatusDetail | null`** — `StatusResponse` gains an optional `search` sub-object: `{"max_fanout": int, "top_k_max": int}`. Both fields always reflect the live `SearchConfig` values (TOML overrides or defaults). Strictly-validating clients with `extra="forbid"` must add `search` to their `StatusResponse` type stubs. Tolerant clients are unaffected.

4. **New `GET /collections/{name}/documents` REST endpoint** — cursor-paginated document listing: `limit` (1–200, default 50), `cursor` (opaque, `None` to start from the beginning). Returns `{"items": [DocumentInfoItem…], "next_cursor": str | null, "total": int}`. Additive — no existing endpoints are modified. Clients that enumerate allowed routes must add this path.

5. **MCP `list_documents` gains optional `cursor` parameter** — the tool now accepts `cursor: str | None = None` in addition to the existing `collection` and `limit` parameters. Additive — existing calls without `cursor` are unaffected.

**Migration**:
- For items 1 and 2: if your client-side schema set `maximum: 100` on `top_k` from the OpenAPI snapshot, remove that static constraint. For fanout (`len(collections)`) and `top_k` bound violations specifically, the 422 `detail` value changed from a Pydantic validation-error list to a plain string — update client code that parsed `detail` as a list for these specific errors. Other Pydantic validation errors (e.g. wrong field type for `top_k`) still produce the list shape. Alternatively, regenerate client types from the updated `GET /openapi.json` snapshot.
- For item 3: regenerate client types from `GET /openapi.json` or add `search: { max_fanout: int, top_k_max: int } | null` to your `StatusResponse` type stubs.
- For items 4 and 5: purely additive; no migration required.

---

### [next release] — E0b: additive fields on SearchResponse, StatusResponse, StatsResponse; new FAILED_EXPIRED job status

**Surface**: `POST /search`, `GET /status`, `GET /telemetry/stats` REST responses; MCP `search` and `search_with_context` tool returns; `JobStatus` enum.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

- `POST /search` and MCP `search`/`search_with_context` responses gain `expansion_used: bool` and `expansion_warning: str | null`. `expansion_used = hyde_applied OR rag_fusion_applied`. `expansion_warning` is non-null when expansion was requested but failed (fell back to the original query embedding).
- `GET /status` response gains `hyde: HydeStatusDetail | null` (present when `[hyde].enabled = true`), `rag_fusion: RagFusionStatusDetail | null` (present when `[rag_fusion].enabled = true`), and `failed_expired_ingest_count: int` (namespace-scoped — counts only `IngestJob` failures in the authenticated namespace). Each detail object has `key_available: bool`.
- `GET /telemetry/stats` response gains `truncated_count: int` — count of log entries where `result_doc_ids` was trimmed to 8 KB by the writer (since D8). Entries written before E0b have `truncated=None` and are not counted.
- `JobStatus` enum gains `FAILED_EXPIRED = "FAILED_EXPIRED"`. A terminal state for ingest jobs that aged past `retry_max_age_hours` or exhausted all retry attempts. `GET /jobs?status=FAILED_EXPIRED` is a valid filter. All five `_TERMINAL_STATUSES` definitions are updated to include this value.

**Migration**: no changes required for tolerant JSON consumers. Clients that exhaustively switch/match on `JobStatus` must add a case for `FAILED_EXPIRED`. Clients with strict-schema validators (`extra="forbid"`) must add the new fields to their schemas. Regenerate client types from `GET /openapi.json` (the snapshot in `tests/server/openapi_snapshot.json` has been updated).

---

### [next release] — E0b FE-2: `export --wait` and `backup --now --wait` exit codes changed; `--timeout` option added

**Surface**: `archon-search export --wait` and `archon-search backup --now --wait` CLI commands.

**Breaking changes**:
1. `archon-search export --wait` previously exited with code `1` when the job status was `FAILED`. It now exits `2` on `FAILED` or `FAILED_EXPIRED`. Callers that relied on exit code `1` to detect export failure must update to check exit code `2`.
2. `archon-search backup --now --wait` previously exited with code `1` when any backup job status was `FAILED`. It now exits `2` on `FAILED` or `FAILED_EXPIRED`. Same migration requirement.
3. Both commands previously had no timeout — they polled indefinitely. They now exit `0` on timeout (with a recovery hint on stderr) after the `--timeout` duration (default 300 s).

**New behavior summary for both commands**:
- Exit 0 + recovery message on stderr: timed out (job(s) may still be running).
- Exit 0 + success message: job(s) completed successfully (DONE).
- Exit 2 + error on stderr: job confirmed FAILED or FAILED_EXPIRED.
- Exit 1: fatal error (auth failure, network error, HTTP 4xx).

**New `--timeout SECONDS` option** (default 300): controls the maximum seconds `--wait` polls before declaring a timeout. The option is additive and backward-compatible for scripts that do not pass `--timeout`; the only behavioral change is the new finite timeout (previously infinite) and the exit-code changes on FAILED.

**Migration**:
- Scripts that check `exit_code == 1` to detect `export --wait` or `backup --now --wait` failure must update to `exit_code == 2`. Both `FAILED` and `FAILED_EXPIRED` now exit 2.
- A timeout now exits 0; check stderr for the recovery hint string if distinction is required.
- The import command's `--wait` path is explicitly OUT OF SCOPE for E0b — its timeout behavior is noted as tech debt for CLI consistency.

---

### [next release] — E0b FE-1: `maintenance run --wait` timeout exit code changed 1 → 0

**Surface**: `archon-search maintenance run --wait` CLI command.

**Breaking change**: `archon-search maintenance run --wait` previously exited with code `1` when the poll timed out waiting for the maintenance pass to complete. It now exits `0` on timeout and prints a recovery hint on stderr: `"Poll with 'archon-search maintenance status' to check progress."` Callers that relied on exit code `1` to detect timeout must now distinguish between timeout (exit 0 + stderr hint) and success (exit 0, no hint).

**New behavior summary**:
- Exit 0 + recovery message on stderr: timed out (pass may still be running).
- Exit 0 + "Maintenance pass complete." on stdout: pass completed successfully.
- Exit 2 + error on stderr: pass completed with errors (collection-level failures visible via `archon-search maintenance status`).
- Exit 1: fatal error (auth failure, network error, HTTP 4xx).

**New `--timeout SECONDS` option** (default 120): controls the maximum seconds `--wait` polls before declaring a timeout. The option is additive and backward-compatible; existing scripts that do not pass `--timeout` continue to behave as before except for the exit-code change on timeout.

**Migration**: scripts that check `exit_code == 1` to detect a `--wait` timeout must be updated. A timeout now exits 0; check stderr for the recovery hint string `"archon-search maintenance status"` if distinction is required.

---

### [next release] — D7: new `/keys` REST endpoints and `key` CLI commands (additive)

**Surface**: four new REST endpoints under `/keys`; four new MCP tools; new `key` CLI command group.

**New REST endpoints** (all require `Bearer` token):
- `POST /keys` → `201 KeyCreateResponse` — issues a new managed API key; token present once.
- `GET /keys` → `200 KeyListResponse` — lists managed keys; active-only by default.
- `DELETE /keys/{id}` → `200 KeyRevokeResponse` — revokes a managed key; idempotent.
- `POST /keys/rotate` → `200 KeyRotateResponse` — generates a new default key; token present once; returns `409` when `ARCHON_SEARCH_API_KEY` env var is set.

**New MCP tools**: `create_key`, `list_keys`, `revoke_key`, `rotate_key`. MCP tool count: 13 → 17.

**New CLI commands**: `archon-search key create`, `key list`, `key revoke <id>`, `key rotate`.

**New config section**: `[auth]` with `rotate_grace_seconds = 0`.

All changes are **additive** — existing single-key deployments require zero config changes. The `ARCHON_SEARCH_API_KEY` env var and TOML `[namespaces]` tokens continue to work unchanged. No existing REST endpoints, MCP tools, or CLI commands are modified.

**Migration**: none required for existing deployments. Add the four new `/keys` paths to client schemas if strict validation is in use.

---

### [next release] — D6: `CheckStatus` gains `PENDING` and `WARN`; `GET /ready` gains `checks.models`; `GET /status` gains `model_validation`

**Surface**: `GET /ready` and `GET /status` REST responses; `CheckStatus` enum.

- `CheckStatus` now includes two additional values: `PENDING = "pending"` and `WARN = "warn"` (previously only `OK` and `FAIL`). Clients that exhaustively switch/match on `CheckStatus` must add cases for both.
- `GET /ready` `checks` object gains a `models` field (`CheckStatus`): `"pending"` before background model validation completes, then `"ok"` (both probes pass), `"warn"` (provider fallback occurred), or `"fail"` (a model could not load). The top-level `ready: bool` is **NOT** gated on `models` — it remains storage-only, so load balancers keying on `ready` are unaffected.
- `GET /status` gains an optional `model_validation: ModelValidationStatus | null` sub-object (`embedder_ok`, `reranker_ok`, `provider_warnings`, `validated_at`); all fields are `null` while validation is pending.
- Both response changes are additive — permissive clients that ignore unknown fields are unaffected.

**Migration**: add `PENDING` and `WARN` to any exhaustive `CheckStatus` switch/match. Add the `checks.models` and `model_validation` keys to client schema definitions if strict validation is in use. No action required for read-only callers that key only on `ready`.

---

### [next release] — D4: `centroid_incremental_enabled` config field removed

**Surface**: `archon-search.toml` `[database]` section.

- The `centroid_incremental_enabled` field has been removed from `SearchConfig`. The B5 incremental centroid path is now unconditional.
- If present in an existing TOML config, the value is discarded and a WARNING is logged at startup.
- The per-operation full-recompute gate (previously activated by `centroid_incremental_enabled = false`) has been removed. The full-recompute path itself (`recompute_collection_meta`) still exists and fires on `needs_recompute = True` or periodic drift-reset checkpoints.

**Migration**: remove `centroid_incremental_enabled` from your `archon-search.toml`. For operators who never set this flag (or had it set to `true`), there is no functional change — the incremental path was already the default since B5. If you had explicitly set it to `false`, your deployment will now use the B5 incremental centroid path; run `archon-search collection reindex <name>` if you observe centroid drift.

---

### [next release] — D2 job contract: `JobResponse` gains bulk-job subclass fields

**Surface**: `GET /jobs/{job_id}` and `GET /jobs` REST responses (`JobResponse`).

- `JobResponse` now includes four optional nullable fields: `source` (`"user" | "backup" | null`), `collection` (`str | null`), `output_path` (`str | null`), `archive_path` (`str | null`).
- These fields are populated for `ExportJob` and `ImportJob` records; base `IngestJob`, `ReindexJob`, and `DeleteJob` records return them as `null`.
- Additive change. Strict-schema clients will see four new keys; permissive clients are unaffected.

**Migration**: no action required for read-only callers. Add the four keys to client schema definitions if strict validation is in use.

---

### [next release] — D2 status contract: `StatusResponse` will gain a `backup` object

**Surface**: `GET /status` REST response (`StatusResponse`).

- `StatusResponse` will gain an optional `backup: BackupStatusDetail | null` field describing scheduled-backup state (enabled flag, interval, last/next tick, per-collection backup status). Added in a later D2 task; documented here to give clients a heads-up.
- Additive change. Existing callers that ignore unknown fields are unaffected.

**Migration**: no action required for read-only callers. Add the `backup` key to client schema definitions if strict validation is in use, once the field ships.

---

### [next release] — D1/D2 MCP tools: `export_collection` and `import_collection` added (tool count 11 → 13)

**Surface**: MCP tool registry (`create_app`).

- Two new tools registered: `export_collection(collection, output_path="")` and `import_collection(collection, path, force_overwrite=False, ignore_schema_version=False, on_error="fail")`.
- Both tools are **additive** — existing MCP tool consumers are unaffected.
- MCP clients that enumerate the tool list will now see 13 tools instead of 11.

**Migration**: none required.

---

### [next release] — D1 job contract: `progress` field on `JobResponse` and `QUEUED` status

**Surface**: `GET /jobs/{job_id}` REST response (`JobResponse`), `JobStatus` enum.

- `JobResponse` now includes an optional `progress` field (`dict | None`, default `null`). Existing callers that don't reference this field are unaffected; callers that use strict schema validation will now see `progress: null` for job types that don't set it.
- `JobStatus` now includes `QUEUED` as a valid value (between `PENDING` and `RUNNING`). Clients that exhaustively switch on status strings must add a `QUEUED` case.

**Migration**: no action needed for read-only callers. Add `QUEUED` to any exhaustive status switch/match.

---

### [next release] — C7 MCP Pydantic responses: field-narrowing on collection and context tools

**Surface**: MCP tools `list_collections`, `get_collections_meta`, `get_collection_meta`, `update_collection`, `search_with_context`.

All five tools previously returned raw `dataclasses.asdict()` payloads. They now validate return values through explicit Pydantic schemas before serialization. This removes internal/transient fields that were never part of the public contract and renames one field for consistency.

---

**`list_collections` — removed fields + field rename**:
- Removed from every item in the returned list: `centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`.
- Field **renamed**: `active_embedding_model` → `embedding_model`. Clients that read `active_embedding_model` will receive `undefined`/`null` after this change — update to read `embedding_model`.
- Fields `centroid` and `description_embedding` were already stripped before C7; that behaviour is unchanged.

**Migration**: remove any references to the seven removed fields from client code. Replace `active_embedding_model` with `embedding_model`.

---

**`get_collections_meta` — removed fields + field rename + `description_embedding` is always present**:
- Same removals as `list_collections` above (`centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`).
- Field **renamed**: `active_embedding_model` → `embedding_model`.
- `description_embedding` was previously absent when `include_description_embedding=false` (the default). It is now always present in the schema, serialized as `null` when not included. Strict-schema clients that reject unknown or null-valued fields must add `description_embedding: list[float] | null` to their type stubs.

**Migration**: replace `active_embedding_model` with `embedding_model`. Tolerate `description_embedding: null` in the default case, or explicitly request it with `include_description_embedding: true`.

---

**`get_collection_meta` — removed fields + field rename**:
- Removed: `centroid`, `centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`.
- Field **renamed**: `active_embedding_model` → `embedding_model`.
- Previously `centroid` (a raw float vector) was included in the response. It is now absent entirely.
- `description_embedding` was added in B4 to this tool's response. It is now **removed**: `get_collection_meta` uses `CollectionDetailSchema`, which does not include this field. Clients that were reading `description_embedding` from `get_collection_meta` must switch to `get_collections_meta` with `include_description_embedding: true`.

**Migration**: replace `active_embedding_model` with `embedding_model`. Remove references to `centroid` and the internal fields listed above. If you need `description_embedding`, use `get_collections_meta` instead.

---

**`update_collection` — removed fields + field rename**:
- Same removals and rename as `get_collection_meta` above.

**Migration**: same as `get_collection_meta`.

---

**`search_with_context` — context chunk fields removed**:
- Context chunks in `context_before` and `context_after` previously included `start_offset`, `end_offset`, and `custom_score` (only `vector` was stripped in A1). These three transient fields are now excluded from all context chunk items.
- `language` is retained — it was present before C7 and remains in the schema. This is NOT a breaking change.

**Migration**: remove any client-side reads of `start_offset`, `end_offset`, and `custom_score` from context chunk items.

---

**New error code for schema drift**:
- All 11 MCP tools now return `{"error": "<message>", "code": "schema_validation_error"}` when a Pydantic schema validation error is raised during response construction (e.g., a domain dataclass field was added without updating the MCP schema). This replaces a potential silent shape change or unhandled exception.

**Migration**: add handling for `code == "schema_validation_error"` if your client distinguishes error codes.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on MCP tool return dicts

**Surface**: MCP tools `search`, `search_with_context`, and `explain`.

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `search` and `search_with_context` return dicts gain `rag_fusion_applied: bool` (default `false`), `rag_fusion_queries_used: int` (default `0`), and `rag_fusion_attempted: bool` (default `false`).
- `explain` return dict gains `rag_fusion_applied: bool` (default `false`), `rag_fusion_queries_used: int` (default `0`), `rag_fusion_attempted: bool` (default `false`), `rag_fusion_failure_reason: str | null` (default `null`), and `rag_fusion_sub_queries: list[{variant_index, result_count, top_doc_ids}] | null` (default `null`).
- `search`, `search_with_context`, and `explain` tools each gain a new `rag_fusion: bool = false` parameter.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add these fields to their tool return type stubs.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on ExplainResponse

**Surface**: `POST /explain` response (`ExplainResponse`); `POST /explain` request (`ExplainRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `ExplainRequest` gains `rag_fusion: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `ExplainResponse` gains `rag_fusion_applied: bool` (default `false`). Indicates whether the explain used RAG Fusion multi-query decomposition and result fusion.
- `ExplainResponse` gains `rag_fusion_queries_used: int` (default `0`). Number of successful LLM-generated variant searches (not counting the original query).
- `ExplainResponse` gains `rag_fusion_attempted: bool` (default `false`). Indicates whether RAG Fusion generation was attempted (true even if fallback to single-query occurred).
- `ExplainResponse` gains `rag_fusion_failure_reason: str | null` (default `null`). Populated with the error type when RAG Fusion was attempted but fell back.
- `ExplainResponse` gains `rag_fusion_sub_queries: list[RagFusionSubQueryResult] | null` (default `null`). Per-variant result summaries when RAG Fusion succeeded; each entry has `variant_index`, `result_count`, and `top_doc_ids`.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add all six fields to their `ExplainResponse` and `ExplainRequest` type stubs.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on SearchResponse

**Surface**: `POST /search` response (`SearchResponse`); `POST /search` request (`SearchRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `SearchRequest` gains `rag_fusion: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `SearchResponse` gains `rag_fusion_applied: bool` (default `false`). Indicates whether the search used RAG Fusion multi-query decomposition and result fusion.
- `SearchResponse` gains `rag_fusion_queries_used: int` (default `0`). Number of successful LLM-generated variant searches (not counting the original query).
- `SearchResponse` gains `rag_fusion_attempted: bool` (default `false`). Indicates whether RAG Fusion generation was attempted (true even if fallback to single-query occurred).

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add these fields to their `SearchResponse` and `SearchRequest` type stubs.

**Announced in**: this release.

### [next release] — C4 HyDE query expansion: additive fields on SearchResponse and ExplainResponse

**Surface**: `POST /search` response (`SearchResponse`); `POST /search` request (`SearchRequest`); `POST /explain` response (`ExplainResponse`); `POST /explain` request (`ExplainRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `SearchRequest` gains `hyde: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `SearchResponse` gains `hyde_applied: bool` (default `false`). Indicates whether the ANN lookup was driven by a HyDE-generated hypothesis embedding. Clients that validate the response schema strictly must accept this new field.
- `ExplainRequest` gains `hyde: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `ExplainResponse` gains `hyde_applied: bool` (default `false`). Indicates whether the ANN lookup was driven by a HyDE-generated hypothesis embedding. Clients that validate the response schema strictly must accept this new field.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add `hyde_applied: bool` to their `SearchResponse` and `ExplainResponse` type stubs, and `hyde: bool` to their `SearchRequest` and `ExplainRequest` type stubs.

**Announced in**: this release.

### [next release] — C4 HyDE query expansion: MCP `search_with_context` return type change

**Surface**: MCP `search_with_context` tool.

**Breaking change**: The `search_with_context` tool previously returned a bare `list[dict]` (a list of `{result, context_before, context_after}` dicts). It now returns a wrapper object `{"results": list[dict], "hyde_applied": bool}` where `results` carries the same list. This is a **true breaking change** for any MCP client that iterates the return value directly or accesses items by index.

**Migration**: Update consumers to access `response["results"]` instead of iterating the response directly. `response["hyde_applied"]` is the new HyDE status field.

**Announced in**: this release.

### [next release] — C2 language field type change (SearchResult, ScoredSearchCandidate, ExplainResult, ExplainNearMiss)

**Python**: `SearchResult.language`, `ScoredSearchCandidate.language`, `ExplainResult.language`, and `ExplainNearMiss.language` now return `""` (empty string) for legacy/untagged chunks instead of `None`. Update `if result.language is None` guards to `if result.language == ""`.

**REST/JSON**: The `language` field in search and explain responses now serializes as `""` (empty string) instead of `null`. OpenAPI clients must update their type stubs (`nullable: false`, `type: string`).

**Migration**: Replace `if result.language is None` with `if result.language == ""` in any code that checks for an untagged language. REST consumers should expect `""` instead of `null` for the `language` field in `SearchResultSchema`, `ExplainResult`, and `ExplainNearMiss`.

**Announced in**: this release.

### [next release] — C1 per-collection embedding model (schema changes)

**Surface**: REST `GET /collections/{name}` response (breaking rename); `GET /collections/` response (additive); `POST /collections/` request (additive); `PATCH /collections/{name}` (new endpoint); `POST /search` response (additive); MCP (new `update_collection` tool).

**`GET /collections/{name}` — breaking rename + additive fields**:
- `embedding_model` field **renamed** to `active_embedding_model`. Clients that read `embedding_model` will receive `null`/`undefined` — update to read `active_embedding_model`.
- Three new fields added: `pending_embedding_model` (nullable string), `needs_reindex` (bool), `reindex_job_id` (nullable string).

**`GET /collections/` — additive fields**:
- Each `CollectionSummary` entry gains `active_embedding_model: str` and `needs_reindex: bool`. Non-breaking for tolerant consumers.

**`POST /collections/` — additive optional request field**:
- Request body gains optional `embedding_model: str | null`. When provided, the collection is initialized with that model as `active_embedding_model`; when omitted, the global `config.embedding_model` is used. Unknown models return 422.

**`PATCH /collections/{name}` — new endpoint**:
- Updates the embedding model for a collection. Implements the per-collection model state machine: clearing `pending_embedding_model` (when setting the same model as active), setting `pending_embedding_model` (model change requiring reindex), or directly updating `active_embedding_model` (when no data exists yet). Returns `CollectionDetail` (same shape as `GET /collections/{name}`). Dimension validation runs before any state mutation. Returns 404 for unknown collections, 409 when a reindex job is already in progress, 422 for unknown models or dimension mismatches.

**`POST /search` — additive field**:
- `SearchResponse` gains `embedding_model: str`. For single-collection searches this is the collection's `active_embedding_model`; for multi-collection fan-out it is `config.embedding_model` (the global default).

**`PATCH /collections/{name}` — 422 conditions**:
- Returns `422` for unknown models **and** for dimension mismatches (when the requested model produces vectors of a different dimension than the existing index).

**MCP — new `update_collection` tool** (11th tool):
- Accepts `collection_name: str` and `embedding_model: str`. Implements the same state machine as `PATCH /collections/{name}`. Returns the updated `CollectionMeta` dict or `{error, code}` on failure.

**Migration**:
- `GET /collections/{name}` consumers: replace `embedding_model` with `active_embedding_model` in client code. Add handling for the three new fields (`pending_embedding_model`, `needs_reindex`, `reindex_job_id`).
- `GET /collections/` consumers: tolerate the new `active_embedding_model` and `needs_reindex` keys.
- `POST /search` consumers: tolerate the new `embedding_model` key.

**Announced in**: this release.

### [next release] — B5 incremental centroid maintenance (additive internal columns)

**Surface**: `_archon_collection_meta` LanceDB table (internal schema). No public REST or MCP contract change.

**Additive internal columns**:
- `centroid_sum_json` (`utf8`) — JSON-encoded element-wise sum of all chunk vectors; combined with `chunk_count` satisfies `centroid = centroid_sum / chunk_count`. Empty string when unset.
- `mutations_since_recompute` (`int64`) — incremented on every ingest/delete; reset to `0` after a full recompute. `-1` sentinel = pre-B5 row.
- `needs_recompute` (`bool`) — set `True` when incremental maintenance cannot proceed (model mismatch, NaN/Inf in vectors, missing seed sum). Cleared on successful `recompute_collection_meta`.

These columns are **not exposed** via any REST endpoint, MCP tool, or CLI command. They are internal routing metadata. Adding them is **non-breaking** for any public API consumer.

**Mixed-version deployment caveat**: an older binary's `update_collection_meta` uses its own schema (without the three new columns). If that binary performs a delete-then-insert upsert on a table that already carries the new columns, LanceDB's upsert semantics may null out the new columns on the affected row, which the B5 runtime treats identically to `needs_recompute = True` and recovers via a full recompute on next access. Do not run mixed-version deployments without first verifying your LanceDB version's actual upsert behavior. The safe upgrade path is to restart all instances to the same binary version before relying on incremental centroid accuracy.

**Migration**: none required for operators. The three columns are populated lazily on first ingest or reindex after upgrade. Existing collections are treated as `needs_recompute = True` until their first post-upgrade recompute.

**Announced in**: this release.

### [next release] — B4 hybrid collection routing

**Surface**: MCP `get_collection_meta`, `get_collections_meta`, and `list_collections` tools.

**`get_collection_meta` — additive output key (breaking for strict-validating clients)**:
- The single-collection `get_collection_meta` tool now returns `description_embedding: list[float] | null` on the `CollectionMeta` dict. The field is `null` when no embedding has been computed (e.g., immediately after upgrade before the startup migration runs). For **tolerant JSON consumers**: non-breaking additive key. For **strict-validating MCP clients** (`extra="forbid"` schemas): the new key is a true contract change — relax the client schema or handle the new field.

**`get_collections_meta` — additive optional INPUT parameter**:
- Gains an optional `include_description_embedding: bool = False` parameter. When `false` (the default), `description_embedding` is stripped from every `CollectionMeta` in the returned list. When `true`, the field is included. The `description_embedding` field was never present pre-B4, so stripping it by default is safe for existing consumers. Clients that reject unknown MCP tool input parameters may need to be updated to tolerate the new parameter.

**`list_collections` — no change for existing clients**:
- `list_collections` has always stripped `centroid` from the returned dict; it likewise strips `description_embedding`. No behavior change for existing clients — the field was never present.

**REST `/route` shape unchanged**: the routing strategy and description-weight blending are internal to `MultiCollectionRouter`; the `/route` response schema (`{pre_context, pinned_names, routable_names, decomposer_invoked}`) is unchanged.

**Migration**:
- `get_collection_meta` consumers using strict schema validation: add `description_embedding: list[float] | null` to your client-side schema, or relax to tolerate unknown fields.
- `get_collections_meta` consumers: no action needed — `description_embedding` is absent by default. To include it, pass `include_description_embedding: true`.
- To enable hybrid routing: set `[routing] routing_strategy = "hybrid"` and optionally tune `routing_description_weight` (default `0.3`) in `archon-search.toml`.

**Announced in**: this release.

### [next release] — B3 multi-collection search

**Surface**: REST `POST /search` and `POST /explain` (additive responses + request-schema change); MCP `search`/`explain` tools (additive response-shape change for strict clients).

**Request — additive/optional on both surfaces**:
- `POST /search` and the MCP `search` tool gain an optional `collections: list[str]` field for fanning out a single query across multiple collections in one request. `POST /explain` and the MCP `explain` tool gain the same field.

**Request-schema change (REST `/search`)**:
- `SearchRequest.collection` changes from required (`str`) to optional (`str | None = None`). Exactly one of `collection` / `collections` must be supplied. Existing clients that omit `collection` now receive a different 422 message (`"supply either collection or collections"` instead of Pydantic's `"field required"`); clients that explicitly send `collection: null` get the same new error. This is a request-schema behavioral change for clients that relied on the old validation message. The MCP `search` tool preserves its existing `default_collection` fallback when neither field is supplied.

**Response — additive keys**:
- Every result in `POST /search` / `POST /explain` (REST) and the `search`/`explain` MCP tools gains a `collection` key naming its origin collection. Each response gains an `excluded_collections` list (entries `{"name", "reason"}`) reporting collections dropped from a multi-collection request (e.g. embedding-model mismatch).
- For tolerant JSON consumers (REST), these are non-breaking additive keys. For **strict-validating MCP clients** (schemas with `extra="forbid"`), the new `collection` and `excluded_collections` keys are a true contract change — the same class as A1's additive-key break. Relax the client schema to tolerate the new keys.

**`/explain` multi-collection constraint**:
- `POST /explain` (and MCP `explain`) with `rerank=false` AND more than one collection returns 422 / a `validation_error` with message `"reranking cannot be disabled for multi-collection search in v1"`. Single-collection `rerank=false` is unchanged.

**Announced in**: this release.

### [next release] — B2 (additive): `GET /ready` endpoint and `readiness` field on `GET /status`

**Surface**: REST (additive — new endpoint and new field on existing endpoint).

**Changes**:
- **New `GET /ready` endpoint** (unauthenticated) — returns `{"ready": bool, "checks": {"storage": "ok"|"fail"}}`. HTTP 200 when the storage layer is connected; HTTP 503 with the same `ReadinessResponse` body when not. This is a readiness probe, not a liveness probe — `/health` (`{status, version}`) remains the liveness signal. Both endpoints are unauthenticated and listed in `_EXEMPT_PATHS`.
- **New `readiness` field on `GET /status` response** — `StatusResponse` gains an optional `readiness: ReadinessDetail | None` sub-object containing `storage_connected`, `embedder_warm`, `reranker_warm`, `jobs` (pending/running counts), `collections_indexing`, `collections_failed`, and `watcher.running`. This field is absent (`null`) when the server cannot populate it; tolerant consumers require no migration.

Both changes are **additive and backward-compatible**. Existing consumers of `GET /health`, `GET /status`, or any other endpoint are unaffected.

**Announced in**: this release.

### [next release] — B1 observability: `stage_timings_ms` on `POST /explain` and MCP `explain`

**Surface**: REST `POST /explain` (additive new field), MCP `explain` tool (additive new field in returned dict).

**Change**: When `[observability].stage_timings_enabled = true` (the default), the `ExplainResponse` returned by `POST /explain` and the `explain` MCP tool gains a `stage_timings_ms: dict[str, float]` field containing per-stage blocked-coroutine wall times in milliseconds. When `stage_timings_enabled = false`, the field is absent entirely (not `null`).

This is a **new optional field**; it does not appear on any other endpoint or MCP tool.

**Affected clients**:
- Tolerant JSON consumers (e.g., `response.json()["stage_timings_ms"]` with a `.get()` fallback): no migration needed.
- Pydantic models or other strict-schema validators with `extra="forbid"` on the `ExplainResponse` shape will **reject** responses when timings are enabled. Either relax the model or set `stage_timings_enabled = false` in `archon-search.toml`.

**Migration**:
- To suppress the field: set `[observability] stage_timings_enabled = false` in `~/.archon-search/archon-search.toml`.
- To tolerate it in strict validators: add `stage_timings_ms: dict[str, float] | None = None` to your client-side schema.

**Announced in**: this release.

### [next release] — A2 query-side filters

**Surface**: REST (additive, non-breaking for tolerant JSON consumers); MCP (additive, non-breaking — all new params are optional with sensible defaults).

**REST — additive**:
- `POST /search` request body gains an optional `filters` field (`SearchFilters` object, `null` = no filter). Existing clients that omit `filters` are unaffected.
- `SearchFilters` fields: `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`, `include_metadata`.
- **C2 update**: `language` is now an active filter (ISO 639-1/639-3 code or `"unknown"`). Previously rejected with 422 for any non-empty value. Invalid values (not matching `[a-z]{2,3}` or `"unknown"`, or used with multi-collection fan-out) still return 422.

**MCP — additive**:
- `search` tool gains optional kwargs: `include_metadata` (bool, default `false`), `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`. All default to `None`/`false`; existing callers passing only `query` and `collection` are unaffected.
- `search_with_context` tool gains the same optional kwargs.
- **C2 update**: `language` is now an active filter; valid ISO codes and `"unknown"` are accepted. Invalid values still return `{error: ..., code: "validation_error"}`.

**Operational — datetime normalization**:
- Date-range filters (`indexed_after`, `indexed_before`) compare lexicographically against the `indexed_at` column using fixed-width UTC format (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). Collections indexed before A2 may contain variable-precision timestamps that produce incorrect date-range results. Run `archon-search collection reindex-metadata <name> --normalize-timestamps` (introduced in this release) to rewrite all rows to the fixed-width format. This command requires the server to be running and routes through `POST /collections/{name}/reindex-metadata`.

**Migration**:
- REST: no migration needed for existing clients — `filters` is optional.
- MCP: no migration needed — new kwargs are optional with defaults.
- Operators using date-range filters on pre-A2 collections: run `reindex-metadata --normalize-timestamps` before relying on date-range filter results.

**Announced in**: this release.

### [next release] — A4 explain endpoint (purely additive)

**Surface**: REST (new endpoint), MCP (new tool), telemetry enum, internal diagnostics, routing.

**Change**: entirely additive — no existing API surface is modified.

- **New `POST /explain` REST endpoint** — returns per-stage retrieval/reranking score breakdown plus optional routing decision. Authenticated identically to all other REST endpoints. All schemas use `extra="forbid"`; unknown request fields produce `422`.
- **New `explain` MCP tool** (10th tool) — same response structure as REST; returns `ExplainResponse.model_dump(mode="json", exclude_none=False)`. When `config` is absent from `create_app`, collectionless calls fall back to `default_collection`.
- **New `EndpointKind.explain` enum value** in `telemetry/entry.py` — additive to the `StrEnum`; no existing `EndpointKind` value is changed or removed.
- **New optional fields on `ScoredSearchCandidate`** (`_diagnostics.py`) — `acl: list[str] | None = None` plus A1/A2 metadata fields (`file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`, `metadata`). All defaulted; existing construction sites are unaffected.
- **Determinism improvement on `MultiCollectionRouter.rank()`** — equal-similarity entries now have a stable ascending-`name` tie-break. Previously the order was undefined for tied scores. This is a determinism fix, not a breaking change; callers that relied on undefined ordering may observe a different (now stable) ordering.

**Migration**: none required. All changes are purely additive. Existing `/search`, `/route`, and MCP tool consumers are unaffected.

### [next release] — A1 metadata schema v1

**Surface**: MCP (breaking for strict-validating clients), REST (additive, non-breaking for tolerant JSON consumers).

**MCP — truly breaking**:
- `search` tool: response result items gain `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, `acl`.
- `search_with_context` tool: result items gain the same six keys; `context_before` / `context_after` items gain `file_type`, `updated_at`, `ingested_by`, `metadata`, and **no longer include `vector`** (raw embeddings were never useful at the MCP boundary and inflated payload size by `~dim*4` bytes per neighbor).
- `list_documents`: items gain the same six keys when sourced from rows that carry them.

A1 is the **last** untyped MCP shape break before C7 wraps responses in Pydantic models.

**REST — additive (non-breaking)**:
- `/search` result items gain the same six keys. Tolerant JSON consumers see new fields appear; strict-schema consumers (e.g., generated clients pinned to the older OpenAPI snapshot) must regenerate. (There is no `/search/context` REST endpoint; the equivalent capability is the MCP `search_with_context` tool, listed separately in the MCP section above.)

**New 503 contract on `/ingest` and `/collections` (A5c)**:
- During an active reindex of the same collection, the store may raise `StoreBusyError` after a 30s lock-acquisition timeout. HTTP `POST /ingest` and `POST /collections` now return HTTP 503 with `Retry-After: 30` and `{"error": "store_busy", ...}` synchronously; ingest into a different collection succeeds normally. MCP `ingest_file` and `ingest_directory` surface `StoreBusyError` synchronously as `McpErrorResponse(code="store_busy")`. (A5c closes A1's deferred 503 surface.)

**`X-Ingested-By` header normalization**:
- Missing/empty → `"http"`.
- Canonical values (`cli`, `http`, `watcher`, `reindex`) pass through.
- Legacy `"archon-search-cli"` is normalized to `"cli"` at the boundary — clients that pass legacy and inspect the stored value will now see `"cli"`.
- Unknown values are coerced to `"http"` with a WARNING log (value truncated to 32 chars).

**Migration**:
- MCP consumers: regenerate types or relax strict-mode validation; the new keys are additive on every response item, never replace existing ones.
- REST consumers using tolerant JSON parsing: nothing to do. Strict-typed clients: regenerate from the updated OpenAPI.
- Existing collections: pre-A1 rows continue to read as-is via the read-boundary normalizer (`ingested_by` legacy → `"cli"`, empty `file_type` → `""`, `updated_at` falls back to `indexed_at`). To populate real values on pre-A1 rows, run `archon-search collection reindex-metadata <name>` (requires the server to be running; routes through `POST /collections/{name}/reindex-metadata`).

**Announced in**: this release. No prior deprecation — the impacted MCP shape was never documented as stable.

### [next release] — `POST /search` pipeline-exception behavior (CON-5 / A3)

**`POST /search` pipeline-exception behavior** (`routes_search.py`, CON-5 / A3):
- Pipeline stage exceptions (embedder, store, reranker) now return **HTTP 500** with a **plain-text** body `Internal Server Error` (Content-Type `text/plain`) instead of HTTP 200 with `{results: [], acl_filtered: false}`. The route bare-re-raises the exception; Starlette's `ServerErrorMiddleware` produces the default text response — this is **not** a JSON envelope. Callers MUST NOT call `.json()` on the 500 response body; doing so will raise a JSON decode error.
- A hung pipeline call now returns **HTTP 504** with `{"detail": "Search timed out"}` after ~30 s (matching the `/route` timeout contract).
- Migration: callers that treated an empty-results 200 as a pipeline-error signal must now handle HTTP 5xx. Callers that already treat 5xx as errors are unaffected.
- The `503` meta-lookup path (`get_collection_meta` raises) is unchanged.
- MCP `search` / `search_with_context` tools are unchanged.

### [next release] — A5a ingest path safety

**Surface**: MCP (behaviour change), REST (additive).

- MCP `ingest_file` and `ingest_directory` previously accepted paths containing `..` segments, empty strings, whitespace-only strings, NUL bytes, and non-absolute paths, and silently followed/resolved them. They now reject those inputs and return `McpErrorResponse(error=..., code="path_unsafe")` with an LLM-readable reason.
- HTTP `POST /collections` and `POST /jobs/ingest` gain a new `400` response (`ErrorDetail`, `detail` prefixed `"path is unsafe:"`) for the same input classes — additive (the `400` was not previously in the OpenAPI schema).

**Migration**: callers must pass absolute paths without `..` traversal. `path: null` (documents-only) ingest on `POST /jobs/ingest` was unaffected when this entry was written; the S293 entry above supersedes that — every `documents[].source_path` now gets the same check, in the same release. Symlinks and absolute-path scope are intentionally NOT validated (deferred to a future `allowed_dirs` feature).

**Announced in**: this release. No prior deprecation — the silent-acceptance behaviour was never documented as stable.

### [next release] — MCP `search` tool response shape

**Surface**: MCP (`mcp.py` `search` tool)
**Change**: `search` tool now returns `{"results": [...], "acl_filtered": bool}` instead of `[{...}, {...}]` (bare list of result dicts).
**Migration**: Update consumers to access `response["results"]` instead of iterating the response directly. `response["acl_filtered"]` provides the ACL filter flag previously unavailable on the MCP surface.
**Announced in**: this release (no prior deprecation period — the old shape was never documented as stable).

### [next release] — REST `/search` per-request `top_k` no longer honored

**Surface**: REST (`/search` POST)
**Change**: The `top_k` field in `SearchRequest` is now ignored at the route level; the pipeline uses `config.top_k_return` instead. Previously, each request could specify its own `top_k`.
**Migration**: Configure `[database] top_k_return` in `archon-search.toml` to set the desired result count.
**Announced in**: this release (the behavior was supported but never documented as stable).

### [next release] — D3 migration tooling: new REST endpoints and `STORE_SCHEMA_VERSION` policy

**Surface**: two new REST endpoints on `routes_collections.py`; `GET /status` response (`StatusResponse`).

**New endpoints**:
- `GET /collections/{name}/migrations/pending` — returns `{collection, pending: [MigrationSpec], schema_version}`. Each `MigrationSpec` has `name`, `kind` (`"in_place"`, `"rewrite"`, or `"export_rebuild"`), `description`, and `introduced_at`. Returns `404` for unknown or cross-namespace collections.
- `POST /collections/{name}/migrate` — accepts `{backup_confirmed: bool, dry_run: bool}`. In-place-only migrations return `200` with `{migrations_applied: [name…]}` synchronously; no `MigrationJob` is created. Rewrite migrations require `backup_confirmed: true` (returns `422` without it) and return `202` with a `MigrationJob` job ID. `export_rebuild` migrations always return `422` (execution deferred to D5). `409` when a `ReindexJob` is active for the collection.

**`StatusResponse` additions**:
- `store_schema_version: int` — current `STORE_SCHEMA_VERSION` constant.
- `collections_schema_behind: int` — count of collections whose `schema_version < STORE_SCHEMA_VERSION`.

All changes are additive. Strict-schema clients will see new fields; tolerant clients are unaffected.

**`STORE_SCHEMA_VERSION` bump policy**: increment this constant whenever a structural change to `_schema()` (the shared chunk-table schema) or `_meta_schema()` (the collection-metadata schema) requires existing rows to be migrated. **Exception:** per-collection chunk-table-only changes (e.g. `migrate_acl`) do NOT require a version bump — only changes to the shared `_schema()` or `_meta_schema()` require it. Every bump must also add a corresponding `MigrationSpec` entry to `SearchStore._all_migrations()`. `STORE_SCHEMA_VERSION = 0` for D3 (all five formalised startup migrations have `introduced_at = 0`).

**Migration**: no action required for existing callers. Add the new endpoint paths to client schemas if strict validation is in use. Add `store_schema_version` and `collections_schema_behind` to `StatusResponse` type stubs.

---

### [next release] — D5 maintenance jobs: `IngestJob` base class gains `source`, `source_path`, `collection`, `retry_count` fields (BE-7)

**Surface**: `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/ingest`, `DELETE /jobs/{job_id}` REST responses (`JobResponse`); all IngestJob-family subclasses (`ReindexJob`, `DeleteJob`).

**Changes**:
- `source: str` — moved to `IngestJob` base class with default `"user"`. Previously absent (`null`) for base `IngestJob`, `ReindexJob`, and `DeleteJob` responses. Now serializes as `"user"` for all IngestJob-family types. For `ExportJob`, `ImportJob`, and `MigrationJob`, the Literal type is narrower (`"user" | "backup"`) and is unchanged in practice. **Breaking for strict consumers**: `source` changes from `null` to `"user"` for base ingest, reindex, and delete jobs.
- `source_path: str` — new field on `IngestJob` base; defaults to `""`. Set by the ingest worker when a file ingest job is created. `JobResponse` gains `source_path: str = ""`.
- `collection: str` — moved to `IngestJob` base class with default `""`. Previously `null` for base `IngestJob`, `ReindexJob`, and `DeleteJob` responses. Now serializes as `""` for those types. **Breaking for strict consumers**: `collection` changes from `null` to `""` for base ingest, reindex, and delete jobs.
- `retry_count: int` — new field on `IngestJob` base; defaults to `0`. Incremented by `MaintenanceLoop` on each retry attempt. `JobResponse` gains `retry_count: int = 0`.

**Migration**: update client schemas to accept `source: "user"` (not `null`) for base ingest/reindex/delete job responses, and `collection: ""` (not `null`) for the same. Add `source_path: str` and `retry_count: int` to `JobResponse` type stubs.

**Announced in**: this release.

---

### [next release] — E0b BE-7: ingest job `result` contract changes from `null` to `{"warnings": [...]}`

**Surface**: `GET /jobs/{id}` REST response (`JobResponse.result`); `GET /jobs` list items.

**Changes**:
- `result: null | dict` — previously `null` for all ingest jobs (`FileIngestJob`, `DirectoryIngestJob`) after `DONE`. Now always `{"warnings": list[str]}` where `warnings` is the list of non-fatal warning strings collected during ingest (e.g. oversized ACL sidecar). An empty list (`[]`) means no warnings. **Breaking for strict consumers** that assert `result == null` after a completed ingest job.

**Migration**: relax client checks from `result == null` to `result == null or isinstance(result, dict)`. Add `warnings: list[str]` to `JobResponse.result` type stubs. An empty list is the normal (no-warning) case.

**Announced in**: this release.

---

### [next release] — D5 maintenance jobs: `GET /status` gains `maintenance` field (BE-4, BE-8)

**Surface**: `GET /status` REST response (`StatusResponse`).

**Change** (additive):
- `maintenance: MaintenanceStatusDetail | null` — new nullable field on `StatusResponse`. Present when `app.state.maintenance_loop` is set (always when the server starts normally). `null` only when the maintenance loop is absent (e.g. custom startup without `create_app`).
- `MaintenanceStatusDetail` contains: `enabled: bool`, `last_run_at: string | null`, `next_run_at: string | null`, `collection_health: CollectionHealthEntry[]`.
- `CollectionHealthEntry` contains: `collection: string`, `namespace: string`, `fts_optimized_at: string | null`, `orphans_removed_last_run: int`, `last_retry_at: string | null`, `last_error: string | null`, `mutations_since_recompute: int`, `centroid_recompute_threshold: int`, `meta_chunk_count: int`.
- `collection_health` is namespace-scoped to the caller's API key namespace.

**Additive change**: fully backward-compatible for tolerant consumers. Strict-validating REST clients (`extra="forbid"` schemas) must add `maintenance` to `StatusResponse`.

**Migration**: regenerate client types from `GET /openapi.json`. No behavior changes to existing `GET /status` fields.

**Announced in**: this release.

---

### [next release] — D5 maintenance jobs: new `POST /maintenance/trigger` endpoint (BE-4)

**Surface**: REST API — new route.

**Change** (additive):
- `POST /maintenance/trigger` — new endpoint. Triggers an immediate maintenance pass. Returns `202 Accepted` with `{"status": "triggered"}` when the pass is enqueued. Returns `202 Accepted` with `{"status": "already_triggered"}` when a pass is already pending or running. Requires `Bearer` token (same auth middleware as all other routes).

**Additive change**: no existing endpoints changed. Strict-validating REST clients that enumerate allowed routes must add `/maintenance/trigger`.

**Announced in**: this release.

---

### [next release] — D3 migration tooling: new nullable fields on `JobResponse` (BE-11)

**Surface**: REST (all endpoints that return `JobResponse`: `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/ingest`, `POST /jobs/export`, `POST /jobs/import`, `DELETE /jobs/{job_id}`, `POST /jobs/{id}/resume`)

**Change**: `JobResponse` gains three new nullable fields (default `null`):
- `kind: string | null` — migration sub-kind (`"in_place"`, `"rewrite"`, `"export_rebuild"`); `null` for all non-migration jobs
- `migrations_applied: string[] | null` — list of migration names applied by a `MigrationJob`; `null` for all non-migration jobs
- `backup_confirmed: boolean | null` — whether the operator confirmed a backup before a rewrite migration; `null` for all non-migration jobs

For tolerant JSON consumers: fully additive — no client changes required. For strict-validating REST clients (`extra="forbid"` schemas): the three new keys are a true contract change — relax the client schema or regenerate from the updated OpenAPI snapshot.

**Migration**: regenerate client types from `GET /openapi.json`. No behavior changes to existing job kinds.

**Announced in**: this release.

### [next release] — E2f synonym edges: additive `GraphCollectionStats` fields and `GraphEdgeResponse.relationship_type`

**Surface**: REST `GET /status` (field `graph.collections[].synonym_edge_count`, `graph.collections[].singleton_node_pct`, `graph.collections[].synonym_link_rate`) and `GET /graph/{collection}` (field `edges[].relationship_type`).

**Change**: fully additive — no existing fields are removed, renamed, or have their types changed.

- `GraphCollectionStats` (each entry in `GET /status` → `graph.collections`) gains three new fields:
  - `synonym_edge_count: int = 0` — number of edges with `relationship_type='synonym_of'` in the collection
  - `singleton_node_pct: float = 0.0` — percentage of entity nodes with no edges (isolated nodes)
  - `synonym_link_rate: float = 0.0` — fraction of all edges that are synonym edges (0.0–1.0)
  - Note: `connected_component_count` was drafted but removed before release (never computed, always returned 0). It is NOT in the schema.
- `GraphEdgeResponse` (each entry in `GET /graph/{collection}` and `GET /graph/cross-collection` → `edges[]`) gains:
  - `relationship_type: str = "related_to"` — the edge's relationship type; `"synonym_of"` for synonym edges, `"related_to"` for all pre-E2f edges

For tolerant JSON consumers: fully additive — no client changes required. For strict-validating REST clients (`extra="forbid"` schemas): the new keys are a true contract change — relax the client schema or regenerate from the updated OpenAPI snapshot.

**Migration**: regenerate client types from `GET /openapi.json`. No behavior changes to existing graph traversal or search.

**Announced in**: this release.

---

### [next release] — S310: namespace key collections now stored under the caller's namespace (not `default`)

**Surface**: `POST /ingest`, `GET /collections`, `POST /search` — multi-namespace deployments only.

**Change**: `SearchStore.ingest_chunks` was dropping the `namespace` parameter on its internal meta write, causing every collection to be stored with `namespace='default'` regardless of the bearer token used. Fixed in commit `ddc78995`. Going forward, a collection ingested via a namespace key is stored under that key's namespace.

**Impact on existing deployments**: collections ingested by a namespace key before this fix have their `CollectionMeta.namespace` persisted as `'default'`. After upgrading:
- `GET /collections` with the namespace key still returns `[]` for those collections.
- `POST /search` with the namespace key still returns 404 for those collections.
- The default key can still see and search them (the data is not lost).

**Migration** (per affected collection):
1. Note the collection name.
2. Delete it via the **default** key: `DELETE /collections/{name}` — this removes the stale meta row. The chunk data in LanceDB is also dropped, so you must re-ingest.
3. Re-ingest using the **namespace key**: `POST /ingest` with the namespace bearer token. The collection will now be stored under the correct namespace.

There is no in-place namespace update API; the delete + re-ingest path above is the only option.

**Announced in**: this release.

---

### [next release] — brief 270: `BackupTriggerResponse.queued` changed from `list[string]` to `list[QueuedBackupJob]`

**Surface**: REST `POST /backup/trigger` response body field `queued`.

**Change**: `queued` was `string[]` (bare job IDs); it is now `QueuedBackupJob[]` where each entry has:
- `collection: string` — the collection name being backed up
- `job_id: string` — the job ID to track

The `skipped` field is unchanged.

For tolerant JSON consumers: the array element type changed from `string` to `object` — this is NOT additive; clients iterating `queued` as strings will break. For strict-validating REST clients: regenerate from the updated OpenAPI snapshot.

**Migration**: update any client that reads `response.queued[i]` as a string to read `response.queued[i].job_id` for the job ID and `response.queued[i].collection` for the collection name.

**Announced in**: this release.
