## Bug: Docs omit the precondition for naive graph expansion: graph_expansion_applied is silently false unless a query n-gram matches an extracted entity NAME

**ID**: S59-naive_expansion_precondition_is_undocumented
**Scenario**: S59
**Severity**: low
**Version**: archon-search, version 26.8.1751

### What happened
On a graph-enabled instance with a populated entity graph, `naive` mode returns `graph_expansion_applied: true` ONLY when the query literally contains an n-gram that matches an extracted entity name. A query with the same meaning but no matching n-gram returns `graph_expansion_applied: false` with the same number of hybrid results and no error, no warning, and nothing in the response explaining why expansion did not fire.

Observed on one corpus, same collection, same mode, four queries:
  'how do we roll out a release'         -> true   ('release' matches entity 'Release')
  'how do we deploy our software'        -> false  (same intent, no entity n-gram)
  'what does the platform team do'       -> true   (matches entity 'The Platform Team')
  'who looks after the deployment tool'  -> false  (about 'Release', no entity n-gram)

The precondition is entirely undocumented, so a user who follows the doc's own worked example against their own corpus sees `false` and has no way to tell whether the feature is broken, the graph is empty, or their phrasing simply missed. Cost us several hours of fixture work to establish empirically.

### What should happen
docs/UserManual/65_graph_search.md:54-63 presents a worked example (`{"query": "how do we roll out a release", "graph_mode": "naive"}`) and states at :63 only that 'The response carries `graph_expansion_applied: true` when expansion fired. Nothing needs to be pre-built beyond the graph itself.' That last sentence is the whole stated precondition, and it is incomplete: the graph existing is necessary but not sufficient — expansion additionally requires the query text to overlap an entity NAME.

The docs should state the precondition explicitly: that naive expansion is triggered by lexical overlap between the query and extracted entity names (NOT by semantic relevance), that entity names come from spaCy proper-noun NER, and that a non-matching query yields `graph_expansion_applied: false` with plain hybrid results and no error. A one-line note next to the worked example would remove the entire ambiguity.

### Steps to reproduce
1. Start a graph-enabled instance ([graph] enabled = true) and ingest a corpus containing proper nouns.
2. Inspect the extracted entity names: curl -s -H "Authorization: Bearer $KEY" $BASE/graph/archon_test_docs  (read the `nodes[].entity_name` values).
3. Search with a query that contains one of those names verbatim:
   curl -s -X POST $BASE/search -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"collection":"archon_test_docs","query":"how do we roll out a release","graph_mode":"naive"}'
4. Repeat with a semantically equivalent query that contains none of them, e.g. "how do we deploy our software".
5. Compare `graph_expansion_applied` in the two responses.

### Evidence
```
Probed 2026-08-01 against a fresh isolated graph-enabled instance.

Extracted entity names (GET /graph/archon_test_docs):
['Acme Corp', 'Acme Corp.
Billing Service', 'Alice Nguyen', 'Auth Service', 'Billing Service',
 'Bob Martinez', 'Carol Smith', 'Grafana', 'Kubernetes', 'Kubernetes for Billing Service',
 'Redis', 'Release', 'Staging to Production on Kubernetes', 'Stripe', 'The Platform Team',
 'Token Cache']

POST /search, graph_mode=naive, identical collection:
HTTP 200  graph_expansion_applied=True   n_results=4  query='how do we roll out a release'
HTTP 200  graph_expansion_applied=False  n_results=4  query='how do we deploy our software'
HTTP 200  graph_expansion_applied=True   n_results=4  query='what does the platform team do'
HTTP 200  graph_expansion_applied=False  n_results=4  query='who looks after the deployment tool'

Note on the exact matching rule: it is NOT plain substring matching. In an earlier corpus a markdown
heading produced the entity name 'Release
Release'; the query 'how do we roll out a release' returned
false even though 'release' is a substring of that name. Nor is it strict whole-name equality: the query
n-gram 'platform team' fired against the entity 'The Platform Team'. Some token-level normalisation is
involved. The precise rule is not determinable black-box — which is itself the point of this report: the
docs need to state it, because no amount of probing pins it down.

This is filed as a DOCUMENTATION defect, not a product bug. The feature behaves consistently; only the
stated precondition is missing. Distinguishing the two is what kept S59 from being filed as a broken
feature — the test passes with its assertion unchanged once the fixture corpus satisfies the (undocumented)
precondition.
```
