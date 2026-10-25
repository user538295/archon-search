#!/bin/sh
# Fails unless torch and the base image agree on the CUDA major; the GPU arm of
# the Dockerfile runs it after installing the project. torch floats with PyPI
# (no pin), while the base image pins one CUDA major (its CUDA_VERSION). When
# PyPI moves torch to a new CUDA major, the (annotated) GPU build therefore
# fails, by design, until the base tag is bumped, instead of shipping an image
# whose `cuda` device silently falls back to CPU. A missing version (a CPU
# torch build, or a base without CUDA_VERSION) fails too.
# A failing probe (e.g. an import error) prints nothing, so the message below
# fires; its traceback stays on stderr.
set -eu

torch_cuda="$(python3 -c 'import torch; print(torch.version.cuda or "")' || true)"
base_cuda="${CUDA_VERSION:-}"

if [ -z "$torch_cuda" ] || [ -z "$base_cuda" ] || [ "${torch_cuda%%.*}" != "${base_cuda%%.*}" ]; then
    echo "CUDA majors differ or are missing: torch '${torch_cuda}', base image CUDA_VERSION '${base_cuda}'" >&2
    exit 1
fi
