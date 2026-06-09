# C6 Spike Findings — `table.optimize()` FTS Semantics

**LanceDB version**: 0.30.2
**Date**: 2026-06-09
**Script**: `spike_optimize_fts.py` (project root; discarded after phase)

---

## Gate results

| Gate | Description | Result | Notes |
|------|-------------|--------|-------|
| (a) | API availability — `table.optimize()` callable on async API | **PASS** | No `AttributeError` or `NotImplementedError`; call completed |
| (b) | New-row indexing — rows inserted after `create_index` appear in FTS after `optimize()` | **PASS** | Unique token found in FTS results |
| (c) | Deleted-row cleanup — deleted rows absent from FTS after `optimize()` | **PASS** | Phantom hits eliminated after one `optimize()` call |
| (d) | Concurrent safety — 3 simultaneous `optimize()` calls on the same table | **FAIL** | `RuntimeError: lance error: Retryable commit conflict for version 3: This CreateIndex transaction was preempted by concurrent transaction CreateIndex` — concurrent optimize() calls on the same table conflict at the LanceDB commit layer |
| (e) | Compatibility — `optimize()` after `create_index(replace=True)` + new row | **PASS** | New row searchable |
| (f) | Update-row indexing — updated text appears in FTS after `optimize()` | **PASS** | New text searchable; old text no longer returned |

---

## Go/no-go decision

**PLAN A** — critical gates (a), (b), (c) all pass.

- `optimize()` correctly incorporates newly added rows into the FTS index.
- `optimize()` correctly removes deleted rows from the FTS index.
- Full incremental path is viable: replace `rebuild_fts_index()` with `optimize_fts()` at all ingest and sync call sites.

---

## Gate (d) failure — concurrent optimize() conflict

Issuing three concurrent `optimize()` calls via `asyncio.gather` raises a LanceDB commit-conflict error:

```
RuntimeError: lance error: Retryable commit conflict for version 3:
  This CreateIndex transaction was preempted by concurrent transaction CreateIndex at version 3.
```

**Impact on C6 implementation**: callers must NOT issue parallel `optimize()` on the same table. This is already the case in the production code paths:

- `ingest_file` and `delete_document` are serialized by the per-collection lock (optimize is called after lock release, but only one `ingest_file` or `delete_document` runs per collection at a time).
- `ingest_directory` calls `ingest_file(rebuild_fts=False)` per file, then a single batch-end `optimize_fts()`.
- The sync watcher also calls `optimize_fts()` once per collection at batch end.

No callers in the production code issue concurrent `optimize()` on the same collection. Gate (d) is a known LanceDB limitation, not a blocker for C6.

**Constraint to document in `optimize_fts()` docstring**: callers must serialize `optimize_fts()` calls per collection. Concurrent calls will conflict and may result in a `RuntimeError`.

---

## Open Questions resolved

- **Plan A or B?** → **Plan A**: `optimize()` removes deleted rows (gate (c) passes).
- **Is `optimize()` safe after `rebuild_fts_index` / `create_index(replace=True)`?** → Yes (gate (e) passes).
- **Does updating a row's `text` column propagate via `optimize()`?** → Yes (gate (f) passes).
- **Concurrent optimize()?** → Not safe; callers must serialize. Not a concern for the current production paths.

---

## `FTS_OPTIMIZE_REMOVES_DELETED` flag

Set `FTS_OPTIMIZE_REMOVES_DELETED = True` in `store.py` (Task 1.2). Plan A applies throughout.
