## Bug: `PATCH /collections/{name}` embedding-model state machine: the three documented branches

**ID**: S327-balanced_profile_model_also_sets_pending
**Scenario**: S327
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: PATCH to the documented balanced-profile model BAAI/bge-base-en-v1.5 was rejected 422: {'detail': 'model dimension mismatch: current vectors are 384-dim, new model produces 768-dim; delete and recreate collection to change dimensions'}. UserManual/50_ingestion_and_collections.md:200 documents this case (indexed data + different model) as setting pending_embedding_model with needs_reindex = true, committed later via POST /collections/{name}/reindex (:203); the only documented 422 on this surface is an unknown model name at creation (:189).
assert 422 != 422

### What should happen
- **Branch "indexed data + different model" (step 4, :200)** — `pending_embedding_model` becomes `sentence-transformers/all-MiniLM-L6-v2`, `needs_reindex` becomes `true`, and `active_embedding_model` is **unchanged** ("the current index keeps serving until you reindex"). Fields read from `GET /collections/{name}` (:203).
- **Branch "same model as active" (step 5, :199)** — patching back to the value currently in `active_embedding_model` clears the pending change: `pending_embedding_model` is null/absent and `needs_reindex` is `false`. This step deliberately follows step 4 so there is a pending change to clear.
- **Branch "no indexed data yet" (step 6, :201)** — on the collection whose `chunk_count` is 0, `active_embedding_model` is updated **directly** to the new model, with `pending_embedding_model` null/absent and `needs_reindex` `false`.
- **No reindex is triggered by any branch** (:199, :201 "no reindex"; :200 "keeps serving until you reindex") — `reindex_job_id` (:203) stays null after each PATCH.
- **Step 7 (:200 again, with a different documented model)** — `BAAI/bge-base-en-v1.5` on the same collection with indexed data is also "indexed data + different model", so :200 applies verbatim: `pending_embedding_model` is set and `needs_reindex` becomes `true`. It is a documented, shipped profile model (`UserManual/10_installation.md:106`), so the `422` that :189 reserves for an unknown model name at creation cannot apply to it. No `422` for this case appears anywhere in `./docs/` — grepping the shipped docs for "dimension" returns **no hits at all**, so no documented exception qualifies :200.

### Steps to reproduce
1. `mkdir -p /tmp/archon_s327_docs && printf '# S327\nThe quick brown fox jumps over the lazy dog.\n' > /tmp/archon_s327_docs/doc.md && mkdir -p /tmp/archon_s327_empty`
2. `curl -s -X POST -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"path":"/tmp/archon_s327_docs"}' http://127.0.0.1:8765/collections/`
3. `curl -s -X POST -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"path":"/tmp/archon_s327_empty"}' http://127.0.0.1:8765/collections/`
4. `curl -s -X PATCH -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"embedding_model":"sentence-transformers/all-MiniLM-L6-v2"}' http://127.0.0.1:8765/collections/archon_s327_docs`
5. `curl -s -X PATCH -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"embedding_model":"BAAI/bge-small-en-v1.5"}' http://127.0.0.1:8765/collections/archon_s327_docs`
6. `curl -s -X PATCH -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"embedding_model":"sentence-transformers/all-MiniLM-L6-v2"}' http://127.0.0.1:8765/collections/archon_s327_empty`
7. `curl -s -X PATCH -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"embedding_model":"BAAI/bge-base-en-v1.5"}' http://127.0.0.1:8765/collections/archon_s327_docs`
8. `archon-search collection remove archon_s327_docs && archon-search collection remove archon_s327_empty`

### Evidence
```
E   AssertionError: PATCH to the documented balanced-profile model BAAI/bge-base-en-v1.5 was rejected 422: {'detail': 'model dimension mismatch: current vectors are 384-dim, new model produces 768-dim; delete and recreate collection to change dimensions'}. UserManual/50_ingestion_and_collections.md:200 documents this case (indexed data + different model) as setting pending_embedding_model with needs_reindex = true, committed later via POST /collections/{name}/reindex (:203); the only documented 422 on this surface is an unknown model name at creation (:189).
E   assert 422 != 422
```
