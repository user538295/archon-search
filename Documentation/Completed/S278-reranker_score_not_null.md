## Bug: Balanced profile server starts and search returns results with reranker scores

**ID**: S278-reranker_score_not_null
**Scenario**: S278
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: result[0] has null reranker_score — MiniLM-L-12-v2 reranker may not have run; result={'doc_id': '[REDACTED]', 'chunk_id': '[REDACTED]-000000', 'text': '# balanced_test
assert None is not None

### What should happen
- GET /health → HTTP 200.
- GET /ready → HTTP 200.
- POST /search → HTTP 200 with a non-empty `results` array.
- Every object in `results` has a `reranker_score` field that is not null (proves `Xenova/ms-marco-MiniLM-L-12-v2` ran).

### Steps to reproduce
1. ```bash
   curl -s $BALANCED_BASE_URL/health
   ```
2. ```bash
   curl -s $BALANCED_BASE_URL/ready
   ```
3. ```bash
   curl -s -X POST $BALANCED_BASE_URL/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"collection":"balanced_test","query":"fox"}'
   ```

### Evidence
```
E   AssertionError: result[0] has null reranker_score — MiniLM-L-12-v2 reranker may not have run; result={'doc_id': '[REDACTED]', 'chunk_id': '[REDACTED]-000000', 'text': '# balanced_test
The quick brown fox jumps over the lazy dog.
', 'score': 0.22927944362163544, 'source_path': '/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-kxipctsh/seed-balanced_test/doc.md', 'file_type': 'md', 'language': '', 'indexed_at': '2026-08-06T20:27:12.835753Z', 'updated_at': '2026-08-06T20:27:12.812374+00:00', 'ingested_by': 'http', 'metadata': {}, 'acl': None, 'collection': 'balanced_test', 'acl_gate': None}
E   assert None is not None
E    +  where None = <built-in method get of dict object at 0x10a60d180>('reranker_score')
E    +    where <built-in method get of dict object at 0x10a60d180> = {'acl': None, 'acl_gate': None, 'chunk_id': '[REDACTED]-000000', 'collection': 'balanced_test', ...}.get
```
