## Bug: `embedder_cache_size` exceeded by an eager warm-up: startup logs the eviction warning, warm-up still completes, per-collection models keep working

**ID**: S486-startup_logs_distinct_models_exceed_cache_size
**Scenario**: S486
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S486-startup_logs_distinct_models_exceed_cache_size.md — documented 'eager_load_embedders: N distinct models exceed embedder_cache_size' (80:176) never appears; the log instead has 'eager_load_embedders: 2 per-collection models other than the default exceed embedder_cache_size=1'

### What should happen
- Step 4 (80:176): the restart log contains `eager_load_embedders: N distinct models exceed embedder_cache_size` with `N` greater than the capacity `1`.
- Step 4 (80:176 "Eviction alone does not fail warm-up"; 80:177; 30:77): `/ready` reaches `200`, `ready: true`, with `checks.models` not `"fail"`; the log carries `model warm-up complete (3 embedding models + reranker)` and no `model warm-up failed` line.
- Step 5 (50:224, 50:238): each collection still reports the model it was created with in `active_embedding_model`, and the three collections are on distinct models (setup guard: otherwise the cache was never exceeded).
- Step 5 (80:176, 60:49): every search returns `200` — an evicted model's first search only "pays the load again". The corpus is small and on-topic per query, so a non-empty `results` list is also expected, though the doc only guarantees the status code.
- Step 6 (30:76): `embedder_cache_size = 0` is outside the documented `≥1` domain — the server does not become healthy, exits `1` with an `Error: …` line naming `embedder_cache_size` (`30_configuration.md:13`; `90_incident_runbook.md:216`; the wording is not documented).

### Steps to reproduce
1. Start the isolated instance; wait for `GET /health` → `200`.
2. `POST /collections/ {"path": "<corpus_a>", "embedding_model": "BAAI/bge-small-en-v1.5"}` `POST /collections/ {"path": "<corpus_b>", "embedding_model": "BAAI/bge-base-en-v1.5"}` and `POST /collections/ {"path": "<corpus_c>", "embedding_model": "snowflake/snowflake-arctic-embed-xs"}` — the default `bge-small` is pinned, so B and C are two **non-default** models over a cache of capacity `1`; poll each `GET /jobs/<id>` to `DONE`.
3. Stop the server and start it again on the same data dir and config — the warm-up now covers three distinct models (the pinned default plus two non-default) over a non-default LRU of capacity `1`.
4. Poll `GET /ready` until `ready: true`; read the server log written since the restart (`serve.log` + `<data-dir>/logs/archon-search.log`).
5. `GET /collections/{name}` and `POST /search {"collection": "<name>", "query": "container workloads billing"}` against A, then B, then C, then B, then C.
6. Start a second instance with `embedder_cache_size = 0` → observe.

### Evidence
```
E   Failed: bug: bugs/S486-startup_logs_distinct_models_exceed_cache_size.md — documented 'eager_load_embedders: N distinct models exceed embedder_cache_size' (80:176) never appears; the log instead has 'eager_load_embedders: 2 per-collection models other than the default exceed embedder_cache_size=1'
```
