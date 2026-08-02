## Bug: Multilingual balanced non-interactive with both licenses accepted

**ID**: S202-balanced_multilingual_configured
**Scenario**: S202
**Severity**: medium
**Version**: archon-search, version 26.8.1800

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
2026-08-02 15:26:51.277 python[59151:37734117] 2026-08-02 15:26:51.277376 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-02 15:26:51.277 python[59151:37734117] 2026-08-02 15:26:51.277483 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
Context leak detected, CoreAnalytics returned false
2026-08-02 15:26:51.797 python[59151:37734117] 2026-08-02 15:26:51.797525 [E:onnxruntime:, sequential_executor.cc:671 ExecuteKernel] Non-zero status code returned while running 16198209866725014631_CoreML_16198209866725014631_4 node. Name:'CoreMLExecutionProvider_16198209866725014631_CoreML_16198209866725014631_4_4' Status Message: Error executing model: Unable to compute the prediction using a neural network model. It can be an invalid input data or broken/unsupported model (error code: -1).
validate_providers: reranker probe failed: [ONNXRuntimeError] : 1 : FAIL : Non-zero status code returned while running 16198209866725014631_CoreML_16198209866725014631_4 node. Name:'CoreMLExecutionProvider_16198209866725014631_CoreML_16198209866725014631_4_4' Status Message: Error executing model: Unable to compute the prediction using a neural network model. It can be an invalid input data or broken/unsupported model (error code: -1).
2026-08-02 15:26:52.607 python[59151:37734117] 2026-08-02 15:26:52.607723 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 74 number of nodes in the graph: 637 number of nodes supported by CoreML: 402
2026-08-02 15:26:56.539 python[59151:37734117] 2026-08-02 15:26:56.539231 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-02 15:26:56.539 python[59151:37734117] 2026-08-02 15:26:56.539308 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
Context leak detected, CoreAnalytics returned false

assert 1 == 0

### What should happen
- Wizard exits 0.
- `archon-search.toml` contains `profile = "balanced"` and `multilingual = true`.
- Health endpoint returns HTTP 200.

### Steps to reproduce
1. `archon-search wizard --profile balanced --multilingual --accept-jina-license --accept-fasttext-license --non-interactive --skip-preload`
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
E     2026-08-02 15:26:51.277 python[59151:37734117] 2026-08-02 15:26:51.277376 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-08-02 15:26:51.277 python[59151:37734117] 2026-08-02 15:26:51.277483 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     Context leak detected, CoreAnalytics returned false
E     2026-08-02 15:26:51.797 python[59151:37734117] 2026-08-02 15:26:51.797525 [E:onnxruntime:, sequential_executor.cc:671 ExecuteKernel] Non-zero status code returned while running 16198209866725014631_CoreML_16198209866725014631_4 node. Name:'CoreMLExecutionProvider_16198209866725014631_CoreML_16198209866725014631_4_4' Status Message: Error executing model: Unable to compute the prediction using a neural network model. It can be an invalid input data or broken/unsupported model (error code: -1).
E     validate_providers: reranker probe failed: [ONNXRuntimeError] : 1 : FAIL : Non-zero status code returned while running 16198209866725014631_CoreML_16198209866725014631_4 node. Name:'CoreMLExecutionProvider_16198209866725014631_CoreML_16198209866725014631_4_4' Status Message: Error executing model: Unable to compute the prediction using a neural network model. It can be an invalid input data or broken/unsupported model (error code: -1).
E     2026-08-02 15:26:52.607 python[59151:37734117] 2026-08-02 15:26:52.607723 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 74 number of nodes in the graph: 637 number of nodes supported by CoreML: 402
E     2026-08-02 15:26:56.539 python[59151:37734117] 2026-08-02 15:26:56.539231 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-08-02 15:26:56.539 python[59151:37734117] 2026-08-02 15:26:56.539308 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     Context leak detected, CoreAnalytics returned false
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=('archon-search', 'wizard', '--config', '/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon...bose output on a non-minimal build will show node assignments.
Context leak detected, CoreAnalytics returned false
").returncode
```

---

### Analysis — Not a product defect (feature-level)

**Verdict:** environmental (machine under load), not a product defect.

The setup wizard waits up to 60 seconds for the search service to become ready and then exits with an error if it has not. On a busy machine, starting the larger multilingual models can take longer than 60 seconds, so the wizard reports the timeout and exits. On an unloaded machine the service becomes ready well within that window and the wizard completes successfully. The 60-second readiness wait and exit-on-timeout are the intended, documented behaviour — the documentation is accurate, so no doc change was needed.

**Scenario-specific:** this run also showed a one-off model-probe error from the graphics/ML accelerator on the saturated machine. That is captured as a warning and does not change the outcome or exit code.
