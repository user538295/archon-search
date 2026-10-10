# 16. Wizard Validates CUDA Before Writing It

**Status**: Accepted
**Date**: 2026-10-10
**Deciders**: archon-search maintainers
**Supersedes**: partially supersedes [ADR 15 — Unavailable ONNX Providers Fall Back to CPU With a Warning](15_unavailable_onnx_providers_fall_back_to_cpu.md) — only its Negative / Tradeoffs bullet "The NVIDIA wizard default still writes `CUDAExecutionProvider` with no check, so a native install without `onnxruntime-gpu` gets a config that never runs on the GPU and a permanent WARN. Answering `n` or passing `--disable-gpu` avoids it." (`15_unavailable_onnx_providers_fall_back_to_cpu.md:40`). ADR 15's runtime fallback and its strict installer probes are unchanged and still authoritative.

## Context

On an NVIDIA host (`nvidia-smi` succeeds) where the operator accepted CUDA, the wizard's Step 9 called `configure_providers(gpu=GpuType.CUDA)` and wrote `[database].providers = ["CUDAExecutionProvider"]` with no check. Nothing installs `onnxruntime-gpu`: the only code that could, `install_deps`, had no production caller (debt ARCH-5 in `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`). fastembed 0.9.0 and gliner depend on the CPU `onnxruntime`, which has no CUDA provider. So the config and the "enable CUDA acceleration?" prompt promised GPU embedding that never happened: per ADR 15 the runtime dropped the provider with a WARNING, loaded on CPU, and `GET /ready` reported `checks.models: "warn"` forever. ADR 15 recorded this as a tradeoff (`15_unavailable_onnx_providers_fall_back_to_cpu.md:40`), and the D6 plan had deferred CUDA install-time validation (`Documentation/Completed/D6-provider-validation-team-plan.md:62`).

The Apple Silicon path already did the right thing: `_probe_and_configure_coreml` (`archon_search/install/installer.py:262`) calls `validate_providers`, which returns `False` without probing when the installed onnxruntime lacks a requested provider (`installer.py:203-260`), and writes `providers = []` with a warning on failure.

On Linux x86_64 the same CUDA branch also reinstalls torch/torchvision as the `+cu126` build (`archon_search/install/extras.py:161`) for docling and the graph engine. That build's cuDNN wheel (`nvidia-cudnn-cu12`) and the one `onnxruntime-gpu[cudnn]` pulls (`nvidia-cudnn-cu13`) both install `nvidia/cudnn/lib/libcudnn.so.9`, so on an environment that does have `onnxruntime-gpu` the swap would overwrite the cuDNN it loads.

## Decision

**The wizard writes `CUDAExecutionProvider` only after loading both models under it, and runs the `+cu126` torch swap only when it did not.**

- **Probe** (`installer.py:305-328`, `_probe_and_configure_cuda`): calls `validate_providers(["CUDAExecutionProvider"])`, the probe CoreML uses, and returns whether CUDA was written. Pass → `configure_providers(gpu=gpu)` writes `["CUDAExecutionProvider"]`, `clear_reranker_providers()` removes a stale `reranker_providers`, and the summary's `Providers:` reads `CUDA`. Fail → `write_gpu_providers_disabled` writes `providers = []` (so a re-run does not keep an earlier CUDA list), a WARNING is logged, and stdout carries `Warning: CUDA validation failed — falling back to CPU.` plus three lines ending in the GitHub URL of `Documentation/UserManual/10_installation.md#nvidia-gpu-run-embedding-and-reranking-on-cuda` (`_CUDA_GUIDE_URL`; a URL because pip and uv tool installs ship no docs); the summary reads `(CPU default)`. There is no embedder-only split for CUDA.
- **Dry-run** (`installer.py:289-293`, `_configure_cuda`): unchanged narration (`configure_providers` prints the write), no probe, because the probe loads models.
- **Torch swap rule** (`installer.py:288-302`, `_configure_cuda`): on Linux x86_64 the `+cu126` swap runs in dry-run or after a failed probe — docling's PDF/OCR parsing and the graph engine use torch, independent of onnxruntime. After a passed probe it is skipped with `Keeping the installed PyTorch build: onnxruntime-gpu shares its CUDA libraries, which the CUDA 12.6 PyTorch build would replace.`
- **CUDA library preload** (`archon_search/onnx_providers.py`): when the configured providers include `CUDAExecutionProvider` and the installed onnxruntime is a CUDA build (non-empty module-level `cuda_version`), archon calls `onnxruntime.preload_dlls(msvc=False)` once per process before the provider query, so the CUDA/cuDNN libraries from the `nvidia-*` wheels are loaded before the first session; the `onnxruntime-gpu` CUDA provider library has no RPATH to them. On CPU and CoreML builds `cuda_version` is empty and `preload_dlls` is not called, so there it has no side effects: onnxruntime 1.21-1.23 `preload_dlls` would print a "not built with CUDA 12.x support" warning to stdout on such a build ([v1.21.0 `__init__.py`](https://github.com/microsoft/onnxruntime/blob/v1.21.0/onnxruntime/__init__.py#L277-L281)).
- **Receipt excludes** (`archon_search/uv_tool.py:36`, `:144-147`, `:150`; `archon_search/install/extras.py:113`): `excludes` joined the reproducible uv tool receipt keys. When the wizard adds an extra it re-runs `uv tool install … --excludes -` with the recorded names on stdin (`install_extra_command` returns both as one `ToolInstall`), so a manual `--excludes onnxruntime` GPU install survives it.
- **Dead code removed** (ARCH-5): `install_deps`, `check_deps` and `_SEARCH_PACKAGES`.

## Consequences

### Positive

- The config never claims CUDA for ONNX on a host whose onnxruntime cannot run it, so a stock NVIDIA install no longer carries a permanent `checks.models: "warn"`.
- The operator sees why at install time and is pointed to the manual `onnxruntime-gpu` steps.
- An environment that has `onnxruntime-gpu` keeps a single CUDA 13 stack: the wizard does not swap in the CUDA 12.6 torch.

### Negative / Tradeoffs

- A stock NVIDIA install now writes `providers = []` where it wrote `["CUDAExecutionProvider"]` (`BREAKING.md`). Embedding and reranking ran on CPU either way.
- The GPU path is unverified on real hardware: the manual install steps were checked by dependency resolution (uv 0.12.23, 2026-10-10; one CUDA 13 stack on Linux x86_64, Linux aarch64, Windows x86_64) and uv receipt behaviour, and the probe pass branch by tests with a mocked probe. No run on an NVIDIA GPU.
- The probe cannot see one failure: when `CUDAExecutionProvider` is available but its libraries fail at session creation, fastembed 0.9.0 builds the session on CPU and only emits `RuntimeWarning("Attempt to set CUDAExecutionProvider failed. …")` (`fastembed/common/onnx_model.py:152-161`). The probe then passes, the wizard writes CUDA, and the models run on CPU with no `/ready` signal.
- Each NVIDIA wizard run on a GPU-ORT environment pays the probe's model loads, as Apple Silicon runs already do.

## Alternatives Considered

- **Install `onnxruntime-gpu` from the wizard.** Rejected. PyPI `onnxruntime-gpu` 1.27+ is built for CUDA 13.0 and needs driver ≥580, while CUDA 12 builds need CUDA ≥12.8 and live on a non-PyPI feed ([ONNX Runtime CUDA EP](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html), [CUDA 13.0 release notes](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html)); the wizard's torch is `+cu126`, chosen for the widest driver support (`extras.py:144-155`). `uv pip compile` of fastembed 0.9.0 + `onnxruntime-gpu[cuda,cudnn]` + torch 2.13.0 with `--torch-backend cu126` resolved both `onnxruntime` 1.31.0 and `onnxruntime-gpu` 1.31.0 plus a second (CUDA 13) nvidia stack beside the CUDA 12 one, and fastembed documents that the two runtimes "can't be installed in the same environment" ([fastembed GPU guide](https://qdrant.github.io/fastembed/examples/FastEmbed_GPU/)). Removing `onnxruntime` needs install-method-specific steps (uv `--excludes`, pip uninstall) that the operator is better placed to run once.
- **Keep writing CUDA unchecked and rely on ADR 15's runtime fallback.** Rejected: the config and the prompt keep promising GPU work that never happens, with a permanent WARN.

## Cross-References

- [ADR 15 — Unavailable ONNX Providers Fall Back to CPU With a Warning](15_unavailable_onnx_providers_fall_back_to_cpu.md): the runtime fallback and the strict installer probe this decision reuses.
- [ADR 10 — CoreML Split-Provider Configuration](10_coreml_split_providers.md): the CoreML probe whose pattern CUDA now follows (without the split).
- `Documentation/UserManual/10_installation.md` → "NVIDIA GPU: run embedding and reranking on CUDA"; `Documentation/UserManual/20_wizard.md` → Step 3 and the `[database]` matrix.
- `archon_search/install/installer.py`: `_configure_cuda`, `_probe_and_configure_cuda`, `validate_providers`; `archon_search/install/extras.py`: `_install_cuda_torch`; `archon_search/uv_tool.py`: `install_extra_command`, `ToolInstall`; `archon_search/onnx_providers.py`.
- `tests/test_install_cuda_torch.py`, `tests/test_install_uv_tool_extras.py`, `tests/test_uv_tool.py`.
