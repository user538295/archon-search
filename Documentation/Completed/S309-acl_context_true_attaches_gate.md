## Bug: `acl_context` false/omitted → `acl_gate` absent, no error

**ID**: S309-acl_context_true_attaches_gate
**Scenario**: S309
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: POST /search returned 0: None
assert 0 == 200

### What should happen
- Step 2 (control): HTTP `200`; each object in `results` has a populated `acl_gate` object (a dict, carrying at least `source`) — proving the gate is attached by the flag (lines 279, 292). If the top-K is empty, the scenario is skipped, not failed (empty `200` is doc-valid, line 48).
- Step 3 (`acl_context:false`): HTTP `200`; each result's `acl_gate` is omitted or `null` — i.e. not a populated object (line 297). Same empty-top-K skip guard.
- Step 4 (`acl_context` omitted): HTTP `200` with a `results` list — the server ignores the absent field and returns no error.
- **Undocumented note**: line 297 documents the null-gate behavior only for `acl_context` being *false*. That an *omitted* `acl_context` defaults to `false` is inferred from its `bool` typing (line 42), not stated verbatim, so step 4 asserts only the directly observable "server ignores / no error" (HTTP `200` + a `results` list) and makes no claim about the gate.

### Steps to reproduce
1. Ingest `alpha.md`, `beta.md` (both mention "programming language") into a collection on an isolated server.
2. `POST /search {"collection": "...", "query": "programming language", "acl_context": true}` — positive control.
3. `POST /search {"collection": "...", "query": "programming language", "acl_context": false}`
4. `POST /search {"collection": "...", "query": "programming language"}` — `acl_context` field omitted entirely.

### Evidence
```
E   AssertionError: POST /search returned 0: None
E   assert 0 == 200
```
