"""archon-search package root.

Pins native-library environment defaults (Arrow's memory allocator, TorchDynamo)
before any Arrow- or torch-backed module loads.
"""
import os
import sys

# pyarrow 25's default bundled allocator (mimalloc) segfaults on macOS/arm64
# during per-thread heap init (`mi_thread_init`) — a known upstream defect
# (apache/arrow #37010, #41696, #44342). Route Arrow through the system
# allocator on macOS only; Linux keeps the faster mimalloc default (the crash
# is macOS-specific). `setdefault` leaves an operator override intact.
#
# This runs before any submodule imports pyarrow/lancedb (both are lazy), so it
# wins the race in every entry point: CLI, `serve`, the launchd/systemd
# service, and smoke subprocesses. ponytail: env var, not a per-callsite pool
# swap — one chokepoint covers every process.
if sys.platform == "darwin":
    os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")

    # torch.compile (TorchDynamo) crashes on Apple Silicon (MPS) when docling's
    # layout stage compiles its RT-DETR V2 model: graph capture reaches
    # `torch._check_tensor_all_with`, which raises inside the captured graph and
    # kills the process (launchd then restarts it, so the crash becomes a loop).
    # MPS has no working inductor path anyway (no CUDA SMs for
    # `max_autotune_gemm`), so there is no compiled-speed to trade away —
    # disabling dynamo is pure win on this platform. `setdefault` leaves an
    # operator override intact.
    os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")
