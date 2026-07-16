# Feature Brief: Live Smoke Test Suite

## Problem

Bugs 001–010 were all found by a user hitting real problems — none were caught proactively. The existing test suite exercises internal logic through `TestClient` (an in-process HTTP client that bypasses the CLI and process boundary entirely), which means it cannot catch the class of bugs that affect real users: slow startup, raw Python object output, blocking terminal behavior, and unhelpful error messages. A developer shipping a change today has no automated way to verify "does the CLI actually behave well when a real person runs it?"

## Goal

A repeatable smoke test suite that starts a real server process, runs every major CLI command as a real subprocess, and asserts that each command responds quickly, exits cleanly, and produces human-readable output. A failing smoke test means a user would have had a bad experience. A passing suite means the golden paths work end-to-end before the change ships.

## Users & Context

Developers working on archon-search CLI commands, the wizard, or server routes — running the suite locally before a PR, or in CI on release branches. Also useful as a diagnostic tool after a fresh install ("does my setup work?").

## Core Flow

1. Developer runs `uv run pytest tests/smoke/ --no-cov` (or it runs in release CI automatically).
2. Suite starts a real `archon-search serve` process in the background (using `subprocess.Popen`, bound to a random port, pointed at a temp data directory with a pre-seeded tiny collection).
3. Each test issues one CLI command or HTTP request against the live server and checks:
   - **Timing**: command completes within a time budget (e.g. 5s for reads, 30s for ingest of a tiny fixture).
   - **Exit code**: 0 for success, non-zero for expected errors.
   - **Output**: human-readable — no `CollectionMeta(name=...` reprs, no stack traces, no raw Python objects.
   - **Error messages**: when something is wrong, the message names what to do next (not an errno or exception name).
4. Suite tears down the server (SIGTERM → SIGKILL fallback) and cleans up the temp directory.
5. Findings are reported as standard pytest failures with the actual vs expected output shown.

## In Scope

- All read-only CLI commands: `collection list`, `collection info`, `config show`, `status`, `key list`.
- Write commands that should return quickly: `collection add` (submits job, returns job ID — post bug-005 fix), `maintenance run`.
- Error path commands: `maintenance run` without server (post bug-006 fix: readable message, not errno).
- Server REST endpoints: `GET /health`, `GET /ready`, `GET /status`, `GET /collections`, `GET /collections/{name}`, `POST /search`.
- Startup timing assertion: `archon-search --help` must complete within 2s (catches import-overhead regressions from bug-004 class).
- Output format assertions: no `CollectionMeta(`, no raw embedding vectors, no Python stack traces in user-visible output.

## Out of Scope

- Ingest quality (recall, reranking accuracy) — covered by `tests/eval/`.
- Internal unit logic — covered by `tests/`.
- The wizard interactive flow — too complex for subprocess automation; covered by unit tests in `tests/test_install.py`.
- Graph community building, full reindex, backup/restore — slow operations; add in a later iteration.
- Windows / container-specific behavior — this suite targets macOS/Linux dev environment.

## Key Decisions

- **Real subprocess, not TestClient**: The whole point is catching CLI-layer bugs (slow startup, bad output formatting, blocking behavior) that TestClient cannot see. `subprocess.run` / `subprocess.Popen` for every assertion.
- **Real server process, not mocked**: `archon-search serve --port {random_port} --data-dir {tmp_path}` started once per session via a session-scoped pytest fixture. Avoids per-test server startup cost (~5s each); shares one server for all tests. Uses `tmp_path_factory` (not `tmp_path`) since the fixture is session-scoped.
- **Explicit pattern assertions, not agent-judged**: CI needs deterministic pass/fail. Each test asserts specific string patterns (`assertNotIn("CollectionMeta(", output)`, `assertLess(elapsed, 5.0)`, `assertIn("job_id", output)`). Agent-based judgment is for the `/bug-sweep` skill (Option C), not this suite.
- **Excluded from default `pytest` run**: `norecursedirs = ["tests/smoke/"]` in `pyproject.toml` (same pattern as `tests/eval/live_benchmark/`) — smoke tests spawn a real server with real models (~2GB RAM); including them in the default `-n 4` xdist run would OOM the machine. Run explicitly: `uv run pytest tests/smoke/ --no-cov`.
- **Must use `xdist_group("smoke_e2e")`** on all smoke tests to serialize them on one worker — 4 concurrent real servers × 2GB models = OOM.
- **Pre-seeded tiny fixture**: The session fixture ingests 3–5 small documents at startup (inline text, no file I/O) so search and info tests have something to return. Avoids depending on the user's real data directory.
- **Findings flow**: A failing smoke test is filed as a bug brief (same `bug-NNN-*-brief.md` format) if it reveals a new issue. The suite itself does not auto-generate briefs — it just fails loudly with the actual vs expected output.

## Edge Cases & Constraints

- **Server not starting in time**: Session fixture polls `GET /health` with a 30s timeout; fails the entire suite with a clear "server did not start" message if exceeded.
- **Port collision**: Use `port=0` (OS assigns a free port) and read the actual port from the server's stdout on startup.
- **SIGTERM return code varies (0, -15, or 143 by OS/wrapper)**: Teardown asserts `returncode not in {1, 2}`, not `== 0`. Cleanup assertions belong in teardown only; behavior assertions belong in test functions.
- **`ANTHROPIC_API_KEY` cleared by root conftest**: Smoke tests must set their own env explicitly if any test exercises HyDE/RAG Fusion paths.
- **OOM risk**: Session-scoped server means 1 server per worker, not 4. The `xdist_group` constraint enforces 1 worker. Never run smoke tests with `-n auto` or alongside the full default suite.
- **`archon-search serve` start-up logging**: The fixture captures stderr and surfaces it on failure so a server crash is debuggable, not silent.
- **Timing assertions are environment-dependent**: Budgets (2s for CLI reads, 5s for server reads) are calibrated for the CI machine. Mark them `@pytest.mark.skip` with a note if they flake on slow hardware; never bake them into `addopts`.

## Open Questions

- Should `tests/smoke/` have its own marker (`@pytest.mark.smoke`) so developers can run `uv run pytest -m smoke` without knowing the directory name? (Recommend yes — mirrors `live_benchmark` marker pattern.)
- Does `archon-search serve` write its actual bound port to stdout or a status file? If not, a startup-port-detection mechanism needs to be added to the server (e.g. `--port-file /tmp/archon-port` flag). Check `serve.py` before implementing the fixture.
- After bug-005 (collection add → REST proxy), does `collection add` require the server running? If so, the smoke suite naturally covers this path once bug-005 ships. Until then, `collection add` testing is out of scope for this suite.
- CI trigger: should smoke tests run on every PR or only on release branches? Given 5–10 min runtime and the model download requirement, release-branch-only is the safer default to start.

## Future Iterations

- Wizard smoke test: non-interactive `archon-search wizard --non-interactive` with known flags, verify the resulting TOML matches expectations.
- Output snapshot tests: capture full `collection info` output and assert it matches a golden file (update with `--update-snapshots`).
- Performance regression gate: track `config show` latency over time; fail if it regresses by more than 0.5s.
- `/bug-sweep` Claude skill (Option C from the broader exploration discussion): wraps this suite + the code audit agents into a single on-demand command that produces ranked bug briefs.

## References

- **Team plan:** [2026-07-15-010-live-smoke-test-team-plan.md](./2026-07-15-010-live-smoke-test-team-plan.md)
- [[archon_search/cli/main.py]] `[code-agent]` — CLI entry point, lists all subcommands
- [[archon_search/cli/collection.py]] `[code-agent]` — collection commands (add, list, info, remove)
- [[archon_search/server/routes_*.py]] `[code-agent]` — all REST endpoints
- [[learnings.md]] (lines 47, 68) `[docs-agent]` — prior smoke test design decisions and OOM constraints
- [[pyproject.toml]] (lines 96–116) `[docs-agent]` — `norecursedirs` and `addopts` conventions for excluded test dirs
- [[Documentation/Backlog/2026-07-15-210-cli-store-commands-slow-brief.md]] `[user]` — bug this suite would catch
- [[Documentation/Backlog/2026-07-15-190-cli-startup-latency-brief.md]] `[user]` — timing regression this suite gates
- [[Documentation/Backlog/2026-07-15-350-collection-info-display-brief.md]] `[user]` — output format this suite asserts

## Recommendation

Build this. The 10 bugs found in this session represent one developer's spot-check — not a systematic sweep. This suite would have caught at least 6 of them (startup time, raw output, blocking terminal, error messages) before they reached a user. The hardest part is the session fixture: getting `archon-search serve` to start reliably, report its port, and tear down cleanly. Once that fixture exists, each individual test is 5–10 lines. Start with 5–6 golden-path tests (`config show` timing, `collection list` output, `collection info` format, `GET /health` response, `maintenance run` without server error message) and expand from there. Do not block bug-001–bug-010 fixes on this — implement the fixes first, then use this suite to verify they hold.
