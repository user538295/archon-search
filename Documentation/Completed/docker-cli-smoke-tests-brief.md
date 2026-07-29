# Feature Brief: Docker CLI Smoke Tests

## Problem
Running `archon-search` CLI commands inside a Docker container produces raw Python tracebacks instead of clean errors — `status` crashes because it calls `systemctl`, which doesn't exist in a container. There are no tests that run inside Docker to catch this or any other container-specific regression.

## Goal
All CLI commands work correctly inside Docker: service management commands (`status`, `start`, `stop`, `install`, `uninstall`) emit a clear, actionable message instead of crashing; `status` falls back to the HTTP endpoint when systemctl is absent; a suite of smoke tests runs inside the container and fails on any regression.

## Users & Context
Operators and developers who run archon-search via Docker and use the CLI to inspect or control it. They expect the same CLI surface as the native install, and a traceback erodes trust fast.

## Core Flow

**Fix path (TDD sequence):**
1. Write Docker smoke tests that call each CLI command via subprocess inside the `archon-test-runner` container.
2. Run the tests — they fail on the existing crash and any other gaps.
3. Fix `linux.py` so `FileNotFoundError` from a missing `systemctl` is caught and converted to a clean message with exit code 1.
4. Fix `status` so that when systemctl is absent, it skips the service-layer query and falls back directly to the HTTP endpoint (`GET /status`) — same output it already produces for the telemetry section.
5. Re-run tests — they pass.

**Running the Docker smoke tests:**
```
docker compose run archon-test-runner uv run pytest tests/smoke/docker/ --no-cov
```

## In Scope
- New `tests/smoke/docker/` directory with a `test_docker_cli.py` file
- Tests spawn `archon-search serve` inside the container and exercise every CLI command group
- Fix `linux.py`: catch `FileNotFoundError` (and `PermissionError`) from `systemctl` calls; return `ServiceStatus(running=False)` with a logged warning
- Fix `status` command: when `_get_service().status()` returns no PID/uptime due to absent systemctl, fall back to the HTTP endpoint already called later in `status.py`
- Clean user-facing message for `start`/`stop`/`install`/`uninstall` in container mode: `"Service management is not available in container mode. Use 'archon-search serve' to run the server."`
- Commands covered by smoke tests: `--help`, `--version`, `serve` (startup + shutdown), `status` (HTTP fallback), `key list`, `collection list`, `collection add`, `collection info`, `config show`, `ingest`, `jobs status`, `maintenance run`, `start` (clean error), `stop` (clean error), `install` (clean error), `uninstall` (clean error)

## Out of Scope
- Changing `wizard` behavior in container mode — it has its own separate brief scope
- Windows or macOS container scenarios — those platforms don't run this image
- CI pipeline wiring — the tests are written to run locally and in CI manually; hooking them into the release workflow is a follow-up
- GPU image variant — the fix is platform-level, not image-variant-level

## Key Decisions
- **TDD:** Tests are written first, confirmed failing, then the fix is applied. This ensures the tests are genuine regression guards, not post-hoc validators.
- **HTTP fallback for `status`, clean error for others:** `status` is the one service command with a meaningful Docker equivalent (the running server has a `/status` endpoint). The others (`start`, `stop`, `install`, `uninstall`) are genuinely meaningless in a container — a clean message is the right UX, not a silent no-op.
- **Tests live in `tests/smoke/docker/`:** They follow the existing smoke test pattern (subprocess calls, `xdist_group` serialization, excluded from default `pytest` runs). They do not use `docker compose exec` — they run *inside* the container, calling `archon-search` directly.
- **`ARCHON_SEARCH_CONTAINER=1` is the detection signal:** Already set by `Dockerfile` and `Dockerfile.test`. The fix uses this env var (or `systemctl` absence) to gate behavior.

## Edge Cases & Constraints
- **`status` when server is not running AND systemctl absent:** Shows "server not reachable" via HTTP (ConnectError path already handled in `status.py`) rather than a service-layer error. No traceback.
- **`status` when server IS running AND systemctl absent:** HTTP fallback shows full server telemetry — collection counts, job queues, graph GC status. Same output as the second half of the current `status` command.
- **Existing native Linux tests must not regress:** The fix in `linux.py` must only activate when `systemctl` is genuinely absent — not on any Linux host where it exists.
- **`Dockerfile.test` uses `uv sync` into `/venv`:** `archon-search` is on `PATH` directly. Tests call `["archon-search", ...]`, not `["uv", "run", "archon-search", ...]`.
- **Smoke tests are serialized:** `pytestmark = pytest.mark.xdist_group("smoke_e2e")` is required at module level, same as existing smoke tests, to prevent concurrent server subprocess instances.

## Open Questions
- Should `ARCHON_SEARCH_CONTAINER=1` be the primary detection mechanism (explicit), or should the fix purely rely on catching `FileNotFoundError` from `systemctl` (implicit)? Explicit is cleaner for test stubs; implicit is more robust for edge-case installs without systemd. Consider using both: env var short-circuits, `FileNotFoundError` is the safety net.
- The `archon-test-runner` service in `docker-compose.override.yml` currently runs the full test suite. Should the Docker smoke tests require a separate running `archon-dev` container, or should the fixture spawn its own `archon-search serve` subprocess inside the test container? The existing `conftest.py` pattern (spawn subprocess inside) is simpler and avoids inter-container networking.
- Should `norecursedirs` in `pyproject.toml` be extended to include `tests/smoke/docker/` as a tertiary directory, or is `-m "not smoke"` sufficient to exclude these from the default run? Given the existing pattern, the `-m` filter is sufficient since the new tests will carry `@pytest.mark.smoke`.

## Future Iterations
- Wire Docker smoke tests into the release CI workflow as a required gate before PyPI publish
- Add `wizard` container-mode detection (skip service installation steps when `ARCHON_SEARCH_CONTAINER=1`)
- Explore a `--no-service` global flag that disables all systemd/launchd calls for use outside containers (e.g., minimal Linux environments without systemd)

## References
- **Team plan:** [`docker-cli-smoke-tests-team-plan.md`](./docker-cli-smoke-tests-team-plan.md) — role-split development plan generated from this brief
- [`Documentation/docker-test-runner.md`](../docker-test-runner.md) `[docs-agent]` — Guide to running tests in Docker; describes `archon-test-runner` and `archon-dev-shell` services
- [`Documentation/UserManual/08_running_with_docker.md`](../UserManual/08_running_with_docker.md) `[docs-agent]` — Production Docker deployment guide
- [`Documentation/Completed/C9-container-support-plan.md`](../Completed/C9-container-support-plan.md) `[docs-agent]` — Container support plan (marked complete but `status` crash reveals a gap)
- [`Documentation/Completed/2026-07-15-010-live-smoke-test-brief.md`](../Completed/2026-07-15-010-live-smoke-test-brief.md) `[docs-agent]` — Prior smoke test brief; this feature follows the same pattern
- [`Documentation/Architecture/200_testing_strategy.md`](../Architecture/200_testing_strategy.md) `[docs-agent]` — Smoke test marker rules, `norecursedirs` policy, xdist serialization requirement
- [`tests/smoke/conftest.py`](../../tests/smoke/conftest.py) `[code-agent]` — Existing fixture: spawns real server, seeds corpus — new fixture follows this pattern
- [`tests/smoke/test_cli.py`](../../tests/smoke/test_cli.py) `[code-agent]` — Existing CLI smoke tests — new `test_docker_cli.py` mirrors this structure
- [`Dockerfile.test`](../../Dockerfile.test) `[code-agent]` — Test runner image; `archon-search` available directly on PATH
- [`docker-compose.override.yml`](../../docker-compose.override.yml) `[code-agent]` — `archon-test-runner` and `archon-dev-shell` services
- [`archon_search/platform/linux.py`](../../archon_search/platform/linux.py) `[code-agent]` — Where the crash originates: `_run()` raises `FileNotFoundError` when `systemctl` is absent
- [`archon_search/cli/_helpers.py`](../../archon_search/cli/_helpers.py) `[code-agent]` — `_get_service()` — returns `SystemdSearchService` on Linux
- [`archon_search/cli/status.py`](../../archon_search/cli/status.py) `[code-agent]` — Already calls HTTP endpoint; HTTP fallback is the second half of this command

## Recommendation
Build this now. The crash in `status` is a trust-breaker for anyone using Docker, and C9 being marked "complete" while it crashes means operators are hitting a silent gap. The fix in `linux.py` is small (catch `FileNotFoundError`, return a clean status). The real value is the test suite: without it, this class of regression will recur. The hardest part is the fixture design — specifically, whether the Docker smoke tests spawn their own server subprocess (simpler, recommended) or depend on a sibling container. Go with the subprocess approach to keep the tests self-contained and runnable with a single `docker compose run`.
