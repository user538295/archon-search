## Bug: `acl_gate.warnings` non-empty when the `.acl` sidecar is malformed

**ID**: S311-malformed_sidecar_acl_gate_warnings_non_empty
**Scenario**: S311
**Severity**: medium
**Version**: archon-search, version 26.8.1916

### What happened
AssertionError: POST /search (namespace key) returned 404: {'detail': 'collection not found'}
assert 404 == 200

### What should happen
- Step 3: HTTP 200, results present (namespace key is in the sidecar ACL); `results[0].acl_gate.warnings` is a **non-empty** list (80_explain_and_debugging.md:147: "non-empty when ACL parsing hit a recoverable problem (e.g. an oversized or malformed sidecar)"; 60_searching.md:295 names the "malformed value" trigger). The `deny-all` entry in the sidecar is an invalid namespace name (SecurityGuide/03 — reserved sentinel rejected by `is_acl_namespace_valid`) and constitutes the recoverable problem.

The mere *presence* of the `warnings` field (a list, possibly empty) is owned by S075/S228. S311's unique observable is the list being **non-empty** for a malformed sidecar entry.

### Steps to reproduce
1. Start isolated `archon-search serve` with a `[namespaces]` block granting `s311-team` namespace key.
2. Create `doc.md` + `doc.md.acl` (content: `s311-team\ndeny-all\n`) in a temporary directory; `POST /ingest` and wait for DONE.
3. `POST /search {"collection": "...", "query": "...", "acl_context": true}` with the **namespace** key (`s311-team`).

### Evidence
```
E   AssertionError: POST /search (namespace key) returned 404: {'detail': 'collection not found'}
E   assert 404 == 200
```
