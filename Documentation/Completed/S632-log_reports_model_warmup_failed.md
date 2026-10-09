## Bug: Invalid embedding_model: warm-up logs 'model warm-up complete', never 'model warm-up failed: ...'

**ID**: S632-log_reports_model_warmup_failed
**Scenario**: S632
**Severity**: low
**Version**: archon-search, version 26.10.2150

### What happened
With [database].embedding_model = "nonexistent-org/no-such-model-s632", /ready reaches 200 ready:true with checks.models "fail", but the server log never carries 'model warm-up failed: ...'. The embedder build fails ('warm-up: embedder failed; first search will pay the init cost', 'EmbedderCache.preload: failed to warm up ...; evicting from cache'), yet the warm-up ends with INFO 'model warm-up complete (1 embedding models + reranker)'. /status readiness.embedder_warm stays false.

### What should happen
- Step 1: the server starts (`/health` `200`) — a failed warm-up is non-fatal (80:123).
- Step 2: `/ready` reaches HTTP `200`, `ready: true`, with `checks.models == "fail"` (20:75, 20:113, 80:123).
- Step 3: the log contains `model warm-up failed: ` (20:113, 80:123, 90:116).

### Steps to reproduce
1. Write the isolated `archon-search.toml` with the invalid `embedding_model` and start `archon-search serve` against it; wait for `GET /health` → `200`.
2. Poll `curl http://127.0.0.1:$PORT/ready` until `ready: true` (deadline under the 600 s warm-up ceiling).
3. Read the server log (`serve.log` + `<data-dir>/logs/archon-search.log`).

### Evidence
```
serve.log excerpt:
WARNING archon_search.pipeline warm-up: embedder failed; first search will pay the init cost
ValueError: Model nonexistent-org/no-such-model-s632 is not supported in TextEmbedding.
WARNING archon_search.embedder_cache EmbedderCache.preload: failed to warm up 'nonexistent-org/no-such-model-s632'; evicting from cache
INFO archon_search.server.app model warm-up complete (1 embedding models + reranker)
GET /ready -> 200 {"ready": true, "checks": {"storage": "ok", "models": "fail", "sync": "ok"}}
grep 'model warm-up failed: ' serve.log + logs/archon-search.log -> no match
```
