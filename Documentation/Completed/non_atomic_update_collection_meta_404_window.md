## Bug: non-atomic `update_collection_meta` opens a transient 404 window on live collections

**ID**: non_atomic_update_collection_meta_404_window
**Severity**: major
**Discovered**: 2026-08-02, during S52 (`reindex-metadata --dry-run`) review

### What happened
`SearchStore.update_collection_meta` implements an upsert as a non-atomic
`table.delete(...)` **then** `table.add(...)` on the shared `_archon_collection_meta`
table, held under `lock_for(collection)`. `SearchStore.get_collection_meta` is
**lock-free** — it does not take that lock. A concurrent resolve landing in the
delete→add window sees zero rows, returns `None`, and `POST /search` for the
collection returns `404 "collection not found"` even though the collection exists.

S52 fixed this for the `reindex-metadata --dry-run` path only (by not writing meta
on a dry-run). The window is still open for every **real** meta write:
- `reindex_metadata` non-dry-run: the pre-202 `metadata_reindex_job_id` set **and**
  the `_reindex_metadata_task` `finally` clear (two writes per run).
- `reindex_collection`, `patch_collection`, `rebuild_communities`
  (`routes_graph.py`), and ~30 other `update_collection_meta` call sites.

### Why it was not fixed in S52
Pre-existing (the delete-then-add predates S52) and cross-cutting (one shared store
primitive behind ~30 call sites). Correctly deferred out of the targeted one-line
dry-run guard to keep that change's blast radius small. This is an independent,
testable unit of work — the S52 route guard and this atomic-upsert fix are
orthogonal and compose (the S52 regression test does not depend on delete-then-add).

### What should happen
No meta-write path exposes a rowless window. A concurrent `get_collection_meta`
during any meta upsert must see either the old row or the new row, never zero rows.

### Suggested fix
Replace the delete-then-add in `update_collection_meta` (`store.py`, ~1218-1265,
`_do_write_meta_unlocked`) with an atomic LanceDB `merge_insert` keyed on
`(name, namespace)` — the pattern already used in `pipeline.py` / `graph_store.py`.
That closes the window for all callers at the root. Add a regression test that
drives a **non-dry-run** meta write and asserts a concurrent resolve never returns
`None`.

### Affected files
- `archon_search/store.py` — `update_collection_meta` / `_do_write_meta_unlocked`
  (non-atomic upsert), `get_collection_meta` (lock-free reader).
- Write routes: `archon_search/server/routes_collections.py`,
  `archon_search/server/routes_graph.py`.
