# Feature Brief: Non-Blocking Stop with a "Stopping" State

## Problem
When a user runs `archon-search stop`, the terminal freezes for up to 10 seconds while the tool waits to confirm the server is fully dead. At the same time, anyone polling `GET /health` during that window still gets a healthy-looking "200 OK" response — even though shutdown is already underway.

## Goal
`archon-search stop` returns immediately (within milliseconds). From that point on, any tool or script that checks `GET /health` sees the server's actual state: 503 while it is shutting down, then connection refused once it is gone.

## Users & Context
- **Developers and operators** running `archon-search stop` from the terminal — they want their prompt back right away, not after a 10-second wait.
- **Monitoring scripts and health-check pollers** that query `GET /health` on a schedule — they need a clear, non-200 response the moment shutdown begins, so they stop routing work to the server before it goes silent.

## Core Flow
1. User runs `archon-search stop`.
2. The tool sends the shutdown command to the OS service manager (launchctl on macOS, systemd on Linux) and immediately prints `"archon-search stop requested"` and exits.
3. The OS delivers a shutdown signal to the server process.
4. The server receives the signal and immediately starts returning `503 Service Unavailable` from `GET /health` — it is now in the **stopping** state.
5. The server finishes draining any in-flight requests and exits cleanly.
6. `GET /health` becomes unreachable (connection refused) — the server is now **stopped**.
7. `archon-search status` reports `stopped`.

The three observable states, in order: **running** → **stopping** (503 on `/health`) → **stopped** (connection refused).

## In Scope
- `archon-search stop` returns immediately after issuing the OS stop command — no polling, no waiting.
- The server sets an internal "shutting down" flag the moment it receives the shutdown signal, before any teardown begins (`app.state.stopping = True` in the FastAPI lifespan).
- `GET /health` returns `503 Service Unavailable` when that flag is set.
- The S04 smoke test is updated to poll until `/health` is non-200 (rather than relying on the blocking stop call to guarantee it).
- Unit tests cover both the happy path (flag set → 503) and the existing unit tests for stop CLI output.
- Docs updated: `UserManual/40_running_the_server.md` and `Architecture/160_operational_readiness_monitoring_and_reliability.md`.

## Out of Scope
- `archon-search status` showing a "stopping" label — the window is under half a second; a one-shot CLI check cannot reliably catch it, and adding the complexity is not worth it.
- Any change to `GET /ready` or `GET /status` — those endpoints serve different purposes and are not affected.
- Container deployments (Docker/tini path) — SIGTERM already flows correctly there; no change needed.
- Windows — the platform layer raises `NotImplementedError` on Windows and this feature does not change that.

## Key Decisions
- **Stop returns immediately, not after confirmation:** The prior fix (S04) made stop block for up to 10 seconds to guarantee the process was down before returning. This is removed. The "stopped" guarantee now comes from polling `GET /health` externally, not from the stop command itself.
- **503 during stopping, not a custom status code:** 503 Service Unavailable is the established HTTP convention for "I am alive but not accepting work" — load balancers and health-check systems know what to do with it.
- **CLI `status` is unchanged:** It reports `running` or `stopped` based on the OS supervisor. The brief "stopping" window between those two is too short for a one-shot command to reliably catch.

## Edge Cases & Constraints
- **Stop issued when server is already stopped:** The OS stop command is a no-op; the tool exits 0 and prints the same message. No regression here — existing behaviour.
- **Stop issued when server is unresponsive (hung):** The OS supervisor sends SIGTERM; if the server does not respond within the supervisor's configured timeout, SIGKILL follows. The tool already returned immediately and never sees this.
- **`/health` is auth-exempt:** It is listed in `_EXEMPT_PATHS`. The 503 response must also be returned without requiring authentication — the flag check happens before any auth gate.
- **The stopping window is brief:** Callers that poll `GET /health` should use retries with a timeout (e.g. up to 10 seconds) to reliably detect the stopped state, rather than expecting a 503 and then connection refused in a single check.

## Open Questions
- Should `HealthResponse` include a `"status": "stopping"` field in the JSON body during the 503 response, or is the HTTP status code alone enough for downstream consumers?
- Should the 503 response include a `Retry-After` header to signal pollers how long to wait before rechecking?
- Does the `_wait_until_stopped()` method on `SearchServiceLifecycle` stay (it is only used by `stop()`, which now no longer calls it) or should it be removed entirely? Removal is cleaner but touches platform code — flag for planning to decide.

## Future Iterations
- A `--wait` flag on `archon-search stop` that optionally blocks until confirmed stopped, for scripts that need the old behaviour.
- `archon-search status --watch` — a continuous monitor that updates in place as state changes from running → stopping → stopped.

## References
- **Team plan:** [`non-blocking-stop-with-stopping-state-team-plan.md`](non-blocking-stop-with-stopping-state-team-plan.md) — role-split team development plan generated from this brief
- [`Documentation/Backlog/S04-health_unreachable_after_stop.md`](S04-health_unreachable_after_stop.md) `[user+docs-agent]` — bug report that triggered this discussion
- [`Documentation/UserManual/40_running_the_server.md`](../UserManual/40_running_the_server.md) `[docs-agent]` — canonical user doc for start/stop/status; currently documents blocking stop behaviour that this brief replaces
- [`Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) `[docs-agent]` — authoritative architecture doc: lifecycle ABC, HTTP endpoint catalogue, container model
- [`Documentation/OperatorGuide/10_deployment_topologies.md`](../OperatorGuide/10_deployment_topologies.md) `[docs-agent]` — platform topology reference; SIGTERM shutdown contract
- [`Documentation/OperatorGuide/20_monitoring_and_alerts.md`](../OperatorGuide/20_monitoring_and_alerts.md) `[docs-agent]` — monitoring endpoint matrix; connection refused = critical alert trigger
- [`Documentation/OperatorGuide/90_incident_runbook.md`](../OperatorGuide/90_incident_runbook.md) `[docs-agent]` — incident runbook; uses /health → /ready → /status triage chain
- [`Documentation/Architecture/120_services_and_integration_architecture.md`](../Architecture/120_services_and_integration_architecture.md) `[docs-agent]` — route group table; /health is the only auth-exempt path
- [`Documentation/Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) `[docs-agent]` — full REST reference; HealthResponse and StatusResponse schemas
- [`Documentation/Completed/B2-deeper-health-readiness-brief.md`](../Completed/B2-deeper-health-readiness-brief.md) `[docs-agent]` — prior decision establishing /health = liveness-only, /ready = storage readiness; this brief must align with it
- [`Documentation/Completed/2026-07-15-120-cli-server-proxy-brief.md`](../Completed/2026-07-15-120-cli-server-proxy-brief.md) `[docs-agent]` — CLI proxy brief; established "connection refused → server not running" UX
- [`Documentation/Completed/2026-07-15-260-connection-refused-ux-brief.md`](../Completed/2026-07-15-260-connection-refused-ux-brief.md) `[docs-agent]` — connection-refused UX brief; `_require_server()` helper and canonical stopped-state messages
- [`archon_search/cli/stop.py`](../../archon_search/cli/stop.py) `[code-agent]` — stop CLI command; blocking call to service.stop() is what this brief removes
- [`archon_search/platform/service.py`](../../archon_search/platform/service.py) `[code-agent]` — ServiceStatus dataclass (no stopping field); blocking `_wait_until_stopped()` to be removed or made unused
- [`archon_search/platform/macos.py`](../../archon_search/platform/macos.py) `[code-agent]` — LaunchdSearchService.stop(): remove the _wait_until_stopped() call
- [`archon_search/platform/linux.py`](../../archon_search/platform/linux.py) `[code-agent]` — SystemdSearchService.stop(): remove the _wait_until_stopped() call
- [`archon_search/server/app.py`](../../archon_search/server/app.py) `[code-agent]` — FastAPI lifespan: add app.state.stopping = True at the top of the finally block
- [`archon_search/server/routes_health.py`](../../archon_search/server/routes_health.py) `[code-agent]` — GET /health: add stopping flag check, return 503 when set
- [`tests/smoke/test_s04_health_after_stop.py`](../../tests/smoke/test_s04_health_after_stop.py) `[code-agent]` — S04 regression smoke test; must be updated to poll instead of relying on blocking stop

## Recommendation
Build this now. The blocking stop was a pragmatic patch for S04 that solved the observable symptom but introduced a worse UX problem — nobody expects a stop command to freeze their terminal. The three-state model (running → stopping → stopped) is architecturally clean, costs around 20–30 lines of code, and aligns with how every serious service management system works. The hardest part is updating the S04 smoke test so it polls correctly without the blocking guarantee — that test design needs care to avoid flakiness. Do not compromise on the 503 during shutdown: that is the signal monitoring systems depend on, and dropping it back to "connection refused only" removes real operational value.
