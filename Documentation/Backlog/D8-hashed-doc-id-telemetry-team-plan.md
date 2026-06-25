---
id: D8
feature: Hashed doc_id Mode for Telemetry (SEC-2)
brief: D8-hashed-doc-id-telemetry-brief.md
purpose: Operators can enable HMAC hashing of telemetry doc_ids so log files are opaque to anyone without the server-side salt.
audience: Backend developer (all implementation); CLI developer (status command); tester (e2e + close-out).
status: planned
roles: [frontend, backend, tester]
architecture: clean
---

# D8 · Hashed doc_id Mode for Telemetry — Team Plan

**How to read this file**
- **Architecture approach:** Clean Architecture (default). **Layers:** Presentation · Use Cases · Interface Adapters · Entities · Frameworks & Drivers. Each task's first sub-bullet names the layer it touches.
- The **Frontend, Backend, and Tester** sections are the **depth view** — each role's work, grouped by layer.
- The **Task Breakdown** is the **order view** — every task is a single-role checkbox in execution order, opening with a dependency graph.
- **Phases are vertical slices**: each delivers a working end-to-end increment. Sliced with the **`vertical-slicer` skill**.
- Each task carries the **role tag at the end of its title line**, then sub-bullets: **layer · estimate**, **needs · completes**, and a **Tests** block.
- **Tests:** unit and integration tests belong to the implementing dev (test-first); e2e and manual tests are the tester's tasks. Close-out writes no tests.
- **Contracts:** authored as core-construct `.tsp` files (TypeSpec v1.13.0 available; no OpenAPI emit — project has not established an `api-contracts/` pattern). All three contracts compiled clean with `tsp compile --no-emit`.
- **Role tags** (`#frontend-role`, `#backend-role`, `#tester-role`) mark each role-owned section.
- IDs (`S#`, `C#`, `BE-#`/`FE-#`/`T-#`/`K#`, `Q#`) are the traceability thread.
- **Rule:** edit your own tasks freely; change a contract only by team agreement.

---

## Background

When telemetry is enabled, `result_doc_ids` in JSONL logs are SHA-256 hashes of filesystem paths. Because LanceDB stores the same mapping unencrypted, anyone with read access to `~/.archon-search/` can reverse the hash to exact file paths — making the "hash" effectively a lookup table. Operators who share logs or run archon-search on multi-user hosts inadvertently expose their filesystem layout.

---

## Goal

Operators can set `[telemetry] hash_doc_ids = true` in `archon-search.toml` to apply a second-stage HMAC-SHA256 transform to all `result_doc_ids` before they are written to JSONL. The mapping to LanceDB source paths is severed; doc_id uniqueness (for counts and per-document metrics) is preserved. Hashing state is visible in `GET /status` and `archon-search status`.

---

## Scope

### In Scope
- `TelemetryConfig.hash_doc_ids: bool = False` (TOML field, default off)
- Salt lifecycle: generate 32-byte salt on first start (mode 600, log WARNING); load existing; fallback on unreadable (log ERROR, disable hashing for session)
- `hash_doc_id(salt, doc_id) -> str` pure function → 64-char HMAC-SHA256 hex
- `doc_id_hasher: Callable[[str], str] | None` injected into `from_search_tool_result` factory
- `doc_ids_hashed: bool` field added to `TelemetryEntry` and `DOCUMENTED_SCHEMA_FIELDS`
- All 3 `from_search_tool_result` call sites updated: the call in the `/search` route handler (`routes_search.py`), the call in the MCP `search` tool, and the call in the MCP `search_with_context` tool (current lines: `routes_search.py:268`, `mcp.py:407`, `mcp.py:546` — anchor by symbol, not line number)
- `TelemetryStatusDetail` model + `GET /status` `telemetry` sub-object
- `archon-search status` CLI gains a NEW HTTP `GET /status` code path to display `hash_doc_ids_enabled`. Today `cli/status.py` only reports OS service state via `_get_service().status()` and makes no HTTP call; FE-1 adds the HTTP+auth path modelled on `maintenance_cmd.py` (the command that already calls `GET /status`)
- `archon-search.toml.example` updated
- Docs: `150_security_and_privacy_architecture.md`, `CLAUDE.md` telemetry section, ADR-05 amendment, `530_technical_debt_refactoring_roadmap.md` (close SEC-2)
- Tests: unit + integration (dev-owned, test-first); e2e per scenario (tester-owned)

### Out of Scope
- Retroactive re-hashing of existing JSONL entries
- Hashing `doc_id` in the LanceDB store
- Hashing any telemetry field other than `result_doc_ids`
- Salt rotation CLI/API
- `export_enabled` telemetry path
- `--json` flag for `archon-search status` (not established by this feature)

---

## Acceptance criteria
- With `hash_doc_ids = false` (default): JSONL entries contain raw `result_doc_ids` and `doc_ids_hashed: false`; existing deployments are unaffected.
- With `hash_doc_ids = true`: every `result_doc_ids` value in JSONL is a 64-char HMAC-SHA256 hex string; no raw path-derived hash is present; `doc_ids_hashed: true` in every entry.
- Salt file created on first start with `hash_doc_ids = true` (mode 600, WARNING logged); reused on subsequent starts (values stable).
- Salt file unreadable → ERROR logged, server falls back to `hash_doc_ids = false` for the session; server does not crash.
- `GET /status` → `telemetry.hash_doc_ids_enabled: true` when hashing is active, `false` otherwise.
- `archon-search status` displays `hash_doc_ids_enabled` from the server.
- `from_explain_result`, `from_error`, `from_route_response`, and `from_search_multi_result` factories are NOT modified and do NOT receive a hasher.
- MCP `search` and `search_with_context` tools hash doc_ids consistently with the REST endpoint.
- All existing telemetry tests pass; coverage gate passes.

---

## What does NOT change
- `from_search_multi_result`, `from_explain_result`, `from_error`, `from_route_response` factory signatures and behaviour
- The telemetry writer, reader, and pruner (no code changes needed — they use `model_dump` and `model_validate`)
- LanceDB store schema
- The no-raw-query structural invariant in `entry.py`
- Existing JSONL files (not retroactively modified)

---

## Known limitations / accepted trade-offs
- Toggling `hash_doc_ids` (on→off or off→on) breaks metric continuity across log segments — accepted; `doc_ids_hashed` field lets consumers detect the boundary (informally covered by test_e2e_toggle_continuity_visible_in_jsonl in T-1).
- A server restart that reads a new salt (e.g. salt file deleted) also breaks continuity — accepted; a WARNING is logged.
- Salt rotation is deferred to a future iteration.
- **Threat-model scope (salt co-location):** the salt at `get_data_dir() / ".telemetry-salt"` sits alongside LanceDB, which stores the raw `source_path` in plaintext. HMAC hashing protects telemetry logs **shared/exported separately** from the data directory (the stated threat); it does **not** protect against an attacker with read access to the whole `~/.archon-search/` directory (they hold both the salt and the plaintext paths). Stated honestly here and in `150_security_and_privacy_architecture.md`.
- **`doc_ids_hashed` field semantics:** `True` means "the hashing mode was active and applied to this entry's `result_doc_ids` (including an empty `[]` list — S7)". `False` means there were no `result_doc_ids` to hash (`None` — S6), or hashing was off / fell back. Consumers must read it as "mode was active for this entry", not "this entry contains ≥1 hashed value".
- **Forward-compatibility:** `TelemetryEntry` uses `ConfigDict(extra="forbid", frozen=True)` (`entry.py`). Adding `doc_ids_hashed` is backward-compatible (new code reading old JSONL: field defaults to `False`), but **not** forward-compatible — pre-D8 code/tooling reading post-D8 JSONL entries will raise a Pydantic `ValidationError` on the unknown field. Accepted: the writer and reader are upgraded together.

---

## Approach & architecture

The new `archon_search/telemetry/hasher.py` module **spans two layers** — exactly like the existing `key_manager.py` precedent it follows. `hash_doc_id(salt, doc_id) -> str` is a pure, stateless transform with no dependencies: an **Entities**-level primitive. `load_or_create_salt(...)` performs filesystem I/O (read/generate/`atomic_write_bytes` at mode 0o600): a **Frameworks & Drivers** concern. The plan keeps both in one module for cohesion (as `key_manager.py` does) but does not pretend the file is purely "Use Cases". `load_or_create_salt` runs once at lifespan startup and stores the result on `app.state.salt_bytes`. Route and MCP handlers construct a `doc_id_hasher` closure if `app.state.salt_bytes` is set, and pass it as an optional keyword argument to `from_search_tool_result`. The factory sets `doc_ids_hashed` on the entry. (Note: `from_search_tool_result` already calls `uuid4()`/`datetime.now()`, so it is not a "pure" factory — the justification for injecting a `Callable` is testability and avoiding module-global state, not factory purity.) The Presentation layer (CLI `status` command) gains a NEW `GET /status` HTTP code path: `cli/status.py` today only reports OS service state (`_get_service().status()`) and makes no HTTP call, so FE-1 adds the HTTP+auth plumbing, modelled on `maintenance_cmd.py` (the command that already calls `GET /status` via httpx). Because `/status` requires a `Bearer` token, this also pulls in API-key resolution.

```mermaid
flowchart TD
  P["Presentation — FE<br/>cli/status.py"]
  AD["Interface Adapters — BE<br/>routes_search.py · mcp.py · routes_status.py"]
  EN["Entities — BE<br/>telemetry/entry.py (TelemetryEntry + factory)<br/>config.py (TelemetryConfig)<br/>telemetry/hasher.py: hash_doc_id (pure fn)"]
  FW["Frameworks & Drivers — BE<br/>server/app.py (lifespan salt init)<br/>server/schemas.py (TelemetryStatusDetail)<br/>telemetry/hasher.py: load_or_create_salt (file I/O)"]
  P --> AD
  AD --> EN
  FW --> AD
  FW --> EN
```

**Layer map**

| Layer | Role | Components |
|-------|------|-----------|
| Presentation | **Frontend** | `archon_search/cli/status.py` |
| Interface Adapters | Backend | `archon_search/server/routes_search.py`, `archon_search/server/mcp.py`, `archon_search/server/routes_status.py` |
| Entities | Backend | `archon_search/telemetry/entry.py`, `archon_search/config.py`, `archon_search/telemetry/hasher.py` → `hash_doc_id` (pure fn) |
| Frameworks & Drivers | Backend | `archon_search/server/app.py`, `archon_search/server/schemas.py`, `archon_search/telemetry/hasher.py` → `load_or_create_salt` (file I/O) |

> `telemetry/hasher.py` (new) intentionally spans Entities + Frameworks & Drivers, mirroring the `key_manager.py` precedent — pure transform alongside its salt-file I/O in one cohesive module.

**What changes**
- `config.py`: `TelemetryConfig` gains `hash_doc_ids: bool = False`; `_apply_toml()` branch added
- `telemetry/hasher.py` (new): `hash_doc_id(salt, doc_id) -> str` + `load_or_create_salt(hash_doc_ids_enabled) -> bytes | None`
- `telemetry/entry.py`: `TelemetryEntry` gains `doc_ids_hashed: bool = False`; `DOCUMENTED_SCHEMA_FIELDS` updated; `from_search_tool_result` gains `doc_id_hasher` param
- `server/app.py`: lifespan calls `load_or_create_salt(config.telemetry.hash_doc_ids)`, stores `salt_bytes` on `app.state.salt_bytes` (for status check in BE-5) and constructs `doc_id_hasher` closure stored on `app.state.doc_id_hasher` (injected into routes and MCP)
- the `from_search_tool_result` call in the `/search` route handler (`routes_search.py`, currently line 268): reads `app.state.doc_id_hasher` and passes it
- the `from_search_tool_result` calls in the MCP `search` tool and the MCP `search_with_context` tool (`mcp.py`, currently lines 407 and 546): reads `app.state.doc_id_hasher` and passes it
- `server/schemas.py`: new `TelemetryStatusDetail` model added
- `server/routes_status.py`: `_build_telemetry_status()` helper + `telemetry` field in response
- `archon_search/cli/status.py`: enhanced to call `GET /status`, display `hash_doc_ids_enabled`
- `server/schemas_telemetry.py`: **no change needed** (verified) — it holds only the `/telemetry/stats` and `/telemetry/entries` response models, which do not enumerate per-entry fields; the brief's mention of it is superseded. The `doc_ids_hashed` field flows through the writer (`model_dump`) and reader (`model_validate`) without code changes.
- **Schema impact on all entry types**: because `doc_ids_hashed: bool = False` is added to the shared `TelemetryEntry` model, *every* entry type (`from_error`, `from_route_response`, `from_explain_result`, `from_search_multi_result`, …) now emits `doc_ids_hashed: false` in its JSONL. This is an intentional additive schema change across all entry types, not just the search path.

**Key decisions (from the brief)**
- HMAC-SHA256 over double-SHA256: salt breaks correlation without knowing it
- 64-char output (full HMAC): zero schema friction, no length-validator breaks
- `doc_ids_hashed: bool` in every entry: consumers can distinguish log segments
- `hash_doc_ids_enabled` in GET /status: observable without opening config
- Salt stored on disk (not in TOML): outside config files operators share
- Default off: existing deployments unaffected
- Callable injected into factory (not module-global): keeps factories pure and testable

---

## Contracts / seams

Boundaries where roles must agree. All seams are **internal logical** (no HTTP/API boundary change to factory or hasher — only GET /status gains a new response field). Authored as core-construct TypeSpec (validated clean). No OpenAPI emit (project has not established `api-contracts/` pattern).

**C1 — Salt Lifecycle & hash_doc_id function** *(Use Cases ↔ Interface Adapters)*
`load_or_create_salt(hash_doc_ids_enabled) -> bytes | None` and `hash_doc_id(salt, doc_id) -> str` in `telemetry/hasher.py`. Route/MCP adapters: after `load_or_create_salt` returns in the lifespan, the caller immediately constructs a single `doc_id_hasher: Callable[[str], str] | None` closure (e.g. `lambda id_: hash_doc_id(salt_bytes, id_)` when salt is non-null, else `None`), stores it on `app.state.doc_id_hasher`, and passes it as a new `doc_id_hasher` parameter to `create_mcp_http_app(...)`. This mirrors how `writer` is handled. MCP tool closures capture `doc_id_hasher` at construction time — they do NOT access `app.state.salt_bytes` (the MCP sub-app has its own state namespace). See [`d8-telemetry-hasher.tsp`](d8-telemetry-hasher.tsp).
- Realised by: BE-2, BE-4 · Verified by: BE-2 (unit + integration), T-1 (e2e)

**C2 — TelemetryEntry factory seam** *(Entities ↔ Interface Adapters)*
`from_search_tool_result` gains optional `doc_id_hasher: Callable[[str], str] | None = None`. When provided, applies it to each `result_doc_ids` element and sets `doc_ids_hashed=True`. `doc_ids_hashed: bool = False` added to `TelemetryEntry` model and `DOCUMENTED_SCHEMA_FIELDS`. See [`d8-telemetry-entry.tsp`](d8-telemetry-entry.tsp).
Note: the `.tsp` file models the hasher as `docIdHasherPresent: boolean` (TypeSpec cannot represent Python Callables); the Python signature is the authoritative seam — `doc_id_hasher: Callable[[str], str] | None = None`.
- Realised by: BE-3, BE-4 · Verified by: BE-3 (unit), BE-4 (integration), T-1 (e2e)

**C3 — GET /status response extension** *(Interface Adapters ↔ Presentation)*
`StatusResponse` gains `telemetry: TelemetryStatusDetail | None`. `TelemetryStatusDetail` = `{enabled: bool, hash_doc_ids_enabled: bool}`. Present when telemetry is enabled; null otherwise. `hash_doc_ids_enabled` is true only when both config flag and loaded salt are non-null (guards fallback case). See [`d8-telemetry-status.tsp`](d8-telemetry-status.tsp).
- Realised by: BE-5, FE-1 · Verified by: BE-5 (integration), T-2 (e2e)

---

## Scenarios #tester-role

| id | Scenario (Given / When / Then) |
|----|-------------------------------|
| **S1** | **Given** `hash_doc_ids = false` (default) · **When** a search returns doc_ids · **Then** JSONL entry has raw `result_doc_ids` and `doc_ids_hashed: false` |
| **S2** | **Given** `hash_doc_ids = true` and salt loaded · **When** a search returns N doc_ids · **Then** JSONL entry has N 64-char hex strings in `result_doc_ids` and `doc_ids_hashed: true`; none match the raw SHA-256 of the ingested file paths |
| **S3** | **Given** `hash_doc_ids = true` and no salt file exists · **When** the server starts · **Then** a `.telemetry-salt` file is created with mode 600, a WARNING is logged, and subsequent searches produce hashed entries |
| **S4** | **Given** `hash_doc_ids = true` and salt file already exists · **When** the server restarts · **Then** the same hashed values are produced for the same doc_ids (salt is reused, not regenerated) |
| **S5** | **Given** `hash_doc_ids = true` and the salt file is unreadable (mode 000) · **When** the server starts · **Then** an ERROR is logged, hashing falls back to disabled for the session, JSONL entries have raw doc_ids, and the server does not crash |
| **S6** | **Given** `hash_doc_ids = true` and salt loaded · **When** an entry is built whose `result_doc_ids` is `None` (note: `from_search_tool_result` requires a `list[str]`, so `None` arises only via the model default on non-search factories) · **Then** the entry is written normally; no hasher is called; `doc_ids_hashed: false` |
| **S7** | **Given** `hash_doc_ids = true` and salt loaded · **When** a search returns an empty `result_doc_ids` list · **Then** entry has `result_doc_ids: []` and `doc_ids_hashed: true` |
| **S8** | **Given** `hash_doc_ids = true` · **When** many search requests arrive concurrently (async, single event loop — the writer is an `asyncio.Queue`, the hasher is a pure function over an immutable salt, so there is no thread-level race) · **Then** exactly N JSONL entries are written, each is valid JSON, each `result_doc_ids` has the expected length, and all have `doc_ids_hashed: true` |
| **S9** | **Given** `hash_doc_ids = true` and salt loaded · **When** an MCP `search` or `search_with_context` call is made · **Then** JSONL entry has hashed doc_ids (same behaviour as REST endpoint) |
| **S10** | **Given** server running with `hash_doc_ids = true` and salt loaded · **When** `GET /status` is called · **Then** response includes `telemetry.hash_doc_ids_enabled: true` |
| **S11** | **Given** server running with `hash_doc_ids = false` · **When** `GET /status` is called · **Then** response includes `telemetry.hash_doc_ids_enabled: false` (or `telemetry: null` if telemetry disabled) |
| **S12** | **Given** `hash_doc_ids = true` · **When** `archon-search status` is run · **Then** output displays `hash_doc_ids_enabled: true`. S12 covers both: (a) server reachable → output displays hash_doc_ids_enabled; (b) server unreachable → service state shown, telemetry section omitted. |
| **S13** | **Given** the same salt and the same doc_id · **When** `hash_doc_id()` is called twice · **Then** both calls return the identical 64-char hex string (determinism) |
| **S14** | **Given** the same salt and two different doc_ids · **When** `hash_doc_id()` is called for each · **Then** the two results are distinct (distinct outputs for distinct inputs — a smoke check, not a proof of HMAC collision resistance) |
| **S15** | **Given** `ARCHON_SEARCH_DATA_DIR=/custom/path` and `hash_doc_ids = true` · **When** the server starts · **Then** the salt file is created at `/custom/path/.telemetry-salt` (not `~/.archon-search/.telemetry-salt`) |
| **S16** | **Given** `hash_doc_ids = true` · **When** an entry goes through `from_explain_result`, `from_error`, `from_route_response`, or `from_search_multi_result` · **Then** those entries are unaffected — no hasher param, `doc_ids_hashed` stays `false` (`from_search_multi_result` does not populate `result_doc_ids`, so it must never gain a hasher) |

---

## Frontend — Presentation #frontend-role

**Scope:** Add a NEW `GET /status` HTTP code path to `archon_search/cli/status.py` and display `telemetry.hash_doc_ids_enabled`. Today `cli/status.py` only reports OS service state via `_get_service().status()` — it makes no HTTP call. FE-1 adds the HTTP path, modelled on `maintenance_cmd.py` (the command that already calls `GET /status`). Because `/status` is NOT in `middleware_auth.py`'s `_EXEMPT_PATHS`, it requires a `Bearer` token: resolve the API key (option → `ARCHON_SEARCH_API_KEY` env → key file) and send `Authorization: Bearer <token>`. **Note:** `_resolve_api_key` is already duplicated across 5 CLI modules (`maintenance_cmd.py`, `collection.py`, `backup_cmd.py`, `key_cmd.py`, `export_cmd.py`). For this task, **copy the established pattern** (a 6th copy) to stay in scope; extracting a shared helper into `cli/_helpers.py` is pre-existing tech-debt, tracked separately, NOT part of FE-1. No web UI exists; Presentation = CLI.
**Owns layer:** Presentation.

**Tasks** *(checkable in the Task Breakdown)*
- Presentation: FE-1 — Enhance `archon-search status` CLI to display hash_doc_ids_enabled

**Done when**
- [x] `archon-search status` shows `hash_doc_ids_enabled` from the live server — S12
- [x] When server is unreachable, status shows service state only (graceful degradation) — S12

---

## Backend — Entities · Use Cases · Adapters · Frameworks #backend-role

**Scope:** All implementation. No web frontend. Writes unit and integration tests test-first for every task.
**Owns layers:** Entities, Use Cases, Interface Adapters, Frameworks & Drivers.

**Tasks by layer** *(checkable in the Task Breakdown)*
- Entities: BE-1 (TelemetryConfig), BE-3 (TelemetryEntry + factory), BE-2 partially (`hash_doc_id` pure fn)
- Interface Adapters: BE-4 (wire hasher in routes + MCP), BE-5 (GET /status extension)
- Frameworks & Drivers: BE-2 partially (`load_or_create_salt` file I/O + app.py lifespan salt init), BE-5 partially (schemas.py model) — `telemetry/hasher.py` spans Entities + Frameworks like `key_manager.py`; see layer map

**Done when**
- [ ] JSONL entries have `doc_ids_hashed: bool` in all code paths — S1, S2
- [ ] With `hash_doc_ids = true`, `result_doc_ids` are HMAC-hashed (64 chars, not raw) — S2
- [ ] Salt file created on first start (mode 600, WARNING); reused on restart — S3, S4
- [ ] Salt unreadable → ERROR + fallback, no crash — S5
- [ ] `result_doc_ids = None` / empty list handled correctly — S6, S7
- [ ] MCP search tools hash consistently with REST — S9
- [ ] `GET /status` includes `telemetry.hash_doc_ids_enabled` — S10, S11
- [ ] `from_explain_result`, `from_error`, `from_route_response` unchanged — S16
- [ ] `hash_doc_id()` is deterministic and collision-resistant at scale — S13, S14
- [ ] `ARCHON_SEARCH_DATA_DIR` override places salt in correct dir — S15

---

## Tester #tester-role

**Scope:** the tester owns **e2e and manual** tests plus the project **close-out**. Unit and integration tests belong to the implementing dev in each task's `Tests` block. Per project mandate: **every scenario must have at least one e2e test; no scenario is manual-only**.

**Tasks** *(checkable in the Task Breakdown)*
- T-1 — e2e: core hashing behaviour (S1, S2, S6–S9, S13, S14, S16)
- T-2 — e2e: status observability (S10–S12)
- T-3 — e2e: salt edge cases and data-dir override (S3–S5, S15)
- T-4 — close-out

**Allocation** — each scenario at the cheapest level that proves it; e2e mandatory per mandate

| Scenario | Cheapest unit/integration level | e2e (mandatory) |
|----------|---------------------------------|-----------------|
| S1 — default off, raw doc_ids | unit (entry factory) | ✓ T-1 |
| S2 — hashing on, HMAC output | unit (hash_doc_id) + integration (routes) | ✓ T-1 |
| S3 — salt generated on first start | integration (lifespan + real file) | ✓ T-3 |
| S4 — salt reused across restarts | integration (lifespan restart sim) | ✓ T-3 |
| S5 — salt unreadable fallback | integration (lifespan with mode-000 file) | ✓ T-3 |
| S6 — result_doc_ids=None | unit (entry factory) | ✓ T-1 |
| S7 — empty result_doc_ids | unit (entry factory) | ✓ T-1 |
| S8 — concurrent requests | integration (async test client, many concurrent requests — NOT ThreadPoolExecutor; the writer is single-loop asyncio) | ✓ T-1 |
| S9 — MCP tools hash correctly | integration is **white-box** (assert the hasher closure is wired into the MCP tool — TestClient cannot drive the FastMCP transport); the real MCP wire path is exercised only by the T-1 e2e via an actual MCP client | ✓ T-1 |
| S10 — GET /status hash_doc_ids_enabled=true | integration (TestClient) | ✓ T-2 |
| S11 — GET /status hash_doc_ids_enabled=false | integration (TestClient) | ✓ T-2 |
| S12 — CLI status shows flag | unit (mocked HTTP) + integration | ✓ T-2 |
| S13 — determinism | unit (hash_doc_id) | ✓ T-1 |
| S14 — distinct outputs | unit (hash_doc_id) | ✓ T-1 |
| S15 — data-dir override | integration (env var + tmp_path) | ✓ T-3 |
| S16 — other factories unaffected | unit (factory tests) | ✓ T-1 |

---

## Documentation update

Docs the feature touches — the close-out task works through this list.

- [x] `Documentation/Backlog/D8-hashed-doc-id-telemetry-brief.md` — corrected: removed the stale "32 hex" truncation in Core Flow (→ full 64-char), removed `from_search_multi_result` from the hashed-factory scope, clarified the `result_doc_ids = None` edge case, and added the salt-co-location threat caveat + wrong-size-salt fallback
- [ ] `Documentation/Backlog/D8-hashed-doc-id-telemetry-team-plan.md` — this file
- [ ] `Documentation/Backlog/d8-telemetry-hasher.tsp` — this file (contract artefact)
- [ ] `Documentation/Backlog/d8-telemetry-entry.tsp` — this file (contract artefact)
- [ ] `Documentation/Backlog/d8-telemetry-status.tsp` — this file (contract artefact)
- [ ] `archon_search/CLAUDE.md` / `CLAUDE.md` — update telemetry section to describe `hash_doc_ids`, salt file, `doc_ids_hashed` field
- [ ] `Documentation/Architecture/150_security_and_privacy_architecture.md` — remove "accepted risk" caveat for path-leak via `doc_id`; document the HMAC-hashing mode
- [ ] `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` — close SEC-2
- [ ] `Documentation/ADRs/ADR-05` (or equivalent telemetry ADR) — append "Amendment" section recording that hashed-doc-id mode is implemented (ADRs are append-only)
- [ ] `archon-search.toml.example` — add `hash_doc_ids = false` with explanatory comment
- [ ] `tests/server/openapi_snapshot.json` — regenerate with `uv run --python 3.12` after BE-5 adds `telemetry` field to StatusResponse (this is the snapshot `tests/server/test_openapi_snapshot.py` actually checks; a stale `tests/contract/openapi_snapshot.json` exists but is not the one under test)
- [ ] `Documentation/SecurityGuide/04_telemetry_privacy.md` — update to document the HMAC hashing mode, salt lifecycle, and the salt co-location threat model scope

---

## Open questions

All open questions from the brief were resolved before planning. No new unknowns surfaced during investigation. Table Q1 (CLI offline behavior) is also resolved: omit telemetry section if server unreachable, don't hard-fail (followed by FE-1).

**Resolved in this revision:**
- Q1 (doc_ids_hashed field): yes — added as `bool = False`, every entry
- Q2 (status observability): yes — `telemetry.hash_doc_ids_enabled` in GET /status
- Q3 (truncation length): 64-char full HMAC output (Option B) — no length validators to break

| id     | Area                    | Question                                                                                                                                                                                                                                                                                 |
| ------ | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Q1** | CLI status offline mode | ~~Should `archon-search status` show `[server unavailable]` or omit the telemetry section when the server is unreachable?~~ **Resolved:** omit telemetry section if server unreachable, don't hard-fail (followed by FE-1). `maintenance_cmd.py` — which does call `GET /status` — falls back gracefully when the server is down; follow the same pattern. |

---

## Task Breakdown

Single-role tasks in execution order, grouped into vertical slices.

### Dependency graph

```mermaid
flowchart LR
  K1([K1 · align])

  subgraph P1["Phase 1 · Search with hashed doc_ids"]
    BE1[BE-1 TelemetryConfig]
    BE2[BE-2 hasher.py + salt]
    BE3[BE-3 TelemetryEntry + factory]
    BE4[BE-4 wire routes + MCP]
    T1[T-1 e2e hashing]
  end

  subgraph P2["Phase 2 · Verify hashing state"]
    BE5[BE-5 GET /status + schemas]
    FE1[FE-1 CLI status]
    T2[T-2 e2e status]
    T3[T-3 e2e salt edge cases]
  end

  subgraph P3["Close-out"]
    T4([T-4 · close-out])
  end

  K1 --> BE1
  K1 --> BE2
  BE1 --> BE3
  BE2 --> BE3
  BE3 --> BE4
  BE4 --> T1
  BE1 --> BE5
  BE2 --> BE5
  BE5 --> FE1
  BE4 --> T2
  BE5 --> T2
  FE1 --> T2
  BE4 --> T3
  BE2 --> T3
  T1 --> T4
  T2 --> T4
  T3 --> T4
```

### Phase 0 · Kickoff

- [x] **K1** — Agree contracts C1, C2, C3 and scenarios S1–S16 with the team #team
    - — · 1.0h
    - completes C1, C2, C3
    - Tests

---

### Phase 1 · Search with hashed doc_ids *(walking skeleton: operator enables hashing, search writes HMAC doc_ids to JSONL)*

- [x] **BE-1** — Add `hash_doc_ids: bool = False` to `TelemetryConfig` and TOML parser #backend-role
    - Entities · 1.0h
    - needs K1 · completes C1 (partial)
    - Tests
        - [x] #unit_test — `test_hash_doc_ids_defaults_to_false` — TelemetryConfig() has hash_doc_ids=False
        - [x] #unit_test — `test_hash_doc_ids_parsed_from_toml_true` — `[telemetry] hash_doc_ids = true` sets the field
        - [x] #unit_test — `test_hash_doc_ids_parsed_from_toml_false` — explicit false parses correctly
        - [x] #integration_test — `test_telemetry_config_hash_doc_ids_in_load_config` — full `load_config()` round-trip with the field

- [x] **BE-2** — Implement `hash_doc_id()` and `load_or_create_salt()` in `archon_search/telemetry/hasher.py` (new file) + wire salt init into `app.py` lifespan #backend-role
    - Entities (`hash_doc_id`) + Frameworks & Drivers (`load_or_create_salt`, lifespan) — spans layers like `key_manager.py` · 3.0h
    - needs K1 · completes C1
    - Tests
        - [x] #unit_test — `test_hash_doc_id_returns_64_char_hex` — output is exactly 64 lowercase hex chars
        - [x] #unit_test — `test_hash_doc_id_differs_from_plain_sha256` — **(security)** for a known doc_id, `hash_doc_id(salt, doc_id) != hashlib.sha256(doc_id.encode()).hexdigest()` — proves the output is genuinely HMAC'd, not a silent no-op (both are 64 hex, so a format check alone would not catch a no-op) (S2)
        - [x] #unit_test — `test_hash_doc_id_deterministic` — same salt + input → same output (S13)
        - [x] #unit_test — `test_hash_doc_id_distinct_inputs` — different doc_ids → different outputs (S14)
        - [x] #unit_test — `test_load_or_create_salt_generates_file_atomically_mode_600` — creates file via `atomic_write_bytes` with mode 0o600, returns 32 bytes, WARNING logged (S3)
        - [x] #unit_test — `test_load_or_create_salt_reuses_existing` — existing file read without regenerating; returned bytes equal file contents (S4)
        - [x] #unit_test — `test_load_or_create_salt_returns_none_when_disabled` — flag=False → None, no file
        - [x] #unit_test — `test_load_or_create_salt_unreadable_logs_error_and_returns_none` — unreadable file → None + ERROR logged (S5). **Must not rely on `chmod 000`** (root bypasses POSIX bits in CI containers): either `@pytest.mark.skipif(os.getuid() == 0, ...)` or monkeypatch the read to raise `PermissionError`, so the fallback branch is deterministically exercised
        - [x] #unit_test — `test_load_or_create_salt_wrong_size_treated_as_corrupt` — existing file with != 32 bytes (0/16/1000) → ERROR logged, returns None (no weak HMAC from a short key) (F8)
        - [x] #integration_test — `test_app_state_set_on_startup_with_hashing_enabled` — lifespan sets `app.state.salt_bytes` (bytes) for status reporting and `app.state.doc_id_hasher` (Callable) for route/MCP injection when flag=True

- [x] **BE-3** — Add `doc_ids_hashed: bool = False` to `TelemetryEntry`; update `DOCUMENTED_SCHEMA_FIELDS`; add `doc_id_hasher` param to `from_search_tool_result` #backend-role
    - Entities · 2.0h
    - needs BE-1, BE-2 · completes C2
    - Tests
        - #unit_test — `test_from_search_tool_result_no_hasher_raw_ids_and_false_flag` — no hasher → raw ids, doc_ids_hashed=False (S1)
        - #unit_test — `test_from_search_tool_result_with_hasher_hashes_ids_and_sets_true` — hasher provided → hashed ids, doc_ids_hashed=True (S2)
        - #unit_test — `test_entry_with_none_result_doc_ids_has_hashed_false` — an entry whose `result_doc_ids` is `None` (the model default, as produced by non-search factories — `from_search_tool_result` requires a `list[str]` and is never passed `None`) has `doc_ids_hashed=False` and never invokes a hasher (S6)
        - #unit_test — `test_from_search_tool_result_empty_list_with_hasher` — [] → [], doc_ids_hashed=True (S7)
        - #unit_test — `test_doc_ids_hashed_in_documented_schema_fields` — "doc_ids_hashed" in DOCUMENTED_SCHEMA_FIELDS
        - #unit_test — `test_other_factories_have_no_doc_id_hasher_param` — from_explain_result, from_error, from_route_response, from_search_multi_result signatures unchanged (S16)
        - #unit_test — `test_doc_ids_hashed_field_defaults_false_in_model` — TelemetryEntry default
        - #integration_test — `test_telemetry_entry_jsonl_round_trip_with_doc_ids_hashed` — entry with doc_ids_hashed=True serialises and deserialises correctly via writer + reader

- [x] **BE-4** — Wire hasher into the `from_search_tool_result` call in the `/search` route handler (`routes_search.py`, currently line 268) and into the MCP `search` + `search_with_context` tools (`mcp.py`, currently lines 407 and 546); add toml.example entry #backend-role
    - Interface Adapters · 2.0h
    - needs BE-3 · completes S2, S8, S9
    - Scope notes
        - Thread the hasher into the MCP tool closures: `create_mcp_http_app()` must accept a `doc_id_hasher: Callable[[str], str] | None` parameter so both MCP call sites capture it at construction time — mirroring how `writer` is passed. MCP closures do NOT access `app.state` directly (the sub-app has its own state namespace). The closure is already built by the lifespan per C1; no construction happens inside `create_mcp_http_app`. Without this parameter, MCP cannot hash. (F16)
    - Tests
        - #integration_test — `test_search_endpoint_with_hashing_enabled_writes_hashed_doc_ids` — real app + search → JSONL entry has hashed ids (S2)
        - #integration_test — `test_search_endpoint_with_hashing_disabled_writes_raw_doc_ids` — flag=False → raw ids (S1)
        - #integration_test — `test_mcp_search_tool_with_hashing_enabled_writes_hashed_doc_ids` — MCP search path (S9)
        - #integration_test — `test_mcp_search_with_context_hashing` — MCP search_with_context path (S9)
        - #integration_test — `test_concurrent_async_search_requests_all_entries_consistent` — N concurrent searches via the async test client (NOT ThreadPoolExecutor); assert exactly N JSONL entries, each is valid JSON, each `result_doc_ids` has the expected length, all `doc_ids_hashed=true` (S8)

- [x] **T-1** — e2e: core hashing behaviour — S1, S2, S6, S7, S8, S9, S13, S14, S16 #tester-role
    - — · 4.0h
    - needs BE-4 · completes S1, S2, S6, S7, S8, S9, S13, S14, S16
    - Tests
        - #e2e_test — `test_e2e_hashing_disabled_raw_doc_ids_in_jsonl` — real server, hashing off, verify JSONL entries contain raw doc_ids and doc_ids_hashed=false (S1)
        - #e2e_test — `test_e2e_hashing_enabled_hmac_doc_ids_in_jsonl` — real server, hashing on; for each ingested file compute its raw path-derived SHA-256 doc_id, then assert NONE of those raw values appear in the JSONL `result_doc_ids`, every JSONL value is 64-char hex, and `doc_ids_hashed=true`. (Asserting "64 hex" alone is insufficient — raw SHA-256 is also 64 hex; the test must prove hashed ≠ raw.) (S2)
        - #e2e_test — `test_e2e_non_search_entry_unaffected_by_hashing` — when hashing is enabled, an entry produced by the error path (e.g. trigger a search error) has `doc_ids_hashed: false` and `result_doc_ids: null` (S6, S16)
        - #e2e_test — `test_e2e_empty_result_doc_ids_with_hashing` — search returning empty list, hashed flag true (S7)
        - #e2e_test — `test_e2e_concurrent_searches_all_entries_consistent` — parallel search calls, all JSONL entries correct (S8)
        - #e2e_test — `test_e2e_mcp_search_hashes_doc_ids` — MCP search tool via real MCP endpoint, verify JSONL hashed (S9)
        - #e2e_test — `test_e2e_hash_doc_id_deterministic_across_requests` — two searches with same doc, verify JSONL shows same hash both times (S13)
        - #e2e_test — `test_e2e_different_docs_different_hashes` — two different docs, verify distinct hashes (S14)
        - #e2e_test — `test_e2e_explain_and_error_entries_unaffected` — explain and error entries have no doc_ids_hashed=true anomaly (S16)
        - #e2e_test — `test_e2e_toggle_continuity_visible_in_jsonl` — start with hashing off, write one entry, restart with hashing on, write another entry; verify JSONL contains both `doc_ids_hashed: false` and `doc_ids_hashed: true` entries distinguishable by the field (S1+S2 cross-entry read, proves log-segment discriminator per Known Limitations) (informally covers the toggle transition gap)

---

### Phase 2 · Verify hashing state *(operator can confirm hashing is active from status)*

- [x] **BE-5** — Add `TelemetryStatusDetail` to `schemas.py`; add `_build_telemetry_status()` to `routes_status.py`; add `telemetry` field to `StatusResponse` #backend-role
    - Interface Adapters · 2.0h
    - needs BE-1, BE-2 · completes C3, S10, S11
    - Tests
        - [x] #integration_test — `test_telemetry_status_detail_hash_enabled_when_salt_loaded` — HTTP-layer: flag=True + salt loaded → hash_doc_ids_enabled=True (S10)
        - [x] #integration_test — `test_telemetry_status_detail_hash_disabled_when_no_salt` — HTTP-layer: flag=False + salt=None → hash_doc_ids_enabled=False (S11)
        - [x] #integration_test — `test_telemetry_status_null_when_telemetry_disabled` — telemetry.enabled=False → telemetry: null
        - [x] #integration_test — `test_get_status_telemetry_s5_hash_configured_but_salt_missing` — S5 fallback: hash_doc_ids=True but salt=None → hash_doc_ids_enabled=False
        - [x] #integration_test — `test_openapi_snapshot_reflects_telemetry_field` — GET /openapi.json includes telemetry in StatusResponse schema

- [x] **FE-1** — Add a NEW `GET /status` HTTP code path (with bearer-token auth) to `archon_search/cli/status.py` to display `hash_doc_ids_enabled` — today the command only reports OS service state and makes no HTTP call #frontend-role
    - Presentation · 3.0h
    - needs BE-5 · completes S12
    - Tests
        - [x] #unit_test — `test_status_cli_shows_hash_doc_ids_enabled_true` — mocked authenticated GET /status response with hash_doc_ids_enabled=True → output contains flag (S12)
        - [x] #unit_test — `test_status_cli_shows_hash_doc_ids_enabled_false` — flag=False displayed correctly
        - [x] #unit_test — `test_status_cli_sends_bearer_token` — the resolved API key (option → `ARCHON_SEARCH_API_KEY` → key file) is sent as `Authorization: Bearer <token>` on the GET /status call
        - [x] #unit_test — `test_status_cli_handles_401_unauthorized` — server returns 401 → clear auth-failure message, no crash (distinct from unreachable)
        - [x] #unit_test — `test_status_cli_graceful_when_server_unreachable` — connection error → service state shown, no crash, telemetry section omitted
        - [x] #integration_test — `test_status_cli_integration_with_real_server` — real server running, status output contains hash_doc_ids_enabled (S12)

- [ ] **T-2** — e2e: status observability — S10, S11, S12 #tester-role
    - — · 2.0h
    - needs BE-5, FE-1 · completes S10, S11, S12
    - Tests
        - #e2e_test — `test_e2e_get_status_hash_doc_ids_enabled_true` — real server with hashing on, GET /status → telemetry.hash_doc_ids_enabled=true (S10)
        - #e2e_test — `test_e2e_get_status_hash_doc_ids_enabled_false` — hashing off → false (S11)
        - #e2e_test — `test_e2e_cli_status_displays_hash_doc_ids_flag` — run archon-search status CLI against real server, verify output contains hash_doc_ids_enabled (S12)

- [ ] **T-3** — e2e: salt lifecycle edge cases and data-dir override — S3, S4, S5, S15 #tester-role
    - — · 3.0h
    - needs BE-2, BE-4 · completes S3, S4, S5, S15
    - Tests
        - #e2e_test — `test_e2e_salt_file_created_on_first_start_with_mode_600` — real tmp data dir, hashing on, server start → .telemetry-salt exists with mode 600 (S3)
        - #e2e_test — `test_e2e_salt_reused_across_server_restarts` — explicit steps: (1) start server, (2) search and record the hashed doc_id from JSONL, (3) stop server, (4) restart with the same data dir, (5) search the same doc, (6) assert the JSONL hashed doc_id is byte-identical to step 2. Not merely "salt file still exists". (S4)
        - #e2e_test — `test_e2e_unreadable_salt_server_falls_back_and_does_not_crash` — make the salt file unreadable before server start → server starts, hashing disabled, raw doc_ids in JSONL (S5). **`chmod 000` is bypassed by root** (CI containers) — guard with `@pytest.mark.skipif(os.getuid() == 0, ...)`, or inject the failure another way, so the fallback is genuinely exercised rather than silently passing.
        - #e2e_test — `test_e2e_custom_data_dir_salt_in_correct_location` — ARCHON_SEARCH_DATA_DIR=/tmp/custom, hashing on → salt at /tmp/custom/.telemetry-salt (S15)

---

### Phase 3 · Close-out

- [ ] **T-4** — Project close-out & acceptance fact-check #tester-role
    - — · 4.0h
    - needs T-1, T-2, T-3 · completes (acceptance gate)
    - Tests
    - Duties
        - Update all documentation per the "Documentation update" section — 150_security_and_privacy_architecture.md (remove accepted-risk caveat), 530_technical_debt_refactoring_roadmap.md (close SEC-2), ADR-05 (append Amendment), CLAUDE.md telemetry section, archon-search.toml.example, tests/server/openapi_snapshot.json (regenerate with `uv run --python 3.12`), Documentation/SecurityGuide/04_telemetry_privacy.md (HMAC hashing mode, salt lifecycle, salt co-location threat model scope).
        - Fix all build / compiler warnings, if any.
        - Run the full test suite (`uv run pytest`); fix every failing test, including any unrelated to this feature.
        - Validate every Acceptance criterion one-by-one with a fact check — no assumptions; confirm each is genuinely done.

**Critical path:** K1 → BE-1 → BE-3 → BE-4 → T-1 → T-4.
