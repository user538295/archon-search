"""Opens each soname given as an argument the way the dynamic loader resolves it.

Not a test module: ``tests/_gpu_image_probe.py`` runs this source, which it
receives as data, in a fresh interpreter inside the GPU image — one that has
imported neither torch nor onnxruntime (both can load CUDA libraries on
import). Prints one JSON object: the sonames that failed to open and, per
soname, every file of that name the process then has mapped.
"""

import ctypes
import json
import os
import sys

sonames = sys.argv[1:]
failures = {}
for soname in sonames:
    try:
        ctypes.CDLL(soname)
    except OSError as error:
        failures[soname] = str(error)
with open("/proc/self/maps") as maps:
    mapped = {fields[5].strip() for fields in (line.split(maxsplit=5) for line in maps) if len(fields) == 6}
loaded_files = {soname: sorted(path for path in mapped if os.path.basename(path) == soname) for soname in sonames}
print(json.dumps({"failures": failures, "loaded_files": loaded_files}))
