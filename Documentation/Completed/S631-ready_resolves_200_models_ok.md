## Bug: Default config (`eager_load_embedders` unset): startup warm-up gates `/ready` (`503`, `checks.models: "pending"`), then `200` with the default embedder and reranker warm

**ID**: S631-ready_resolves_200_models_ok
**Scenario**: S631
**Severity**: medium
**Version**: archon-search, version 26.10.2181

### What happened
AssertionError: expected checks.models 'ok' after a clean default warm-up (20_monitoring_and_alerts.md:75); got 'pending'. Body: {'ready': True, 'checks': {'storage': 'ok', 'models': 'pending', 'sync': 'ok'}}
--- server log tail ---
INFO:     127.0.0.1:59265 - "GET /health HTTP/1.1" 200 OK
INFO:     127.0.0.1:59266 - "GET /ready HTTP/1.1" 503 Service Unavailable
2026-10-12 15:15:08.997 python[6376:6029229] 2026-10-12 15:15:08.996759 [W:onnxruntime:, coreml_execution_provider.cc:141 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
2026-10-12 15:15:08.999 python[6376:6029203] 2026-10-12 15:15:08.998812 [W:onnxruntime:, coreml_execution_provider.cc:141 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
INFO:     127.0.0.1:59267 - "GET /health HTTP/1.1" 200 OK
INFO:     127.0.0.1:59268 - "GET /ready HTTP/1.1" 503 Service Unavailable
INFO:     127.0.0.1:59269 - "GET /health HTTP/1.1" 200 OK
INFO:     127.0.0.1:59270 - "GET /ready HTTP/1.1" 503 Service Unavailable
INFO:     127.0.0.1:59271 - "GET /health HTTP/1.1" 200 OK
INFO:     127.0.0.1:59272 - "GET /ready HTTP/1.1" 503 Service Unavailable
2026-10-12 15:15:09.636 python[6376:6029203] 2026-10-12 15:15:09.635806 [W:onnxruntime:, session_state.cc:1598 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-10-12 15:15:09.636 python[6376:6029203] 2026-10-12 15:15:09.635852 [W:onnxruntime:, session_state.cc:1600 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
2026-10-12 15:15:09.778 python[6376:6029203] 2026-10-12 15:15:09.778499 [W:onnxruntime:, session_state.cc:1598 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-10-12 15:15:09.778 python[6376:6029203] 2026-10-12 15:15:09.778554 [W:onnxruntime:, session_state.cc:1600 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
INFO:     127.0.0.1:59273 - "GET /health HTTP/1.1" 200 OK
INFO:     127.0.0.1:59274 - "GET /ready HTTP/1.1" 200 OK
INFO:     127.0.0.1:59275 - "GET /status HTTP/1.1" 200 OK
2026-10-12T13:14:43Z INFO archon_search.server.app API key authentication enabled (source: env var)
2026-10-12T13:14:45Z INFO archon_search.server.app MCP HTTP endpoint mounted at /mcp
2026-10-12T13:15:09Z INFO archon_search.server.warmup model warm-up complete (1 embedding model + reranker)
assert 'pending' == 'ok'

- ok

### What should happen
- Step 2: at least one `/ready` sample is HTTP `503` with `ready: false`, `checks.storage == "ok"` and `checks.models == "pending"` (20:49, 30:72, 80:123), and `/health` already answers `200` at that moment (80:123 "does not delay the port binding"; 20:14).
- Step 2: `/ready` then reaches HTTP `200` with `ready: true` and `checks.models == "ok"` (80:123 "`200` after"; 20:75 — both models loaded cleanly).
- Step 3: `readiness.embedder_warm` and `readiness.reranker_warm` are both `true` although no search was issued (50:240; 30:72; 20:66 side-effect-free read).
- Step 4: the log contains `model warm-up complete (N embedding models + reranker)` (80:123).

### Steps to reproduce
1. Write an isolated `archon-search.toml` (minimal-profile models, no `eager_load_embedders` key, no collections) and start `archon-search serve` with `ARCHON_SEARCH_DATA_DIR`/`ARCHON_SEARCH_CONFIG`/`ARCHON_SEARCH_PORT` pointing at it.
2. From launch, poll `GET /health` and `GET /ready` every 0.2 s; record each `/ready` `(status, body)` until `ready: true` (deadline under the documented 600 s warm-up ceiling).
3. Without issuing any search: `curl -H "Authorization: Bearer $KEY" http://127.0.0.1:$PORT/status`.
4. Read the server log (`serve.log` + `<data-dir>/logs/archon-search.log`).

### Evidence
```
E   AssertionError: expected checks.models 'ok' after a clean default warm-up (20_monitoring_and_alerts.md:75); got 'pending'. Body: {'ready': True, 'checks': {'storage': 'ok', 'models': 'pending', 'sync': 'ok'}}
E     --- server log tail ---
E     INFO:     127.0.0.1:59265 - "GET /health HTTP/1.1" 200 OK
E     INFO:     127.0.0.1:59266 - "GET /ready HTTP/1.1" 503 Service Unavailable
E     2026-10-12 15:15:08.997 python[6376:6029229] 2026-10-12 15:15:08.996759 [W:onnxruntime:, coreml_execution_provider.cc:141 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
E     2026-10-12 15:15:08.999 python[6376:6029203] 2026-10-12 15:15:08.998812 [W:onnxruntime:, coreml_execution_provider.cc:141 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
E     INFO:     127.0.0.1:59267 - "GET /health HTTP/1.1" 200 OK
E     INFO:     127.0.0.1:59268 - "GET /ready HTTP/1.1" 503 Service Unavailable
E     INFO:     127.0.0.1:59269 - "GET /health HTTP/1.1" 200 OK
E     INFO:     127.0.0.1:59270 - "GET /ready HTTP/1.1" 503 Service Unavailable
E     INFO:     127.0.0.1:59271 - "GET /health HTTP/1.1" 200 OK
E     INFO:     127.0.0.1:59272 - "GET /ready HTTP/1.1" 503 Service Unavailable
E     2026-10-12 15:15:09.636 python[6376:6029203] 2026-10-12 15:15:09.635806 [W:onnxruntime:, session_state.cc:1598 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-10-12 15:15:09.636 python[6376:6029203] 2026-10-12 15:15:09.635852 [W:onnxruntime:, session_state.cc:1600 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     2026-10-12 15:15:09.778 python[6376:6029203] 2026-10-12 15:15:09.778499 [W:onnxruntime:, session_state.cc:1598 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-10-12 15:15:09.778 python[6376:6029203] 2026-10-12 15:15:09.778554 [W:onnxruntime:, session_state.cc:1600 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     INFO:     127.0.0.1:59273 - "GET /health HTTP/1.1" 200 OK
E     INFO:     127.0.0.1:59274 - "GET /ready HTTP/1.1" 200 OK
E     INFO:     127.0.0.1:59275 - "GET /status HTTP/1.1" 200 OK
E     2026-10-12T13:14:43Z INFO archon_search.server.app API key authentication enabled (source: env var)
E     2026-10-12T13:14:45Z INFO archon_search.server.app MCP HTTP endpoint mounted at /mcp
E     2026-10-12T13:15:09Z INFO archon_search.server.warmup model warm-up complete (1 embedding model + reranker)
E   assert 'pending' == 'ok'
E     
E     - ok
E     + pending
```
