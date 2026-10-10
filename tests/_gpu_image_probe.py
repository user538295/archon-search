"""Reports what decides whether onnxruntime's CUDA provider can load in the GPU image.

Not a test module: ``tests/test_docker_gpu_image.py`` sends this file to
``python3 -`` in the GPU image over stdin. Arguments: the JSON-prefix sentinel,
the source of ``tests/_dlopen_probe.py``, ``CUFFT_MAJOR_BY_CUDA_MAJOR`` as JSON
and the cuDNN soname.

Prints one sentinel-prefixed JSON line: the providers the build ships, the
libraries its CUDA provider links against (the path fastembed takes when
nothing preloads them), whether every library the CUDA provider loads opens by
soname without ``onnxruntime.preload_dlls()`` and which file each one loads
from, the cuDNN file of the ``nvidia/cudnn/lib`` wheel directory torch's
nvidia-cudnn-cu13 installs, and the CUDA versions the onnxruntime build, torch
and the base image were made for. The sonames are those ORT 1.31's
``preload_dlls`` loads on Linux
(https://github.com/microsoft/onnxruntime/blob/v1.31.0/onnxruntime/__init__.py
lines 233-240 for CUDA, 275-277 for cuDNN), with the CUDA major taken from
``onnxruntime.cuda_version``, as ORT does (line 208).
"""

import importlib.metadata
import json
import os
import pathlib
import subprocess
import sys

import onnxruntime
import torch

sentinel, dlopen_probe_source, cufft_major_by_cuda_major_json, cudnn_soname = sys.argv[1:]
cufft_major_by_cuda_major = {int(major): cufft for major, cufft in json.loads(cufft_major_by_cuda_major_json).items()}

site_packages = pathlib.Path(onnxruntime.__file__).parent.parent
cuda_provider = site_packages / "onnxruntime" / "capi" / "libonnxruntime_providers_cuda.so"
ldd = subprocess.run(["ldd", str(cuda_provider)], capture_output=True, text=True, check=True).stdout
cuda_major = int(onnxruntime.cuda_version.split(".")[0])
if cuda_major not in cufft_major_by_cuda_major:
    sys.exit(f"CUDA major {cuda_major} missing from CUFFT_MAJOR_BY_CUDA_MAJOR {cufft_major_by_cuda_major}; add its cuFFT major")
cuda_provider_sonames = [
    f"libcublasLt.so.{cuda_major}",
    f"libcublas.so.{cuda_major}",
    f"libnvrtc.so.{cuda_major}",
    # Fixed across CUDA majors in ORT's own list (v1.31.0 onnxruntime/__init__.py lines 237, 256).
    "libcurand.so.10",
    f"libcufft.so.{cufft_major_by_cuda_major[cuda_major]}",
    f"libcudart.so.{cuda_major}",
    cudnn_soname,
]
dlopen = json.loads(
    subprocess.run(
        [sys.executable, "-c", dlopen_probe_source, *cuda_provider_sonames],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
)
report = {
    "onnxruntime_version": onnxruntime.__version__,
    "providers": onnxruntime.get_available_providers(),
    "onnxruntime_cuda_version": onnxruntime.cuda_version,
    "torch_version": torch.__version__,
    "torch_cuda_version": torch.version.cuda,
    "image_cuda_version": os.environ["CUDA_VERSION"],
    "ldd_not_found": [line.strip() for line in ldd.splitlines() if "not found" in line],
    "dlopen_failures": dlopen["failures"],
    "dlopen_loaded_files": dlopen["loaded_files"],
    "cudnn_wheel_library": os.path.realpath(site_packages / "nvidia" / "cudnn" / "lib" / cudnn_soname),
    "onnxruntime_distributions": sorted(
        dist.metadata["Name"].lower()
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"].lower().startswith("onnxruntime")
    ),
}
print(sentinel + json.dumps(report))
