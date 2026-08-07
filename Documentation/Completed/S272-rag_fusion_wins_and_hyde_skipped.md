## Bug: When both hyde=true and rag_fusion=true, RAG Fusion executes and hyde_applied is false

**ID**: S272-rag_fusion_wins_and_hyde_skipped
**Scenario**: S272
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: rag_fusion_attempted must be true; got: False
assert False is True

### What should happen
- HTTP 200 (if the `rag_fusion` provider package is installed) or HTTP 422 (if neither provider package is installed).
- On 200: `hyde_applied` is `false` — RAG Fusion executes and HyDE is skipped. `rag_fusion_attempted` is `true`.
- On 422: response body is valid JSON (not a 5xx).
- This is NOT a 422 due to the combination itself — the server silently skips HyDE rather than rejecting the request.

### Steps to reproduce
1. ```bash
   curl -s -X POST http://127.0.0.1:8765/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"collection":"archon_test_docs","query":"fox","hyde":true,"rag_fusion":true}'
   ```

### Evidence
```
E   AssertionError: rag_fusion_attempted must be true; got: False
E   assert False is True
E    +  where False = <built-in method get of dict object at 0x10a4cf680>('rag_fusion_attempted')
E    +    where <built-in method get of dict object at 0x10a4cf680> = {'acl_filtered': False, 'applied_filters': None, 'embedding_model': 'BAAI/bge-small-en-v1.5', 'excluded_collections': [], ...}.get
```
