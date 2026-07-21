# Feature Brief: Permission-Aware Search Snippets (G15)

## Problem

When an operator or developer searches a collection and gets results back, they can't tell — for any given result — whether it's visible to everyone, restricted to specific teams, or accidentally open because an access-rules file failed to load. All three cases look identical in the current response.

## Goal

Each search result optionally includes an `acl_gate` field that tells the caller exactly who can see that chunk, where the access rule came from, and whether anything went wrong when the rule was loaded. The `/explain` debugging endpoint always includes `acl_gate`, with no opt-in needed.

## Users & Context

**Primary user**: an operator or developer auditing their ACL setup — checking that access rules are applied correctly, or investigating why a document is visible to unexpected parties. They're examining search results closely, often using `curl` or a script, and need complete diagnostic information.

**Secondary user**: an application developer building a product on top of the search API. They use the `allowed_principals` field (the list of who can see each result) to drive client-side behavior — for example, showing a "restricted" badge next to certain results. They ignore the `source` and `warnings` fields.

## Core Flow

1. Caller sends a search request with `acl_context: true` added to the query.
2. The server runs the search as normal — results that the caller can't see are already excluded before this feature does anything.
3. For each result that comes back, the server attaches an `acl_gate` object describing: who can see this chunk, where that rule came from, and whether any warning fired when the rule was loaded at index time.
4. The caller inspects `acl_gate` on each result to understand the access state of that chunk.
5. For the `/explain` endpoint, `acl_gate` is always included — no flag needed.

## In Scope

- `acl_context: bool` (default `false`) query parameter on `POST /search`
- `acl_gate` object on every search result when `acl_context=true`:
  - `allowed_principals: list[str] | null` — the full list of namespace names that can see this chunk (`null` means open to all)
  - `source: "frontmatter" | "sidecar" | "collection_default"` — where the access rule came from
  - `sidecar_path: str | null` — path to the sidecar file, if the rule came from one
  - `warnings: list[str]` — any problems that occurred when loading the access rule at index time (e.g., sidecar file was too large and fell back to open)
- `acl_gate` added unconditionally to `/explain` endpoint responses
- Three new per-chunk columns stored at ingest time: `acl_source`, `acl_sidecar_path`, `acl_warning` (all nullable; per-collection migration, no global schema bump)
- Minimum acceptance: `acl_gate` tested for sidecar, front-matter, collection-default, and warning cases; confirmed that chunks the requester cannot see remain excluded even with `acl_context=true`

## Out of Scope

- **Filtering search results by ACL source or warning** — this feature is read-only audit metadata; controlling what results are returned belongs to a future ACL policy feature (E6).
- **Admin-only view of principals** — all API keys see the full `allowed_principals` list; a privileged admin tier does not exist in the current auth model and is not introduced here.
- **MCP tool support** — the MCP `search` tool does not yet expose `acl_context`; add in a follow-up once the REST surface is stable.
- **Backfill of `acl_source` / `acl_sidecar_path` / `acl_warning` for existing chunks** — chunks indexed before G15 will show `null` for these three fields; re-indexing is the only path to populate them.

## Key Decisions

- **Return the full `allowed_principals` list**: the roadmap spec includes all namespace names, not just a confirmation that the caller can see the chunk. This exposes who else has access, which is intentional for a debugging/audit feature. In a shared multi-tenant deployment where separate customers use the same server, this leaks namespace names between tenants — documented as a future concern if SaaS hosting is ever pursued.
- **Store source provenance at ingest, not re-derive at query time**: checking whether the sidecar file still exists on disk at search time is fragile (the file may have moved or been deleted since indexing). Storing `acl_source`, `acl_sidecar_path`, and `acl_warning` as nullable columns at ingest is the reliable path.
- **Always return `acl_gate` on every result when the flag is set**: if `acl_gate` were omitted for open chunks, a caller couldn't tell "this chunk is open" from "we didn't check." `source: "collection_default"` makes the open-by-default case explicit.
- **Store warnings per chunk**: without a per-chunk `acl_warning`, an operator auditing search results cannot distinguish "intentionally open" from "open because the access-rules file failed to load." Both look identical without the stored warning.

## Edge Cases & Constraints

- **Pre-G15 chunks**: `acl_source`, `acl_sidecar_path`, and `acl_warning` will be `null` for all chunks indexed before this feature ships. `acl_gate` will still be returned with `source: null` on those chunks — the response schema is consistent; the provenance fields are just unknown.
- **Deny-all chunks (`acl: []`)**: these are already excluded by ACL filtering before results are returned; the caller never sees them and `acl_gate` is never constructed for them.
- **Multi-collection search**: `acl_gate` is per-result, so each result from each collection carries its own gate. The pool-wide `acl_filtered: bool` flag (already in the response) remains unchanged.
- **`acl_context=true` with no ACL-restricted chunks**: the flag is safe to pass on any collection; all results will show `source: "collection_default", allowed_principals: null, warnings: []`.
- **Telemetry**: the `acl_gate` fields do not contain query text and are safe to log; `allowed_principals` contains namespace names, which are already present in the server's key store and not considered private in the telemetry model.

## Open Questions

- Does `per-collection migrate` need to be called explicitly by the operator after upgrade, or can G15 apply the three new columns lazily on first ingest? (The existing `migrate_acl` pattern is lazy; check whether that precedent holds or whether explicit migration is safer here.)
- `source: "frontmatter"` vs `"sidecar"` — the roadmap spec uses `"sidecar"` to cover both front-matter and sidecar-file cases. Confirm the intended `source` enum values before implementation; three-way (`frontmatter | sidecar | collection_default`) is more precise but requires verifying front-matter detection is distinct in `resolve_acl`.
- Should `sidecar_path` store an absolute path or a path relative to the data directory? Absolute paths expose the server's filesystem layout to API callers.
- Does the `/explain` endpoint already have a Pydantic schema for its response, or is the schema added as part of G15? (A4 completed the endpoint; check `schemas.py` for existing `ExplainResponse`.)

## Future Iterations

- **MCP `search` tool support** for `acl_context` — deferred until REST surface is validated.
- **Admin-only principals visibility** — if multi-tenant SaaS hosting becomes a goal, add a privileged namespace tier that sees full `allowed_principals` while regular namespaces see only a boolean `accessible: true`.
- **ACL coverage stats surface** — `GET /collections/{name}/stats` already calls `get_acl_stats`; expose `acl_source` breakdown (how many chunks are sidecar-sourced vs. front-matter vs. default) as a collection health metric.
- **Per-collection ACL policy defaults (E6)** — allows a collection-wide default ACL that chunks inherit, making `source: "collection_default"` mean something more than just "null."

## References

- **Team plan:** [2026-07-15-360-permission-aware-search-snippets-team-plan.md](./2026-07-15-360-permission-aware-search-snippets-team-plan.md)
- [[Documentation/Backlog/03_world_class_roadmap.md]] `[user+docs-agent]` — Complete G15 specification: `acl_gate` field schema, effort/impact estimates (effort 0.28, impact 0.62, ratio 2.21), minimum acceptance criteria
- [[Documentation/Architecture/150_security_and_privacy_architecture.md]] `[docs-agent]` — Foundational ACL semantics (three-state: null/open, empty/deny-all, list/allow); source resolution order; fail-open behavior; namespace isolation
- [[Documentation/Completed/A4-explain-endpoint-brief.md]] `[docs-agent]` — Explain endpoint design; G15 adds `acl_gate` to this completed endpoint
- [[Documentation/Completed/A4-explain-endpoint-plan.md]] `[docs-agent]` — Detailed explain endpoint implementation guide; ACL filtering on routing candidates
- [[Documentation/Completed/e0b-silent-failure-transparency-brief.md]] `[docs-agent]` — Shipped ACL feature (L14): sidecar warning handling pattern; `IngestResult.warnings` flow
- [[Documentation/Completed/B3-server-side-multi-collection-search-brief.md]] `[docs-agent]` — Multi-collection ACL filtering; pool-wide `acl_filtered` flag
- [[Documentation/Architecture/600_api_reference_or_public_interface.md]] `[docs-agent]` — Current `SearchResponse` schema showing existing `acl` field on each result
- [[archon_search/acl.py]] `[code-agent]` — `resolve_acl`, `apply_acl_filter`, `is_acl_allowed`; three-state semantics; fail-open behavior
- [[archon_search/_types.py]] `[code-agent]` — `ChunkRecord` and `SearchResult` dataclasses; existing `acl: list[str] | None` field
- [[archon_search/server/routes_search.py]] `[code-agent]` — `SearchRequest` / `SearchResponse` schemas; where `acl_context` parameter and `acl_gate` field are added
- [[archon_search/pipeline.py]] `[code-agent]` — Search pipeline; ACL filtering at multiple stages; `search_with_context`
- [[archon_search/store.py]] `[code-agent]` — `SearchResult` construction from LanceDB rows; `get_acl_stats`; migration pattern reference
- [[archon_search/filters.py]] `[code-agent]` — `SearchFilters` model; no existing ACL filter field

## Recommendation

G15 is the right feature to build now — it has the highest effort-to-impact ratio in the G-series roadmap (2.21) and closes a real operator pain point: you currently can't tell from a search response whether a document's open access is intentional or accidental. The hardest part is the ingest-side schema change: three new nullable columns must be added to every collection's chunk table, and chunks indexed before the upgrade will permanently show `null` provenance — operators should know that before shipping. What must not be compromised is the per-chunk warning field: omitting it would make `acl_gate` look like a complete audit record when it isn't, which is worse than not having it at all.
