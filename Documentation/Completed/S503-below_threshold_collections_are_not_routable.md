## Bug: Collections below `[routing] routing_confidence_threshold` are excluded from `POST /route`

**ID**: S503-below_threshold_collections_are_not_routable
**Scenario**: S503
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: POST /route returned routable_names=['s503_a', 's503_b'] on an instance where every collection scores below the configured routing_confidence_threshold=1.0 (the server's own /explain says chosen_below_threshold=True with candidates [{'collection': 's503_b', 'centroid_score': 0.5948991851837552}, {'collection': 's503_a', 'centroid_score': 0.5901629179526872}]). OperatorGuide/80_capacity_and_performance.md:126 states 'Below this, `MultiCollectionRouter.rank` returns `[]`' and UserManual/30_configuration.md:90 calls it the 'Minimum centroid confidence to dispatch to a collection'; 60_searching.md:96 makes routable_names the set /search is called for. pinned_names=[], so the documented pinned fallback does not apply. body={'pre_context': None, 'pinned_names': [], 'routable_names': ['s503_a', 's503_b'], 'decomposer_invoked': False}
assert ['s503_a', 's503_b'] == []

Left contains 2 more items, first extra item: 's503_a'
Use -v to get more diff

### What should happen
- Step 3: `routing.confidence_threshold` is **`1.0`** — the configured value reached the server
  (`80_explain_and_debugging.md:116`), so any step-4 result is attributable to the setting rather
  than to an ignored key.
- Step 3: every entry of `routing.candidates[]` has a `centroid_score` **below `1.0`**, and
  `routing.chosen_below_threshold` is **`true`** — the server's own statement that even the
  best-scoring collection is below the confidence threshold
  (`80_explain_and_debugging.md:116,117`). This is the precondition of the assertion below.
- Step 4: `POST /route` returns `200` with **`routable_names == []`** —
  `OperatorGuide/80_capacity_and_performance.md:126`, "Below this, `MultiCollectionRouter.rank`
  returns `[]`", and `UserManual/30_configuration.md:90`, "Minimum centroid confidence to dispatch
  to a collection". `pinned_names` is also `[]`, so `:126`'s pinned fallback does not apply.
- Step 5 (control, `routing_confidence_threshold = 0.0`): `routing.chosen_below_threshold` is
  `false` and `POST /route` returns **non-empty** `routable_names`. A permissive threshold routes,
  which is what makes the empty list at `1.0` a real gate rather than a router that never returns
  anything.

### Steps to reproduce
1. Start an isolated instance whose config carries:
   ```toml
   [routing]
   routing_confidence_threshold = 1.0
   ```
   (`1.0` is the top of the documented `[0.0, 1.0]` domain, `30_configuration.md:90`.)
2. Ingest two same-topic collections `s503_a` and `s503_b`, waiting for each job to reach `DONE`.
3. `curl -s -X POST "$BASE/explain" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"query":"how does the router work?","top_k":3}' | jq .routing`
4. `curl -s -X POST "$BASE/route" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"query":"how does the router work?"}' | jq .`
5. Start a second isolated instance with `routing_confidence_threshold = 0.0` and repeat steps 2–4.

### Evidence
```
E   AssertionError: POST /route returned routable_names=['s503_a', 's503_b'] on an instance where every collection scores below the configured routing_confidence_threshold=1.0 (the server's own /explain says chosen_below_threshold=True with candidates [{'collection': 's503_b', 'centroid_score': 0.5948991851837552}, {'collection': 's503_a', 'centroid_score': 0.5901629179526872}]). OperatorGuide/80_capacity_and_performance.md:126 states 'Below this, `MultiCollectionRouter.rank` returns `[]`' and UserManual/30_configuration.md:90 calls it the 'Minimum centroid confidence to dispatch to a collection'; 60_searching.md:96 makes routable_names the set /search is called for. pinned_names=[], so the documented pinned fallback does not apply. body={'pre_context': None, 'pinned_names': [], 'routable_names': ['s503_a', 's503_b'], 'decomposer_invoked': False}
E   assert ['s503_a', 's503_b'] == []
E     
E     Left contains 2 more items, first extra item: 's503_a'
E     Use -v to get more diff
```
