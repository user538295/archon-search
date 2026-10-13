**Purpose**: Run `archon-search` from the published Docker image — `docker run`, `docker compose`, and the build-arg matrix for CPU/GPU images.
**Audience**: End users / operators who want a portable, reproducible deployment unit.
**Status**: Stable
**Last reviewed**: 2026-10-10 / **Next review**: 2027-10-10

# Running with Docker

The Docker image is the only deployment unit that does not require a host-level service install. The container runs `archon-search serve` (see [`40_running_the_server.md`](40_running_the_server.md)) in the foreground, binds to `0.0.0.0:8765` inside the container, persists all runtime state under `/data`, and writes structured logs to stderr so `docker logs` works.

## Principles

1. **One container, one volume.** A single mounted volume at `/data` holds the LanceDB index, logs, telemetry JSONL, the API key file, the jobs file, the fastembed model cache, and the ingest history. There is no separate state directory to manage.
2. **Env vars first, TOML optional.** Every required deployment knob is reachable via an environment variable — `ARCHON_SEARCH_API_KEY`, `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_DATA_DIR`. The TOML file is optional and is only read when you explicitly mount one and point `ARCHON_SEARCH_CONFIG` at it.
3. **Stderr is the only log sink by default.** `ARCHON_SEARCH_CONTAINER=1` is baked into the image; `logging_setup.configure_logging()` attaches a `StreamHandler(sys.stderr)` so `docker logs` (and Kubernetes log collectors) capture every line.
4. **Non-root by default.** The image runs as UID 1000 (`appuser`). `/data` is pre-`chown`ed so anonymous-volume runs (`docker run` without `-v`) work. If you bind-mount a host directory, make sure UID 1000 can write to it (`chown 1000:1000 /path/to/data` or `chmod 0777`).
5. **Single writer.** LanceDB is single-writer per `db_path`. Do not mount the same volume into more than one running container. This includes the `/pip-packages` volume: every container's entrypoint may replace that volume's content when it starts, so give each running container its own.
6. **Optional extras are installed at runtime, not bake-time.** The base image ships only the core package. For the optional feature sets (`graph`, `code`, `multilingual`) the entrypoint script installs only the packages they add to the image into a `/pip-packages` named volume — on first start, and again whenever `ARCHON_EXTRAS` or the image's installed packages change — so the image stays lean and the extras are cached across restarts.

## Entrypoint and runtime extras installation

The container entrypoint is `scripts/docker-entrypoint.sh` (copied to `/entrypoint.sh` in the image) and runs under `tini` as PID 1. Before handing off to `archon-search serve`, it:

1. Reads `ARCHON_EXTRAS` (default `graph,code,multilingual`; empty string = core-only, no install).
2. Checks that every name in `ARCHON_EXTRAS` is an extra of the image's archon-search. Names are compared PEP 685-normalised, so `rag_fusion` matches `rag-fusion`. An unknown name, such as a typo, logs `ERROR: unknown extra(s) in ARCHON_EXTRAS: <names>; valid extras: <names>`; an empty entry (a stray or doubled comma such as `code,`, or a blank value such as `" "`) logs `ERROR: empty entry in ARCHON_EXTRAS (stray or doubled comma, or a blank value); valid extras: <names>`. Either way the container exits with a non-zero code before anything is installed.
3. Builds a fingerprint from the `ARCHON_EXTRAS` value, the Python version (`sys.version`) and the image's own package list (`pip list --format=freeze`, which includes the archon-search version). If the stamp file `/pip-packages/.extras-installed` holds the same fingerprint, the install is skipped.
4. Otherwise resolves `archon-search[${ARCHON_EXTRAS}]==<image version>` against the image's own environment (`python3 -I -m pip install --dry-run --report`) and installs exactly the artifacts that resolution chose, one `<name> @ <url>` requirement per package, with `python3 -I -m pip install --no-cache-dir --no-deps --target /pip-packages/.staging`. It makes up to 3 attempts, each into an emptied `.staging` directory. Only after an attempt succeeds does it remove the old stamp and every other entry of the volume root, move the staged packages into the root, and write the new stamp last, so an interrupted install is redone on the next start. If all 3 attempts fail, the container exits with a non-zero code and the volume keeps its previous packages and stamp. During a reinstall (an upgrade, a rebuilt image, or a changed `ARCHON_EXTRAS`) the volume therefore holds the previous packages and the staged new ones at the same time, so `/pip-packages` needs free space for both sets at once; without it the install fails and the volume keeps the previous packages.
   On the `:gpu` image it leaves out the CPU `onnxruntime` that the resolution adds back (fastembed and magika, in the image's base closure, require `onnxruntime` by name, as does gliner 0.2.26–0.2.28 of the `graph` extra — 0.2.29 only through its `onnx` extra — and the image replaced it with `onnxruntime-gpu`, which ships the same `onnxruntime` import package) and logs both versions: `Leaving out onnxruntime==<resolved version>: the image's onnxruntime-gpu <image version> replaces it (same onnxruntime import package).` The two versions can differ, and the extras' requirements on `onnxruntime` are not re-checked against the GPU build: the image's `onnxruntime-gpu` stands in for whichever `onnxruntime` version the resolution picked. A CPU copy in `/pip-packages`, first on `PYTHONPATH`, would shadow the image's GPU build (`scripts/docker-entrypoint.sh`, `print_image_onnxruntime_gpu_version`).
5. Prepends `/pip-packages` to `PYTHONPATH` and execs the CMD (`archon-search serve`).

The volume never holds archon-search itself, so the container always runs the image's own code. A package the image already provides at a version the extras accept is not copied either. A base package lands in the volume only when an extra requires a different version — for example, the `code` extra pins `tree-sitter<0.26` (`pyproject.toml`) while the image's base closure can ship a newer tree-sitter (0.26 or later), so a 0.25.x tree-sitter goes into the volume, the same way `pip install "archon-search[code]"` resolves it in a plain environment.

There is **no model-provisioning step**. The entrypoint installs Python packages and nothing else: the graph extra brings the `gliner` library, never its checkpoint. See [Graph extraction in a container](#graph-extraction-in-a-container) below for what that means if you turn `[graph].enabled` on.

**First start** triggers a pip install whose duration is network-bound. It downloads only the packages the extras add to the image, not the full dependency set; but the image bakes `PIP_NO_CACHE_DIR=1`, so pip keeps no download cache anywhere and each install, including every retry attempt, downloads those packages again. Every later reinstall also needs free space on `/pip-packages` for the previous and the new packages at once (step 4 above). The `HEALTHCHECK` allows up to 10 minutes (600s start-period) before it counts failures. Later starts with the same image and `ARCHON_EXTRAS` skip the install (stamp matches). Mount `/pip-packages` as a named volume to persist the install across container recreates:

```bash
docker volume create archon-search-packages
docker run -d \
  --name archon-search \
  -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
  -v archon-search-data:/data \
  -v archon-search-packages:/pip-packages \
  -p 8765:8765 \
  ghcr.io/user538295/archon-search:latest
```

**Upgrading the image.** Keep the same `/pip-packages` volume; you do not need to remove it. A new archon-search version always changes the fingerprint (the package list includes it), as does any other change to the image's Python version or installed packages. On the first start of such an image the entrypoint reinstalls the extras against the new image and then replaces the volume's content, including any archon-search copy that an older image's entrypoint left there. Until that reinstall succeeds the volume holds the previous image's packages and the staged new ones side by side, so it needs free space for both. If that install fails (for example, because the network is down or the volume is full), the container exits and the volume keeps the previous image's packages and stamp, so a rollback to the previous image starts without reinstalling. To confirm the container runs the new code, check that `/health` reports the new version.

To skip extras entirely (lean core-only deployment), pass an empty value:

```bash
docker run ... -e ARCHON_EXTRAS="" ...
```

When `ARCHON_EXTRAS` is set to an empty string, the entrypoint skips the pip install block, does not add `/pip-packages` to `PYTHONPATH`, and proceeds directly to exec. Note: if `ARCHON_EXTRAS` is **unset** (not passed at all), the default `graph,code,multilingual` applies.

## Image variants

The CPU / GPU variant is chosen at **build time** via the `BASE_IMAGE` build-arg (see the `Dockerfile` header); the release workflow publishes both variants to GHCR under these tags:

| Tag | Base image | Use when |
| --- | --- | --- |
| `:latest`, `:<version>` | `python:3.12-slim` | Any host without an NVIDIA GPU. CPU inference only. |
| `:gpu`, `:<version>-gpu` | `nvidia/cuda:13.0.3-base-ubuntu22.04` (amd64 only) | NVIDIA host with driver 580 or later and the NVIDIA Container Toolkit; start it with `--gpus all`. Uses `onnxruntime-gpu` instead of CPU `onnxruntime` (fastembed 0.9.0, the version verified on 2026-10-10, ships no `[gpu]` extra, so the GPU image swaps it manually). See [Running the GPU image](#running-the-gpu-image). |

The `:latest` CPU image ships the **CPU-only** PyTorch build: the image's pip configuration (`/etc/pip.conf`) adds the PyTorch CPU wheel index, so pip installs `torch==<ver>+cpu`. PDF and image-OCR parsing (docling) still works, but pulls no NVIDIA CUDA runtime, so the image carries no GPU libraries it cannot execute. For GPU-accelerated docling in a container, use the `:gpu` image.

Both images embed the source commit as `org.opencontainers.image.revision`. Inspect a running tag with:

```bash
docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  ghcr.io/user538295/archon-search:gpu
```

A failed GPU build does **not** delete the previous floating `:gpu` tag — operators pulling `:gpu` may receive an older successful build. Read the release notes for the version you expect, and confirm the SHA above matches.

### Running the GPU image

The `:gpu` image needs two things on the host: NVIDIA driver 580 or later, because CUDA 13.x requires it ([CUDA 13.0 release notes](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html), Table 2; check with `nvidia-smi`), and the NVIDIA Container Toolkit. Start the container with `--gpus all`; without it the container starts on any host and everything runs on CPU.

```bash
docker run -d \
  --name archon-search \
  --gpus all \
  -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
  -v archon-search-data:/data \
  -v archon-search-packages:/pip-packages \
  -p 8765:8765 \
  ghcr.io/user538295/archon-search:gpu
```

**A driver older than 580 stops the container at `docker run --gpus`.** The base image sets `NVIDIA_REQUIRE_CUDA=cuda>=13.0 …`, so the toolkit refuses to start it with `nvidia-container-cli: requirement error: unsatisfied condition: cuda>=13.0, please update your driver to a newer version, or use an earlier cuda container` (message format: [nvidia-container-toolkit#940](https://github.com/NVIDIA/nvidia-container-toolkit/issues/940)). The same variable also accepts data-center and professional GPU brands (Tesla, GRID, Quadro, NVIDIA RTX and others; not GeForce or Titan) on the 535, 550, 565, 570 and 575 driver branches — its space-separated conditions are ORed ([nvidia-container-runtime README](https://github.com/NVIDIA/nvidia-container-toolkit/blob/main/cmd/nvidia-container-runtime/README.md)) — for CUDA forward compatibility through the image's `cuda-compat-13-0` package. That forward-compatibility path has not been tested with this image.

**How embedding and reranking land on CUDA.** The container leaves `[database].providers` at its default `[]` (`archon_search/config.py:260`). With an empty list archon-search passes no provider list to fastembed (`archon_search/onnx_providers.py`, `select_onnx_providers`), and fastembed 0.9.0 (the version verified on 2026-10-10) then picks `CUDAExecutionProvider` whenever the installed ONNX Runtime offers it (`fastembed/common/onnx_model.py:101-107`), which `onnxruntime-gpu` does. If the CUDA provider still fails to load when a session is created, fastembed runs that model on CPU and only emits `RuntimeWarning: Attempt to set CUDAExecutionProvider failed. …` on stderr; `GET /ready` does not report it. If the GPU stays idle, look for that line in `docker logs`.

**One CUDA stack, from pip wheels.** PyPI's Linux torch (a CUDA 13.0 build) and `onnxruntime-gpu[cuda,cudnn]` 1.27 or later (CUDA 13.0 and cuDNN 9, [ONNX Runtime CUDA provider requirements](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)) share one set of CUDA 13 / cuDNN 9 `nvidia-*` wheels; the `-base` image adds only the CUDA runtime and environment. The 2026-10-10 verification build resolved torch 2.14.1 and `onnxruntime-gpu` 1.31.0; neither is pinned, so a later build installs whatever PyPI then offers. The build registers the wheels' `nvidia/*/lib` directories with the dynamic loader (`/etc/ld.so.conf.d/archon-search-cuda-wheels.conf`, then `ldconfig`), so the CUDA provider finds them without `onnxruntime.preload_dlls()`, and it fails unless `onnxruntime-gpu`, torch and the base image's `CUDA_VERSION` have the same CUDA major (`scripts/check-gpu-cuda-majors.sh`, run by the `Dockerfile`; the opt-in tests also run it against a mismatched `CUDA_VERSION` to check that it fails): when PyPI moves torch or `onnxruntime-gpu` to a new CUDA major, the GPU build fails, by design, until the base tag is bumped. The base is not `13.0.3-cudnn-runtime` because that image's system cuDNN is 9.14.0.64 ([its Dockerfile](https://gitlab.com/nvidia/container-images/cuda/-/raw/master/dist/13.0.3/ubuntu2204/runtime/cudnn/Dockerfile)), older than the `nvidia-cudnn-cu13==9.24.0.43` torch 2.14.1 requires ([PyPI](https://pypi.org/pypi/torch/2.14.1/json)). Two `libcudnn.so.9` copies in one process means the first one loaded serves both, and torch refuses a runtime cuDNN whose minor version is older than the one it was compiled against (`torch/backends/cudnn/__init__.py`); that torch 2.14.1 is compiled against exactly 9.24 is inferred from its pin.

**What is verified.** A local `--platform linux/amd64` build without a GPU (2026-10-10): every library `libonnxruntime_providers_cuda.so` links against resolves, except `libcuda.so.1`, which the host driver provides at run time; every library ORT's `preload_dlls` would load opens by soname without it, and `libcudnn.so.9` loads from the one copy, in the `nvidia/cudnn/lib` wheel directory; ONNX Runtime lists `CUDAExecutionProvider`; no CPU `onnxruntime` is installed, in the image or in `/pip-packages`. Inference on a real NVIDIA GPU has not been run. The opt-in container tests check the same points (no CI workflow runs them; `release.sh`'s docker lane runs them before a release tag):

```bash
ARCHON_SEARCH_RUN_DOCKER_SMOKE=1 uv run pytest tests/test_docker_gpu_image.py \
  --no-cov -o addopts= --strict-markers --strict-config -n0 -m docker
```

**Upgrading from an earlier `:gpu` image.** Earlier `:gpu` images (base `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04`) installed the newest `onnxruntime-gpu`, a CUDA 13 build from 1.27 on, and so ran embedding and reranking on CPU even with `--gpus all`: a pre-fix build with `onnxruntime-gpu` 1.31.0 links its CUDA provider against `libcublasLt.so.13`, `libcublas.so.13` and `libcudart.so.13`, which the CUDA 12.1 base does not have. Their entrypoint also put the CPU `onnxruntime` into `/pip-packages`, where it shadowed `onnxruntime-gpu`. Before you pull the new image, check that the host driver is 580 or later. Keep the `/pip-packages` volume: the new image's package list changes the fingerprint, so its first start reinstalls the extras and replaces the volume's content, the CPU `onnxruntime` included (see **Upgrading the image** above). Images built `FROM` the `:gpu` image no longer find system CUDA 12.1 or cuDNN 8 libraries. Locally, the new image takes 12.6 GB on disk (4.15 GB content) against 17.6 GB (6.16 GB) before (`docker image ls`).

## `docker run` quickstart

Ephemeral — key regenerates every restart, no persistence:

```bash
docker run --rm -p 8765:8765 ghcr.io/user538295/archon-search:latest
```

Production — supply the key via env, persist runtime state to a named volume:

```bash
docker run -d \
  --name archon-search \
  -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
  -v archon-search-data:/data \
  -p 8765:8765 \
  ghcr.io/user538295/archon-search:latest
```

Smoke-test from the host:

```bash
# readiness — 503 until storage is connected, the startup model warm-up and the background model validation finished, and the startup sync finished;
# up to 600 attempts a second apart (each capped at 5 s), so at least the image's 600s HEALTHCHECK start-period
for _ in $(seq 1 600); do curl -sf --max-time 5 http://127.0.0.1:8765/ready > /dev/null && break; sleep 1; done
curl -s http://127.0.0.1:8765/ready    # prints the readiness body (empty if the server isn't listening yet); a first start with the extras install may need the loop rerun
curl http://127.0.0.1:8765/health      # liveness — always 200 when the process is up
curl -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/status
```

`/health` answering `200` does not mean `/ready` will. On every start — whatever `[database].eager_load_embedders` is set to — the server loads the default embedding model and the configured reranker cross-encoder in a background warm-up (`eager_load_embedders = true` only widens it to every per-collection model), and `/ready` answers `503` with `{"ready": false, "checks": {"storage": "ok", "models": "pending", "sync": "ok"}}` until that warm-up and the background model validation finish. Models are downloaded under `/data` when not already cached there, so a container without a persistent `/data` volume downloads them in every new container. In one example run (a fresh container of image 26.10.2181, 2026-10-12), `/health` answered `200` at 34 s and `/ready` stayed `503` (`models: "pending"`) until 58 s; a first start that runs the extras install (see "Entrypoint and runtime extras installation" above) can take several minutes longer. Poll `/ready` as above rather than curling it once.

The image declares a `HEALTHCHECK` that polls `/ready` every 15s after a 600s start-period. The generous start-period accommodates the network-bound first-start extras install (see "Entrypoint and runtime extras installation" above); later starts with the same image and extras skip the install (stamp matches), but the start-period is fixed at image level. `docker ps` shows `(healthy)` once the storage layer is up, the startup model warm-up (run on every start) and the background model validation have finished (or warm-up failed, which reports `checks.models: "fail"` at once), and the server's own startup collection sync has finished.

**First-boot corpus sync can exceed the 600s start-period.** `/ready` is gated on that startup sync unconditionally, independently of the model warm-up gate. The sync itself is bounded to 1800s (`_STARTUP_SYNC_TIMEOUT_SECONDS`): a wedged or oversized sync is force-marked `checks.sync: "fail"` at that point and `/ready` recovers to `200`, so it cannot hang forever. That bound is well above the default `start-period=600s` + `interval=15s --retries=3` (~645s of grace), so seeding a large corpus on first boot can still make `docker ps` report the container **unhealthy** well before the sync finishes — and under a restart policy or `depends_on: service_healthy` (plain `docker run`/`docker-compose` does not restart on unhealthy by itself), an orchestrator that restarts unhealthy containers may restart it somewhere in that ~645s–1800s window, discarding the in-flight attempt. Because the sync is incremental — already-indexed files are skipped — a restarted attempt still makes progress, so successive boots converge even under repeated restarts, but avoiding the restarts entirely is still preferable. If your first-boot corpus is large: raise `--start-period` (or the compose/Kubernetes equivalent) past the expected sync duration, or pre-seed the mounted volume with an already-synced index before wiring the container into anything that restarts on unhealthy. `curl /ready | jq .checks.sync` reads `"pending"` while this is the cause, or `"fail"` if the 1800s bound was hit; `docker logs` also shows `"startup sync"` log lines while it runs. This restart-loop risk only applies once at least one collection is configured (`[collections]` or `pinned_collections` non-empty) — the startup sync task is never created with an empty config, so the bare `docker run` quickstart above never hits `checks.sync: "pending"` and always sees `"ok"`. The task is likewise not created when the crash-loop guard fires (the previous container died mid-ingest, so loading the job store rewrote an ingest-family job to `FAILED / "process_restart"`) **or** a sticky suppression sentinel file (`archon-search-sync-suppressed.json`, under the data dir volume) already exists from an earlier container start: `checks.sync` reads `"warn"`, `/ready` answers `200` immediately, `docker ps` reports `(healthy)`, and `GET /status` reports `sync_result: "suppressed"`. Suppression **survives container restarts** — it keeps firing on every restart, not just the one immediately after the crash, because the sentinel lives on the mounted data volume — the index stays as the crash left it until an operator runs `POST /sync` (or `archon-search sync`), whose clean completion deletes the sentinel and moves `sync_result` to `"done"` and `checks.sync` back to `"ok"`.

## `docker compose`

The shipped [`docker-compose.yml`](../../docker-compose.yml) declares three sibling services — `archon-dev`, `archon-test`, `archon-prod` — each with its own named volume and host port. The compose file uses variable substitution for the image (`ARCHON_SEARCH_IMAGE`), so you can override it locally:

```bash
# .env (next to docker-compose.yml; see .env.example)
ARCHON_SEARCH_IMAGE=ghcr.io/user538295/archon-search:25.6.1   # pin a release tag
# ARCHON_SEARCH_IMAGE=archon-search:latest                    # or a locally-built image
ARCHON_SEARCH_API_KEY=replace-me-with-a-64-char-hex-string
```

```bash
docker compose up archon-dev
docker compose down archon-dev
```

| Service | Host port | Volume | Restart policy |
| --- | --- | --- | --- |
| `archon-dev` | `18765` | `archon-dev-data` | none |
| `archon-test` | `18766` | `archon-test-data` | none |
| `archon-prod` | `8765` | `archon-prod-data` | `unless-stopped` |

Each service has `stop_grace_period: 30s` so `docker compose down` lets uvicorn drain in-flight HTTP requests before the runtime kills it.

> **Multi-instance warning:** `docker-compose.yml` interpolates `ARCHON_SEARCH_API_KEY` into *every* service. Setting it in `.env` silently gives all three the same key. Leave it unset for multi-instance runs and let each instance auto-generate its own key from its own volume — see [`150_multi_instance_setup.md`](150_multi_instance_setup.md).

### Sharing the fastembed model cache

By default each service downloads its own fastembed model weights into its own `/data/fastembed-cache`, on its own data volume. To share one copy across services and download the several hundred MB once instead of once per data volume, follow the three-step uncomment procedure marked in `docker-compose.yml`:

1. Uncomment `FASTEMBED_CACHE_PATH: /data/fastembed-cache` in the `environment:` block of every service that should share it.
2. Uncomment `- archon-model-cache:/data/fastembed-cache` in the `volumes:` block of those services.
3. Uncomment the `archon-model-cache:` declaration at the bottom of the file under `volumes:`.

Step 1 only restates the value the image already bakes in; steps 2 and 3 must be uncommented together — compose rejects the config otherwise (`undefined volume`).

The image pre-creates `/data/fastembed-cache` owned by the non-root server user (UID 1000), and Docker seeds a fresh named volume from that directory, so a new `archon-model-cache` volume is writable. A volume first created from an image that lacked the directory (26.10.2181 and earlier) is root-owned, and the server cannot download weights into it; fix it once with `docker run --rm -u 0 --entrypoint chown -v archon-model-cache:/c <image> 1000:1000 /c` (compose prefixes the volume name with the project name — check `docker volume ls`).

> **`archon-dev` keeps its host cache while `docker-compose.override.yml` is loaded.** `docker compose` reads the override automatically unless `-f` is given, and merges `volumes:` by container target, so the override's `~/.cache/fastembed:/data/fastembed-cache` bind replaces archon-dev's `archon-model-cache` mount. The shared volume then reaches `archon-test` and `archon-prod` only; archon-dev joins it only with `docker compose -f docker-compose.yml`.

## Environment variables

The container reads the same env vars as a host-installed server, plus several that are pre-set in the image:

| Variable | Default in container | Effect |
| --- | --- | --- |
| `ARCHON_SEARCH_API_KEY` | unset | Bearer token. If unset and no `/data/.search.env` exists, the server auto-generates one on startup. **Without a persistent volume this regenerates every restart.** |
| `ARCHON_SEARCH_HOST` | `0.0.0.0` (set by `serve`) | Bind address. The `serve` subcommand defaults host to `0.0.0.0`; explicit env or TOML still wins. |
| `ARCHON_SEARCH_PORT` | `8765` | Bind port. 1–65535. |
| `ARCHON_SEARCH_DATA_DIR` | `/data` (baked into image) | Root of every runtime path: `db_path` (`/data/search`), `log_file` (`/data/logs/archon-search.log`), `telemetry.log_dir` (`/data/search-logs`), key file (`/data/.search.env`), managed key store (`/data/keys.json`), jobs file (`/data/archon-search-jobs.json`), sync-suppression sentinel (`/data/archon-search-sync-suppressed.json`, present only while the startup-sync crash-loop guard is suppressed), fasttext models (`/data/models`), ingest history (`/data/history/sessions`). One env var relocates the entire runtime tree — do not change it unless you also remount the volume. |
| `ARCHON_SEARCH_CONTAINER` | `1` (baked into image) | Adds `StreamHandler(sys.stderr)` to the `archon_search` logger so `docker logs` captures output. |
| `FASTEMBED_CACHE_PATH` | `/data/fastembed-cache` (baked into image) | Keeps fastembed-downloaded model weights (embedder + reranker) in their own directory on the mounted volume, apart from `/data/models`. archon-search passes it as fastembed's `cache_dir` (`paths.get_fastembed_cache_dir()`); an empty value counts as unset. Unset, the weights go to `$ARCHON_SEARCH_DATA_DIR/models`. |
| `ARCHON_SEARCH_KEY_FILE` | unset | Overrides the key file path. Takes precedence over `ARCHON_SEARCH_DATA_DIR` for the key file only. |
| `ARCHON_SEARCH_CONFIG` | unset | Points at a TOML config file. Optional — when unset, the default path is `~/.archon-search/archon-search.toml`, which resolves to `/data/.archon-search/archon-search.toml` inside the container (because `HOME=/data`), so `collection add/remove` already writes to the mounted volume. Set explicitly (e.g. `ARCHON_SEARCH_CONFIG=/data/archon-search.toml`) only if you need a specific location. |
| `ARCHON_EXTRAS` | `graph,code,multilingual` | Comma-separated list of optional package extras; the entrypoint installs the packages they add to the image into `/pip-packages` and puts that directory on `PYTHONPATH`. Set to `""` to skip extras and run core-only (`/pip-packages` is then not on `PYTHONPATH`). A name that is not an archon-search extra, or an empty entry such as the one a trailing comma leaves, stops the container with an error that lists the valid extras. Change the value, or start an image with a different Python version or package list, and the entrypoint re-installs and replaces the volume's content on the next start (fingerprint stamp). |
| `HOME` | `/data` (baked into image) | Required so pip operations inside the container write to the persistent volume rather than the ephemeral layer. Do not override unless you also relocate `/data`. |

## Persistence layout

When `/data` is mounted, the volume looks like this after a few requests:

```
/data
├── .search.env                  # API key, mode 0600
├── keys.json                    # Managed key store, mode 0600; created on first key create/rotate
├── archon-search-jobs.json      # JobStore, atomic-rename writes
├── archon-search-sync-suppressed.json  # present only while the startup-sync crash-loop guard is suppressed
├── search/                      # LanceDB index
├── logs/
│   └── archon-search.log        # only when [logging].log_file is non-empty
├── search-logs/
│   └── YYYY-MM-DD.jsonl         # telemetry, when [telemetry].enabled=true
├── models/
│   ├── lid.176.ftz              # fasttext language detector (multilingual only)
│   └── graph/                   # GLiNER prose-extraction checkpoint cache — only if you
│                                # put it there yourself (see below); nothing in the
│                                # container provisions it
├── fastembed-cache/             # fastembed model weights
└── history/
    └── sessions/                # history sessions directory
```

> **Upgrading from 26.9.2101 – 26.10.2181 (CPU and `:gpu` images alike):** those images ignored the baked `FASTEMBED_CACHE_PATH=/data/fastembed-cache` and kept the fastembed weights in `/data/models/` (S493). Current images honour it again, so the weights move to `/data/fastembed-cache/`: the first start re-downloads them once. Afterwards the old fastembed entries under `/data/models/` can be deleted: the `models--*` directories plus huggingface_hub's cache-wide `blobs/` store (where newer huggingface_hub releases keep the large files those directories link to), its `.huggingface-shared-blobs` marker and `.locks/` — keep `lid.176.ftz` and `graph/`. This applies only to a container running with the image's baked value on a `/data` volume of its own. In `archon-dev` (`docker-compose.override.yml`), `/data/models` is the bind-mounted host `~/.archon-search/models`, which still holds a native install's live fastembed weights — never delete anything there. archon-dev's own fastembed weights now load from `/data/fastembed-cache`, the bind-mounted host `~/.cache/fastembed`, which native installs no longer fill, so its first run after upgrading downloads them there once.

When `/pip-packages` is mounted (recommended), it holds the optional-extras install:

```
/pip-packages
├── .extras-installed            # stamp: fingerprint = ARCHON_EXTRAS value, Python version, image package list
├── .staging/                    # only while an install runs; its packages move into the root once an attempt succeeds, so a reinstall needs free space for the old and the new packages at once
└── <installed packages …>       # only the packages the extras add to the image; never archon-search itself
```

No model weights live here. The graph extra installs the `gliner` library into `/pip-packages`; its checkpoint, if present at all, lives under the data directory at `/data/models/graph/` — see below.

## Graph extraction in a container

`[graph].enabled` defaults to `false`. **A default container runs no graph extraction at all and emits no warning about it** — everything in this section applies only once you have turned the flag on.

If you do turn it on, understand what the container does and does not give you. The `graph` extra makes `gliner` importable, which is what the startup check requires, so the server boots. But **nothing in the container provisions the prose extraction checkpoint**: the install wizard is the only thing that pre-warms it, and the container never runs the wizard. The runtime's own lazy fetch from Hugging Face is the sole remaining path, and where that is unavailable — an air-gapped host, a blocked egress, a transient failure — the load fails. A failed load latches for the life of the process, so from that point on the container runs **permanently degraded: code-symbol extraction only, prose entities and typed relations skipped, one warning per affected ingest** in `IngestResult.warnings`. It never fails an ingest and never returns a 503.

The workaround is to provision on the host and mount the result in. Run `archon-search wizard` on the host (or otherwise let a host-side `gliner` load populate the cache), then bind-mount the host models directory over the container's:

```yaml
volumes:
  - ~/.archon-search/models:/data/models
```

This is exactly what the `archon-dev` service in `docker-compose.override.yml` already does. The container-side path the runtime looks in is `get_graph_models_dir()` — `/data/models/graph/<model>-<revision>/` with `ARCHON_SEARCH_DATA_DIR=/data` — so mounting the whole `models/` tree keeps host and container agreeing on the layout without hardcoding the revision anywhere.

## Development workflow (claude_cli + full extras)

`docker-compose.override.yml` in the repo root defines an `archon-dev` service that wires up a full-featured dev container: multilingual embeddings, graph extraction, HyDE + RAG Fusion via the `claude_cli` provider, and a fastembed model cache bind-mounted from the Mac host.

### Prerequisites

- `~/.cache/fastembed` on the host — archon-dev's fastembed cache (the override sets `FASTEMBED_CACHE_PATH: /data/fastembed-cache` and bind-mounts this directory there). A native archon-search does not fill it (it caches in `~/.archon-search/models`), so the first archon-dev start downloads the weights into it once; copy them from another machine to skip that. Create it before the first start (`mkdir -p ~/.cache/fastembed`): on a Linux host the Docker daemon creates a missing bind-mount source root-owned, and the container's UID 1000 user cannot write into it.
- Claude Code installed on the Mac host and logged in.

### Steps

1. **Start the host-side claude proxy** in a separate terminal:

   ```bash
   python3 scripts/claude-proxy.py
   # claude proxy listening on 127.0.0.1:18766
   ```

   This HTTP server accepts `POST /` from inside the container, runs the real `claude` binary on the Mac host, and streams stdout back. The container mounts `scripts/claude-container-wrapper` at `/usr/local/bin/claude` — a minimal Python shim that POSTs to `http://host.docker.internal:18766`.

2. **Start the dev container:**

   ```bash
   docker compose up archon-dev
   ```

   The override file mounts:
   - `archon-dev-data:/data` — persistent LanceDB index and key file
   - `archon-dev-packages:/pip-packages` — cached optional extras
   - `./archon-search.docker-dev.toml:/config/archon-search.toml:ro` — dev config (full profile, graph enabled, claude_cli HyDE/RAG Fusion)
   - `~/.cache/fastembed:/data/fastembed-cache` — host fastembed model cache (avoids re-downloading ~500 MB after the first run)
   - `~/.archon-search/models:/data/models` — host model cache: the fasttext language detector, and the graph prose-extraction checkpoint if the host has provisioned one

3. **First start:** the entrypoint installs the packages the graph + code + multilingual extras add to the image — a network-bound pip install (longer on a slow uplink; the image allows up to 10 minutes before the healthcheck counts failures). It runs again after a rebuild that changes the image's Python version or installed packages, including the archon-search version. Watch progress with `docker compose logs -f archon-dev`. No model weights are downloaded here; the `~/.archon-search/models:/data/models` mount above is what supplies the graph checkpoint, if your host has one.

4. **Smoke-test:**

   ```bash
   KEY=$(docker compose exec archon-dev cat /data/.search.env | grep -o '[^=]*$')
   curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8765/status
   ```

The dev config (`archon-search.docker-dev.toml`) enables:
- `[database] profile = "max"` with `multilingual-e5-large` embeddings and `jina-reranker-v2-base-multilingual`
- `[graph] enabled = true`
- `[hyde] provider = "claude_cli" model = "haiku"`
- `[rag_fusion] provider = "claude_cli" model = "haiku"`

## CLI behavior in Docker

When `ARCHON_SEARCH_CONTAINER=1` is set (baked into the image), the CLI adapts its behavior to the container context.

### What works

All commands that contact the running server via HTTP work identically inside the container:

| Command | Notes |
|---|---|
| `archon-search --help`, `--version` | Offline; always exit 0 |
| `archon-search config show` | Reads local TOML; no server required |
| `archon-search status --api-url <url> --api-key <key>` | HTTP fallback — shows telemetry from `GET /status` |
| `archon-search key list --api-url <url> --api-key <key>` | HTTP |
| `archon-search collection list --api-url <url> --api-key <key>` | Proxies `GET /collections/` |
| `archon-search collection info <name> --api-url <url> --api-key <key>` | HTTP |
| `archon-search collection add <path> --wait --api-url <url> --api-key <key>` | HTTP; use `--wait` to confirm job completion |
| `archon-search ingest --path <file> --collection <name> --wait --api-url <url> --api-key <key>` | HTTP; use `--wait` to confirm job completion |
| `archon-search jobs status <id> --api-url <url> --api-key <key>` | HTTP |
| `archon-search maintenance run --api-url <url> --api-key <key>` | HTTP |

### Service management commands (clean error, not a traceback)

`start`, `stop`, `install`, and `uninstall` manage the host-level service (systemd/launchd) and are meaningless in a container. In container mode they emit a single actionable message and exit 1 — no Python traceback:

```
Service management is not available in container mode. Use 'archon-search serve' to run the server.
```

### `status` HTTP fallback

`status` has two information sources: the platform service layer (systemctl) and the HTTP `/status` endpoint. In container mode, systemctl is absent and the service-section line (`stopped`) is suppressed. The `_fetch_server_status()` call runs unconditionally — when the server is reachable, the full HTTP telemetry is shown; when unreachable, the command exits 0 silently (no traceback).

Example (server running inside the container):

```bash
archon-search status --api-url http://127.0.0.1:8765 --api-key $ARCHON_SEARCH_API_KEY
# Telemetry: enabled
#   hash_doc_ids_enabled: False
# Collections: 2
#   smoke (1 document)
# ...
```

The exact fields rendered depend on the server's telemetry config and the ingested corpus. When no collections are present, the `Collections:` line is omitted entirely.

## TLS termination

The container speaks plaintext HTTP only. Put a reverse proxy (nginx, Caddy, Traefik) in front of it for any non-loopback exposure. See [`../OperatorGuide/10_deployment_topologies.md`](../OperatorGuide/10_deployment_topologies.md) for the reverse-proxy patterns.

## Known limitations

- **`GET /ready` does not gate readiness on the model-validation outcome** — a `checks.models` value of `ok`/`warn`/`fail` reports the background probe result and does not affect the HTTP status or the `ready` flag. A still-running probe does gate: the container stays unhealthy (503, `checks.models: "pending"`) until it finishes, bounded as listed under [`validation_timeout_seconds`](30_configuration.md#database); a timeout reports `checks.models: "fail"`. It **also** gates on model warm-up, on every configuration: the container stays unhealthy (503, `checks.models: "pending"`) until warm-up finishes. `[database].eager_load_embedders = true` only extends warm-up to every per-collection model; left at its default `false`, the default embedder and the reranker are still warmed before the container reports healthy, so the first `/search` after a healthy start no longer pays the cold model-load tax (S279). Only a per-collection model outside warm-up's scope is still loaded lazily.
- **`GET /ready` also gates on the startup collection sync, unconditionally** — the container stays unhealthy (503, `checks.sync: "pending"`) until the lifespan's own startup `collection_sync.sync()` over all configured collections finishes (see "First-boot corpus sync can exceed the 600s start-period" under "`docker run` quickstart" above for the restart-loop risk on a large first-boot corpus). `POST /sync` (and `archon-search sync`) returns `409` for the same window. The one exception is the crash-loop guard: when the previous run died mid-ingest, or a sticky suppression sentinel from an earlier boot is still present on the data volume, no startup sync task is created at all, so the container reports `(healthy)` immediately with `checks.sync: "warn"` and `sync_result: "suppressed"` — informational, never a 503. Suppression persists across container restarts until a clean `POST /sync` clears the sentinel.
- **In-flight ingest jobs are not awaited on SIGTERM** — the container exits cleanly, but a job in progress is marked `FAILED` on the next start. Tune `stop_grace_period` to your workload.
- **`archon-search collection add` writes to the TOML file via the server** — see `ARCHON_SEARCH_CONFIG` above. `collection remove` also proxies through the server. The default config path (`/data/.archon-search/archon-search.toml`) is already on the mounted volume, so `ARCHON_SEARCH_CONFIG` is optional for collection management.
- **No Apple Silicon / Metal GPU image.** Apple GPUs are not supported in v1.

## Development and testing with Docker

Separate from the production image above, `docker-compose.override.yml` defines two containers — built from `Dockerfile.test`, not the production `Dockerfile` — for running the test suite in a clean Linux environment. They mount your source live and run as a non-root user (uid 1000).

- **`archon-test-runner`** — one-shot. Runs the full suite plus the smoke tests, then exits:

  ```bash
  docker compose build archon-test-runner        # one-time
  docker compose run --rm archon-test-runner      # full suite + smoke
  ```

- **`archon-dev-shell`** — persistent. Start it, shell in to work interactively, stop it when done:

  ```bash
  docker compose up -d archon-dev-shell
  docker compose exec archon-dev-shell bash       # run pytest / serve inside
  docker compose stop archon-dev-shell
  ```

Both share a named venv volume (`archon-docker-venv`), so the one-time install (core + `graph` extra, packages only — no model weights) is paid once and reused across both services and across restarts. Embedding model weights live in a bind mount of your host `~/.cache/fastembed` (`FASTEMBED_CACHE_PATH=/cache/fastembed`): a cache these services own — a native archon-search does not write there — so the first run downloads the weights once and later runs reuse them.

For the full explanation — volume architecture, the two-phase test split, and why the graph extra matters — see [`../docker-test-runner.md`](../docker-test-runner.md).

## Related documents

- [`00_index.md`](00_index.md) — UserManual table of contents and reading order.
- [`40_running_the_server.md`](40_running_the_server.md) — the `serve` subcommand and the rest of the CLI lifecycle.
- [`10_installation.md`](10_installation.md) — host-level install via the wizard, for non-container deployments.
- [`150_multi_instance_setup.md`](150_multi_instance_setup.md) — running a native-service prod instance and a Docker dev-UAT instance side by side on the same machine.
- [`../OperatorGuide/10_deployment_topologies.md`](../OperatorGuide/10_deployment_topologies.md) — reverse-proxy patterns and topology comparison.
- [`../Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) — health/ready surface, runbooks.
</content>
</invoke>
