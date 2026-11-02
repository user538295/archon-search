# 20. An Application-Service Layer With Test-Enforced Boundaries and Interface Parity

**Status**: Accepted
**Date**: 2026-11-02
**Deciders**: archon-search maintainers
**Supersedes**: none. No earlier ADR describes the layering of `server/` or the location of the authorization policy.

## Context

REST routes, MCP tools and the OpenAI shim implement the same features separately, and the CLI duplicates its own helpers. Nothing stopped the drift between them:

- The modules shared by search, search with context and explain (the search budget, the HyDE resolution, the scope-filter validator) and the transport-free half of the authorization policy lived under `archon_search/server/` only because the first callers were routes. MCP, the search and explain routes (and the shim, for the search budget only) imported them from there, and `server/authz.py` mixed the FastAPI dependencies with the policy.
- No layer-boundary check existed. `tests/test_import_boundary.py` forbids only imports from the `archon.` namespace. Outside the composition root `app.py`, eight imports crossed from a `server/` module to a route module (the list is the allowlist under Decision 3).
- No test compared what REST, MCP and the CLI expose. A feature could exist on one interface and be missing from another without anyone having decided that.
- The CLI kept five copies of `_resolve_api_key` and seven copies of the default API URL (six command modules plus `_helpers.py`), and four command modules (`sync`, `ingest`, `jobs_cmd`, `graph_cmd`) imported them from the `collection` command module.

The interface-parity roadmap (`Documentation/Backlog/interface-parity/roadmap.md`) makes every feature available on every interface through one implementation. Its first slice, S0, sets the layer contract and enforces it, so each later slice touches one service and one thin adapter per interface.

## Decision

**Add `archon_search/services/` as the application layer, enforce the dependency direction and interface parity with two new test files (an import-boundary scan and a parity registry check), and move the four shared modules into it.** A feature has one implementation, in `services/`; REST, MCP and the shim are adapters over it (roadmap principle 1). Dependencies point inward: adapters (REST routes, MCP, the shim) import `services/`; `services/` imports the core (every `archon_search` module outside `server/` and `cli/`, including `install` and `platform`) and none of the packages rule 1 below denies; the CLI reaches the server over HTTP and imports neither `services/` nor `server/` (rule 4; the one exception is the lazy `run_server` import in `cli/serve.py`; the CLI still imports core modules). The one-implementation rule is a design direction, not a check: this ADR builds the layer and the guards, and each roadmap slice moves its feature into `services/`.

1. **Four modules move to `services/`; the old modules are deleted with no shim, alias or re-export.**
   - `server/_search_budget.py` becomes `services/search_budget.py` (moved, behaviour unchanged).
   - `server/_hyde.py` becomes `services/request_hyde.py` (renamed because the root `hyde.py` exists); its logger is now `archon_search.services.request_hyde`.
   - `server/_validators.py` becomes `services/validators.py`.
   - The policy half of `server/authz.py` becomes `services/authz.py`: `AuthIdentity`, `ForbiddenError`, `FORBIDDEN_CODE`, `ADMIN_REQUIRED_DETAIL`, `ADMIN_KEY_GRANT_DETAIL`, `FOREIGN_NAMESPACE_DETAIL`, `require_admin`, `authorize_key_create`, `authorize_key_listing_namespace`, `can_manage_key` and `caller_may_revoke`. `server/authz.py` keeps `identity_of`, `require_admin_dependency`, `caller_identity` and `ADMIN_ONLY_RESPONSES`, imports the policy from services and defines no `__all__`.
   - `services/__init__.py` is a docstring only. The adapters keep their module-level `_SEARCH_TIMEOUT_SECONDS` aliases and pass the timeout to each budgeted call explicitly, so a test that patches an alias still takes effect. Wire text, behaviour and both OpenAPI snapshots are unchanged.
   - `tests/services/test_old_server_modules_removed.py` asserts the three old `server` modules no longer import.

2. **`tests/test_layer_import_boundary.py` enforces four rules by AST.** It scans every `.py` file under `archon_search/services/`, `archon_search/server/` and `archon_search/cli/` and derives one edge per (importing file, imported module, scope), where the scope is module level, function local or `TYPE_CHECKING` only. Function-local and type-only imports count like module-level ones; `from pkg import name` targets the submodule when it exists; a relative import is resolved first.
   - **Rule 1.** `services/` imports nothing from `archon_search.server` or `archon_search.cli` and nothing from `fastapi`, `starlette`, `fastmcp`, `click`, `httpx` or `uvicorn`, in any scope; it does not import `anthropic` at module level (a function-local or `TYPE_CHECKING` import passes; ADR C4 keeps the package optional, `C4-hyde-external-llm-dependency.md:26`; denying the module-level import is this ADR's reading, and ADR 19 moved C4's missing-package refusal to a startup-only guard).
   - **Rule 2.** No `server/` file imports a `routes_*` module (the OpenAI shim is one) or `archon_search.server.mcp`.
   - **Rule 3.** A `cli/` file imports no other `cli/` module except `cli._helpers`; `cli/main.py`, which registers the commands, is exempt.
   - **Rule 4.** `cli/` imports nothing from `archon_search.services` or `archon_search.server`.
   - **Permanent exceptions** (hard-coded, never stale): `server/app.py`, the composition root, imports any `routes_*` module and `server.mcp` in any scope; `cli/serve.py` may import `archon_search.server.app` function-locally (today `from archon_search.server.app import run_server`).

3. **A ratcheting allowlist holds the known violations.** Eight rule 2 edges exist today and are seeded, each keyed by (file, module, scope): `_collection_job.py` to `routes_jobs`; `mcp.py` to `routes_explain` and `routes_export` (module level) and to `routes_collections` (function local); `routes_collections.py`, `routes_graph.py` and `routes_sync.py` to `routes_jobs`; `routes_status.py` to `routes_collections`. The test fails on a violation that is not listed and on a listed edge that is no longer a violation, so the list only shrinks. Each later slice deletes the entries it resolves; roadmap S33 (close-out) does not name the list, so an entry no slice has resolved by then has no owner yet and must be removed or turned into a documented permanent exception there.

4. **A second scan in the same file (the moved-authz scan) keeps the moved authz names off `server/authz`.** No file under `archon_search/` or `tests/` may import one of the eleven moved names (or `*`) from `archon_search.server.authz`, absolute or relative, in any scope, and that module may define no `__all__`. It has no allowlist.

5. **The "never `str(exc)` on the wire" guard now covers `services/`** (`tests/test_no_wire_str_exc.py` guards `server/`, `jobs/` and `services/`, and fails when a guarded directory is missing or holds no Python files).

6. **`tests/parity/` is the parity registry.** `tests/parity/registry.py` is a plain data module of capabilities: one per roadmap slice S1-S32 (S33 is the close-out and has no row of its own), the roadmap waivers (including the MCP transport mount) and a row for collection descriptions. Each lists its service method and, per interface (REST, MCP, CLI), the live surface identifier or a waiver: `permanent`, or `pending` naming the slice that closes the gap. `tests/parity/test_parity.py` reads the live surfaces: the REST routes of an app built with docs and the OpenAI shim enabled (without running its lifespan; the identifier is `METHOD path` with `HEAD` dropped, a `Mount` is its path), the real FastMCP tool list through `tests/_mcp_tools.py` (key tools included), and the Click tree through `list_commands` and `get_command` (space-joined paths; a group counts only when invokable, as `backup`). It fails on four things: a live surface no claim names, a claimed surface that is not live (except `/mcp`, which exists only during the app lifespan), a gap with neither identifier nor waiver, and a pending waiver that names no slice. The seed was derived by hand from the live surfaces (every live route, tool and command is claimed), with the roadmap waivers and a permanent waiver for the missing MCP describe tool (roadmap D9, ADR 19).

7. **One strict API key resolver and one default server URL for the CLI.** `cli/_helpers.py` holds `_resolve_api_key`, `_DEFAULT_API_URL` and the `load_key` import. No command module imports another command module for them (rule 3). The best-effort lookups `api_key or load_key()` in `status.py`, `backup_cmd.py` and `maintenance_cmd.py` are deliberate and stay, so those modules still import `load_key`.

8. **The guards are proven to fail.** `tests/smoke/test_t3_guards_fail_in_real_pytest_run.py` copies each guard with the code it scans to a temporary directory, injects a violation (a `fastapi` import in `services/validators.py`; a deleted registry row) and runs the copy in a real pytest process, which must exit 1 and name the edge (boundary) or the unclaimed surface (parity). It lives in the local smoke lane, which the default run and CI exclude, so it is evidence that the guards bite, not a build gate.

The remaining pieces of the brief are **not** part of this decision and are built by roadmap slice S1 with its first consumer: `ServiceError`, the services container, `SearchService` and the per-adapter error-mapping tables. The policy now lives in `services/authz`, but making each service the single enforcement point (roadmap D8) arrives with each slice's service; admin-only routes keep their `require_admin_dependency` until then.

## Consequences

### Positive

- The direction of dependencies is checked, not conventional: a service that imports `fastapi`, a route that imports another route or MCP, or a command module that imports a sibling fails the build and the failure names the edge.
- A live REST route, MCP tool or CLI command that the registry does not claim fails the build. An explicit gap must carry a waiver, and a waiver for a missing slice must name that slice, so each recorded gap is written down and each slice closes its own.
- Both guards follow the repo's existing pattern of in-repo tests with no new dependency (`test_no_wire_str_exc`, `test_no_raw_durable_writes` and `test_no_fstring_sql` also scan the source); the boundary test ratchets an allowlist as `test_no_wire_str_exc` does, and the registry does not.
- Authorization policy has one definition shared by REST and MCP, and it loads without FastAPI.

### Negative / Tradeoffs

- **Only direct imports are checked.** A transitive coupling through a core module is invisible (`install` is core and imports `click`, so `services/` could reach `click` through it). The layer scan covers only `services/`, `server/` and `cli/`, so a core module that imports `services/`, `server/` or `cli/` goes unflagged (the only module outside the scanned trees importing `cli` today is `__main__.py`). The moved-authz scan is the only one over `archon_search/` and `tests/`, and it looks only for moved authz names.
- **Blind spots of a syntactic scan.** `importlib.import_module` and `__import__` are not seen; a `TYPE_CHECKING` alias and the negated guard `if not TYPE_CHECKING:` err on the strict side; `from pkg import *` is an edge to `pkg` only. The moved-authz scan sees only `from archon_search.server.authz import <name>` statements (absolute or relative, and `*`): `import archon_search.server.authz` or `from archon_search.server import authz` followed by attribute access, and a dotted string patch target, escape it.
- **The pattern is reviewed after roadmap slice S1** (the pilot), and this contract is amended before S2 starts; later changes are recorded as dated entries in an Amendments section of this ADR, as ADR 19 does, not by editing the text above.
- **The registry is hand-derived once and is not generated.** It does not ratchet: a waiver that outlives its gap is not flagged (no stale-waiver rule), and a `pending` waiver is checked only for a non-blank slice name, not that the slice exists. Nothing requires a capability to claim all three interfaces: a feature claimed on one interface only passes if its live surface is claimed, so the full three-interface rows rest on the hand-written seed and on review. Field-level parity (every command field on every interface) is not checked; roadmap S1 evaluates it.
- **The registry's `service_method` is `None` and unchecked** until a slice creates the service method; the parity test never reads it.
- **`/mcp` is claimed with its waiver but exempt from the missing check**, because the mount exists only during the app lifespan, which the test does not run.
- **The `anthropic` rule is module level only.** A function-local or `TYPE_CHECKING` import of the optional package in `services/` is allowed.

### Not verified

Behaviour of a future service that needs the HTTP request (the brief's contract C2 is that services never receive a request object, an `HTTPException`, a FastMCP `Context` or a Click context; rule 1 bans the framework imports, not a service that takes an untyped request, and no service exists yet to test that).

## Alternatives Considered

- **import-linter.** Would give contracts and reporting, but the repo has no import tooling and its guards are in-repo AST tests; the test also needs function-local and `TYPE_CHECKING` scopes in the allowlist key. Adopt it if the layer set grows.
- **Leave the shared modules in `server/`.** Keeps the diff small, and every later slice would keep importing across route modules.
- **A production-side registry.** Nothing in production would read it, so it would be unused code; the registry lives in `tests/`.
- **Move more than four modules now.** Speculative; each later slice moves what it needs. `warmup.py`, `readiness.py`, `_ingest_admission.py`, `_ingest_lock.py` and `_collection_job.py` stay.

## Cross-References

- Roadmap and briefs: [`roadmap.md`](../Backlog/interface-parity/roadmap.md), [`00_service_layer_foundation_brief.md`](../Backlog/interface-parity/00_service_layer_foundation_brief.md), [team plan](../Backlog/interface-parity/00_service_layer_foundation-team-plan.md), [task breakdown](../Backlog/interface-parity/00_service_layer_foundation-tasks.md), and the `.tsp` contracts C1-C5 beside them.
- [ADR 01](01_lancedb_as_local_vector_store.md) (a single-process store, no concurrent multi-writer access), [ADR 09](09_mcp_http_mount_and_namespace_propagation.md) (MCP mounted in the same app; the caller is resolved per request; partly superseded by ADR 17), [ADR C4](C4-hyde-external-llm-dependency.md) (the `anthropic` rule), [ADR 17](17_mcp_namespace_via_public_get_http_request.md) (`get_http_request` stays a module attribute of `server/mcp.py`), [ADR 19](19_shared_llm_client_and_provider_based_collection_descriptions.md) (the descriptions waiver).
- Code: `archon_search/services/`, `archon_search/server/authz.py`, `archon_search/cli/_helpers.py`.
- Tests: `tests/test_layer_import_boundary.py`, `tests/parity/`, `tests/services/`, `tests/test_no_wire_str_exc.py`, `tests/smoke/test_t3_guards_fail_in_real_pytest_run.py`.
