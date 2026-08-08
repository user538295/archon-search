## Bug: `acl_gate.sidecar_path` provided → ACL rules applied; `acl_filtered: true`

**ID**: S310-namespace_key_acl_gate_sidecar_path_populated
**Scenario**: S310
**Severity**: medium
**Version**: archon-search, version 26.8.1916

### What happened
AssertionError: POST /search (namespace key, COLLECTION_NS) returned 404: {'detail': 'collection not found'}
assert 404 == 200

### What should happen
- Step 3 (default key, excluded): HTTP 200, `acl_filtered: true` (60_searching.md:44; SecurityGuide/03 decision table — DEFAULT_NAMESPACE is not in the sidecar ACL).
- Step 4 (namespace key, allowed): HTTP 200, results present; `results[0].acl_gate.source == "sidecar"` (60_searching.md:293); `results[0].acl_gate.sidecar_path` is non-empty (60_searching.md:294; 80_explain_and_debugging.md:146).

Adjacent observables owned by siblings and not re-asserted here: `acl_filtered` presence by S227; `acl_gate` presence and field shape on ACL-free corpus by S075/S228/S309.

### Steps to reproduce
1. Start isolated `archon-search serve` with a `[namespaces]` block granting `s310-team` namespace key.
2. Create `doc.md` + `doc.md.acl` (content: `s310-team\n`) in a temporary directory; `POST /ingest` and wait for DONE.
3. `POST /search {"collection": "...", "query": "..."}` with the **default** key (DEFAULT_NAMESPACE, excluded by sidecar ACL).
4. `POST /search {"collection": "...", "query": "...", "acl_context": true}` with the **namespace** key (`s310-team`, allowed).

### Evidence
```
E   AssertionError: POST /search (namespace key, COLLECTION_NS) returned 404: {'detail': 'collection not found'}
E   assert 404 == 200
```
