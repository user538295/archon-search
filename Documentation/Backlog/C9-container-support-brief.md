# Feature Brief: C9 — Container Support

## Problem
Running archon-search requires host-level service installation (launchd/systemd) and manual path management. There is no portable, reproducible deployment unit — making it difficult to run isolated dev/test/prod environments or hand off a working setup to another operator.

## Goal
A working `docker run` (and `docker compose up`) that starts archon-search fully configured via env vars, persists data on a mounted volume, and produces two image variants: CPU (`:latest`) and NVIDIA GPU (`:gpu`).

## Users & Context
- **Developers** running local dev and test environments alongside each other without interference.
- **Operators** deploying to Linux servers (bare metal or cloud VMs) who want a portable, self-contained unit.
- **CI pipelines** that need a reproducible archon-search instance for integration or benchmark tests.

## Core Flow
1. Operator pulls `archon-search:latest` (CPU) or `archon-search:gpu` (NVIDIA).
2. Operator sets env vars (`ARCHON_SEARCH_API_KEY`, `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_DATA_DIR`) — no config file required for basic usage.
3. Operator mounts a persistent volume at `$ARCHON_SEARCH_DATA_DIR` (default `/data`).
4. Container starts, auto-generates API key if not provided, serves HTTP on the configured port.
5. Operator uses the provided `docker-compose.yml` as a reference for multi-environment setups (dev/test/prod each mount a different named volume and expose a different port).

## In Scope

### Dockerfiles and compose
- `Dockerfile` with a `--build-arg VARIANT=cpu|gpu` (single file, two outputs)
- CPU base: `python:3.12-slim`; GPU base: `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` + Python 3.12 (verify tag exists in NVIDIA Container Registry before wiring the Dockerfile)
  - GPU dependency resolution: `fastembed` bundles its own `onnxruntime` CPU build; installing `onnxruntime-gpu` alongside it causes package conflicts. The correct approach is either `pip install fastembed[gpu]` (if the extra exists in the version in use) or uninstalling the CPU build first. Verify the correct install strategy against the fastembed version pinned in `pyproject.toml` before writing the Dockerfile.
- `docker-compose.yml` with example mounts, env vars, and a comment showing dev/test/prod config swap pattern. The compose file must define three services (dev, test, prod) each with their own named volume — NOT a single service with a shared volume, which violates the LanceDB single-writer constraint.
- `.dockerignore`
- `HEALTHCHECK` instruction in the Dockerfile pointing at `GET /ready` with an appropriate interval and timeout. `/ready` is unauthenticated and checks storage connectivity, making it a better liveness signal than `/health` in a container context.
- Non-root user (`appuser`, UID 1000) in the Dockerfile; document that the mounted volume must be owned by UID 1000 (or use `--user` flag).
- `stop_grace_period: 30s` in `docker-compose.yml` to give the lifespan shutdown (store disconnect, telemetry drain) time to complete before Docker sends SIGKILL; the default 10s is insufficient if active telemetry drain is in progress.
- PID 1 signal handling: use `tini` as the `ENTRYPOINT` (`ENTRYPOINT ["tini", "--"]`) or document that `docker run --init` is required. Without an init process, SIGTERM may not be propagated correctly to the uvicorn process, making the `stop_grace_period` ineffective.

### New `serve` subcommand
- Add a `serve` subcommand (or `run` — to decide) to `archon_search/cli/` that starts uvicorn in the foreground without invoking platform service management; this is the `CMD` entry point for the container. The existing `start` subcommand (launchd/systemd) is not usable inside a container.
- Note: `app.py:run_server()` already implements foreground uvicorn startup; the `serve` CLI subcommand is a thin Click wrapper (~10 lines) that calls this existing function. The naming decision (`serve` vs `run`) is the only open question.

### ARCH-2: config env var overrides (prerequisite)
- Fix **ARCH-2**: add env var overrides for `host` and `port` (`ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`) so the container is fully configurable without a mounted TOML.
- When `ARCHON_SEARCH_CONTAINER=1` is set (or when the new `serve` command is used), `ARCHON_SEARCH_HOST` defaults to `0.0.0.0` — this is the dataclass default for the `serve` command, NOT an override of an explicit `ARCHON_SEARCH_HOST` value. If the operator explicitly sets `ARCHON_SEARCH_HOST=127.0.0.1`, that value takes precedence per the env var precedence rule.

### ARCH-3: relocatable path root (prerequisite)
- Add `ARCHON_SEARCH_DATA_DIR` env var that sets the base path for db, logs, key file, and telemetry. This requires a path-unification refactor as a prerequisite — see ARCH-3 note in Key Decisions below.
- At least 5 runtime-relevant modules contain hardcoded `Path.home() / ".archon-search"` sites that will execute inside the container and must be fixed: `key_manager.py` (KEY_FILE), `jobs/model.py` (JOBS_FILE), `language_detector.py` (FASTTEXT_MODELS_DIR), `cli/ingest.py` (history sessions default path), and `config.py` string defaults. All 5 runtime state paths — db path, log file, telemetry log dir, key file, and ingest history sessions dir — must be redirected through `ARCHON_SEARCH_DATA_DIR` for it to work as a single knob.
- Service/install-only modules (`install.py`, `platform/linux.py`, `platform/macos.py`) also contain `Path.home()` references but are service management code that will never execute inside a container; they are out of scope for ARCH-3.

### Container-mode logging
- Logging is redirected to stderr by adding a `StreamHandler` to the root logger when `ARCHON_SEARCH_CONTAINER=1` is set, rather than relying on `log_file = ""` which may raise an error if the logging module attempts to open an empty path. Note: if ARCH-3 delivers full env-var overrides for all config fields, `ARCHON_SEARCH_CONTAINER` can be dropped in favor of explicitly setting `log_file=` in the Dockerfile `ENV`; retain it for now as an explicit toggle that is easier to document for operators.

### Release pipeline
- Image published to a container registry on tag push (extend `archon-search-release.yml`)
- README section: "Running with Docker"

## Out of Scope
- Apple Silicon (Metal/MPS) GPU support — Metal cannot be passed through to Docker containers; deferred to a future remote-inference feature.
- Remote inference / embedding microservice split — significant architectural change; separate feature.
- Kubernetes manifests, Helm charts — follow-up once the base image is stable.
- Multi-instance / horizontal scaling — LanceDB single-writer constraint makes this non-trivial; deferred.
- Windows container support — platform code raises `NotImplementedError`; pre-existing debt (PLT-1).

## Key Decisions
- **Two image variants, one Dockerfile**: build arg keeps duplication minimal; operators choose CPU or GPU explicitly via tag — no silent fallback that hides misconfiguration.
- **ARCH-2 and ARCH-3 are prerequisites, not part of Docker packaging**: the config-layer refactoring (env var overrides + relocatable path root) should be a separate, independently tested ticket that the Docker packaging ticket depends on. This avoids a PR that is too large to review, and prevents shipping a Docker image with subtle path bugs.
- **ARCH-2 fixed before Docker packaging**: host/port env overrides are required for the container to be usable; shipping without them produces an image that can't change its port without a mounted config file.
- **ARCH-3: relocatable path root requires a path-unification refactor**: `ARCHON_SEARCH_DATA_DIR` does not work as a single knob until all hardcoded `Path.home() / ".archon-search"` sites in runtime-relevant modules are replaced with a centralized accessor. This is a prerequisite that must be completed and tested independently before Docker packaging ships.
- **Env var precedence**: `env var > TOML value > dataclass default`. All new env vars (`ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_DATA_DIR`, `ARCHON_SEARCH_CONTAINER`) follow this order. No other precedence is valid. When `ARCHON_SEARCH_DATA_DIR` is set, it overrides TOML path values; this follows the stated `env var > TOML > dataclass default` precedence.
- **`ARCHON_SEARCH_CONTAINER` semantics**: if both `ARCHON_SEARCH_CONTAINER=1` and an explicit `log_file` value exist, the explicit value wins.
- **No bundled corpus**: image is a clean slate; `docker-compose.yml` shows how to mount data. Keeps the image minimal and avoids stale demo data.
- **GPU = NVIDIA CUDA only**: Apple Silicon GPU is not addressable from Docker; this is documented explicitly so operators don't spend time debugging a platform constraint.

## Edge Cases & Constraints
- **API key on first start**: if `ARCHON_SEARCH_API_KEY` is not set and the volume is empty, key is auto-generated and written to `$ARCHON_SEARCH_DATA_DIR/.search.env`. On container restart the key persists via the volume. Without a persistent volume the key regenerates on every start — document this prominently. Note: `key_manager.py` resolves `KEY_FILE` at import time; `jobs/model.py` resolves `JOBS_FILE` at import time; `language_detector.py` resolves `FASTTEXT_MODELS_DIR` at import time — `ARCHON_SEARCH_DATA_DIR` cannot redirect any of these without restructuring each module to use a lazy accessor. All three must be addressed in the ARCH-3 path-unification prerequisite. Additionally, consider failing loudly (exit 1 with a clear message) rather than silently generating an ephemeral key when no persistent volume is detected and no `ARCHON_SEARCH_API_KEY` is provided — a running-but-unreachable service is worse than a clear startup failure.
- **Container host binding**: the default `host = 127.0.0.1` makes the container unreachable from outside. When running via the `serve` command or with `ARCHON_SEARCH_CONTAINER=1`, `ARCHON_SEARCH_HOST` defaults to `0.0.0.0` (dataclass default for that command path), but an explicit `ARCHON_SEARCH_HOST` env var always takes precedence.
- **LanceDB single-writer**: only one container instance may write to a given `ARCHON_SEARCH_DATA_DIR` at a time. Running two containers against the same volume is undefined behavior — document and do not add a lock guard in this feature.
- **SIGTERM handling**: FastAPI lifespan handlers disconnect the store and drain telemetry. In-flight ingest jobs are not awaited — document as known limitation (existing behavior, not introduced by this feature).
- **Reverse proxy requirement**: the container serves plaintext HTTP; TLS termination is the operator's responsibility. Document in the compose file with a comment.
- **Model weight download at startup**: fastembed and the cross-encoder pull model weights on first run. In a container, these land inside the image layer unless `model_cache_dir` is also mounted. Document the optional volume mount for model caching to avoid re-downloads on every container recreate. Use `GET /ready` as the container readiness probe — it is unauthenticated and checks storage connectivity. Note: `/ready` does not verify model weight availability; a model warmup step or separate probe is needed if operators require search readiness before marking the container ready.

## Test Strategy

### Config env var overrides (ARCH-2)
- Unit tests for `ARCHON_SEARCH_HOST` and `ARCHON_SEARCH_PORT` precedence: env var wins over TOML value, env var wins over dataclass default.
- Invalid values: non-integer port, empty string — must raise a clear validation error.
- Port range validation still applies to env-sourced values (i.e., env var does not bypass existing validation).

### `serve` subcommand
- Unit test that `serve` calls `uvicorn.run` (mocked) with the expected host/port.
- Verify `serve` respects `ARCHON_SEARCH_HOST` and `ARCHON_SEARCH_PORT` env vars.
- Verify `serve` does not invoke platform service management (no launchd/systemd calls).

### `ARCHON_SEARCH_DATA_DIR` path derivation (ARCH-3)
- Unit tests for each of the 4 sub-paths (`db_path`, `log_file`, `telemetry.log_dir`, key file) deriving correctly from `ARCHON_SEARCH_DATA_DIR`.
- `key_manager.KEY_FILE` resolves from `ARCHON_SEARCH_DATA_DIR` when set, from default when unset.
- `jobs/model.JOBS_FILE` resolves from `ARCHON_SEARCH_DATA_DIR` when set.
- `language_detector.FASTTEXT_MODELS_DIR` resolves from `ARCHON_SEARCH_DATA_DIR` when set.
- `cli/ingest.py` default history sessions path resolves from `ARCHON_SEARCH_DATA_DIR` when set.
- Verify no path is computed at module import time (no `Path.home()` at module level).
- Edge cases: empty `ARCHON_SEARCH_DATA_DIR`, `ARCHON_SEARCH_DATA_DIR="/"`, trailing slashes.

### Container detection
- `ARCHON_SEARCH_CONTAINER=1` triggers stderr logging (StreamHandler added to root logger).
- `ARCHON_SEARCH_CONTAINER` unset leaves existing `log_file` behavior untouched.
- Explicit `log_file` in TOML takes precedence over container-detection default.
- `ARCHON_SEARCH_CONTAINER=1` + no explicit `ARCHON_SEARCH_HOST` → host resolves to `0.0.0.0` when using the `serve` command.

### SIGTERM shutdown
- Unit test that SIGTERM triggers the FastAPI lifespan shutdown (store disconnect, telemetry flush).
- Verify process exits 0 after SIGTERM.

### Regression
- Full `load_config()` test with no env vars set must produce identical output compared to a recorded baseline snapshot (`tests/test_config_defaults.py` or equivalent) — assert each field value explicitly, not just `config is not None`. This guards against breaking TOML-only setups.

### Docker smoke test (CI)
- Build CPU image, start in background, wait for readiness from the host, then clean up:
  ```bash
  # Start in background, wait for health, verify from host
  CID=$(docker run -d -e ARCHON_SEARCH_API_KEY=test -p 18765:8765 archon-search:latest)
  for i in $(seq 1 30); do curl -sf http://localhost:18765/ready && break; sleep 1; done
  docker rm -f "$CID"
  ```
  Use host-side `curl` against `GET /ready` (unauthenticated); do not invoke `curl` inside the container (`python:3.12-slim` does not include it).
- Verify the container exits 0 and returns HTTP 200.
- The smoke test must verify that the container can write to `ARCHON_SEARCH_DATA_DIR` as UID 1000 — use `docker run --user 1000 -v /tmp/test-data:/data ...` and verify key file creation succeeds.

### GPU variant
- CI build-only check: `docker build --build-arg VARIANT=gpu` succeeds.
- Import smoke test: `docker run --rm archon-search:gpu python -c "import onnxruntime; assert 'CUDAExecutionProvider' in onnxruntime.get_available_providers()"` (note: requires GPU runner; tag as optional/manual in CI).

## Open Questions
- Which container registry to publish to (GHCR vs Docker Hub)? GHCR is the natural fit given the GitHub Actions release pipeline — confirm before wiring up the release workflow.
- Should the model cache directory (`~/.cache/huggingface` or fastembed equivalent) be an explicit named volume in the compose file, or left as an optional note?
- What should the `serve` subcommand be named — `serve` or `run`?

## Future Iterations
- Remote inference / embedding microservice: run embedder and reranker as a separate service on the host (enables Apple Silicon GPU, Apple Neural Engine, or dedicated inference hardware).
- Kubernetes manifests and Helm chart.
- Multi-instance support once LanceDB write coordination is resolved.
- Apple Silicon GPU passthrough if Docker/macOS adds Metal support in a future version.
- Drop `ARCHON_SEARCH_CONTAINER` once ARCH-3 delivers full per-field env overrides, in favor of explicit `log_file=` in the Dockerfile `ENV`.

## Recommendation
This is the right feature to build now — containerization unblocks reproducible dev/test/prod environments and makes operator onboarding dramatically simpler. The hardest parts are ARCH-2 and ARCH-3 equally: path unification is a prerequisite that touches at least 5 runtime-relevant modules with hardcoded `Path.home()` sites (including import-time constants in `key_manager.py`, `jobs/model.py`, and `language_detector.py` that each require a lazy-accessor refactor), and env var overrides must be threaded through `config.py` carefully without breaking existing TOML-based setups. Do not compromise on either — a container image that can't change its port without a mounted config file is not usable, and one where `ARCHON_SEARCH_DATA_DIR` silently fails to redirect the key file, jobs file, or model cache is not trustworthy. Treat ARCH-2 and ARCH-3 as separate, independently reviewable tickets that the Docker packaging ticket depends on. Everything else (Dockerfile, compose file, release pipeline extension) is straightforward once those two are solved and tested.
