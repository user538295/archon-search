# Bug Brief: Job-level collection exclusivity is silently dropped — same-collection ingests interleave, three disjoint lock domains, thread-unsafe parser init race

**ID:** 2026-08-19-060 · **Severity:** P1 · **Status:** Open
**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]]

## Problem

Three related concurrency gaps around ingest:

1. **Job-level exclusivity doesn't exist.** REST handlers pre-acquire the store's per-collection
   lock (`server/_ingest_lock.py:16-43` → `store.lock_for`, `store.py:283`), but
   `_default_ingest_task_with_lock` **releases it before dispatch** (`routes_jobs.py:261-265`).
   The release is *necessary* under the current design — store operations re-acquire the same
   non-reentrant lock per call (`StoreBusyError` sites `store.py:1225,1776,2267`) — but the net
   effect is that after the release, a second `POST /ingest` or `collection add` on the **same**
   collection acquires freely and the two directory ingests interleave at per-operation
   granularity (interleaved `delete_document` / `ingest_chunks` / FTS ops on the same table).
   The comment "pipeline.ingest_file / ingest_directory will acquire it internally" is misleading:
   the *pipeline* acquires nothing; only individual store calls do.
2. **Sync uses a different lock registry.** `SearchCollectionSync` serializes via its own
   `_collection_locks` (`sync.py:842-846`, used at :536/:697), disjoint from `store.lock_for` and
   from the route pre-acquire. A watcher/startup sync and a job ingest of the same collection run
   fully concurrently. (The startup sync additionally holds a third lock, `app.state.sync_lock` —
   `app.py:716` — which only excludes other *syncs*.)
3. **DocumentParser lazy init is racy under exactly this concurrency.**
   `parser.py:192-194` documents the `self._pool` check-then-set (`:196-201`) as "NOT THREAD SAFE …
   The RAG pipeline is sequential, so this is an accepted limitation." The premise is false:
   concurrent ingest jobs call `parser.parse()` from multiple `asyncio.to_thread` workers, so the
   race can construct **two `ProcessPoolExecutor`s** — doubling the model/OCR stacks that
   [[2026-08-19-010-image-ocr-unbounded-memory-brief.md]] shows cost GBs — and docling models are
   themselves not thread-safe.
   **Blast radius reduced 2026-08-19** by brief 010's fix: the duplicate is now a second parse
   *worker process* rather than a second `DocumentConverter` inside the server, so the extra model
   stack is out of process and dies with the executor that owns it. **For the owner:** the P1
   severity above was set with the in-process cost in mind, so item 3's contribution to it may now
   read as overstated — verdict left unchanged pending your call. Items 1 and 2 are unaffected.

## Failing repro

- Concurrency exists in production: on 2026-08-19 the two user jobs (financialwell 11:36:59,
  n1ka 11:37:11) ran concurrently in one process — their per-file log lines interleave from
  11:37 to 11:41.
- Same-collection interleave: submit two `POST /ingest` for the same collection+directory
  back-to-back. The second is **not** rejected/queued once the first has passed its dispatch
  point (lock released at `routes_jobs.py:264-265`); both walk the directory concurrently.
  Assert via a spy on `store.ingest_chunks` observing interleaved doc_ids from both jobs.
- Parser race: from two threads, call a fresh `DocumentParser().parse()` on two images
  concurrently with an instrumented `ProcessPoolExecutor` construction; a barrier there makes both
  threads pass the `self._pool is None` check → 2 constructions.

## Root cause

One lock object (`store.lock_for`) is overloaded for two different scopes (whole-job admission
vs single store operation), forcing the job path to release it; nothing re-establishes job-scope
exclusivity. Sync grew its own registry independently. The parser predates concurrent callers.

## Fix

1. **Separate job-scope from op-scope locking.** Introduce a per-collection *ingest-job* lock
   (own registry, e.g. on the store or a small `IngestAdmission` helper) that
   `_default_ingest_task_with_lock` holds for the **entire** dispatch (`try/finally`), while
   store ops keep using `lock_for` internally. Second same-collection job → existing 503
   `store_busy` path in `acquire_collection_lock_or_503` (keep the response shape).
2. **Unify sync with the same job-scope lock:** `SearchCollectionSync._get_lock` should delegate
   to that registry (or take it as a constructor dependency) so watcher/startup syncs and REST
   jobs mutually exclude per collection. Delete `_collection_locks`.
3. **Fix the parser race regardless:** guard `_docling_pool`'s lazy init with a
   `threading.Lock` (double-checked), exactly as the comment at `parser.py:194` anticipates, and
   delete the stale "pipeline is sequential" justification. (Brief 010's subprocess isolation has
   landed, so the converter itself is already out of process — what the race now duplicates is the
   pool. The lazy init and its comment are unchanged, so this item still stands.)
4. Correct the misleading comment at `routes_jobs.py:261-263`.

## Verification

- Test: two concurrent same-collection ingest jobs → second receives 503 `store_busy` (or queues,
  per chosen semantics); different collections still run concurrently.
- Test: watcher-triggered `sync_collection` blocks while a job ingest of the same collection is
  in flight (and vice versa).
- Test: parser barrier repro above constructs exactly one `ProcessPoolExecutor`.

## References

- [[archon_search/server/routes_jobs.py]] — release-before-dispatch (:256-266), finally-release (:300-302)
- [[archon_search/server/_ingest_lock.py]] — pre-acquire + 503 shape (:16-43)
- [[archon_search/store.py]] — `lock_for` (:283), per-op acquisition/`StoreBusyError` (:1225, :1776, :2267)
- [[archon_search/sync.py]] — disjoint `_collection_locks` (:842-846; used :536, :697)
- [[archon_search/parser.py]] — `_docling_pool` documented race (:181-202; comment :192-194, check-then-set :196-201)
