# syntax=docker/dockerfile:1.7
#
# archon-search container image.
#
# The CPU / GPU variant is selected via the BASE_IMAGE build-arg:
#
#   docker build .                                                          # CPU (default)
#   docker build --build-arg BASE_IMAGE=python:3.14-slim .                   # CPU (explicit)
#   docker build --build-arg BASE_IMAGE=nvidia/cuda:13.4.3-base-ubuntu22.04 .  # GPU (release tag: GPU_BASE_IMAGE in .github/workflows/archon-search-release.yml)
#
# The CPU base ships Python 3.14 already; the NVIDIA CUDA base ships only the
# CUDA runtime and is Ubuntu-based, so we conditionally install Python 3.14
# from deadsnakes when the base does not provide it. The GPU variant runs on
# PyPI's CUDA build of PyTorch: its CUDA 13 / cuDNN 9 wheels carry the libraries
# torch preloads itself, so the GPU base is the slim CUDA 13 `-base` tag (no
# system cuDNN to clash with the wheels'); CUDA 13 needs an NVIDIA driver >= 580.

ARG BASE_IMAGE=python:3.14-slim
FROM ${BASE_IMAGE}

# Re-declare ARGs after FROM so they are visible to RUN instructions.
ARG BASE_IMAGE=python:3.14-slim
ARG GIT_COMMIT=unknown
# Release tag passed by the release workflow. `.git` is dockerignored, so
# hatch-vcs cannot derive the version at build time; we stamp it explicitly.
ARG RELEASE_VERSION=0.0.0

LABEL org.opencontainers.image.source="https://github.com/user538295/archon-search"
LABEL org.opencontainers.image.title="archon-search"
LABEL org.opencontainers.image.description="Hybrid retrieval + routing server (LanceDB + PyTorch embedder + reranker)"
LABEL org.opencontainers.image.revision=$GIT_COMMIT
LABEL org.opencontainers.image.version=$RELEASE_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=10

# Install base OS dependencies. On the python:3.14-slim base, Python is
# already present and apt is Debian-based; on the NVIDIA CUDA Ubuntu base,
# Python 3.14 must be added via the deadsnakes PPA (Ubuntu 22.04 ships 3.10).
# The `tini` package provides the init-style PID 1 used by ENTRYPOINT.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tini libxcb1 libx11-6 libxext6 libglib2.0-0; \
    if ! command -v python3.14 >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends software-properties-common gnupg; \
        add-apt-repository -y ppa:deadsnakes/ppa; \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            python3.14 python3.14-venv python3.14-dev; \
    fi; \
    # Ensure a working `python3` shim points to 3.14 on the GPU base. The
    # CPU base already ships `python3` as 3.14, so this is a no-op there.
    if [ ! -e /usr/local/bin/python3 ] || ! /usr/local/bin/python3 -c 'import sys; assert sys.version_info[:2]==(3,14)' >/dev/null 2>&1; then \
        ln -sf "$(command -v python3.14)" /usr/local/bin/python3; \
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
    # fasttext-predict (the multilingual extra, installed by the entrypoint at start-up) has no
    # CPython 3.14 wheel on PyPI, only an sdist (0.9.2.4, checked 2026-10-29), and the entrypoint's
    # pip would need a C++ compiler to build it. Build the wheel once here into /opt/wheelhouse
    # (PIP_FIND_LINKS below points the entrypoint's pip at it) and purge the compiler in the same
    # layer, so the image ships no toolchain. Remove this once PyPI carries a cp314 wheel.
    apt-get install -y --no-install-recommends g++; \
    /usr/local/bin/python3 -m pip wheel --no-deps --wheel-dir /opt/wheelhouse "fasttext-predict>=0.9.2.4"; \
    apt-get purge -y g++; \
    apt-get autoremove -y; \
    # opencv-python (a dependency of rapidocr and docling) links libGL.so.1 at load time. `apt-get
    # install libgl1` drags in Mesa's software renderer through libglx0 (LLVM, ~190 MB); nothing here
    # renders, so the three glvnd packages are unpacked alone and their Mesa dependency is skipped.
    # `--force-depends` leaves libglx0's Mesa dependency unmet in dpkg's database, and apt refuses
    # every later install/purge/autoremove while one is unmet: this unpack MUST be the last package
    # operation of this RUN (the GPU base installs python3.14 and curl above through apt).
    (cd /tmp && apt-get download libgl1 libglvnd0 libglx0 \
        && dpkg --force-depends -i libgl1_*.deb libglvnd0_*.deb libglx0_*.deb \
        && rm -f libgl1_*.deb libglvnd0_*.deb libglx0_*.deb); \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# The entrypoint's runtime `pip install` of the extras resolves fasttext-predict to the wheel built above.
ENV PIP_FIND_LINKS=/opt/wheelhouse

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
# CUDA 13 build (`+cu130`, with the CUDA 13 / cuDNN 9 wheels it depends on).
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
# For the GPU variant, scripts/check-gpu-cuda-majors.sh fails the build unless
# torch and the base agree on the CUDA major (rationale in its header).
RUN set -eux; \
    SETUPTOOLS_SCM_PRETEND_VERSION="${RELEASE_VERSION}+docker" \
        /usr/local/bin/python3 -m pip install --no-cache-dir .; \
    # Installed packages ship their own test suites (pandas, scipy, numpy, sympy, torch/test: ~240 MB)
    # that nothing imports at runtime. Pruned here, in the install layer: a later RUN would not shrink it.
    for lib_dir in $(/usr/local/bin/python3 -c 'import sysconfig; print(" ".join({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")}))'); do \
        find "${lib_dir}" -depth -type d \( -name tests -o -name test \) -exec rm -rf {} +; \
    done; \
    # A missing shared library must fail the build, not the first OCR request.
    /usr/local/bin/python3 -c 'import cv2'; \
    case "${BASE_IMAGE}" in \
        *nvidia/cuda*) \
            sh /app/scripts/check-gpu-cuda-majors.sh; \
            ;; \
    esac

# Non-root runtime user. The data directory is pre-created with the
# correct ownership: anonymous-volume runs (`docker run` without `-v`)
# rely on this so UID 1000 can write the auto-generated key file. The
# model cache (<data dir>/models, get_models_dir()) is pre-created too:
# Docker seeds a fresh named volume mounted there (the compose
# `archon-model-cache` opt-in) from this directory, ownership included,
# and a mountpoint the image lacks would come up root-owned.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin appuser; \
    mkdir -p /data/models /pip-packages; \
    chown appuser:appuser /data /data/models /pip-packages

# Model files the document parser would otherwise download on its first PDF or image: docling's layout and
# table-structure models, about 0.5 GB. Fetched on use they sat inside `[ingest].parse_timeout_seconds` (300 s)
# and needed the network. `scripts/prefetch-parse-models.py` runs the real parser, so it fetches exactly what
# docling needs and fails the build if a PDF or scanned image cannot be parsed. `HF_HUB_CACHE` moves only the
# Hugging Face hub cache off the data volume (embedder, reranker and graph weights keep using
# `<data dir>/models`, and the Hub's xet chunk cache stays under `HOME`); the files are owned by appuser, so a
# root filesystem mounted read-only still reads them. RapidOCR's checkpoints ship inside its wheel.
ENV HF_HUB_CACHE=/opt/hf-cache
RUN set -eux; \
    mkdir -p "${HF_HUB_CACHE}"; \
    /usr/local/bin/python3 /app/scripts/prefetch-parse-models.py; \
    chown -R appuser:appuser "${HF_HUB_CACHE}"

# Runtime configuration. `ARCHON_SEARCH_DATA_DIR` redirects every runtime
# path (db, logs, telemetry, key file, jobs, model weights under `models/`,
# ingest history, default `archon-search.toml`) onto the mounted volume.
# `ARCHON_SEARCH_CONTAINER=1` tells
# `configure_logging()` to attach a StreamHandler(sys.stderr) so
# `docker logs` captures application logs.
ENV ARCHON_SEARCH_DATA_DIR=/data \
    ARCHON_SEARCH_CONTAINER=1 \
    HOME=/data

COPY scripts/docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER appuser

VOLUME ["/data"]
EXPOSE 8765

# `/ready` is the readiness probe exposed by the FastAPI app. urllib is
# used to avoid pulling curl into the slim base image. The port follows
# `ARCHON_SEARCH_PORT` (unset or empty means the default, 8765). Exit non-zero
# on any error so the orchestrator can mark the container unhealthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://localhost:%s/ready' % (os.environ.get('ARCHON_SEARCH_PORT') or '8765'))" || exit 1

# tini reaps zombies and forwards SIGTERM/SIGINT to uvicorn so the server
# exits cleanly during `docker stop` (within the compose `stop_grace_period`).
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
CMD ["archon-search", "serve"]
