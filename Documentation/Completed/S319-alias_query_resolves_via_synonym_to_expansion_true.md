## Bug: The doc's own `alias_file` example (`"K8s" = "Kubernetes"`, both independently extracted entities): does a `synonym_of` edge form, and does `naive` return `graph_expansion_applied: true`?

**ID**: S319-alias_query_resolves_via_synonym_to_expansion_true
**Scenario**: S319
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: no edge connecting 'K8s' and 'Kubernetes' entity IDs found in edges[] after alias_file pinned them (65_graph_search.md:106-111) -- the edge is absent (not merely a flag/search-side issue). alias-related log lines: ["2026-08-06T20:49:01Z WARNING archon_search.alias_loader AliasLoader: alias pair ('K8s', 'Kubernetes') — 'K8s' resolved to zero nodes in default/s319_synonym_docs; skipping", "2026-08-06T20:49:01Z WARNING archon_search.alias_loader AliasLoader: alias pair ('K8s', 'Kubernetes') — 'K8s' resolved to zero nodes in default/s319_synonym_docs; skipping", "2026-08-06T20:49:01Z WARNING archon_search.alias_loader AliasLoader: alias pair ('K8s', 'Kubernetes') — 'K8s' resolved to zero nodes in default/s319_synonym_docs; skipping", "2026-08-06T20:49:01Z WARNING archon_search.alias_loader AliasLoader: alias pair ('K8s', 'Kubernetes') — 'Kubernetes' resolved to zero nodes in default/s319_synonym_docs; skipping"] -- if this log shows the alias loader itself reporting 'resolved to zero nodes' for one or both names, that log claim APPEARS to contradict this same GET /graph response's own nodes[] (both names ARE present there, confirmed at fixture setup -- 65_graph_search.md:103), though this is not asserted as a confirmed causal explanation on its own -- two hedged, non-exclusive hypotheses for that apparent contradiction are recorded for a human reader to weigh: (1) the alias loader's own node-lookup genuinely disagrees with GET /graph's node-lookup for the identical collection, or (2) an ordering/race -- the alias loader ran its lookup before these nodes were persisted, and GET /graph is simply reflecting a later state (this scenario cannot distinguish the two from the outside; doing so would require internal timing visibility this black-box suite does not have). Observed nodes[].entity_type={'Continuous': 'person', 'K8s': 'person', 'Kubernetes': 'system', 'Docker': 'person'} -- a type mismatch between the two entities is a further, secondary hypothesis for why the lookup might fail (the doc restricts the *automatic* detector to same-type pairs, 65_graph_search.md:105; whether alias_file shares that restriction is undocumented), not asserted as confirmed on its own. raw edges[]=[]. content-without-graph diagnostic (non-asserting, content_diag=True): Kubernetes content already appears in plain hybrid results for 'K8s' with no graph involved at all.
assert False

### What should happen
- Step 3: HTTP 200; `nodes[].entity_name` contains **both** `"K8s"` and `"Kubernetes"` (line 103's own premise — both are entities before linking); the response body has an `edges` key.
- Step 4 (control): no `edges[]` entry connects "K8s" and "Kubernetes" with no `alias_file` configured, within the same poll window as the alias treatment. (`graph_expansion_applied` is deliberately not asserted for this control — the docs do not state its value for a verbatim-matched entity with zero first-degree neighbours. If an edge DOES appear within the window, that is a confounded control — the automatic detector, `65_graph_search.md:105`, linking the pair without any `alias_file` — and is treated as a setup-phase condition, not a pass/fail outcome for this step.)
- Step 5/6: an `edges[]` entry connects the "K8s" and "Kubernetes" entity IDs (with `relationship_type: "synonym_of"`, the edge-type label `65_graph_search.md:99` lists) once `alias_file` links them — the primary claim. Secondarily, `graph_expansion_applied` is `true` (line 63) for the same query, and `results` includes a result whose `source_path` references the Kubernetes document (line 119, checked as a documented consequence, not independent proof).
- If step 5 never produces a connecting edge within the poll window, `alias_file`-pinned synonym resolution does not behave as `65_graph_search.md:106-111` documents for this pair — a real product/documentation discrepancy, after ruling out a silently-ignored alias_file via the server's own log. The optional content diagnostic is recorded as context only and does not change this verdict either way. If an edge DOES form but `graph_expansion_applied` stays false, that is a distinct, search-side discrepancy (naive mode not traversing a confirmed edge) rather than a linking failure, and must be reported as such, not conflated with the edge-absent case.

### Steps to reproduce
1. Write `aliases.toml` containing `"K8s" = "Kubernetes"` to a path outside the server's own data dir, then boot an isolated server with:
   ```toml
   [graph]
   enabled = true
   alias_file = "/abs/path/to/aliases.toml"
   ```
   Boot a second, control server with only `[graph]\nenabled = true` (no `alias_file`).
2. Ingest the seven-document corpus described in Preconditions into one collection on **both** instances.
3. ```bash
   curl -s http://127.0.0.1:<port>/graph/<collection> \
     -H "Authorization: Bearer <key>"
   ```
   (against either instance — confirms both entities extracted and `edges` key present)
4. Control — poll (up to 30s, the same window as step 5) `GET /graph/<collection>` against the **no-alias** instance: confirm no `edges[]` entry connects the "K8s" and "Kubernetes" entity IDs within that window. (A connecting edge appearing here means the automatic detector linked the pair on its own, 65_graph_search.md:105 — a confounded control run, not evidence for or against `alias_file` either way.)
5. Poll (up to 30s) `GET /graph/<collection>` against the **alias** instance for an `edges[]` entry connecting the "K8s" and "Kubernetes" entity IDs.
6. Once found (or the window elapses), issue a `graph_mode: naive` search for "K8s" against the alias instance:
   ```bash
   curl -s -X POST http://127.0.0.1:<port>/search \
     -H "Authorization: Bearer <key>" \
     -H "Content-Type: application/json" \
     -d '{"collection": "<collection>", "query": "K8s", "graph_mode": "naive"}'
   ```
   If step 5's edge never appeared, optionally also issue a plain hybrid search (no `graph_mode`) for "K8s" against the **alias** instance itself, purely as non-asserting diagnostic context (see "On discriminating by content vs. structure" above) — not a check with its own pass/fail outcome, and independent of whether the separate control instance is even available.

### Evidence
```
E   AssertionError: no edge connecting 'K8s' and 'Kubernetes' entity IDs found in edges[] after alias_file pinned them (65_graph_search.md:106-111) -- the edge is absent (not merely a flag/search-side issue). alias-related log lines: ["2026-08-06T20:49:01Z WARNING archon_search.alias_loader AliasLoader: alias pair ('K8s', 'Kubernetes') — 'K8s' resolved to zero nodes in default/s319_synonym_docs; skipping", "2026-08-06T20:49:01Z WARNING archon_search.alias_loader AliasLoader: alias pair ('K8s', 'Kubernetes') — 'K8s' resolved to zero nodes in default/s319_synonym_docs; skipping", "2026-08-06T20:49:01Z WARNING archon_search.alias_loader AliasLoader: alias pair ('K8s', 'Kubernetes') — 'K8s' resolved to zero nodes in default/s319_synonym_docs; skipping", "2026-08-06T20:49:01Z WARNING archon_search.alias_loader AliasLoader: alias pair ('K8s', 'Kubernetes') — 'Kubernetes' resolved to zero nodes in default/s319_synonym_docs; skipping"] -- if this log shows the alias loader itself reporting 'resolved to zero nodes' for one or both names, that log claim APPEARS to contradict this same GET /graph response's own nodes[] (both names ARE present there, confirmed at fixture setup -- 65_graph_search.md:103), though this is not asserted as a confirmed causal explanation on its own -- two hedged, non-exclusive hypotheses for that apparent contradiction are recorded for a human reader to weigh: (1) the alias loader's own node-lookup genuinely disagrees with GET /graph's node-lookup for the identical collection, or (2) an ordering/race -- the alias loader ran its lookup before these nodes were persisted, and GET /graph is simply reflecting a later state (this scenario cannot distinguish the two from the outside; doing so would require internal timing visibility this black-box suite does not have). Observed nodes[].entity_type={'Continuous': 'person', 'K8s': 'person', 'Kubernetes': 'system', 'Docker': 'person'} -- a type mismatch between the two entities is a further, secondary hypothesis for why the lookup might fail (the doc restricts the *automatic* detector to same-type pairs, 65_graph_search.md:105; whether alias_file shares that restriction is undocumented), not asserted as confirmed on its own. raw edges[]=[]. content-without-graph diagnostic (non-asserting, content_diag=True): Kubernetes content already appears in plain hybrid results for 'K8s' with no graph involved at all.
E   assert False
```
