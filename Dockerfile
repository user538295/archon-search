# syntax=docker/dockerfile:1.7
#
# archon-search container image.
#
# The CPU / GPU variant is selected via the BASE_IMAGE build-arg:
#
#   docker build .                                                          # CPU (default)
#   docker build --build-arg BASE_IMAGE=python:3.12-slim .                   # CPU (explicit)
#   docker build --build-arg BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04 .  # GPU
#
# The CPU base ships Python 3.12 already; the NVIDIA CUDA base ships only the
# CUDA runtime and is Ubuntu-based, so we conditionally install Python 3.12
# from deadsnakes when the base does not provide it. fastembed 0.8.0 does
# not publish a `[gpu]` extra, so the GPU branch swaps the default CPU
# `onnxruntime` for `onnxruntime-gpu` after the base archon-search install.

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# Re-declare ARGs after FROM so they are visible to RUN instructions.
ARG BASE_IMAGE=python:3.12-slim
ARG GIT_COMMIT=unknown

LABEL org.opencontainers.image.source="https://github.com/user538295/archon-search"
LABEL org.opencontainers.image.title="archon-search"
LABEL org.opencontainers.image.description="Hybrid retrieval + routing server (LanceDB + fastembed + reranker)"
LABEL org.opencontainers.image.revision=$GIT_COMMIT

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

# Install archon-search into the system Python. We deliberately do NOT use
# `uv` inside the container: the production image only needs to *run* the
# server, not re-resolve dependencies. `pip install --no-cache-dir .` is
# both smaller and avoids pulling uv into the runtime layer.
#
# For the GPU variant the project's CPU `onnxruntime` is swapped for
# `onnxruntime-gpu` after the base install. fastembed 0.8.0 does not ship
# a `[gpu]` extra (verified against the package metadata) so this is the
# documented manual-swap path.
RUN set -eux; \
    /usr/local/bin/python3 -m pip install --no-cache-dir .; \
    case "${BASE_IMAGE}" in \
        *nvidia/cuda*) \
            /usr/local/bin/python3 -m pip uninstall -y onnxruntime; \
            /usr/local/bin/python3 -m pip install --no-cache-dir onnxruntime-gpu; \
            ;; \
    esac

# Non-root runtime user. The data directory is pre-created with the
# correct ownership: anonymous-volume runs (`docker run` without `-v`)
# rely on this so UID 1000 can write the auto-generated key file.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin appuser; \
    mkdir -p /data; \
    chown appuser:appuser /data

# Runtime configuration. `ARCHON_SEARCH_DATA_DIR` redirects every runtime
# path (db, logs, telemetry, key file, jobs, fasttext models, ingest
# history) onto the mounted volume. `ARCHON_SEARCH_CONTAINER=1` tells
# `configure_logging()` to attach a StreamHandler(sys.stderr) so
# `docker logs` captures application logs. `FASTEMBED_CACHE_PATH` keeps
# fastembed's downloaded model weights on the same persistent volume
# instead of the ephemeral container layer.
ENV ARCHON_SEARCH_DATA_DIR=/data \
    ARCHON_SEARCH_CONTAINER=1 \
    FASTEMBED_CACHE_PATH=/data/fastembed-cache

USER appuser

VOLUME ["/data"]
EXPOSE 8765

# `/ready` is the readiness probe exposed by the FastAPI app. urllib is
# used to avoid pulling curl into the slim base image. Exit non-zero on
# any error so the orchestrator can mark the container unhealthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request, sys; urllib.request.urlopen('http://localhost:8765/ready')" || exit 1

# tini reaps zombies and forwards SIGTERM/SIGINT to uvicorn so the server
# exits cleanly during `docker stop` (within the compose `stop_grace_period`).
ENTRYPOINT ["tini", "--"]
CMD ["archon-search", "serve"]
