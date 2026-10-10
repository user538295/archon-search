"""archon-search package root.

Pins native-library environment defaults (Arrow's memory allocator, TorchDynamo,
ONNX Runtime telemetry) before any Arrow-, torch- or onnxruntime-backed module loads.
"""
import os
import sys

# Official onnxruntime wheels (via fastembed, gliner, magika) run Microsoft's 1DS
# telemetry uploader by default on macOS and Linux (onnxruntime docs/Privacy.md).
# That is undisclosed egress, and it aborts the process at exit: `~PosixEnv` tears
# the uploader down while its worker decodes an HTTP response, the worker locks a
# destroyed recursive_mutex, and libc++ calls terminate (SIGABRT, "recursive_mutex
# lock failed"). Only this env var, set before onnxruntime initializes, prevents
# the uploader from being created; `disable_telemetry_events()` is too late.
# All platforms. `setdefault` leaves an operator override intact.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

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
