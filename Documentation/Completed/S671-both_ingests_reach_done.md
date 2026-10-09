## Bug: max + multilingual install: every collection add ingest FAILS with ONNXRuntime 'External data path escapes model directory'

**ID**: S671-both_ingests_reach_done
**Scenario**: S671
**Severity**: high
**Version**: archon-search, version 26.10.2150

### What happened
On a fresh 'wizard --profile max --multilingual --accept-jina-license --accept-fasttext-license --non-interactive' install (archon-search 26.10.2150, models pre-downloaded by the wizard), /ready returned 200, both 'collection add' calls returned 202 with job ids, but both ingest jobs went FAILED within ~20 ms of creation. 'collection list' then shows both collections with docs=0 chunks=0. The job error is an onnxruntime failure loading the multilingual-e5-large embedder: its external-data file 'model.onnx_data' resolves into a different HF cache blob directory than the model, which onnxruntime rejects. Ingestion is impossible on this documented profile; /ready reported the instance ready although the configured embedder cannot be loaded.

### What should happen
- Step 1: exits `0`; `$T/archon-search.toml` has `[database] profile = "max"`, `multilingual = true`, an `embedding_model` naming `multilingual-e5-large`, and `reranker_model = "jinaai/jina-reranker-v2-base-multilingual"` (`20_wizard.md:722-725`, `10_installation.md:110`, `20_wizard.md:104`).
- Step 2: `/ready` returns `200` (`40:53`).
- Steps 3-4: each exits `0` and prints a job ID (`50:119`).
- Step 5: exits `0`; both collections appear as `<name>  docs=<n>  chunks=<n>` lines (`50:113`).
- Step 6: each prints its `job_id`, `status` and `collection` (`100:92`); the exit code is `0` for `DONE`/`PENDING`/`QUEUED`/`RUNNING`/`CANCELLING` and `1` for `FAILED`/`FAILED_EXPIRED`/`CANCELLED` (`100:98`); a `FAILED` job prints its `error` (`100:92`).
- Step 7: each exits `0` — both collections are registered (`50:119`, `50:145`).
- Step 8: both jobs end in `DONE` (`100:55`). Any job that ends `FAILED` carries a non-empty `error` that explains why (`100:56`), and `jobs status` prints it (`100:92`).
- Step 9: both collections show `docs=<n>` with `n > 0` — `docs=0` is the failed-ingest signal (`50:115`).

### Steps to reproduce
1. `archon-search wizard --config $T/archon-search.toml --db-path $T/data/search --profile max --multilingual --accept-jina-license --accept-fasttext-license --non-interactive`
2. `archon-search stop && ARCHON_SEARCH_PORT=$PORT ARCHON_SEARCH_HOST=127.0.0.1 archon-search serve` (background), then poll `curl -s -o /dev/null -w '%{http_code}' $URL/ready` until `200`
3. `archon-search collection add $T/folders/s671_handbook --api-url $URL --api-key $KEY`
4. `archon-search collection add $T/folders/s671_research --api-url $URL --api-key $KEY`
5. `archon-search collection list --api-url $URL --api-key $KEY`
6. `archon-search jobs status <job_id from step 3> --api-url $URL --api-key $KEY` and the same for the step-4 job
7. `archon-search collection info <collection of step 3> --api-url $URL --api-key $KEY` and the same for the step-4 collection
8. Repeat step 6 (and `curl -s -H "Authorization: Bearer $KEY" $URL/jobs/<job_id>`) until both jobs are in a terminal state
9. `archon-search collection list --api-url $URL --api-key $KEY`

### Evidence
```
jobs status 51814e3e-ac50-4f9e-acde-17653bbb7da4 (exit 1):
status:     FAILED
collection: s671_handbook
error:      [ONNXRuntimeError] : 1 : FAIL : External data path validation failed for initializer: embeddings.word_embeddings.weight. Error: External data path escapes model directory. External data path: "model.onnx_data" resolved path: "<data-dir>/models/blobs/9e/9eac14dfffedec847a69f5f1c9ab5403973169a1be627aaa831bfcdfc294baf7" allowed directory: "<data-dir>/models/blobs/29"
(second job eba541d9-84a0-45f6-b99b-04b443f9d3fe, collection s671_research: identical error)
GET /jobs/<id>: created_at 11:32:56.700118, updated_at 11:32:56.717268, result null
collection list after both jobs terminal:
s671_handbook  docs=0  chunks=0
s671_research  docs=0  chunks=0
serve.log: GET /ready 503 then 200 before the adds; POST /collections/ 202 x2
```
