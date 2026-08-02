## Bug: CPU-only `:latest` image must not ship the NVIDIA CUDA/torch stack

**ID**: S269-no_torch_or_cuda_packages_resolved
**Scenario**: S269
**Severity**: medium
**Version**: ghcr.io/user538295/archon-search:latest @ sha256:43e4e63d

### What happened
The :latest / :<version> tag (base python:3.12-slim) is documented as 'Any host without an NVIDIA GPU. CPU inference only.' Yet a pip --dry-run resolve of the default extras inside the image pulls torch 2.13.0 + torchvision + 18 NVIDIA CUDA cu13 packages (nvidia-cublas, nvidia-cudnn-cu13, nvidia-cuda-runtime, nvidia-cufft, nvidia-cusolver, nvidia-cusparse, nvidia-nccl-cu13, nvidia-nvjitlink, cuda-toolkit, cuda-bindings, ...) — 20 GPU packages, ~6.2 GB on disk after install. Every extra (graph, code, multilingual) resolves the identical set, so torch is a base dependency, not extra-specific. These CUDA runtime libraries cannot execute without an NVIDIA GPU, so the CPU image ships GPU code it documents itself as not using. Reproduced on native linux/arm64, image digest sha256:43e4e63d.

### What should happen
- Per the doc, the CPU-only `:latest` image is for hosts without an NVIDIA GPU. The resolved set
  must contain **no** `torch*`, `nvidia-*`, or `cuda*` packages. Any such package is a discrepancy:
  a CPU image cannot execute CUDA runtime libraries, so shipping them contradicts the tag's
  documented "CPU inference only" purpose.

### Steps to reproduce
1. Pull the CPU image:
   ```bash
   docker pull ghcr.io/user538295/archon-search:latest
   ```
2. Resolve (do not install) the default extras inside the image and list what would be pulled:
   ```bash
   docker run --rm --entrypoint sh ghcr.io/user538295/archon-search:latest -c '
     cd /app
     python3 -m pip install --dry-run --ignore-installed --quiet --report /tmp/r.json ".[graph,code,multilingual]" >/dev/null
     python3 -c "import json;print(chr(10).join(sorted(i[chr(39)+\"metadata\"+chr(39)][\"name\"] for i in json.load(open(\"/tmp/r.json\"))[\"install\"])))"
   ' | grep -iE "^(torch|nvidia|cuda)"
   ```

### Evidence
```
docker run --rm --entrypoint sh <image> -c 'pip install --dry-run .[graph,code,multilingual]' | grep -iE torch/nvidia/cuda:
  torch-2.13.0, torchvision-0.28.0, nvidia-cublas-13.1.1.3, nvidia-cudnn-cu13-9.20.0.48, nvidia-cuda-runtime-13.0.96, nvidia-cufft-12.0.0.61, nvidia-cusolver-12.0.4.66, nvidia-cusparse-12.6.3.3, nvidia-nccl-cu13-2.29.7, nvidia-nvjitlink-13.3.33, cuda-toolkit-13.0.3.0, cuda-bindings-13.3.1 (20 pkgs)
du -sh /pip-packages after a real graph,code install -> 6.2G
dry-run torch/nvidia/cuda pkg count per extra: graph=20, code=20, multilingual=20 (identical)
```
