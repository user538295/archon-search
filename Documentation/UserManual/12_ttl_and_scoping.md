**Purpose**: Operator guide for E2a TTL (Time-To-Live) and per-chunk scoping features.
**Audience**: Operators and developers deploying archon-search with session memory or multi-agent corpora.
**Status**: Draft
**Last reviewed**: 2026-07-03

# TTL and Scoping (E2a)

E2a adds optional `expires_at` timestamps and `scopes` tags to ingested chunks. Session data can auto-prune and multi-agent corpora can filter by caller identity.

## Prerequisites: schema migration

E2a adds two new columns to the chunk table and one to the collection-metadata table. These are NOT applied automatically at server startup. After upgrading to E2a, run the in-place migration for each collection before using TTL or scopes:

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

Until migrated, TTL data is silently omitted at ingest time (the store detects the missing columns and skips them). **Do not use `scope_filter` on un-migrated collections** — the store will attempt to execute a SQL predicate referencing the non-existent `scopes` column, which may result in a runtime error from LanceDB. Run the migration for all collections before sending any `scope_filter` parameter.

## Setting a TTL on ingested chunks

TTL precedence: **per-request `chunk_ttl_seconds`** > **collection `default_ttl_seconds`** > **null (no expiry)**.

### Per-request TTL (REST)

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

```bash
# Set a 24-hour default TTL on the collection (forward-only: existing chunks unchanged)
curl -s -X PATCH http://localhost:8765/collections/my-collection \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"default_ttl_seconds": 86400}'

# Clear the default TTL
curl -s -X PATCH http://localhost:8765/collections/my-collection \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"default_ttl_seconds": null}'
```

**Forward-only**: `PATCH default_ttl_seconds` does NOT retroactively update existing chunks. Only newly ingested chunks pick up the new default.

### Viewing upcoming expirations

```bash
# Chunks expiring in the next 24 hours:
curl -s "http://localhost:8765/collections/my-collection/expiring?within_hours=24" \
  -H "Authorization: Bearer <your-api-key>" | python3 -m json.tool
```

## Automatic pruning (maintenance loop)

Enable the maintenance loop with TTL pruning:

```toml
[maintenance]
interval_hours = 24
prune_expired_chunks = true
```

Restart the server after config changes. Then:

```bash
# Trigger an immediate pass (pruning runs in-process):
curl -s -X POST http://localhost:8765/maintenance/trigger \
  -H "Authorization: Bearer <your-api-key>"

# Check results in /status:
curl -s http://localhost:8765/status \
  -H "Authorization: Bearer <your-api-key>" | python3 -m json.tool | python3 -c "
import json, sys
s = json.load(sys.stdin)
m = s.get('maintenance', {})
print('expired_chunk_count:', m.get('expired_chunk_count'))
print('last_expired_pruned_at:', m.get('last_expired_pruned_at'))
"
```

`expired_chunk_count` is the **live point-in-time** count of chunks past their expiry — not the prune-run delta.

## Scoping chunks for multi-agent corpora

### Ingesting with scopes (REST)

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

- `chunk_scopes=[]` is normalized to `null` (unscoped, shared/global). Explicit empty list = same as no scopes.
- Chunks ingested without `chunk_scopes` have `scopes=null` — they are always visible to any `scope_filter`.

### MCP tool surface

The same TTL and scoping parameters are available via the MCP endpoint for callers using Claude or other MCP clients. The parameter names match REST exactly:

- `ingest_file` / `ingest_directory`: accept `chunk_ttl_seconds: int | null` and `chunk_scopes: list[str] | null`
- `search` / `search_with_context` / `explain`: accept `scope_filter: str | null`

All validation rules (TTL range, scope list size/length, scope_filter syntax, scope_filter + graph_mode incompatibility) apply identically. MCP error responses use `{"error": "...", "code": "..."}` shape instead of HTTP status codes. See `Documentation/Architecture/600_api_reference_or_public_interface.md` for the full MCP tool parameter table.

### Searching with scope filter

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

**Unscoped chunks always pass through**: chunks with `scopes=null` are returned alongside scope-matching chunks for any `scope_filter`. This allows shared/public content to coexist with user-specific content in the same collection.

### Invalid scope_filter patterns -> 400

- `"*"` (bare wildcard) -> 400
- `"user:*alice"` (leading wildcard) -> 400
- `"user:**"` (double wildcard) -> 400

### scope_filter + graph_mode -> 422

`scope_filter` is not supported with any `graph_mode` value in E2a. Use `scope_filter` without `graph_mode`.

## Known limitations

- **Watcher-managed files and TTL pruning**: If a collection is managed by the watcher (`archon-search sync`) and its chunks are pruned by the TTL maintenance policy, those chunks will NOT be automatically re-ingested. The watcher tracks files by modification time (`mtime`), not chunk presence. Pruned chunks disappear permanently. To restore them, `touch` the source file (updating its mtime) or re-ingest it via `POST /ingest`. This applies to collections with `default_ttl_seconds` set — ensure the TTL is longer than your maintenance interval if watcher-managed content must survive between watcher runs.
- **Watcher ingests**: files ingested via the background watcher use `chunk_ttl_seconds=None` and `chunk_scopes=None`. The collection `default_ttl_seconds` still applies if set, but per-request scopes cannot be set for watcher-triggered ingests.
- **PATCH forward-only**: `default_ttl_seconds` changes don't retroactively update existing chunks.
- **scope_filter + graph_mode**: not supported in E2a (422). Use scope filtering without graph modes.
- **Pruned chunks are permanent**: TTL chunks are intentionally ephemeral. Pruned chunks disappear permanently unless the source file is re-ingested.
