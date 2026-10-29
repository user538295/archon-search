## Bug: Another namespace's collection cannot be exported or imported over, admin key included

**ID**: S930-export_of_foreign_collection_refused
**Scenario**: S930
**Severity**: medium
**Version**: archon-search, version 26.10.2285

### What happened
POST /collections/owned-a/export by team-b (and by the admin key) for a collection owned by team-a returned 404 {"error":"not_found","detail":"Collection 'owned-a' not found"} instead of the documented fixed refusal text 'collection name is not available to this namespace'.

### What should happen
- Every attempt in steps 1–4 is refused with `collection name is not available to this namespace` (in the HTTP body or the failed job's `error`; no status code is pinned by the docs) — `03_authorization_and_acl.md:33`.
- No `owned-a-*.tar.gz` is created in `exports/team-b/` or `exports/default/`.
- Step 5: `owned-a` still has `chunk_count` 1 — `03_authorization_and_acl.md:15,110`.

### Steps to reproduce
1. `curl -X POST $BASE/collections/owned-a/export -H "Authorization: Bearer $TOK_B" -H "Content-Type: application/json" -d '{}'`
2. Repeat step 1 with the admin key.
3. `curl -X POST $BASE/collections/owned-a/import -H "Authorization: Bearer $TOK_B" -H "Content-Type: application/json" -d '{"path": "<own archive>"}'`
4. Repeat step 3 with the admin key and its own archive.
5. `curl $BASE/collections/owned-a -H "Authorization: Bearer $TOK_A"`

### Evidence
```
HTTP 404 body: {"error": "not_found", "detail": "Collection 'owned-a' not found"} (both callers)
```
