## Bug: collection add never persists the path to archon-search.toml; a restart makes the collection permanently unresolvable

**ID**: S07-collection_add_persists_path
**Scenario**: S07
**Severity**: high
**Version**: archon-search, version 26.8.1751

### What happened
`archon-search collection add <dir>` registers the path in the RUNNING server only. Nothing is written to `archon-search.toml`, so the registration does not survive a server restart — and it cannot be repaired afterwards.

Sequence observed 2026-08-01 on 26.8.1751:
1. `archon-search collection add /tmp/s187_col --wait` -> succeeds. `GET /collections/s187_col` reports `"path": "/private/tmp/s187_col"`.
2. `~/.archon-search/archon-search.toml` is UNCHANGED — its `[collections]` section still reads `collections = []`. No added path appears anywhere in the file.
3. Restart the server. `GET /collections/<name>` now reports `"path": ""` — the collection still exists (docs and chunks intact, `/search` works) but has lost its configured path.
4. Every endpoint that requires a configured path now 404s for it:
   - `GET /collections/{name}/migrations/pending` -> 404 `Collection '<name>' not found`
   - `POST /collections/{name}/migrate` -> 404 `Collection '<name>' not found`
   - `POST /collections/{name}/reindex-metadata` -> 404
5. `archon-search collection add /tmp/s187_col` (the obvious repair) -> `Error: collection name already registered`. That 409 is itself correct and documented (50_ingestion_and_collections.md:112), but it means the damaged state CANNOT be repaired by re-adding.

Net effect: the only recovery is `collection remove` followed by `collection add`, i.e. destroying and fully re-ingesting the collection. An operator restarting the server silently loses migrate/reindex-metadata/migrations-pending on every collection they ever added, with a 404 that says the collection does not exist while `collection list` and `/search` both show it does.

### What should happen
`docs/UserManual/50_ingestion_and_collections.md:104` states plainly:

    "Registers the path as a collection and enqueues an ingest job. The server writes
     the path to `archon-search.toml` server-side (`_maybe_save_config`) — the CLI
     writes no TOML."

So the path must be written to `archon-search.toml` by the server at add time, and the collection must therefore still carry its configured path after a restart, keeping `migrations/pending`, `migrate` and `reindex-metadata` resolvable.

Observed: the file is never modified and the path is lost on restart. Either the documented `_maybe_save_config` write is not happening, or it is conditional in a way the documentation does not disclose.

### Steps to reproduce
1. `mkdir -p /tmp/persist_probe && printf '# Doc
Body.
' > /tmp/persist_probe/doc.md`
2. `archon-search collection add /tmp/persist_probe --wait`
3. `curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8765/collections/persist_probe` — note `"path": "/private/tmp/persist_probe"`
4. `grep -n 'persist_probe' ~/.archon-search/archon-search.toml` — NO MATCH (this is the defect)
5. Restart the server (`archon-search stop && archon-search start`, or `archon-search install`)
6. Repeat step 3 — `"path"` is now `""`
7. `curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" http://127.0.0.1:8765/collections/persist_probe/migrations/pending` -> 404
8. `archon-search collection add /tmp/persist_probe` -> `Error: collection name already registered` — unrepairable

### Evidence
```
Config file after adding several collections (s187_col added minutes earlier and resolving
in the running server with path /private/tmp/s187_col):

  $ sed -n '/^\[collections\]/,/^\[/p' ~/.archon-search/archon-search.toml
  [collections]
  pinned_collections = []
  collections = []
  watch = false

  $ grep -n 's187_col\|s052_col\|ttl_test_docs\|/tmp/' ~/.archon-search/archon-search.toml
  (none — no added path was persisted)

Post-restart state of collections that HAD a configured path:

  GET /collections/s052_col     -> {"name":"s052_col","path":"","doc_count":1,"chunk_count":1,...}
  GET /collections/ttl_test_docs -> {"name":"ttl_test_docs","path":"","doc_count":2,...}

  POST /collections/ttl_test_docs/migrate                 -> 404 {"detail":"Collection 'ttl_test_docs' not found"}
  GET  /collections/ttl_test_docs/migrations/pending      -> 404 {"detail":"Collection 'ttl_test_docs' not found"}

Repair attempt:

  $ archon-search collection add /tmp/s052_col --wait
  Error: collection name already registered
  (path still "" afterwards)

Contrast — a collection added since the last restart works correctly:

  GET /collections/s187_col                        -> "path":"/private/tmp/s187_col"
  GET /collections/s187_col/migrations/pending     -> 200 {"collection":"s187_col","pending":[],"schema_version":1}

SUITE IMPACT: this is the precondition failure behind 17 setup-phase ERRORs across
S52, S54, S151, S154 and S158 after a server restart — every one of which looks like a
product 404 but is this single lost-path defect.
```
