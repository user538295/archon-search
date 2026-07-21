# Feature Brief: Fix backup status showing wrong namespace label

## Problem
`archon-search backup status` shows `namespace: default` for every collection, even when collections belong to a different namespace. Users running multiple isolated workspaces (namespaces) see misleading labels that make the backup report untrustworthy.

## Goal
The namespace column in `backup status` output reflects the actual namespace of each collection, not a hardcoded placeholder.

## Users & Context
Operators and developers who have configured multiple namespaces (e.g. `default`, `team-a`, `production`) and run `archon-search backup status` to verify backups are running correctly.

## Core Flow
1. User runs `archon-search backup status`.
2. CLI calls `GET /status` on the running server.
3. Server returns backup state including per-collection status.
4. CLI displays each collection with its correct namespace label.

## In Scope
- Add `namespace` field to `CollectionBackupStatus` schema in `schemas.py`
- Populate it in the route handler that builds `BackupStatusDetail` (wherever `collection_status` is assembled server-side)
- Remove the hardcoded `"namespace": "default"` string at `backup_cmd.py:287`; replace with `item.get("namespace", "default")` as a safe fallback

## Out of Scope
- Changing the backup schedule or backup behaviour
- Adding namespace filtering to the status command (future)

## Key Decisions
- **Server must return namespace, not CLI guess it:** The CLI has no reliable way to infer namespace from a collection name alone. The fix lives in the server schema first, CLI second.
- **Fallback to `"default"` if field absent:** Keeps backward compatibility with older server versions that don't yet return the field.

## Edge Cases & Constraints
- **Server not yet updated but CLI is:** `item.get("namespace", "default")` fallback means the display degrades to today's behaviour rather than crashing.
- **`CollectionBackupStatus` is part of `StatusResponse`:** Adding a field with a default (`namespace: str = "default"`) is non-breaking for existing clients.
- **OpenAPI snapshot test will fail:** Adding a field to `CollectionBackupStatus` requires regenerating `tests/server/openapi_snapshot.json` with `uv run --python 3.12 pytest tests/server/test_openapi_snapshot.py --update-openapi-snapshot --no-cov -n0`.

## Key Decisions (continued)

- **Fix site: `routes_status.py:362–368`, `_build_backup_status()`**: this function iterates `ns_collection_names`, builds one `CollectionBackupStatus` per collection, and already has `ns` in scope (used at line 353 for the backup archive path). The fix is adding `namespace=ns` to the `CollectionBackupStatus(...)` constructor call. No lookup via `store.get_collection_meta()` is needed.
- **`BackupLoop` does track namespace**: it stores a `job_id → (namespace, collection)` map (line 69), uses `j.namespace` in the trigger loop, and keys the state file as `"{ns}/{col}"`. Namespace is available at every relevant call site.

## Future Iterations
- `backup status --namespace team-a` filter flag to show only one namespace's collections

## References
- [[archon_search/cli/backup_cmd.py:284–291]] `[code-agent]` — hardcoded `"namespace": "default"` site
- [[archon_search/server/schemas.py:112–118]] `[code-agent]` — `CollectionBackupStatus` missing `namespace` field
- [[archon_search/server/schemas.py:120–128]] `[code-agent]` — `BackupStatusDetail` wrapping the collection list

## Recommendation
Small but meaningful correctness fix. The server change (one new field with a default) is non-breaking; the CLI change is two characters. The hardest part is finding where the server builds `CollectionBackupStatus` objects and confirming the namespace is available there. Do it in the same PR as any other `schemas.py` field additions to amortise the OpenAPI snapshot regeneration cost.
