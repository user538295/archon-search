# Feature Brief: E1c — Graph-Path Provenance in /explain

## Problem
When `graph_mode` is active, operators and developers cannot see why a specific chunk was retrieved — the traversal chain from query entity through the graph to the result is invisible, making it impossible to debug graph retrieval quality or tune entity extraction and community parameters.

## Goal
`POST /explain` accepts `graph_mode` and returns a `graph_provenance` block on each graph-retrieved result showing the full traversal chain as a unified `list[TraversalStep]`. A response-level `graph_mode_applied` field confirms which mode was active. Non-graph results carry `graph_provenance: null`.

## Users & Context
Operators and developers who have deployed E1a/E1b and are tuning graph retrieval quality. They use `/explain` (already their go-to debug tool) when graph results look unexpected — wrong entities matched, community boundaries unclear, or graph-retrieved chunks scoring oddly against the reranker.

## Core Flow

1. User sends `POST /explain` with `graph_mode: "naive" | "local" | "global"` alongside a query.
2. The explain pipeline runs the same graph-mode retrieval as `/search` (entity resolution → graph traversal → candidate set) plus the existing per-stage scoring breakdown.
3. Each graph-retrieved result in `results[]` carries a `graph_provenance` object: a list of `TraversalStep` objects showing how the pipeline moved from query entity to this chunk.
4. Non-graph chunks (retrieved via standard hybrid search within the same request) carry `graph_provenance: null`.
5. The response root carries `graph_mode_applied: "naive" | "local" | "global" | null`.
6. Near misses carry no provenance — consistent with their already-reduced schema (no `text` field).

## In Scope

- `graph_mode: Literal["naive", "local", "global"] | None = None` added to `ExplainRequest`
- `graph_mode_applied: Literal["naive", "local", "global"] | None` added to `ExplainResponse`
- `graph_provenance: GraphProvenance | None` added to `ExplainResult` (null for non-graph chunks)
- `GraphProvenance` schema:
  ```
  steps: list[TraversalStep]   # ordered: query → graph → chunk
  ```
- `TraversalStep` unified schema covering all graph modes:
  ```
  entity: str                  # entity name at this step
  entity_id: str               # stable SHA-256 hex ID
  relationship: str | None     # typed relationship (naive mode: USES, DEPENDS_ON etc.; null for community steps)
  community_id: str | None     # community ID (local/global mode; null for naive steps)
  chunk_id: str | None         # terminal step only — the chunk this path leads to
  ```
- Naive mode example path: `[{entity:"AuthService"}, {entity:"TokenValidator", relationship:"DEPENDS_ON", chunk_id:"abc123"}]`
- Local/global mode example path: `[{entity:"AuthService", community_id:"comm_7"}, {chunk_id:"abc123"}]`
- All new fields added with `extra="forbid"` consistent with the existing `/explain` schema posture
- Changelog entry (additive response fields are non-breaking by HTTP convention; no `BREAKING.md` entry required)

## Out of Scope

- Provenance on `near_misses` — near misses already omit `text`; omitting provenance is consistent and avoids over-engineering
- Versioned `/v2/explain` endpoint — response additions are non-breaking; a single endpoint covers all clients
- Graph visualisation or interactive path explorer — rendering provenance is E8 (admin UI)
- `graph_mode` on `POST /search` response schema changes — `/search` returns results only; provenance lives in `/explain`

## Key Decisions

- **Unified `list[TraversalStep]` for all graph modes**: Naive and community traversal are structurally the same concept (query → graph → chunk); a discriminated union would force clients to branch on mode. Null fields (`relationship` for community steps, `community_id` for naive steps) make the mode readable without schema branching.
- **Response-level `graph_mode_applied`**: One field saves clients from scanning all results to determine if graph was active; costs nothing and mirrors the `hyde_applied` / `rag_fusion_applied` pattern already on `ExplainResponse`.
- **No provenance on near misses**: Near misses are secondary diagnostics; full traversal chains there add schema surface without proportional debugging value.
- **Additive change, no version bump**: New response fields don't break existing clients under HTTP conventions; `extra="forbid"` on `/explain` applies to inbound requests, not outbound responses.
- **`graph_mode_applied` semantics**: Set to the mode the pipeline ATTEMPTED to execute (not whether it yielded results). If `graph_mode="naive"` is requested and the graph layer runs (even if zero graph-retrieved results), `graph_mode_applied="naive"`. If `graph_mode="naive"` is requested but graph is disabled (guard fires before pipeline entry), the request fails with 422 — `graph_mode_applied` is irrelevant. The only case where `graph_mode_applied=null` with a successful response is when `graph_mode=null` was requested.

## Edge Cases & Constraints

- **`graph_mode` on `/explain` but graph not enabled**: Return a validation error (`graph_not_enabled`) — same guard as `/search`.
- **`graph_mode` on `/explain` but communities not built (local/global)**: Return a clear error (`graph_communities_not_built`) — same guard as `/search`, not a silent fallback.
- **Mixed results (some graph-retrieved, some standard hybrid)**: Legal — `graph_provenance` is null on standard chunks, populated on graph chunks. Both can appear in the same `results[]` list.
- **`graph_mode=naive` chunk matched via entity expansion but also retrieved by standard hybrid search**: Provenance takes precedence over pure hybrid score; the chunk appears once with `graph_provenance` populated.
- **Empty traversal path**: Should never occur — a graph-retrieved chunk always has at least one step. If it does occur (bug in graph layer), return `steps: []` rather than null, so the presence of the field still signals graph retrieval.
- **`extra="forbid"` on `ExplainRequest`**: Adding `graph_provenance` is a deliberate contract extension. `extra='forbid'` on `ExplainRequest` means any client sending unknown fields (not `graph_provenance` — no client sends response fields in requests) gets a 422. `ExplainResult` is a response model; clients never submit it. The additive field is safe.
- **`TraversalStep` with all-null optional fields**: Disallow at the Pydantic layer — each step must have at least one of `relationship`, `community_id`, or `chunk_id` set, otherwise it's a degenerate step with no graph information.

## Open Questions

- Should `graph_mode` on `ExplainRequest` default to `None` (standard explain, no graph) or mirror whatever `graph_mode` was passed to `/search`? The answer is `None` by default — the caller must explicitly opt in to graph explain, consistent with how `hyde` and `rag_fusion` work on the explain endpoint.
- Should `ExplainNearMiss` gain a `graph_provenance` field in a future iteration, or is the near-miss schema considered frozen? Worth deciding before E1c ships to avoid a follow-up schema bump.

## Future Iterations

- **Near-miss provenance**: Add `graph_provenance` to `ExplainNearMiss` once the core schema is stable and operators ask for it.
- **E8 admin UI**: Visual graph-path rendering — show the `TraversalStep` chain as a clickable node graph in the search playground.
- **Community provenance detail**: Add community member entity list to `TraversalStep` (currently only `community_id`) so operators can see which entities define the community without a separate lookup.

## Recommendation

E1c is the right completion to the E1 trilogy — without it, graph retrieval is a black box and operators cannot tune E1a/E1b effectively. The unified `TraversalStep` schema is the critical design call: get it right now or pay a breaking-change cost later when E1b+ modes land. The hardest part is threading traversal metadata from the graph layer through `ScoredSearchCandidate` to the route handler without polluting the non-graph code paths — the null-provenance pattern keeps this clean. Do not ship E1c before E1a and E1b are stable; provenance on incorrect graph results is misleading, not helpful.
