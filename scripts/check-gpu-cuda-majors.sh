#!/bin/sh
# Fails unless onnxruntime-gpu, torch and the base image agree on the CUDA
# major; the GPU arm of the Dockerfile runs it after installing
# onnxruntime-gpu. torch and onnxruntime-gpu float with PyPI (no pins), while
# the base image pins one CUDA major (its CUDA_VERSION). When PyPI moves
# either package to a new CUDA major, the (annotated) GPU build therefore
# fails, by design, until the base tag is bumped, instead of shipping an image
# whose CUDA provider silently falls back to CPU. A missing version (a CPU
# onnxruntime or torch build, or a base without CUDA_VERSION) fails too.
# onnxruntime's version is its module-level `cuda_version`, the attribute
# archon_search/onnx_providers.py reads.
# A failing probe (e.g. an import error) prints nothing, so the message below
# fires; its traceback stays on stderr.
set -eu

ort_cuda="$(python3 -c 'import onnxruntime; print(onnxruntime.cuda_version or "")' || true)"
torch_cuda="$(python3 -c 'import torch; print(torch.version.cuda or "")' || true)"
base_cuda="${CUDA_VERSION:-}"

if [ -z "$ort_cuda" ] || [ -z "$torch_cuda" ] || [ -z "$base_cuda" ] \
    || [ "${ort_cuda%%.*}" != "${base_cuda%%.*}" ] || [ "${torch_cuda%%.*}" != "${base_cuda%%.*}" ]; then
    echo "CUDA majors differ or are missing: onnxruntime-gpu '${ort_cuda}', torch '${torch_cuda}', base image CUDA_VERSION '${base_cuda}'" >&2
    exit 1
fi
