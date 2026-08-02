## Bug: `POST /collections/` with `embedding_model` field; GET reflects `active_embedding_model`

**ID**: S221-unknown_model_rejected_422
**Scenario**: S221
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: status=500 body=None
assert 500 == 422

### What should happen
- Step 1: HTTP 202; response JSON contains a `job_id` field confirming the collection was accepted.
  (`UserManual/100_jobs_and_async_operations.md:20` — the server "returns `202 Accepted` with the job
  record"; the job record's identifier is `job_id`, `:76`/`:88`/`:158`. The docs never name an `id`
  field on this response.)
- Step 2: HTTP 200; response JSON contains `"active_embedding_model": "BAAI/bge-small-en-v1.5"`.
- Step 3: HTTP 422; the unknown model name is rejected before any job is created.

### Steps to reproduce
Setup:
```bash
mkdir -p /tmp/archon-embed-test
echo "# Embed test" > /tmp/archon-embed-test/doc.md
source ~/.archon-search/.search.env
```

1. ```bash
   curl -s -X POST http://127.0.0.1:8765/collections/ \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"path": "/tmp/archon-embed-test", "embedding_model": "BAAI/bge-small-en-v1.5"}' \
     | jq .
   ```
2. ```bash
   curl -s http://127.0.0.1:8765/collections/archon_embed_test \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     | jq .
   ```
3. ```bash
   curl -s -X POST http://127.0.0.1:8765/collections/ \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"path": "/tmp/archon-embed-test", "embedding_model": "unknown-model-xyz-404"}' \
     | jq .
   ```

### Evidence
```
E   AssertionError: status=500 body=None
E   assert 500 == 422
```
