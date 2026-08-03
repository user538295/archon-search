## Bug: Multilingual balanced non-interactive with both licenses accepted

**ID**: S202-balanced_multilingual_configured
**Scenario**: S202
**Severity**: medium
**Version**: archon-search, version 26.8.1815

### What happened
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
Context leak detected, CoreAnalytics returned false
2026-08-03 11:44:56.998 python[76569:39831104] 2026-08-03 11:44:56.998352 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-03 11:44:56.998 python[76569:39831104] 2026-08-03 11:44:56.998414 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
Context leak detected, CoreAnalytics returned false
2026-08-03 11:44:57.524 python[76569:39831104] 2026-08-03 11:44:57.524159 [E:onnxruntime:, sequential_executor.cc:671 ExecuteKernel] Non-zero status code returned while running 16198209866725014631_CoreML_16198209866725014631_4 node. Name:'CoreMLExecutionProvider_16198209866725014631_CoreML_16198209866725014631_4_4' Status Message: Error executing model: Unable to compute the prediction using a neural network model. It can be an invalid input data or broken/unsupported model (error code: -1).
validate_providers: reranker probe failed: [ONNXRuntimeError] : 1 : FAIL : Non-zero status code returned while running 16198209866725014631_CoreML_16198209866725014631_4 node. Name:'CoreMLExecutionProvider_16198209866725014631_CoreML_16198209866725014631_4_4' Status Message: Error executing model: Unable to compute the prediction using a neural network model. It can be an invalid input data or broken/unsupported model (error code: -1).
2026-08-03 11:44:58.431 python[76569:39831104] 2026-08-03 11:44:58.430883 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 74 number of nodes in the graph: 637 number of nodes supported by CoreML: 402
2026-08-03 11:45:02.519 python[76569:39831104] 2026-08-03 11:45:02.519667 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-03 11:45:02.519 python[76569:39831104] 2026-08-03 11:45:02.519707 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
Context leak detected, CoreAnalytics returned false

assert 1 == 0

### What should happen
- Wizard exits 0.
- `archon-search.toml` contains `profile = "balanced"` and `multilingual = true`.
- Health endpoint returns HTTP 200.

### Steps to reproduce
1. `archon-search wizard --profile balanced --multilingual --accept-jina-license --accept-fasttext-license --non-interactive`
2. `cat ~/.archon-search/archon-search.toml`
3. `curl -s http://127.0.0.1:8765/health`

### Evidence
```
returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     Context leak detected, CoreAnalytics returned false
E     2026-08-03 11:44:56.998 python[76569:39831104] 2026-08-03 11:44:56.998352 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-08-03 11:44:56.998 python[76569:39831104] 2026-08-03 11:44:56.998414 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     Context leak detected, CoreAnalytics returned false
E     2026-08-03 11:44:57.524 python[76569:39831104] 2026-08-03 11:44:57.524159 [E:onnxruntime:, sequential_executor.cc:671 ExecuteKernel] Non-zero status code returned while running 16198209866725014631_CoreML_16198209866725014631_4 node. Name:'CoreMLExecutionProvider_16198209866725014631_CoreML_16198209866725014631_4_4' Status Message: Error executing model: Unable to compute the prediction using a neural network model. It can be an invalid input data or broken/unsupported model (error code: -1).
E     validate_providers: reranker probe failed: [ONNXRuntimeError] : 1 : FAIL : Non-zero status code returned while running 16198209866725014631_CoreML_16198209866725014631_4 node. Name:'CoreMLExecutionProvider_16198209866725014631_CoreML_16198209866725014631_4_4' Status Message: Error executing model: Unable to compute the prediction using a neural network model. It can be an invalid input data or broken/unsupported model (error code: -1).
E     2026-08-03 11:44:58.431 python[76569:39831104] 2026-08-03 11:44:58.430883 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 74 number of nodes in the graph: 637 number of nodes supported by CoreML: 402
E     2026-08-03 11:45:02.519 python[76569:39831104] 2026-08-03 11:45:02.519667 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-08-03 11:45:02.519 python[76569:39831104] 2026-08-03 11:45:02.519707 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     Context leak detected, CoreAnalytics returned false
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=('archon-search', 'wizard', '--config', '/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon...bose output on a non-minimal build will show node assignments.
Context leak detected, CoreAnalytics returned false
").returncode
```
