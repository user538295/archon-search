**Purpose**: Guide for TTL (Time-To-Live) expiry and per-chunk scoping.
**Audience**: Operators and developers running archon-search with session memory or multi-agent corpora.
**Status**: Stable
**Last reviewed**: 2026-07-29

# TTL and Scoping

archon-search supports optional `expires_at` timestamps and `scopes` tags on ingested chunks. Two use cases drive this:

- **TTL** — session or scratch data that should auto-prune after a set lifetime.
- **Scoping** — multi-agent or multi-user corpora where a single collection holds content filtered by caller identity.

Both are opt-in per ingest (or per collection default), and both require a one-time schema migration on each existing collection before use.

## Prerequisites: schema migration

TTL and scoping add two columns to the chunk table and one to the collection-metadata table. These are **not** applied automatically at server startup — you must run the in-place migration for each collection after upgrading. This is the same `POST /collections/{name}/migrate` flow described in the migration guide; see [../MigrationGuide/05_data_migration.md](../MigrationGuide/05_data_migration.md) for the full upgrade procedure, backup guidance, and rollback notes.

```bash
# Migrate a collection:
curl -s -X POST http://localhost:8765/collections/<name>/migrate \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"backup_confirmed": false}'

# Verify — should return {"pending": []} after migration:
curl -s http://localhost:8765/collections/<name>/migrations/pending \
  -H "Authorization: Bearer <your-api-key>"

# Check how many collections are behind the current schema:
curl -s http://localhost:8765/status \
  -H "Authorization: Bearer <your-api-key>" | python3 -m json.tool | grep collections_schema_behind
```

Until a collection is migrated, TTL data is silently omitted at ingest time (the store detects the missing columns and skips them). **Do not send `scope_filter` against an un-migrated collection** — the store would build a SQL predicate referencing the missing `scopes` column and error out. Migrate every collection before using either feature.

## Setting a TTL on ingested chunks

TTL precedence: **per-request `chunk_ttl_seconds`** > **collection `default_ttl_seconds`** > **null (no expiry)**.

### Per-request TTL (REST)

`chunk_ttl_seconds` is accepted by `POST /ingest` and validated to a positive integer range (see `routes_jobs.py`).

```bash
curl -s -X POST http://localhost:8765/ingest \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "collection": "my-collection",
    "path": "/path/to/file.md",
    "chunk_ttl_seconds": 3600
  }'
# chunks expire 1 hour from ingest time
```

### Collection default TTL

Set (or clear) a collection-wide default via `PATCH /collections/{name}`:

```bash
# Set a 24-hour default TTL (forward-only: existing chunks unchanged)
curl -s -X PATCH http://localhost:8765/collections/my-collection \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"default_ttl_seconds": 86400}'

# Clear the default TTL (explicit null)
curl -s -X PATCH http://localhost:8765/collections/my-collection \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"default_ttl_seconds": null}'
```

**Forward-only**: changing `default_ttl_seconds` does not retroactively update existing chunks. Only chunks ingested after the change pick up the new default.

### Viewing upcoming expirations

`GET /collections/{name}/expiring` lists chunks due to expire within a window. `within_hours` is required and must be between 1 and 8760 (one hour to one year):

```bash
# Chunks expiring in the next 24 hours:
curl -s "http://localhost:8765/collections/my-collection/expiring?within_hours=24" \
  -H "Authorization: Bearer <your-api-key>" | python3 -m json.tool
```

## Automatic pruning (maintenance loop)

Expired chunks are removed by the in-process maintenance loop, gated on `[maintenance] prune_expired_chunks` (default `true`). Enable the loop by giving it a run interval:

```toml
[maintenance]
interval_hours = 24
prune_expired_chunks = true
```

Restart the server after config changes. To force an immediate pass without waiting for the interval:

```bash
# Trigger an immediate maintenance pass (pruning runs in-process):
curl -s -X POST http://localhost:8765/maintenance/trigger \
  -H "Authorization: Bearer <your-api-key>"

# Check results in /status:
curl -s http://localhost:8765/status \
  -H "Authorization: Bearer <your-api-key>" | python3 -c "
import json, sys
s = json.load(sys.stdin)
m = s.get('maintenance', {})
print('expired_chunk_count:', m.get('expired_chunk_count'))
print('last_expired_pruned_at:', m.get('last_expired_pruned_at'))
"
```

`expired_chunk_count` is the **live point-in-time** count of chunks past their expiry — not the delta from the last prune run. For the full maintenance-loop surface (FTS optimize, orphan cleanup, graph GC, failed-ingest retry), see [../OperatorGuide/50_maintenance_and_jobs.md](../OperatorGuide/50_maintenance_and_jobs.md).

## Scoping chunks for multi-agent corpora

### Ingesting with scopes (REST)

`chunk_scopes` is accepted by `POST /ingest` and validated for list size and per-item length:

```bash
curl -s -X POST http://localhost:8765/ingest \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "collection": "shared-corpus",
    "path": "/data/session-alice.md",
    "chunk_scopes": ["user:alice", "session:2024-01-15"]
  }'
```

- `chunk_scopes=[]` is normalized to `null` (unscoped / shared). An explicit empty list is the same as omitting the field.
- Chunks ingested without `chunk_scopes` have `scopes=null` — they are always visible to any `scope_filter`.

### Searching with a scope filter

`scope_filter` is a `POST /search` (and `/explain`) parameter. Two forms are valid: an exact scope, or a single trailing-`*` wildcard with a non-empty prefix. Exact scopes are pushed into the SQL predicate; trailing-`*` wildcards are post-filtered Python-side on the top-k set.

```bash
# Exact match: only chunks with exactly "user:alice" in scopes
curl -s -X POST http://localhost:8765/search \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "collection": "shared-corpus",
    "query": "session notes",
    "scope_filter": "user:alice"
  }'

# Wildcard: chunks with "user:alice", "user:alice:thread-1", etc.
curl -s -X POST http://localhost:8765/search \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "collection": "shared-corpus",
    "query": "session notes",
    "scope_filter": "user:alice*"
  }'
```

**Unscoped chunks always pass through**: chunks with `scopes=null` are returned alongside scope-matching chunks for any `scope_filter`, letting shared/public content coexist with user-specific content in one collection.

### Invalid `scope_filter` patterns → 400

The validator (`server/_validators.py`) rejects these with HTTP 400 (`code: invalid_scope_filter`):

| Pattern | Why it fails |
|---|---|
| `""` (empty) | must not be empty |
| `"*"` (bare wildcard) | needs a non-empty prefix |
| `"user:*alice"` (mid-string `*`) | `*` allowed only at the end |
| `"user:**"` (double wildcard) | only one `*` permitted |

### `scope_filter` + `graph_mode` → 422

`scope_filter` is mutually exclusive with any `graph_mode` value — the graph search paths bypass the scope predicate. Sending both returns HTTP 422 (`"scope_filter is not supported with graph_mode"`). Use `scope_filter` without a graph mode, or run a graph query without a scope filter. See [60_searching.md](60_searching.md) and [65_graph_search.md](65_graph_search.md) for the search surface.

## MCP tool surface

The same TTL and scoping parameters are available via the MCP endpoint; parameter names match REST exactly:

- `ingest_file` / `ingest_directory` accept `chunk_ttl_seconds: int | null` and `chunk_scopes: list[str] | null`.
- `search` / `search_with_context` / `explain` accept `scope_filter: str | null`.

All validation rules (TTL range, scope list size/length, `scope_filter` syntax, `scope_filter` + `graph_mode` incompatibility) apply identically. MCP errors use the `{"error": "...", "code": "..."}` shape instead of HTTP status codes. See [../Architecture/600_api_reference_or_public_interface.md](../Architecture/600_api_reference_or_public_interface.md) for the full MCP parameter table.

## Known limitations

- **Watcher-managed files and TTL pruning**: if a collection is managed by the watcher (`archon-search sync`) and its chunks are pruned by the TTL policy, they are **not** automatically re-ingested — the watcher tracks files by modification time (`mtime`), not chunk presence. To restore pruned chunks, `touch` the source file (bumping its mtime) or re-ingest it via `POST /ingest`. If watcher-managed content must survive between watcher runs, set the TTL longer than the maintenance interval.
- **Watcher ingests carry no per-request TTL/scopes**: background-watcher ingests use `chunk_ttl_seconds=None` and `chunk_scopes=None`. A collection `default_ttl_seconds` still applies, but per-request scopes cannot be set for watcher-triggered ingests.
- **`PATCH default_ttl_seconds` is forward-only**: it never retroactively updates existing chunks.
- **`scope_filter` + `graph_mode` is unsupported** (422). Use scope filtering without graph modes.
- **Pruned chunks are permanent**: TTL chunks are intentionally ephemeral. Once pruned they are gone unless the source file is re-ingested.

## Related documents

- [00_index.md](00_index.md) — User Manual table of contents
- [50_ingestion_and_collections.md](50_ingestion_and_collections.md) — ingesting files and managing collections
- [55_chunk_metadata_and_enrichment.md](55_chunk_metadata_and_enrichment.md) — per-chunk metadata and enrichment
- [60_searching.md](60_searching.md) — search parameters, filters, and results
- [65_graph_search.md](65_graph_search.md) — graph search modes (mutually exclusive with `scope_filter`)
- [../OperatorGuide/50_maintenance_and_jobs.md](../OperatorGuide/50_maintenance_and_jobs.md) — maintenance loop and TTL pruning
- [../MigrationGuide/05_data_migration.md](../MigrationGuide/05_data_migration.md) — schema migration procedure
