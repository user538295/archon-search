## Bug: A model name outside the supported registry (`embedding_model` / `reranker_model`) fails the startup model checks: the server stays up, `/ready` answers `503` with `checks.models: "fail"`, and the log carries `unsupported model '<name>'; supported: …`

**ID**: S750-unsupported_reranker_model_keeps_server_up_with_models_fail
**Scenario**: S750
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S750-unsupported_reranker_model_keeps_server_up_with_models_fail.md — with reranker_model='nonexistent-org/no-such-reranker-s750' the server exited with code 1 during startup instead of staying up; UserManual/30_configuration.md:64-65 — an unsupported name 'fails the startup model checks (checks.models: "fail")', and 20_monitoring_and_alerts.md:51 — it 'keeps /ready at 503'. Log tail:
File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_specs.py", line 358, in get_reranker_spec
return _lookup(RERANKER_SPECS, name)
File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_specs.py", line 348, in _lookup
raise UnsupportedModelError(name, tuple(sorted(registry))) from None
archon_search.model_specs.UnsupportedModelError: unsupported model 'nonexistent-org/no-such-reranker-s750'; supported: BAAI/bge-reranker-base, BAAI/bge-reranker-v2-m3, Xenova/ms-marco-MiniLM-L-12-v2, Xenova/ms-marco-MiniLM-L-6-v2, jinaai/jina-reranker-v2-base-multilingual
2026-10-27T22:49:31Z INFO archon_search.server.app API key authentication enabled (source: env var)

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
E   Failed: bug: bugs/S750-unsupported_reranker_model_keeps_server_up_with_models_fail.md — with reranker_model='nonexistent-org/no-such-reranker-s750' the server exited with code 1 during startup instead of staying up; UserManual/30_configuration.md:64-65 — an unsupported name 'fails the startup model checks (checks.models: "fail")', and 20_monitoring_and_alerts.md:51 — it 'keeps /ready at 503'. Log tail:
E     File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_specs.py", line 358, in get_reranker_spec
E       return _lookup(RERANKER_SPECS, name)
E     File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_specs.py", line 348, in _lookup
E       raise UnsupportedModelError(name, tuple(sorted(registry))) from None
E   archon_search.model_specs.UnsupportedModelError: unsupported model 'nonexistent-org/no-such-reranker-s750'; supported: BAAI/bge-reranker-base, BAAI/bge-reranker-v2-m3, Xenova/ms-marco-MiniLM-L-12-v2, Xenova/ms-marco-MiniLM-L-6-v2, jinaai/jina-reranker-v2-base-multilingual
E   2026-10-27T22:49:31Z INFO archon_search.server.app API key authentication enabled (source: env var)
```
