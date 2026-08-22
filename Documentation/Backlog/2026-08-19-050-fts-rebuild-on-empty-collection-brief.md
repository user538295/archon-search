# Bug Brief: FTS optimize/rebuild fires for "ok" zero-chunk results on a never-created table — `ValueError: Table 'X' was not found` fails the whole ingest job

**ID:** 2026-08-19-050 · **Severity:** P1 · **Status:** Open — fix decided (Option B, 2026-08-19)
**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]] — this is what killed the n1ka job at 11:41:29.

## Problem

`ingest_file` returns `status="ok"` with `chunks_created=0` for files that parse to empty text
(`pipeline.py:576-577` — e.g. images whose OCR finds nothing). Such a file never reaches
`ensure_collection` (`pipeline.py:672`), so the LanceDB chunk table is never created. But
`ingest_directory` gates its end-of-run FTS pass on `any(r.status == "ok")`
(`pipeline.py:954`), so a directory whose "successful" files are all zero-chunk triggers
`optimize_fts` → `db.open_table(collection)` → `ValueError: Table 'X' was not found`
(`store.py:1895`); the fallback `rebuild_fts_index` hits the same `open_table`
(`store.py:1876`) and the exception escapes `ingest_directory`, failing the **entire job** —
after every file was already processed. The metadata-update block at `pipeline.py:971` has the
same `status == "ok"` gate and the same latent assumption.

## Failing repro

Ingest a directory containing only files that yield zero chunks (simplest: blank PNGs with OCR
enabled, or files whose parse output is empty):

```
POST /collections/  (or archon-search collection add <dir>)   # dir of blank images
```

**Observed (production, 2026-08-19 11:41:29Z):** job `4746078c…` FAILED with
`Table 'n1ka' was not found`; traceback:
`ingest_directory` (`pipeline.py:957`) → `optimize_fts` (`store.py:1895` `open_table`) →
`ValueError`; handler at `pipeline.py:958-965` → `rebuild_fts_index` (`store.py:1876`
`open_table`) → same `ValueError` → `_dispatch_ingest` → job FAILED. The n1ka corpus was 28
photo/image files: every one either aborted on the spaCy fatal
([[2026-08-19-030-graph-spacy-download-retry-and-ingest-abort-brief.md]]) or returned ok/0-chunks,
so the table never existed.

Unit-level repro: call `ingest_directory` with a pipeline whose `ingest_file` is exercised over
empty-parse files against a fresh (table-less) collection — the FTS block raises.

## Root cause

The "did we ingest anything" signal is conflated with "did any file avoid an error".
`status == "ok"` ⇏ "chunks were written" ⇏ "the table exists". Three call sites in
`ingest_directory` rely on the wrong predicate:

- `pipeline.py:954` (FTS optimize/rebuild gate)
- `pipeline.py:971` (collection-meta/description update gate)
- `pipeline.py:998` (`needs_recompute` aggregation — currently safe only because
  `needs_recompute` implies a real write, but it rides the same block)

**(Review 2026-08-19)** Blast radius is wider than the job path:

- All **four** `ingest_directory` callers are exposed to the escape: ingest job
  (`routes_jobs.py:140`), reindex job (`routes_jobs.py:395`), MCP ingest (`mcp.py:1222`, no
  local try), startup/watcher sync (`sync.py:590`).
- The incremental sync path has its **own ungated copy** of the bug: `sync.py:787-792` calls
  `optimize_fts`/`rebuild_fts_index` unconditionally after its file loop — new-but-all-empty
  files in a fresh collection kill that collection's sync pass identically.
- Predicate faithfulness confirmed: only the `.acl` skip (`pipeline.py:440`) and empty-parse
  (`pipeline.py:577`) produce ok/0-chunks, and neither touches the table. Narrow accepted gap:
  a `StoreBusyError` mid-persist zeroes `chunks_created` after possible deletes
  (`pipeline.py:701-702`), so one FTS optimize can be deferred — harmless, the MaintenanceLoop
  optimizes periodically (`maintenance_loop.py:232,331`).

## Approved fix — Option B (decided 2026-08-19)

Gate on actual writes, at **both** sites of the class:

1. **`ingest_directory` gates:** replace both `any(r.status == "ok" for r in results)`
   occurrences with `any(r.chunks_created > 0 for r in results)` (`pipeline.py:954,971`;
   `IngestResult.chunks_created` verified accurate — 0 on every non-writing path, real count
   after persist). The FTS pass, description update, and recompute all skip when nothing was
   written; the job completes DONE with warnings instead of FAILED. One choke point fixes all
   four callers.
2. **Sync's own FTS pass:** apply the same actual-writes gate to `sync.py:787-792` (the
   incremental path already has per-file results and delete counts in scope — pick the signal
   from those, mirroring the pipeline predicate).
3. **Rejected as primary fix:** store-level swallow (missing table → warn + no-op inside
   `optimize_fts`/`rebuild_fts_index`). It would mask genuine failures where a missing table
   is a real error — e.g. the import/restore path (`routes_export.py:320`). Optional hardening
   at most, never the fix.
4. **Verify during the fix, file separately if real:** MaintenanceLoop may hit the same
   `ValueError` on a meta-registered collection whose table was never created
   (`maintenance_loop.py:232,331` — it handles `FTSIndexNotFoundError`, not a missing table).

## Verification

- Test (regression, from the repro): directory of only empty-yield files → `ingest_directory`
  returns all-ok/0-chunk results, **no** `open_table` call for FTS (mock/spy), job DONE.
- Test: directory with ≥1 real file → FTS pass still runs exactly once (existing behavior).
- Test (sync): incremental sync over only empty-yield files in a fresh (table-less) collection
  → sync pass completes, no FTS call; with ≥1 real write → FTS pass runs.
- Note the LanceDB quirk already in learnings: missing table → `ValueError`.

## References

- [[archon_search/pipeline.py]] — empty→ok (:576-577), FTS gate (:953-968), meta gate (:971-999), `ensure_collection` (:672)
- [[archon_search/store.py]] — `rebuild_fts_index` (:1876), `optimize_fts` (:1895)
- [[archon_search/server/routes_jobs.py]] — `_dispatch_ingest` job failure path (:140, :266), reindex (:395)
- [[archon_search/sync.py]] — `ingest_directory` caller (:590), ungated own FTS pass (:787-792)
- [[archon_search/jobs/maintenance_loop.py]] — periodic optimize, missing-table suspect (:232, :331)
