"""Reports which onnxruntime the container runs after the entrypoint filled the extras volume.

Not a test module: ``tests/test_docker_gpu_image.py`` sends this file to
``python3 -`` (the container CMD, run after the entrypoint) over stdin.
Arguments: the JSON-prefix sentinel and the extras volume path. Prints one
sentinel-prefixed JSON line: the distributions the volume holds and the file
``import onnxruntime`` loads.
"""

import importlib.metadata
import json
import sys

import onnxruntime

sentinel, extras_volume = sys.argv[1:]
report = {
    "volume_distributions": sorted(dist.metadata["Name"] for dist in importlib.metadata.distributions(path=[extras_volume])),
    "onnxruntime_file": onnxruntime.__file__,
}
print(sentinel + json.dumps(report))
