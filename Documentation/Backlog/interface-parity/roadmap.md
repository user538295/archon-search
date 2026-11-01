**Purpose**: Ordered, trackable roadmap for making every application feature available through REST, MCP and CLI on one shared implementation  
**Audience**: Maintainers and contributors working on the interface-parity slices  
**Status**: Draft  
**Last reviewed**: 2026-10-30  
**Next review**: 2026-11-30

# Interface parity roadmap

This file is the single place to see what is done and what is next. Tick a slice (`[x]`) in the same commit that lands it.

## Principles

1. A feature has exactly one implementation, in the application-service layer (`archon_search/services/`). REST, MCP and the OpenAI shim are adapters that convert transport to command and result to transport. The CLI is an HTTP client of REST.
2. The interface does not decide the feature set. A feature available on one interface is available on all of them, unless this roadmap records a waiver with a reason.
3. Dependencies point inward: services never import `server/` or `cli/`; MCP never imports route modules; route modules do not import each other.
4. Clean cutover: when a slice lands, every caller of that feature uses the service and the old code is deleted. No shims, no compatibility aliases. Every behavior change goes into `BREAKING.md`.
5. Each slice is a vertical slice: service + REST + MCP + CLI + tests + docs, shippable on its own.

The contract every slice follows is [Brief 00](./00_service_layer_foundation_brief.md). Briefs for later slices are written when the previous slice has merged, because the pilot (S1) may change the pattern.

## Waivers (agreed 2026-10-30)

- **CLI `start`, `serve`, `stop`, `install`, `uninstall`, `wizard`, `config`, and the local service-manager part of `status`**: local OS and config operations; they act on the machine, not on the running server.
- **`GET /health`, `GET /ready`, `GET /openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`**: probes and HTTP documentation surfaces. `/docs`, `/docs/oauth2-redirect` and `/redoc` exist only when `[server].docs_enabled` is true.
- **`/mcp` mount**: the MCP transport endpoint itself, not a feature. It exists only when `[mcp].enabled` is true and is mounted during app startup.
- **OpenAI-compatible `/v1/chat/completions` and `/v1/models`**: protocol adapter. They exist only when `[openai_shim].enabled` is true. It still calls the same services (S1).
- **Graph HTML viewer and view-token on MCP (S32)**: HTTP-specific browser credential. The CLI gets `graph view`.
- **Key admin toggle**: not built. The `admin` flag is immutable (see D3).
- **Rewrite a collection's description on MCP (`collection describe` on REST and CLI)** (agreed 2026-11-01): permanent waiver, agents keep the manual set and clear tools. A rewrite calls the language model, which costs money with a cloud provider and nothing with a local one, and no per-key limit exists (ADR 19, which lists a describe MCP tool as out of scope). See D9.

## Decisions log

- **D1** (2026-10-30): MCP ingest stays inline; REST ingest stays an async job. The service has one core and adapters pick the entry point.
- **D2** (2026-10-30): `delete_document` and `search_with_context` (MCP only today) get REST and CLI versions.
- **D3** (2026-10-30): Admin operations are exposed on MCP. MCP uses its own dedicated API key, non-admin by default. The `admin` flag is immutable after creation. To elevate, issue a new, time-boxed admin key (`key create --admin --expires`). To de-elevate, revoke that key. There is no in-place toggle (OWASP Session Management Cheat Sheet, "Renew the Session ID After Any Privilege Level Change"; MCP Security Best Practices, "Scope Minimization").
- **D4** (2026-10-30): Graph view and view-token: MCP waived, CLI `graph view` added.
- **D5** (2026-10-30): Where defaults or limits conflict between interfaces, the REST bounded values win (for example list limit 1-200, default 50).
- **D6** (2026-10-30): `ingest`'s `ingested_by` is supplied by the adapter (MCP becomes `"mcp"`, not `"http"`). Before S16, check that nothing keys on the old value.
- **D7** (2026-10-30): One brief per slice, written just-in-time. Brief 00 is the contract; later briefs reference it.
- **D8** (2026-10-30): Authorization is enforced once, inside the service. Adapters pass the caller identity.
- **D9** (2026-11-01): Agents (MCP) get no tool to rewrite a collection's description; they can only set or clear one by hand. The description feature's own plan chose this (ADR 19 lists a describe MCP tool as out of scope). The parity registry carries a permanent waiver for it.

## Slices

Legend: **converge** = the feature already exists on all three interfaces and moves onto the service; **new** = a missing interface gets the feature. Unticked means not started or in progress; add `(in progress)` to the line while a slice is open.

### Phase 0 — Foundation

- [ ] **S0 Service-layer foundation**: contract, module moves, boundary test, parity registry, CLI helper consolidation. [Brief](./00_service_layer_foundation_brief.md)

### Phase A — Retrieval

- [ ] **S1 Search (pilot)**: CLI new; MCP gains `acl_context` (and `top_k` per brief decision); OpenAI shim moves onto the service. [Brief](./01_search_brief.md)
- [ ] **S2 Search with context**: REST new; CLI new.
- [ ] **S3 Explain**: CLI new.
- [ ] **S4 Route**: MCP new; CLI new.

### Phase B — Jobs and keys

- [ ] **S5 Jobs get and list**: MCP new; CLI converge (`jobs show` and `jobs status` overlap).
- [ ] **S6 Job resume and cancel**: MCP new; CLI new.
- [ ] **S7 Keys (create, list, revoke, rotate)**: converge; CLI `key create --admin`; pin that a revoked key is rejected on the next request.

S5 comes before every slice that returns a job (S16-S24): MCP clients need a way to poll. S7 comes before the admin-operation slices (S15, S22-S24, S27): MCP admin access depends on the dedicated MCP key (D3).

### Phase C — Collections

- [ ] **S8 List collections**: converge (REST merges config-path collections, MCP does not).
- [ ] **S9 Collection info**: converge; REST route for all-collection metadata (`get_collections_meta`).
- [ ] **S10 List documents**: converge (cursor, total, 404); CLI new.
- [ ] **S11 Delete document**: REST new; CLI new.
- [ ] **S12 Expiring chunks**: MCP new; CLI new.
- [ ] **S13 Patch collection**: MCP gains `default_ttl_seconds`; CLI new.
- [ ] **S14 Delete collection**: MCP new.
- [ ] **S15 Add collection (admin)**: MCP new.

### Phase D — Ingest and collection lifecycle

- [ ] **S16 Ingest**: converge (D1, D6); REST `documents` input gets MCP and CLI equivalents or a recorded waiver.
- [ ] **S17 Reindex**: MCP new.
- [ ] **S18 Reindex metadata**: MCP new.
- [ ] **S19 Migrate (with pending)**: MCP new.
- [ ] **S20 Export**: converge.
- [ ] **S21 Import**: converge.

### Phase E — Operations

- [ ] **S22 Sync (admin)**: MCP new.
- [ ] **S23 Backup trigger (admin)**: MCP new.
- [ ] **S24 Maintenance trigger (admin)**: MCP new.
- [ ] **S25 Status**: MCP new; CLI keeps its local part (waived). The CLI `backup status` and `maintenance status` subcommands, which render the backup and maintenance sections of `GET /status` plus on-disk state, are claimed in this slice's registry row.
- [ ] **S26 Indexing-state**: decide whether it folds into Status; then MCP new and CLI new.
- [ ] **S27 Telemetry stats and entries (admin)**: MCP new; CLI new.

### Phase F — Graph

- [ ] **S28 Graph single collection**: converge (MCP lacks `format`; salience param naming differs); CLI new.
- [ ] **S29 Graph cross-collection**: converge; CLI new.
- [ ] **S30 Graph impact**: converge; CLI new.
- [ ] **S31 Graph rebuild communities**: MCP new.
- [ ] **S32 Graph view and view-token**: CLI `graph view`; MCP waived (D4).

### Close-out

- [ ] **S33 Close-out**: remove remaining parity waivers, make the parity test strict, refresh all affected docs, resolve ARCH-01 in the quality audit.

## Definition of done for every slice

1. Characterization tests pin the current REST and MCP behavior that is kept; behavior that is intentionally changed is listed in the slice brief.
2. The service implements the feature once; REST, MCP and (where applicable) the OpenAI shim only convert.
3. A CLI command exists, using the shared CLI helpers and the `--json` convention.
4. Duplicated constants, defaults and wire text are removed; each lives in one place.
5. The parity registry lists the feature as covered on every interface, or carries a written waiver.
6. The boundary allowlist loses the entries the slice resolved.
7. Docs and `BREAKING.md` are updated; the default test suite, `uvx ruff@0.17.3 check .` and the 85% coverage gate pass.
8. A smoke run exercises the feature through the real server on every interface it touches.
