## Bug: Unavailable ONNX provider: no CPU fallback — both models fail to load, checks.models 'fail' instead of 'warn'

**ID**: S635-models_load_and_check_reads_warn
**Scenario**: S635
**Severity**: medium
**Version**: archon-search, version 26.10.2150

### What happened
With [database].providers = ["CUDAExecutionProvider"] on macOS (onnxruntime has no CUDA provider), /status model_validation reports provider_warnings ['configured ONNX providers not available: CUDAExecutionProvider'] as documented, but embedder_ok=false and reranker_ok=false and /ready reports checks.models 'fail'. The documented WARN outcome (provider fallback, both models loaded, checks.models 'warn') does not happen; warm-up logs 'ValueError: Provider CUDAExecutionProvider is not available. Available providers: [CoreMLExecutionProvider, AzureExecutionProvider, CPUExecutionProvider]' for both embedder and reranker. Same result with providers = ["CUDAExecutionProvider", "CPUExecutionProvider"].

### What should happen
- Step 2: `/ready` reaches HTTP `200`, `ready: true` (20:54, 20:75).
- Step 3: `model_validation.provider_warnings` contains an entry beginning `configured ONNX providers not available: ` that names `CUDAExecutionProvider` (20:76, 90:117).
- Step 3: `model_validation.embedder_ok` and `reranker_ok` are both `true` — a WARN-class warning means "both models loaded" (90:114, 90:117).
- Step 2: the final `/ready` body has `checks.models == "warn"` (20:75 provider fallback; 90:114).

### Steps to reproduce
1. Write the isolated `archon-search.toml` with `providers = ["CUDAExecutionProvider"]` and start `archon-search serve`; wait for `GET /health` → `200`.
2. Poll `curl http://127.0.0.1:$PORT/ready` until `ready: true`.
3. Poll `curl -H "Authorization: Bearer $KEY" http://127.0.0.1:$PORT/status` until `model_validation.validated_at` is non-null.

### Evidence
```
GET /status model_validation = {'embedder_ok': False, 'reranker_ok': False, 'llama_cpp_ok': None, 'provider_warnings': ['configured ONNX providers not available: CUDAExecutionProvider'], 'validated_at': '2026-10-08T13:09:09.501004Z'}
GET /ready -> 200 {'ready': True, 'checks': {'storage': 'ok', 'models': 'fail', 'sync': 'ok'}}
serve.log: WARNING archon_search.pipeline warm-up: embedder failed; first search will pay the init cost
ValueError: Provider CUDAExecutionProvider is not available. Available providers: ['CoreMLExecutionProvider', 'AzureExecutionProvider', 'CPUExecutionProvider']
WARNING archon_search.pipeline warm-up: reranker failed; first search will pay the init cost
```
