## Bug: A model name outside the supported registry (`embedding_model` / `reranker_model`) fails the startup model checks: the server stays up, `/ready` answers `503` with `checks.models: "fail"`, and the log carries `unsupported model '<name>'; supported: …`

**ID**: S750-unsupported_embedding_model_keeps_server_up_with_models_fail
**Scenario**: S750
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S750-unsupported_embedding_model_keeps_server_up_with_models_fail.md — with embedding_model='nonexistent-org/no-such-model-s750' the server exited with code 1 during startup instead of staying up; UserManual/30_configuration.md:64-65 — an unsupported name 'fails the startup model checks (checks.models: "fail")', and 20_monitoring_and_alerts.md:51 — it 'keeps /ready at 503'. Log tail:
File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_specs.py", line 353, in get_embedder_spec
return _lookup(EMBEDDER_SPECS, name)
File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_specs.py", line 348, in _lookup
raise UnsupportedModelError(name, tuple(sorted(registry))) from None
archon_search.model_specs.UnsupportedModelError: unsupported model 'nonexistent-org/no-such-model-s750'; supported: BAAI/bge-base-en, BAAI/bge-base-en-v1.5, BAAI/bge-large-en-v1.5, BAAI/bge-small-en, BAAI/bge-small-en-v1.5, BAAI/bge-small-zh-v1.5, Qwen/Qwen3-Embedding-0.6B, ibm-granite/granite-embedding-small-english-r2, intfloat/multilingual-e5-large, mixedbread-ai/mxbai-embed-large-v1, sentence-transformers/all-MiniLM-L6-v2, sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2, sentence-transformers/paraphrase-multilingual-mpnet-base-v2, snowflake/snowflake-arctic-embed-l, snowflake/snowflake-arctic-embed-m, snowflake/snowflake-arctic-embed-s, snowflake/snowflake-arctic-embed-xs, thenlper/gte-base, thenlper/gte-large
2026-10-27T22:49:30Z INFO archon_search.server.app API key authentication enabled (source: env var)

### What should happen
- Step 1: the process keeps running and `/health` answers `200` (the unsupported name is a model-check failure, not a startup abort — 30:`embedding_model` row, 20:line 51).
- Step 2: `/ready` → HTTP `503`, `ready: false`, `checks.models == "fail"` (30:`embedding_model` row; 20:line 51).
- Step 3: the output contains `unsupported model '<name>'; supported: ` naming the configured value (160:fail cause 1; 90:Triage; 30:`reranker_model` row).

### Steps to reproduce
1. Start each instance with `archon-search serve`; wait up to 60 s for `GET /health` → `200` or for the process to exit.
2. Poll `curl -s -w "\n%{http_code}" http://127.0.0.1:$PORT/ready` until `checks.models` is no longer `pending`.
3. Read the server output (`serve.log` + `<data-dir>/logs/archon-search.log`).

### Evidence
```
E   Failed: bug: bugs/S750-unsupported_embedding_model_keeps_server_up_with_models_fail.md — with embedding_model='nonexistent-org/no-such-model-s750' the server exited with code 1 during startup instead of staying up; UserManual/30_configuration.md:64-65 — an unsupported name 'fails the startup model checks (checks.models: "fail")', and 20_monitoring_and_alerts.md:51 — it 'keeps /ready at 503'. Log tail:
E     File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_specs.py", line 353, in get_embedder_spec
E       return _lookup(EMBEDDER_SPECS, name)
E     File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_specs.py", line 348, in _lookup
E       raise UnsupportedModelError(name, tuple(sorted(registry))) from None
E   archon_search.model_specs.UnsupportedModelError: unsupported model 'nonexistent-org/no-such-model-s750'; supported: BAAI/bge-base-en, BAAI/bge-base-en-v1.5, BAAI/bge-large-en-v1.5, BAAI/bge-small-en, BAAI/bge-small-en-v1.5, BAAI/bge-small-zh-v1.5, Qwen/Qwen3-Embedding-0.6B, ibm-granite/granite-embedding-small-english-r2, intfloat/multilingual-e5-large, mixedbread-ai/mxbai-embed-large-v1, sentence-transformers/all-MiniLM-L6-v2, sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2, sentence-transformers/paraphrase-multilingual-mpnet-base-v2, snowflake/snowflake-arctic-embed-l, snowflake/snowflake-arctic-embed-m, snowflake/snowflake-arctic-embed-s, snowflake/snowflake-arctic-embed-xs, thenlper/gte-base, thenlper/gte-large
E   2026-10-27T22:49:30Z INFO archon_search.server.app API key authentication enabled (source: env var)
```
