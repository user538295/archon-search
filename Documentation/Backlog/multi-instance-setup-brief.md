# Feature Brief: Multi-Instance Setup (Prod + Dev-UAT)

## Problem
A developer running archon-search locally cannot simultaneously operate a stable production instance and a dev/UAT instance for pre-release testing or e2e live test runs — there is no documented path for this, and the OS service layer supports only one named instance per user.

## Goal
A developer can run prod (native service, GPU-accelerated) and dev-UAT (Docker, version-pinned) side by side on the same machine, each fully isolated, with a step-by-step user manual covering macOS, Linux, and Docker.

## Users & Context
A developer or operator who already has archon-search in production on a workstation or server and needs a second isolated environment — to run e2e live tests against a specific version, validate a release candidate, or reproduce a prod issue without touching prod data.

## Core Flow

1. Install prod as a native OS service (`archon-search install`) using the default data dir and port 8765.
2. Pull the desired dev-UAT image version (e.g. a release candidate tag).
3. Start the dev-UAT container via `docker compose up archon-dev` (port 18765, isolated named volume).
4. Run e2e tests or manual validation against dev-UAT (`http://localhost:18765`).
5. Stop dev-UAT when done (`docker compose stop archon-dev`).
6. Prod continues running undisturbed.

## In Scope
- User manual: installing prod as a native launchd (macOS) or systemd (Linux) service
- User manual: running dev-UAT as the `archon-dev` Docker Compose service
- Running different archon-search versions simultaneously (native prod version vs Docker image tag)
- Port isolation (prod: 8765, dev-UAT: 18765)
- Data directory isolation (native data dir vs Docker named volume)
- API key isolation per instance
- Starting/stopping dev-UAT on demand (not as a persistent service)
- Pointing MCP clients and HTTP clients at the correct instance

## Out of Scope
- Code changes — everything needed (env vars, Dockerfile, docker-compose.yml) already exists
- Named service support (`archon-search install --name`) — no demand signal yet; doc the manual plist workaround if needed
- GPU passthrough for the Docker dev-UAT container — dev-UAT is for functional testing, not performance benchmarking; GPU is the prod-only advantage
- TLS / reverse proxy setup — operator responsibility, documented separately
- The `archon-test` compose service — out of scope for this manual; it's available but undocumented here to keep the manual focused

## Key Decisions
- **Prod = native service**: GPU acceleration and OS-managed lifecycle require native install; Docker GPU passthrough adds complexity without benefit for dev-UAT.
- **Dev-UAT = Docker**: Provides version pinning, full isolation (data, port, keys), and platform-agnostic setup without any code changes; the existing `docker-compose.yml` already defines the `archon-dev` service.
- **No code changes**: All isolation primitives (`ARCHON_SEARCH_DATA_DIR`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_CONFIG`, named Docker volumes) already exist; the gap is documentation only.
- **Manual start for dev-UAT**: Dev-UAT is started on demand (e.g. before running e2e live tests), not as a persistent service — aligns with the `docker compose up` / `stop` lifecycle.
- **Image registry**: `ghcr.io/user538295/archon-search` is the canonical registry path. The manual uses `ARCHON_SEARCH_IMAGE` env var (already the compose default mechanism) and ships a `.env.example` with the real path pre-filled so users have a copy-paste starting point.
- **`archon-test` excluded**: Manual covers prod + dev-UAT only. A "Going further" note acknowledges `archon-test` (port 18766, isolated volume) as a throwaway scratch environment for destructive or parallel testing.

## Edge Cases & Constraints
- **LanceDB is single-writer**: Two containers must never mount the same named volume simultaneously. The compose file uses separate volumes (`archon-dev-data`, `archon-prod-data`) — this constraint must be called out clearly in the manual.
- **API key regeneration**: Without a persistent volume, the API key regenerates on every container start. The named `archon-dev-data` volume prevents this — manual must explain this and warn against `docker run` without `-v`.
- **Port conflict**: If something else occupies 18765, `docker compose up archon-dev` fails silently at the port bind. Manual should note how to check and override the port.
- **Different versions**: The `ARCHON_SEARCH_IMAGE` env var in docker-compose controls the dev-UAT image. Manual must show how to pin a specific version (e.g. `ARCHON_SEARCH_IMAGE=ghcr.io/user538295/archon-search:v1.3.0`) and ship a `.env.example` with the real registry path pre-filled.
- **fastembed model cache**: Dev-UAT will re-download model weights on first start unless the optional shared `archon-model-cache` volume is enabled. Manual should explain the three-step opt-in (already commented in docker-compose.yml) and note that sharing the cache between running instances is safe (read-only after download).

## Open Questions
None — all decisions resolved.

## Future Iterations
- Named service support (`archon-search install --name uat`) so dev-UAT can also be a persistent OS service — deferred until there's a use case beyond Docker.
- Compose override file (`docker-compose.override.yml`) pattern for per-developer port customisation — out of scope; add when a team-shared compose setup is needed.

## Recommendation
Build this now — it's documentation-only, low risk, and directly unblocks e2e live test workflows. The hardest part is not the setup (the infrastructure is already there) but writing a manual that is concrete enough to follow without Docker expertise. The LanceDB single-writer constraint and the API key persistence gotcha must not be buried — they are the two failure modes most likely to confuse a new operator.
