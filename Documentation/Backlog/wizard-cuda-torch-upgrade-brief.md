# Feature Brief: Wizard CUDA-torch upgrade for GPU hosts

## Problem
S269 made the default install ship the **CPU-only** build of the machine-learning
runtime (`torch==<ver>+cpu`, pinned via the PyTorch CPU wheel index in
`pyproject.toml` / `uv.lock`, marker-scoped to `linux x86_64`). That is correct
for CPU hosts and keeps the `:latest` container free of the multi-GB CUDA
runtime. But a user installing archon-search **natively on a machine with an
NVIDIA GPU** now also gets the CPU-only torch, so `docling` (PDF + image-OCR
parsing — the only component that uses torch) runs on the CPU even though a GPU
is present and could accelerate it.

The install wizard already detects the GPU and asks the user to confirm
acceleration (`_prompt_gpu_confirm`, `wizard.py:498`; "NVIDIA GPU detected —
enable CUDA acceleration? [Y/n]"), and already configures **onnxruntime**
execution providers for the embedder/reranker on confirm (`installer.py:723-725`,
`configure_providers(gpu=GpuType.CUDA)`). It does **not** touch torch. So the GPU
"yes" today accelerates embeddings/reranking but silently leaves docling on CPU.

## Goal
When the user opts into CUDA acceleration on a supported GPU host, also replace
the CPU-only torch/torchvision with the CUDA build from the PyTorch CUDA wheel
index, so docling's PDF/OCR uses the GPU. If the swap fails for any reason, the
working CPU build must remain — a failed GPU upgrade must never leave a broken
install. This is opt-in and platform-gated; nothing changes for CPU hosts, for
users who decline, or for the Docker image (GPU containers already select the
`nvidia/cuda` base image — this feature is the native-install path only).

## Users & Context
Operators installing archon-search as a native service (launchd/systemd via the
wizard or `archon-search install`) on a Linux/Windows x86_64 box with an NVIDIA
GPU, who ingest PDFs/images and want GPU-accelerated docling parsing. The wizard
runs only during native install — it is never invoked inside the Docker image.

## Core Flow
1. Wizard/installer detects the GPU (`detect_gpu()` → `GpuType`) as today.
2. The CUDA-torch upgrade is **offered only when ALL hold**: `enable_gpu` is true
   (user confirmed, `--disable-gpu` not passed), `gpu == GpuType.CUDA`, and the
   host is x86_64 (`platform_machine in {"x86_64","AMD64"}`) on Linux or Windows.
   On Apple Silicon (`GpuType.METAL`) the stock build already uses Metal/MPS — no
   swap, no prompt change. On `GpuType.NONE` or ARM Linux — nothing.
3. On confirm, in the CUDA branch of Step 9 (`installer.py:723`), reinstall
   torch/torchvision from the CUDA wheel index (`uv pip install torch torchvision
   --index-url https://download.pytorch.org/whl/<cuXXX>`), reusing the existing
   `install/extras.py` `_install_extra` invocation pattern (uv pip → pip fallback).
   The `<cuXXX>` tag must be the CUDA build matching the torch version pinned in
   `pyproject.toml` (verify against download.pytorch.org at implementation time —
   do NOT hardcode a guess).
4. Dry-run mode prints `[DRY RUN] Would install CUDA torch from <index>` and makes
   no change (mirror the existing dry-run discipline).
5. On any install failure (non-zero exit, network error, missing wheel), log a
   clear WARNING, keep the CPU build, and continue the install to success — the
   GPU upgrade is best-effort.

## In Scope
- A gated CUDA-torch reinstall step in the installer's CUDA GPU branch.
- Platform gate: Linux/Windows x86_64 + `GpuType.CUDA` + `enable_gpu` only.
- CUDA wheel-index selection matching the pinned torch version (verified, not guessed).
- Best-effort failure handling that preserves the working CPU build.
- Dry-run support and `--disable-gpu` / decline / non-interactive coverage.
- Unit tests (mock GPU detection + the install invocation): asserts the CUDA-index
  install runs on x86_64+CUDA+confirm; does NOT run on Metal, ARM, NONE, decline,
  `--disable-gpu`, or dry-run; and that a simulated install failure leaves the CPU
  build and does not fail the install.

## Out of Scope
- Changing the S269 default (CPU torch stays the default for everyone).
- The Docker image (GPU users use the `nvidia/cuda` base image — unchanged).
- Downgrading a CUDA build back to CPU (not needed; reinstall is forward-only).
- Post-install CUDA validation / a GPU smoke probe (the existing CUDA branch
  already leaves `gpu_provider = None` and skips the post-prewarm probe — keep that).
- ROCm / AMD GPUs (`GpuType` has no ROCm member today).
- Windows service support beyond what the installer already does.
