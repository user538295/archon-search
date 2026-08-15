## Bug: Managed server enters deterministic crash loop on macOS — torch.compile fails on RT-DETR V2 model, launchd restarts indefinitely

**ID**: INFRA-managed_server_torch_compile_crash_loop
**Scenario**: N/A — infrastructure / product startup
**Severity**: high
**Version**: archon-search 26.8.1969 (native); 26.8.1969+docker (image sha256:6ff942d4078675c516583e275336153bdba5fe857214ac6ea22ea505fe042e59)

---

### What happened

The managed server (`archon-search start` / launchd) enters an unrecoverable crash loop on macOS (Apple Silicon, arm64). Every server startup eventually fails during `torch.compile` graph capture of the RT-DETR V2 model and the process exits. launchd restarts it automatically, it fails again. The cycle repeats indefinitely — **43 restarts were observed in a single 1.5-hour test session** without recovery.

Each restart cycle takes 3–15 minutes before crashing, because the server loads all other models successfully before reaching the RT-DETR compilation step. Port 8765 never becomes reachable.

**Secondary consequence (test suite):** Any test fixture that calls `restore_shared_service()` or `restore_managed_service()` waits up to 60 s for `GET /health` → 200. With the crash loop active, those fixtures always time out. Affected tests: S579, S581, S584, S585, S587, S588, S591, S592, S593, S594, S595 (all wizard tests that use `full_wizard_tmp` or inline `restore_managed_service()`). After enough consecutive failures, the test suite hangs indefinitely because launchd restarts keep consuming the fixture's retry budget.

---

### What should happen

The server should start and bind to port 8765 successfully. If `torch.compile` fails for a model, the server should fall back to eager (non-compiled) execution and log a warning — it must not exit.

---

### Steps to reproduce

```bash
# 1. Fresh install
uv tool install archon-search
archon-search wizard --profile minimal --non-interactive --skip-preload

# 2. Pre-warm the max-profile models (this is what triggers RT-DETR load)
archon-search collection add <any-dir> --wait

# 3. Restart the managed server (simulates what wizard tests do)
archon-search stop
archon-search install   # re-registers the launchd plist
# Wait 5–15 minutes — server will crash during torch.compile

# 4. Confirm the crash
grep "torch_compilable_check\|resource_tracker.*leaked" \
    ~/.archon-search/logs/archon-search.log | tail -20
```

Alternatively, run the full e2e test suite; the crash loop begins as soon as a wizard test calls `restore_shared_service()` for the first time.

---

### Evidence

#### Crash stack (from `~/.archon-search/logs/archon-search.log`, PID 47622, repeats identically for every instance)

```
W0814 11:19:08 47622 torch/_dynamo/variables/tensor.py:1759] [0/0]
Graph break from `Tensor.item()`, consider setting:
    torch._dynamo.config.capture_scalar_outputs = True
or:
    env TORCHDYNAMO_CAPTURE_SCALAR_OUTPUTS=1
to include these operations in the captured graph.

Graph break: from user code at:
  File "transformers/utils/generic.py", line 900, in wrapper
    output = func(self, *args, **kwargs)
  File "transformers/models/rt_detr_v2/modeling_rt_detr_v2.py", line 1807, in forward
    outputs = self.model(
  File "transformers/utils/generic.py", line 900, in wrapper
    output = func(self, *args, **kwargs)
  File "transformers/models/rt_detr_v2/modeling_rt_detr_v2.py", line 1571, in forward
    decoder_outputs = self.decoder(
  File "transformers/utils/generic.py", line 976, in wrapper
    output = func(self, *args, **kwargs)
  File "transformers/utils/output_capturing.py", line 248, in wrapper
    outputs = func(self, *args, **kwargs)
  File "transformers/models/rt_detr_v2/modeling_rt_detr_v2.py", line 626, in forward
    hidden_states = decoder_layer(
  File "transformers/models/rt_detr_v2/modeling_rt_detr_v2.py", line 409, in forward
    hidden_states, _ = self.encoder_attn(
  File "transformers/models/rt_detr_v2/modeling_rt_detr_v2.py", line 185, in forward
    torch_compilable_check(
    ^
  File "transformers/utils/import_utils.py", line 1552, in torch_compilable_check
    torch._check_tensor_all_with(error_type, cond, msg_callable)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

W0814 11:20:00 47622 torch/_inductor/utils.py:1953] [2/0_1]
Not enough SMs to use max_autotune_gemm mode
```

```
/Users/manczg/.local/share/uv/python/cpython-3.13.14-macos-aarch64-none/lib/python3.13/
multiprocessing/resource_tracker.py:479: UserWarning:
    resource_tracker: There appear to be 1 leaked semaphore objects to clean up at shutdown:
    {'/mp-jetp5mso'}
```

(Leaked semaphore appears after every crash — a clean shutdown does not produce this.)

#### Restart count

```
$ grep -c "Started server process" ~/.archon-search/logs/archon-search.log
43
```

All 43 restarts occurred within a single 1.5-hour test session (09:57–11:42 local time, 2026-08-14).

#### Crash timeline (selected instances)

| PID   | Started (local) | Crashed (local) | Cycle duration |
|-------|----------------|----------------|----------------|
| 47622 | ~11:07         | 11:22          | ~15 min        |
| 48227 | 11:22          | 11:27          | ~5 min         |
| 48697 | 11:27          | ~11:31         | ~4 min         |
| 51186 | 11:31          | 11:34          | ~3 min         |
| 51547 | 11:34          | 11:39          | ~5 min         |
| 55999 | 11:39          | 11:42          | ~3 min         |
| 56391 | 11:42          | (stopped)      | —              |

#### Fixture timeout errors in test suite (from `log/live-errors.txt`)

```
ERROR tests/test_s585_wizard_rerun_preserves_data.py::…::test_data_preserved_after_same_profile_rerun
tests/wizard_support.py:346: RuntimeError:
    could not restore the shared managed service after an isolated wizard run.
    install: exit 1
    Waiting for search service…………… timed out.
    Warning: Search service did not become ready within 60 seconds.
    service registered after the attempt: True

ERROR tests/test_s587_wizard_claude_cli_hyde_rag_fusion_toml.py::…
(same error)

ERROR tests/test_s588_wizard_claude_cli_not_on_path_warns.py::…
tests/conftest.py:394: RuntimeError:
    could not restore the shared managed server; http://127.0.0.1:8765/health never returned 200.
    start: exit 0
    archon-search started
    install: not attempted
    service registered after the attempt: True
```

#### Environment

| Field | Value |
|---|---|
| OS | macOS Darwin 25.5.0 arm64 (Apple Silicon) |
| Python (tool env) | cpython-3.13.14-macos-aarch64-none |
| archon-search version | 26.8.1969 |
| Install method | `uv tool install archon-search[graph,code]` |
| GPU | Apple MPS (no CUDA, not enough SMs for max_autotune_gemm) |
| Profile | minimal (with max-profile models pre-warmed) |

---

### Root cause hypothesis

`torch.compile` is being applied to the RT-DETR V2 model at server startup on Apple Silicon. The compilation fails because:

1. `torch._check_tensor_all_with` raises inside the compiled graph capture of `RTDetrV2Decoder.forward()` → `encoder_attn` → `torch_compilable_check()`. The `graph break` warning for `Tensor.item()` immediately before the crash suggests the model has dynamic control flow that is incompatible with the current dynamo compilation strategy.

2. `Not enough SMs to use max_autotune_gemm mode` — the MPS backend on Apple Silicon does not present enough Shader Multiprocessors for the `max_autotune` kernel selection path, triggering a fallback that may interact badly with the RT-DETR compilation.

The server does not catch the compilation exception and exits instead of falling back to eager execution.

### Suspected fix directions

- **Server**: wrap the `torch.compile` call on RT-DETR in a `try/except`, fall back to eager mode and log a warning. The user loses compilation speed-up but the server stays up.
- **Alternatively**: gate `torch.compile` on GPU availability / CUDA presence; skip it on MPS/CPU-only systems where `max_autotune` is unavailable.
- **Test harness** (separate issue): `restore_shared_service()` 60-second timeout is too short for a cold model load (~3–15 min). Should either be extended or the function should detect a crash loop (e.g. server PID keeps changing) and fail fast instead of waiting.
