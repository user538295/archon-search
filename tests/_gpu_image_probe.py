"""Reports what decides whether PyTorch's CUDA build can serve the GPU image.

Not a test module: ``tests/test_docker_gpu_image.py`` sends this file to
``python3 -`` in the GPU image over stdin. Argument: the JSON-prefix sentinel.

Prints one sentinel-prefixed JSON line: the torch version and the CUDA version
it was built for, the CUDA version of the base image, and the installed
``onnxruntime*`` distributions (the GPU image must carry no ``onnxruntime-gpu``).
"""

import importlib.metadata
import json
import os
import sys

import torch

sentinel = sys.argv[1]

report = {
    "torch_version": torch.__version__,
    "torch_cuda_version": torch.version.cuda,
    "image_cuda_version": os.environ["CUDA_VERSION"],
    "onnxruntime_distributions": sorted(
        dist.metadata["Name"].lower()
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"].lower().startswith("onnxruntime")
    ),
}
print(sentinel + json.dumps(report))
