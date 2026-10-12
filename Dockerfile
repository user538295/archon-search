# syntax=docker/dockerfile:1.7
#
# archon-search container image.
#
# The CPU / GPU variant is selected via the BASE_IMAGE build-arg:
#
#   docker build .                                                          # CPU (default)
#   docker build --build-arg BASE_IMAGE=python:3.12-slim .                   # CPU (explicit)
#   docker build --build-arg BASE_IMAGE=nvidia/cuda:13.0.3-base-ubuntu22.04 .  # GPU (release tag: GPU_BASE_IMAGE in .github/workflows/archon-search-release.yml)
#
# The CPU base ships Python 3.12 already; the NVIDIA CUDA base ships only the
# CUDA runtime and is Ubuntu-based, so we conditionally install Python 3.12
# from deadsnakes when the base does not provide it. fastembed 0.9.0 (the version verified on 2026-10-10) does
# not publish a `[gpu]` extra, so the GPU branch swaps the default CPU
# `onnxruntime` for `onnxruntime-gpu[cuda,cudnn]` after the base archon-search
# install. The CUDA 13 / cuDNN 9 libraries come from the pip wheels torch and
# onnxruntime-gpu share, so the GPU base is the slim CUDA 13 `-base` tag (no
# system cuDNN to clash with the wheels'); CUDA 13 needs an NVIDIA driver >= 580.

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# Re-declare ARGs after FROM so they are visible to RUN instructions.
ARG BASE_IMAGE=python:3.12-slim
ARG GIT_COMMIT=unknown
# Release tag passed by the release workflow. `.git` is dockerignored, so
# hatch-vcs cannot derive the version at build time; we stamp it explicitly.
ARG RELEASE_VERSION=0.0.0

LABEL org.opencontainers.image.source="https://github.com/user538295/archon-search"
LABEL org.opencontainers.image.title="archon-search"
LABEL org.opencontainers.image.description="Hybrid retrieval + routing server (LanceDB + fastembed + reranker)"
LABEL org.opencontainers.image.revision=$GIT_COMMIT
LABEL org.opencontainers.image.version=$RELEASE_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Install base OS dependencies. On the python:3.12-slim base, Python is
# already present and apt is Debian-based; on the NVIDIA CUDA Ubuntu base,
# Python 3.12 must be added via the deadsnakes PPA (Ubuntu 22.04 ships 3.10).
# The `tini` package provides the init-style PID 1 used by ENTRYPOINT.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tini; \
    if ! command -v python3.12 >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends software-properties-common gnupg; \
        add-apt-repository -y ppa:deadsnakes/ppa; \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            python3.12 python3.12-venv python3.12-dev; \
    fi; \
    # Ensure a working `python3` shim points to 3.12 on the GPU base. The
    # CPU base already ships `python3` as 3.12, so this is a no-op there.
    if [ ! -e /usr/local/bin/python3 ] || ! /usr/local/bin/python3 -c 'import sys; assert sys.version_info[:2]==(3,12)' >/dev/null 2>&1; then \
        ln -sf "$(command -v python3.12)" /usr/local/bin/python3; \
    fi; \
    # pip is required to bootstrap the project install.
    if ! /usr/local/bin/python3 -m pip --version >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends curl; \
        curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py; \
        /usr/local/bin/python3 /tmp/get-pip.py; \
        rm -f /tmp/get-pip.py; \
        apt-get purge -y curl; \
        apt-get autoremove -y; \
    fi; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the project sources from the build context. The release workflow
# (.github/workflows/archon-search-release.yml) builds from the tagged
# commit so the image always contains the exact source published in the
# release. `.dockerignore` excludes tests, docs, and dev artefacts.
COPY . /app

# CPU image must resolve CPU-only torch wheels. `torch` is a transitive core
# dependency (docling → docling-ibm-models / accelerate / safetensors[torch]);
# on linux the default PyPI `torch` pulls the full CUDA runtime — torch +
# torchvision + ~18 nvidia-*/cuda* packages, ~6 GB — that a CPU host can never
# execute. Bake an /etc/pip.conf pointing pip at PyTorch's CPU wheel index so
# BOTH the bake-time `pip install .` below AND the entrypoint's runtime install
# of the packages the extras add to the image (into /pip-packages) resolve
# `torch==<ver>+cpu` (which declares no nvidia/cuda dependencies). The
# nvidia/cuda GPU base skips this: its torch is PyPI's Linux wheel, today the
# CUDA 13 build (`+cu130`, with the CUDA 13 / cuDNN 9 wheels the GPU arm below
# shares with onnxruntime-gpu).
RUN set -eux; \
    case "${BASE_IMAGE}" in \
        *nvidia/cuda*) \
            : "GPU base — keep the default index so torch resolves PyPI's CUDA 13 build" ;; \
        *) \
            printf '[global]\nextra-index-url = https://download.pytorch.org/whl/cpu\n' \
                > /etc/pip.conf ;; \
    esac

# Install archon-search into the system Python. We deliberately do NOT use
# `uv` inside the container: the production image only needs to *run* the
# server, not re-resolve dependencies. `pip install --no-cache-dir .` is
# both smaller and avoids pulling uv into the runtime layer.
#
# For the GPU variant the project's CPU `onnxruntime` is swapped for
# `onnxruntime-gpu` after the base install. fastembed 0.9.0 does not ship
# a `[gpu]` extra (verified against the package metadata) so this is the
# documented manual-swap path. PyPI's onnxruntime-gpu is built with CUDA 13.0
# and cuDNN 9 from 1.27 on, needs CUDA >= 13.0 at run time, and works with
# PyTorch's CUDA/cuDNN wheels when both use the same CUDA and cuDNN majors
# (https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html);
# CUDA 13 needs an NVIDIA driver >= 580 (Table 2 of
# https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html).
# Its `[cuda,cudnn]` extras require the same CUDA 13 / cuDNN 9 wheels torch
# already pulls, so the image holds one shared stack, with one `libcudnn.so.9`.
# fastembed preloads nothing, so the `nvidia/*/lib` directories of those wheels
# (beside onnxruntime in the install dir, dist-packages on the Ubuntu base) go
# into the loader cache (ldconfig) for the CUDA provider to find. That glob
# relies on the wheels' layout, the one ORT's own `preload_dlls` looks in:
# `nvidia/cu<major>/lib` for the CUDA 13 toolkit wheels, `nvidia/cudnn/lib`
# for cuDNN (https://github.com/microsoft/onnxruntime/blob/v1.31.0/onnxruntime/__init__.py
# lines 211-216 and 261). If no `nvidia/*/lib` directory exists, `ls -d` fails
# the build; a wheel that moves only some of its libraries elsewhere leaves
# them unregistered, which the GPU image tests catch
# (tests/test_docker_gpu_image.py). scripts/check-gpu-cuda-majors.sh then
# fails the build unless onnxruntime-gpu, torch and the base agree on the CUDA
# major (rationale in its header).
RUN set -eux; \
    SETUPTOOLS_SCM_PRETEND_VERSION="${RELEASE_VERSION}+docker" \
        /usr/local/bin/python3 -m pip install --no-cache-dir .; \
    case "${BASE_IMAGE}" in \
        *nvidia/cuda*) \
            /usr/local/bin/python3 -m pip uninstall -y onnxruntime; \
            /usr/local/bin/python3 -m pip install --no-cache-dir "onnxruntime-gpu[cuda,cudnn]>=1.27"; \
            site_packages="$(/usr/local/bin/python3 -c 'import pathlib, onnxruntime; print(pathlib.Path(onnxruntime.__file__).parents[1])')"; \
            ls -d "${site_packages}"/nvidia/*/lib > /etc/ld.so.conf.d/archon-search-cuda-wheels.conf; \
            ldconfig; \
            sh /app/scripts/check-gpu-cuda-majors.sh; \
            ;; \
    esac

# Non-root runtime user. The data directory is pre-created with the
# correct ownership: anonymous-volume runs (`docker run` without `-v`)
# rely on this so UID 1000 can write the auto-generated key file.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin appuser; \
    mkdir -p /data /pip-packages; \
    chown appuser:appuser /data /pip-packages

# Runtime configuration. `ARCHON_SEARCH_DATA_DIR` redirects every runtime
# path (db, logs, telemetry, key file, jobs, fasttext models, ingest
# history) onto the mounted volume. `ARCHON_SEARCH_CONTAINER=1` tells
# `configure_logging()` to attach a StreamHandler(sys.stderr) so
# `docker logs` captures application logs. `FASTEMBED_CACHE_PATH` gives
# fastembed's downloaded model weights their own directory on the same
# persistent volume (`get_fastembed_cache_dir()`; unset, they would land in
# `/data/models`), so a named volume can share them across containers.
ENV ARCHON_SEARCH_DATA_DIR=/data \
    ARCHON_SEARCH_CONTAINER=1 \
    FASTEMBED_CACHE_PATH=/data/fastembed-cache \
    HOME=/data

COPY scripts/docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER appuser

VOLUME ["/data"]
EXPOSE 8765

# `/ready` is the readiness probe exposed by the FastAPI app. urllib is
# used to avoid pulling curl into the slim base image. Exit non-zero on
# any error so the orchestrator can mark the container unhealthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=3 \
    CMD python3 -c "import urllib.request, sys; urllib.request.urlopen('http://localhost:8765/ready')" || exit 1

# tini reaps zombies and forwards SIGTERM/SIGINT to uvicorn so the server
# exits cleanly during `docker stop` (within the compose `stop_grace_period`).
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
CMD ["archon-search", "serve"]
