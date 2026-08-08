## Bug: POST /ingest ignores bearer key namespace; all collections created under 'default'

**ID**: S310-namespace_key_collection_not_found
**Scenario**: S310
**Severity**: high
**Version**: archon-search, version 26.8.1916

### What happened
GET /collections with a namespace key returns []. GET /collections with the default key shows ALL collections — including ones ingested via the namespace key — with namespace='default'. POST /search with a namespace key + collection name returns 404 ('collection not found in the caller's namespace') even for collections the namespace key itself just ingested (job status: done).

### What should happen
Per SecurityGuide/03_authorization_and_acl.md: 'a namespaced client cannot ingest into or delete collections owned by another namespace' — implying namespace clients CAN own collections. A namespace key calling POST /ingest should create a collection whose CollectionMeta.namespace matches the caller's namespace (the string from the [namespaces] map), not 'default'. The namespace key should then be able to search that collection by name without receiving 404.

### Steps to reproduce
1. Start archon-search serve with [namespaces] block: "<ns_key>" = "s310-team"
2. POST /ingest with ns_key, body {"path": "<dir-with-docs>", "collection": "ns-test"}; wait for job done
3. GET /collections with ns_key → returns []
4. GET /collections with default key → returns [{"name": "ns-test", "namespace": "default", ...}]
5. POST /search with ns_key, body {"collection": "ns-test", "query": "fox"} → 404 {"detail": "collection not found"}

### Evidence
```
Diagnostic from test run 2026-08-08:
GET /collections (ns_key)  = []
GET /collections (default) = [{'name': 's310-ns', ..., 'namespace': 'default', 'doc_count': 1, 'chunk_count': 1}]
POST /search (ns_key, collection='s310-ns') → 404 {'detail': 'collection not found'}
```
