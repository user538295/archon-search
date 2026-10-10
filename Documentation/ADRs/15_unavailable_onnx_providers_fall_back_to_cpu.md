# 15. Unavailable ONNX Providers Fall Back to CPU With a Warning

**Status**: Accepted
**Date**: 2026-10-09
**Deciders**: archon-search maintainers
**Supersedes**: partially supersedes [ADR 14 — Wizard Pre-Warm Loads the Models It Downloads](14_wizard_prewarm_loads_models.md) — only its Negative / Tradeoffs bullet "Hosts whose configured GPU provider the installed onnxruntime lacks now exit `1` at install: fastembed raises for a provider it cannot find (`fastembed/common/onnx_model.py:111-117`). That includes every native NVIDIA install that accepts CUDA without `onnxruntime-gpu`, since fastembed depends on the CPU `onnxruntime`. Before, the same host installed with exit `0` and a broken service; `--disable-gpu` installs on CPU." (`14_wizard_prewarm_loads_models.md:36`). Such hosts now install with exit `0` and a service that runs on CPU. ADR 14's decision — pre-warm loads each model through the service's own backend, and only an embedder failure is fatal — and everything else in it are unchanged and still authoritative.
**Superseded in part by**: [ADR 16 — Wizard Validates CUDA Before Writing It](16_wizard_validates_cuda_before_writing.md) — only the Negative / Tradeoffs bullet "The NVIDIA wizard default still writes `CUDAExecutionProvider` with no check …" (`:40`). The wizard now probes CUDA and writes `providers = []` when the probe fails.

## Context

fastembed 0.9.0 raises `ValueError("Provider <name> is not available. Available providers: […]")` for any requested provider the installed onnxruntime lacks, even when `CPUExecutionProvider` is listed too (`fastembed/common/onnx_model.py:109-117`). archon handed `[database].providers` to fastembed unchanged, so a list naming an accelerator the host's onnxruntime build does not have — CUDA without `onnxruntime-gpu`, CoreML on a non-Apple build — failed every embedder and reranker load.

Startup validation added a pre-flight gate on top: a non-CPU provider missing from `onnxruntime.get_available_providers()` failed both probes without loading either model, so `GET /status` reported `embedder_ok = reranker_ok = false` and `GET /ready` reported `checks.models: "fail"`. The contract said the opposite: the D6 entry in `BREAKING.md:714` defines `checks.models: "warn"` as "provider fallback occurred", and the operator docs described WARN as both models loaded on a fallback provider. S635 (`Documentation/Completed/S635-models_load_and_check_reads_warn.md`) reproduced the gap with `providers = ["CUDAExecutionProvider"]` on macOS: both models failed to load and the check read `fail`.

The wizard writes that exact list on every NVIDIA host that accepts CUDA (`installer.py:810-812`), with no install-time check, while a native `pip`/`uv` install ships the CPU `onnxruntime`. Since ADR 14, pre-warm loads the embedder under it, so those installs exited `1`.

## Decision

**A configured ONNX provider the installed onnxruntime lacks is dropped with a warning, and the models load on the providers that remain, or on CPU. Validation reports that as WARN. The installer's accelerator probes still treat an unavailable provider as not validated.**

- **One selection function** (`archon_search/onnx_providers.py:120-141`): `select_onnx_providers(providers)` keeps the distinct configured providers onnxruntime offers, in configured order, and falls back to an explicit `["CPUExecutionProvider"]` when none remain — explicit because `None` lets fastembed pick CUDA on a CUDA build (`onnx_model.py:101-107`). An unset list stays `None`, and a CPU-only list never queries onnxruntime. When anything is dropped it logs one WARNING per distinct configured list per process, naming the dropped providers, the providers used and the ones the build offers (`onnx_providers.py:144-156`). It imports onnxruntime lazily, only when a model is about to load.
- **Runtime loads** (`embedder.py:54-57`, `reranker.py:54-57`): `ModelEmbedder` and `ModelReranker` build their fastembed model with the selection, so the server's warm-up and searches, collection sync, and the wizard pre-warm all load on CPU instead of raising.
- **Validation** (`model_validation.py:407-416`): `validate_providers_shared` probes both models on the same selection the runtime uses and appends `configured ONNX providers not available: <names>`. Probes that pass return `(True, True, [that warning])`, which `_model_check_status` maps to `checks.models: "warn"` (`routes_ready.py:115-118`). If onnxruntime cannot be queried at all, neither model is probed and the embedder reports a failure (`model_validation.py:407-411`), because fastembed makes the same query before it builds a session (`onnx_model.py:86`).
- **Installer accelerator probes stay strict** (`installer.py:220-252`): `validate_providers` and `validate_embedder_only` return `False` without probing when a requested provider is unavailable (`installer.py:238-241`). Probing it would pass on CPU and lead the wizard to write an accelerator the host cannot use, so the Apple Silicon CoreML check still writes `providers = []` when CoreML is absent (`installer.py:254-270`).
- **Stale-split advisory**: the "re-run the wizard to apply the split configuration" warning fires only when the installed onnxruntime has CoreML (`model_validation.py:555-581`). Without CoreML the reranker ran on CPU, so its failure says nothing about CoreML.
- **Wizard console**: the wizard CLI configures no logging, so the selection's WARNING reaches the operator's stderr through `logging.lastResort` — the route the installer already relies on for its own warnings (`installer.py:476-478`) — once per configured list (`tests/test_install_prewarm_real_fastembed.py:135-138`).

## Consequences

### Positive

- A host whose onnxruntime build lacks the configured accelerator can still search. Before, every load failed, which made the service unusable.
- `checks.models: "warn"` and `embedder_ok = reranker_ok = true` now describe what happened. This matches the D6 contract and the operator runbook.
- A native NVIDIA install without `onnxruntime-gpu` exits `0` again. Unlike before ADR 14, the service it starts embeds.
- The runtime, validation and pre-warm all use one selection rule, so validation cannot pass a list the runtime then rejects, or the reverse.

### Negative / Tradeoffs

- A misconfigured accelerator becomes a slowdown instead of a failure. The only signals are the log warning, the `provider_warnings` entry and a `checks.models: "warn"` that persists until the operator changes the config or installs the matching onnxruntime build. Operators who never watch those signals run on CPU without noticing.
- The NVIDIA wizard default still writes `CUDAExecutionProvider` with no check, so a native install without `onnxruntime-gpu` gets a config that never runs on the GPU and a permanent WARN. Answering `n` or passing `--disable-gpu` avoids it.
- Each process logs the warning once per distinct list. A second model built later in the same process (collection sync, a route-built embedder) logs nothing new.

## Alternatives Considered

- **Keep the FAIL gate and rewrite the docs to say FAIL.** Rejected: the S635 report, the D6 contract (`BREAKING.md:714`) and the operator docs all promised a WARN fallback. Also, an onnxruntime build without a GPU provider should not make search unusable on a host that can run the models on CPU.
- **Filter only in validation.** Rejected: `ModelEmbedder` and `ModelReranker` would still pass the configured list to fastembed, which raises (`onnx_model.py:109-117`). Validation would then report WARN while every runtime load and the wizard pre-warm failed.
- **Fall back to `providers=None` instead of an explicit CPU list.** Rejected: with `None`, fastembed picks the device itself and chooses CUDA on a CUDA build (`onnx_model.py:101-107`). That would override a configured list on exactly the hosts where it differs.

## Cross-References

- [ADR 14 — Wizard Pre-Warm Loads the Models It Downloads](14_wizard_prewarm_loads_models.md): pre-warm still loads through `ModelEmbedder` / `ModelReranker`, and those backends now apply this selection.
- [ADR 13 — Graph Device Probe Is Two-Stage, and the Memory Guardrail Polices Growth](13_graph_device_probe_two_stage_and_memory_guardrail.md): `torch_device.resolve_torch_device` is the torch counterpart for the graph engine. It also steps an unavailable device down to CPU with a WARNING and never raises.
- `archon_search/onnx_providers.py`: `select_onnx_providers`, `unavailable_onnx_providers`, `OnnxProviderSelection`.
- `archon_search/model_validation.py`: `validate_providers_shared`, `_coreml_available`.
- `archon_search/install/installer.py`: `_probe_providers`, `validate_providers`, `validate_embedder_only`.
- `tests/test_onnx_providers.py`, `tests/test_onnx_provider_fallback.py`, `tests/test_install_prewarm_real_fastembed.py`.
