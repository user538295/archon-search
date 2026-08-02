## Bug: graph_mode=local before community build returns 200 with results instead of the documented 422 graph_communities_not_built

**ID**: S60-local_mode_before_community_build_is_unguarded
**Scenario**: S60
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
On a graph-enabled instance whose communities have NEVER been built, POST /search with the SAME query returns:
  - graph_mode=global -> HTTP 422, {"detail": {"code": "graph_communities_not_built", ...}}  (as documented)
  - graph_mode=local  -> HTTP 200 with a populated results array           (NOT as documented)

The not-built guard therefore fires for 'global' only. A caller relying on the documented 422 to detect 'communities are missing' gets unguarded results back from 'local' instead of an error, and has no signal that the community layer it asked for was never consulted.

### What should happen
docs/UserManual/65_graph_search.md:75 states, without qualification: "If you request `local` or `global` before communities exist, the search returns `422` with body {"detail": {"code": "graph_communities_not_built", ...}} (`GraphCommunitiesNotBuiltError`)." The section heading above it (65:64) is "`local` and `global` — need communities first", and OperatorGuide/60_graph_operations.md:42 repeats "`local` and `global` search modes need Leiden communities". So `local` before a build should return 422 with code `graph_communities_not_built`, exactly as `global` does.

Either the guard is missing for `local` (product bug), or `local` genuinely degrades to hybrid results without communities and the docs overstate the guard (documentation bug). Both readings require a change; the docs and the behaviour cannot both stand.

### Steps to reproduce
1. Start a graph-enabled instance with communities NOT built:
   [graph]
   enabled = true
   enrichment_auto = false
   (enrichment_auto = false is required — the post-ingest synonym job otherwise rebuilds communities automatically and the precondition is lost.)
2. Ingest an entity-dense corpus into collection 'archon_test_docs'. Do NOT run 'archon-search graph build-communities'.
3. curl -s -w '
HTTP %{http_code}' -X POST $BASE/search -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"collection":"archon_test_docs","query":"what links our auth and billing systems","graph_mode":"global"}'
4. Same call with "graph_mode":"local".

### Evidence
```
Probed 2026-08-01 against a fresh isolated graph-enabled instance, corpus ingested, communities never built. Identical query in both calls.

=== graph_mode=global -> HTTP 422
{
  "detail": {
    "code": "graph_communities_not_built",
    "message": "No community representatives found for collection 'archon_test_docs'. Run community detection first."
  }
}

=== graph_mode=local -> HTTP 200
{
  "results": [
    {
      "chunk_id": "39bc1015...-000000",
      "text": "Billing Service charges customers through Stripe.
Billing Service reads Token Cache and calls Auth Service...",
      "score": 6.9101362228393555,
      "collection": "archon_test_docs"
    },
    ... (further results)
  ]
}

Doc line: docs/UserManual/65_graph_search.md:75.
Found while implementing S60's fixtures; outside S60's own assertions (S60 asserts the 422 via 'global'), so it is reported separately rather than folded into that test.
```
