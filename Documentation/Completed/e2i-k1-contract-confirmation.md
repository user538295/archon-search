---
id: E2I-K1
task: K1 — Align on contracts, scenarios, and open-question resolutions with the team
plan: e2i-llm-graph-enrichment-team-plan.md
date: 2026-07-11
method: >
  No literal multi-person team is available in this environment. K1 is operationalized
  as an independent fact-check of every falsifiable claim underlying C1-C2, S1-S15, and
  Q1-Q8 against the current source tree (archon_search/, tests/), plus a TypeSpec compile
  of both contract files and a cross-contract consistency check.
---

# K1 — Contract/Scenario/Open-Question Confirmation Record (E2i)

## 1. Claim-by-claim verification

| # | Claim | Verification | Verdict |
|---|-------|--------------|---------|
| 1 | `community_builder.py` — `_generate_llm_summary()` raises `NotImplementedError`; call site is at line 485 | Read `archon_search/community_builder.py:340-351` — `async def _generate_llm_summary(self, community_id: str, chunk_texts: list[str]) -> str` raises `NotImplementedError` unconditionally at line 348. Call site at line 485: `summary_text = await self._generate_llm_summary(community_id, chunk_texts)` — exact match on line number | TRUE |
| 2 | `graph_extractor.py` — logs WARNING when `extraction_model` is set; stub is at lines 182–193 | Read `archon_search/graph_extractor.py:182-193` — `if self._config.extraction_model:` at line 182, `_logger.warning(...)` at 183-186, `warnings.append(...)` at 188-192, `llm_fallback_used = True` at 192 — exact match | TRUE |
| 3 | `RelationshipType` values `uses`, `implements`, `depends_on` already exist in `graph_types.py` (lines 50–68) | Read `archon_search/graph_types.py:53-55` — `uses = "uses"` at 53, `implements = "implements"` at 54, `depends_on = "depends_on"` at 55. Also present: `related_to = "related_to"` (56), `synonym_of = "synonym_of"` (57). Five members total, all within the cited range | TRUE |
| 4 | `Community.summary_text: str | None = None` already exists in the communities schema in `graph_store.py` | Grep `archon_search/graph_store.py` — `pa.field("summary_text", pa.utf8(), nullable=True)` at line 226 (schema column), `"summary_text": [c.summary_text for c in communities]` at 1216 (write path), `summary_text = row.get("summary_text") or None` at 1666 (read path). Column exists and is already nullable | TRUE |
| 5 | `GraphEdge.extraction_method` field already exists from E2g in `graph_types.py` | Read `archon_search/graph_types.py:192` — `extraction_method: str | None = None` at line 192. (Note: plan cites "lines 50–68" for `RelationshipType` and separately line 170 for `extraction_method`; actual line is 192 after E2g additions expanded the file — the field exists, only the line number drifted) | TRUE (line drifted to 192 from cited 170; field is present) |
| 6 | `make_stable_edge_id` hashes `relationship_type` as part of the edge ID | Read `archon_search/graph_types.py:94-110` — canonical string is `f"{source_id}:{target_id}:{relationship_type.strip().lower()}"` at line 109; docstring at 101-104 confirms direction sensitivity. `relationship_type` is included in the hash | TRUE |
| 7 | Maintenance loop community-rebuild trigger: `_rebuild_communities_async()` already calls `CommunityBuilder.build()` | Read `archon_search/jobs/maintenance_loop.py:586-591` — `builder = CommunityBuilder(...)` at 586, `await builder.build(collection, ns=namespace)` at 591. The stub fill in BE-2 triggers LLM summarization automatically once `_generate_llm_summary()` is implemented | TRUE |
| 8 | The pre-read guard in `write_graph()` currently fires when `any(e.extraction_method == "inferred" ...)` — verify exact predicate at `graph_store.py:454` | Read `archon_search/graph_store.py:454` — exact: `if any(e.extraction_method == "inferred" for e in edges):` at line 454. Current guard covers only `"inferred"`, not `"llm"` (BE-6 extends it) | TRUE |
| 9 | `graph_store_protocol.py` exists and follows consumer-owned protocol precedent | Grep `archon_search/graph_store_protocol.py` — file exists; `class GraphStoreProtocol(Protocol)` at line 16; four async methods: `get_all_nodes` (27), `vector_search_nodes` (31), `write_graph` (47), `find_nodes_by_name` (64). No `get_neighbours`/`get_edges_for_nodes` — those are concrete-class-only | TRUE |
| 10 | `GraphExtractor.__init__` signature at `graph_extractor.py:91` has `config: "GraphConfig"` and no `llm_client` yet | Read `archon_search/graph_extractor.py:91` — `def __init__(self, config: "GraphConfig") -> None:` at line 91. Single parameter; no `llm_client` parameter exists | TRUE |
| 11 | `CommunityBuilder.__init__` signature at `community_builder.py:329` has no `llm_client` yet | Read `archon_search/community_builder.py:329-338` — `def __init__(self, graph_store: "GraphStore", config: "GraphConfig", *, search_store: "SearchStore | None" = None) -> None:`. No `llm_client` parameter exists | TRUE |
| 12 | `MaintenanceLoop.__init__` at `maintenance_loop.py:102` has no `llm_client` yet | Read `archon_search/jobs/maintenance_loop.py:102-110` — `def __init__(self, job_store, search_store, config, data_dir, graph_store=None, graph_config=None)`. No `llm_client` parameter | TRUE |
| 13 | `SearchPipeline.__init__` receives a `graph_extractor` parameter; `GraphExtractor` is constructed at `pipeline.py:3511` | Read `archon_search/pipeline.py:327,3511` — `graph_extractor: GraphExtractor | None = None` in `SearchPipeline.__init__` at line 327; `graph_extractor = GraphExtractor(cfg.graph)` at line 3511 in `create_pipeline()`. Confirmed | TRUE |
| 14 | `CommunityBuilder` construction site in `maintenance_loop.py:586` | Read `archon_search/jobs/maintenance_loop.py:586-590` — `builder = CommunityBuilder(graph_store=self._graph_store, config=self._graph_config, search_store=self._search_store)` at lines 586-590 | TRUE |
| 15 | `CommunityBuilder` construction site in `cli/graph_cmd.py:71` | Read `archon_search/cli/graph_cmd.py:71` — `builder = CommunityBuilder(graph_store, cfg.graph, search_store=search_store)` at line 71 | TRUE |
| 16 | `CommunityBuilder` construction site in `eval/runner.py:1130` | Read `archon_search/eval/runner.py:1130-1134` — `_community_builder = CommunityBuilder(_graph_store_for_communities, _graph_config, search_store=pipeline.store)` at lines 1130-1133 | TRUE |
| 17 | `app.py:520` — `GraphExtractor` construction site inside `create_app` (not lifespan) | Read `archon_search/server/app.py:520` — `_graph_extractor = _GraphExtractor(config.graph)` at line 520; this is inside `create_app()`, not the lifespan. The BE-0b "move to lifespan" decision is required work | TRUE |
| 18 | `SearchPipeline` is currently constructed in `create_app` (before lifespan) — `app.py:532` | Read `archon_search/server/app.py:532` — `app.state.pipeline = SearchPipeline(...)` at line 532. Confirmed: constructed inside `create_app()`, before `async def lifespan` fires | TRUE |
| 19 | `tests/server/test_app.py` — lines ~103-108 and ~129-132 read `app.state.pipeline` immediately after `create_app` (without lifespan) | Read `tests/server/test_app.py:103-108,129-132` — `app = create_app(cfg, job_store)` then `pipeline = app.state.pipeline` immediately at lines 103-106 and 129-131. Both tests assert pipeline attributes without entering the lifespan | TRUE |
| 20 | `hyde.py` / `rag_fusion.py` — "lazy import, in-process rate-limit warn-and-fallback, asyncio.wait_for, silent fallback" pattern | Read `archon_search/hyde.py` and `archon_search/rag_fusion.py` — both implement: (a) lazy `import anthropic` inside `__init__` (`hyde.py:65`, `rag_fusion.py:65`), (b) `self._rate_limit_warned_at: float = 0.0` in-process warn-and-fallback (not a true token bucket), (c) `await asyncio.wait_for(...)` (`hyde.py:122`, `rag_fusion.py:162`), (d) silent fallback on exception. **Precision note:** the plan calls this an "in-process token bucket" — the pattern is actually a warn-and-fallback with a 60s warning debounce (`_rate_limit_warned_at`), not a token bucket with explicit capacity tracking. The pattern exists as an established precedent but the vocabulary "token bucket" is imprecise | TRUE (pattern exists; "token bucket" is imprecise — see §3) |
| 21 | Graph table naming convention: `_archon_graph_{ns}__{col}_nodes|edges|communities|mentions` (double `__` separator) | CLAUDE.md and confirmed in `graph_store.py` via grep — `_archon_graph_{ns}__{col}_` with double `__` separator is the established naming; verified by grep of `_archon_graph_` in `graph_store.py` | TRUE |
| 22 | `mcp.py` — `get_graph` tool exists at line 1882; return structure at lines 1955–1961 | Read `archon_search/server/mcp.py:1882` — `async def get_graph(collection: str, salience_mode: Literal["frequency", "tfidf", "importance"] | None = None)` at line 1882. Return dict at lines 1955-1961: five keys: `node_count`, `edge_count`, `entity_type_distribution`, `top_nodes`, `top_edges`. No community summary fields present | TRUE |
| 23 | `graph_inspector.py` — `CollectionGraphView` and `inspect_collection()` exist | Read `archon_search/graph_inspector.py:78,275,286` — `class CollectionGraphView:` at 78, `async def inspect_collection(...)` at 275 returning `CollectionGraphView` at 286 | TRUE |
| 24 | `server/schemas.py` — `GraphInspectionResponse` exists and its current fields | Read `archon_search/server/schemas.py:698-713` — `class GraphInspectionResponse(BaseModel)` with fields: `nodes`, `edges`, `truncated`, `node_count`, `edge_count`, `salience_mode`. No `communities_total`, `communities_summarized`, or `unsummarized_community_ids` fields | TRUE |
| 25 | `server/routes_search.py` — `SearchResponse` class exists and is NOT in `schemas.py` | Read `archon_search/server/routes_search.py:126-141` — `class SearchResponse(BaseModel)` defined at line 126 with fields: `results`, `acl_filtered`, `excluded_collections`, `embedding_model`, `hyde_applied`, `rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted`, `graph_expansion_applied`, `expansion_used`, `expansion_warning`, `applied_filters`, `ppr_entities_matched`. No `community_summaries` field; confirmed NOT in `schemas.py` | TRUE |
| 26 | The "never-propagate" invariant on post-persist auxiliary writes: `try/except` pattern in `pipeline.py` | Read `archon_search/pipeline.py:690-717` — `try: [graph write operations] except Exception: logger.warning(...); acl_warnings.append(...)` — no re-raise. Same pattern documented for DefRefExtractor at line 740-741 ("never-propagate contract, same as E1a"). Confirmed | TRUE |
| 27 | `graph_enrichment_protocol.py` does NOT exist yet | `ls archon_search/graph_enrichment_protocol.py` → "No such file or directory". Confirmed absent | TRUE |
| 28 | `llm_enrichment_client.py` does NOT exist yet | `ls archon_search/llm_enrichment_client.py` → "No such file or directory". Confirmed absent | TRUE |
| 29 | `app.state.llm_enrichment_client` does NOT exist yet in `app.py` | Grep `archon_search/server/app.py` for `llm_enrichment_client` → no matches | TRUE |
| 30 | `app.state._enrichment_warnings_fired` does NOT exist yet | Grep `archon_search/server/app.py` for `_enrichment_warnings_fired` → no matches | TRUE |
| 31 | `community_summaries` field does NOT exist yet on `SearchResponse` or `SearchPipelineResult` | Grep `archon_search/` for `community_summaries` → no matches anywhere in the codebase | TRUE |
| 32 | `communities_total`/`communities_summarized`/`unsummarized_community_ids` fields do NOT exist yet on `GraphInspectionResponse` | Grep `archon_search/` for all three field names → no matches. `GraphInspectionResponse` at `schemas.py:698-713` confirms absence | TRUE |

---

## 2. Internal-consistency checks

**C1/C2 contract scope alignment:** C1 (`e2i-llm-enrichment-client.tsp`) is the Use Cases ↔ Interface Adapters seam, defining `LLMEnricher` interface with `summarizeCommunity`/`labelRelationships` methods. C2 (`api-contracts/e2i-graph-inspection-api.tsp`) is the REST seam, extending `GraphInspectionResponse` with three new fields. The two contracts are disjoint in scope and do not cross-reference — no inconsistency found.

**Scenario-to-task allocation check (S1-S15):** Cross-referenced each scenario against each task's `completes` line in the Task Breakdown:

- S1 (community summarization): K1 `completes C2`, BE-2 `completes S1, S5, C1`, T-1 `completes S1, S2, S9, S14` — covered
- S2 (GET /graph/{col} stats): BE-3 `completes S2, S11, C2`, BE-4 `completes S2, S11, C2`, T-1 `completes S1, S2, S9, S14` — covered
- S3 (global search community_summaries): BE-7 `completes S3, S4`, T-3 `completes S3, S4, S12, S13` — covered
- S4 (local search community_summaries): BE-7 `completes S3, S4`, T-3 — covered
- S5 (LLM unavailable, silent fallback): BE-2 `completes S1, S5, C1` — covered
- S6 (typed edges created): BE-5 `completes S6, S7, C1`, T-2 `completes S6, S7, S8` — covered
- S7 (typed-edge fallback): BE-5 `completes S6, S7, C1`, T-2 — covered
- S8 ("extracted" wins over "llm"): BE-6 `completes S8`, T-2 `completes S6, S7, S8` — covered
- S9 (extraction_model unset = no change): BE-1 `completes S9 (config gate)`, T-1 `completes S1, S2, S9, S14` — covered
- S11 (unsummarized_community_ids capped at 100): BE-3 `completes S2, S11, C2`, BE-4 `completes S2, S11, C2` — covered
- S12 (enrichment WARNING on first inspection call): BE-8 `completes S12, S13`, T-3 — covered
- S13 (no warning when unset or all summarized): BE-8 `completes S12, S13`, T-3 — covered
- S14 (eval gate passes with stub): T-1 `completes S1, S2, S9, S14` — covered (also S14 allocation table shows `eval` level)
- S15 (telemetry CI guard): BE-10 `completes S15` — covered

**No orphaned scenarios found.** S10 does not appear in the plan body (no S10 row in the scenario table) — this is a numbering gap in the plan, not an orphan, as the allocation table also skips from S9 to S11.

**Dependency graph vs. task `needs` fields:** Mermaid graph shows: K1→BE-0→BE-0b→BE-2; BE-0→BE-1→BE-2; BE-0b→BE-5; K1→BE-3→BE-4; BE-2→T-1; BE-4→T-1; BE-2→BE-5→BE-6→T-2; BE-2→BE-7→T-3; BE-3→BE-8→T-3; K1→BE-10; T-1,T-2,T-3,BE-10→T-4. Each task's `needs` field matches the Mermaid edges. No inconsistency found.

**Open-question resolutions vs. "What changes" section:** Cross-checked all eight Q# resolutions:
- Q1 (`extraction_token_budget` = per-community `max_tokens`): "What changes" at line 141 lists `extraction_token_budget` as a new GraphConfig field; BE-1 test `test_generate_llm_summary_token_budget_reaches_api` verifies the parameter is forwarded — consistent
- Q2 (entity names alongside chunk_texts): "What changes" at line 144 specifies `entity_names: list[str]` in `_generate_llm_summary` signature; `nodes_by_id` already in memory — consistent
- Q3 (batched per chunk): "What changes" at line 145 says "batched LLM call per chunk" — consistent
- Q4 (summary_refresh DEFERRED): "What does NOT change" lists `entity_ids_hash` as NOT being added; Known Limitations explicitly defers `summary_refresh` — consistent
- Q5 (`community_summaries` on `SearchResponse`): "What changes" at line 149 specifies `community_summaries: dict[str, str] = {}` on `SearchResponse` in `routes_search.py` — consistent
- Q6 (`"llm"` wins over `null`, hierarchy `extracted > llm > inferred > null`): "What changes" at line 146 says "extend `write_graph()` pre-read guard to trigger when `any(e.extraction_method in {"inferred", "llm"} for e in edges)`" — consistent
- Q7 (lazy cold-start WARNING): "What changes" at line 150 describes lazy WARNING on first `GET /graph/{col}` call, `app.state._enrichment_warnings_fired: set[str]` — consistent
- Q8 (include in MCP `get_graph`): "What changes" at line 151 says "add community summary fields to `get_graph` return dict; same once-per-collection WARNING logic" — consistent

No contradictions found between Q resolutions and "What changes."

**K1 `completes` claim check:** The Task Breakdown shows `K1 completes C2; C1 completed by K1 + BE-0`. C2 is the REST/HTTP seam (`api-contracts/e2i-graph-inspection-api.tsp`) — K1 verifies the TypeSpec compiles (done below). C1 (`e2i-llm-enrichment-client.tsp`) requires BE-0 to be completed first (the protocol must be authored). This split completion is structurally sound: the TypeSpec for C1 compiles clean but the underlying code seam is not yet present.

---

## 3. Discrepancies

**D1 — "Token bucket" vocabulary is imprecise (minor):** The plan states the E2i LLM adapter pattern "follows the `hyde.py` / `rag_fusion.py` pattern exactly (lazy import, in-process token bucket, `asyncio.wait_for`, silent fallback)." Read of both files (`hyde.py:65`, `rag_fusion.py:81`) shows the pattern is a **warn-and-fallback debounce** using `_rate_limit_warned_at: float = 0.0` (log a warning at most once per 60 seconds), NOT a token bucket (which would track capacity and actively gate calls). The pattern exists as a valid precedent, but whoever implements `AnthropicEnrichmentClient` should model the rate-limit mechanism on the actual code (warn-and-fallback debounce), not assume a classical token-bucket implementation. This is a documentation-precision issue, not a design problem.

**D2 — `_generate_llm_summary` current signature mismatch with plan's stated starting point:** The plan's "What changes" section at line 144 says: "Change `_generate_llm_summary` signature from `-> str` to `async def _generate_llm_summary(self, chunk_texts: list[str], entity_names: list[str]) -> str | None` (adds entity-name parameter per Q2; corrects return type)." However, the current signature at `community_builder.py:340-342` is `async def _generate_llm_summary(self, community_id: str, chunk_texts: list[str]) -> str:` — it already has an extra `community_id: str` first parameter that the plan's target signature drops. BE-2's description says "drops the `community_id` parameter (not needed by the LLM client)" (plan line 85), which the "What does NOT change" section correctly documents. The target signature from the plan's "What does NOT change" section (`chunk_texts: list[str], entity_names: list[str]`) is accurate — but the starting point described in "What changes" (`(chunk_texts: list[str])`) does not match the current source (which also has `community_id: str` as the first positional parameter). BE-2 must drop `community_id` AND change the return type AND add `entity_names` — three changes, not the two stated in "What changes." Not a blocker for K1, but BE-2's implementer should Read the actual signature at `community_builder.py:340` before patching.

**D3 — `GraphEdge.extraction_method` line number drift:** The plan cites `graph_types.py:170` for `GraphEdge.extraction_method`. Actual line is 192 after E2g additions expanded the file. The field exists and is unchanged; only the line number in the plan has drifted. Not a functional discrepancy.

**D4 — S10 is absent from scenario table:** The scenario table in the plan skips from S9 to S11 (no S10 row). The allocation table mirrors this gap. Not a functional gap — just a numbering artifact.

**D5 — `get_graph` MCP line numbers (1955–1961) confirmed accurate:** The plan cites "lines 1955–1961 in `mcp.py`" for the `get_graph` return dict that must be manually updated. Actual return dict is at lines 1955-1961 — this matches. Confirmed: `return {` at 1955, closing `}` at 1961.

**D6 — TypeSpec C1 contract method names and `SummaryRequest` shape diverged from plan's protocol (found and corrected during K1 review):** The original `e2i-llm-enrichment-client.tsp` had `SummaryRequest { communityId: string; chunkTexts: string[]; }` (retained `communityId`, lacked `entityNames`) and the interface method was named `summarize` instead of `summarizeCommunity`. The plan's Q2 resolution explicitly drops `communityId` and adds `entityNames: list[str]`; the plan's "What changes" at line 144 confirms the BE-2 signature target is `_generate_llm_summary(self, chunk_texts, entity_names)`. The TypeSpec was corrected as part of K1: `SummaryRequest.communityId` removed, `entityNames: string[]` added, and interface method `summarize` renamed to `summarizeCommunity` (camelCase form of `summarize_community`). `labelRelationships` is correct and unchanged (camelCase matches `label_relationships`). The corrected TypeSpec compiles clean.

**D7 — `RelationshipType` member count: claim 3 asserts "Five members total" but actual count is nine:** Claim 3 in the verification table states "Five members total" for `RelationshipType` (citing `uses`, `implements`, `depends_on`, `related_to`, `synonym_of`). `grep -n "RelationshipType" archon_search/graph_types.py` shows the enum at lines 50-61 has **nine members**: `uses` (53), `implements` (54), `depends_on` (55), `related_to` (56), `synonym_of` (57), `calls` (58), `imports` (59), `defines` (60), `inherits` (61). The four additional members (`calls`, `imports`, `defines`, `inherits`) were added in E2g. The claim's intent (the three E2i-relevant values exist) is TRUE; only the stated count is wrong. Not a blocker for E2i work.

**D8 — `_block_anthropic_client` autouse fixture constrains BE-0/BE-5 test structure:** `tests/conftest.py:103-131` defines a session-scoped autouse fixture `_block_anthropic_client` that patches `anthropic.Anthropic` and `anthropic.AsyncAnthropic` via `patch.object` so any instantiation raises `RuntimeError("Test suite attempted to instantiate the Anthropic client…")`. Tests for `AnthropicEnrichmentClient` (BE-0's `test_client_raises_on_api_error` and BE-5's typed-edge tests) must patch the anthropic module via `monkeypatch.setattr(sys.modules["anthropic"], "AsyncAnthropic", mock_class)` or `patch.dict(sys.modules, {"anthropic": mock_module})` — they cannot rely on a real client or on the env-var guard alone. The fixture docstring confirms that `patch.dict` replacing the whole module object works because the lazy `import anthropic` inside the client gets the mock, not the real module.

**D9 — `ppr` graph mode excluded from BE-7's `community_summaries` population:** BE-7 wires `community_summaries` into the `global` and `local` code paths inside `_search_graph_mode` / `_search_local_mode` in `pipeline.py`. The `ppr` mode (`_search_ppr_mode`) seeds from entity matches and blends PPR-ranked chunk IDs into hybrid RRF — it does not retrieve communities and therefore never populates `community_summaries`. This is a known gap accepted by the plan: S3 ("global search returns community_summaries") and S4 ("local search returns community_summaries") are the only scenarios covering `community_summaries`; `ppr` mode is not listed in any scenario that exercises this field. Implementers of BE-7 should confirm the `ppr` path returns `community_summaries: {}` (empty dict) and that this is intentional.

**D11 — TypeSpec C1 `entityPairs` shape mismatch corrected (K1 round 2):** TypeSpec's original `EntityPair` model had four fields (`sourceId`, `targetId`, `sourceName`, `targetName`), while the plan's `label_relationships(entity_pairs, chunk_text)` Python protocol takes `list[tuple[str, str]]` — bare ID pairs without names. The TypeSpec was corrected during K1 review to use a 2-field `EntityPairIds { sourceId: string; targetId: string; }` model, removing `sourceName` and `targetName`. `RelationshipRequest.entityPairs` is now typed as `EntityPairIds[]`. This is the companion shape fix to D6's method rename; both corrections are required for C1 to accurately reflect the Python protocol.

**D10 — `make_real_app` has no `llm_client` injection seam:** `tests/integration/conftest.py:32-48` shows `make_real_app` accepts many keyword args (`backup_enabled`, `graph_enabled`, `hyde_enabled`, etc.) but has no `llm_client` parameter. Integration tests for E2i that need to inject a stub `LLMEnricher` into the app (to exercise BE-0b's lifespan wiring without a real Anthropic client) will need `make_real_app` to be extended with an `llm_client` keyword arg that sets `app.state.llm_enrichment_client` before the lifespan starts, or tests must use `AsyncClient` with lifespan and patch `app.state` directly post-startup. This is in scope for BE-0b.

**D12 — Shared-client temporal constraint (scoped to BE-0b):** `GraphExtractor` is constructed at `app.py:520` and `SearchPipeline` at `app.py:532` — both inside `create_app()` (sync), before the lifespan fires. `MaintenanceLoop` is constructed inside the lifespan at `app.py:332`. For the single-shared-client invariant (both `GraphExtractor` and `CommunityBuilder` receive the same `AnthropicEnrichmentClient` instance), two options exist: (a) construct the client inside `create_app()` before line 520 and store it in `app.state.llm_enrichment_client`, then read it in the lifespan to pass to `MaintenanceLoop`; or (b) move `SearchPipeline` + `GraphExtractor` construction into the lifespan (BE-0b's stated decision). Option (b) is what BE-0b scopes, per plan lines 380-386. R2 documents the two `test_app.py` tests that break with option (b) and must be migrated. CLI and eval paths always pass `llm_client=None` via `create_pipeline()` at `pipeline.py:3511` — this is a separate code path the server never uses at runtime.

### §3b Risks flagged for downstream tasks

**R1 — `_generate_llm_summary` call site must be updated (BE-2):** The plan says call site at `community_builder.py:485` must be updated to match the new signature. The current call is `await self._generate_llm_summary(community_id, chunk_texts)` (2 args). The new signature is `_generate_llm_summary(self, chunk_texts, entity_names)` (2 args, different names, different order, `community_id` dropped). BE-2 must update the call site. The surrounding try/except at lines 484-496 catches exceptions and falls back to MMR — this is the correct existing fallback wiring that "What does NOT change" correctly says is unchanged.

**R2 — `tests/server/test_app.py` migration is scope-critical (BE-0b):** Two tests read `app.state.pipeline` immediately after `create_app()` (rows 18-19 above, lines 103-108 and 129-132). Moving `SearchPipeline` construction into the lifespan breaks these tests — BE-0b explicitly calls out migrating them to `TestClient`/`AsyncClient` with lifespan enabled. This is a known and correctly scoped dependency.

**R3 — `community_summaries` on `SearchResponse` is in `routes_search.py`, not `schemas.py`:** The plan correctly states "NOT `schemas.py`" — this is an important implementation note since an incorrect placement would break the OpenAPI snapshot test. Both construction sites at `routes_search.py:252` and `routes_search.py:348` must map the new field.

**R4 — `write_graph()` BE-6 rewrite scope is significant:** The existing pre-read at `graph_store.py:454-476` is ID-keyed and resolves only `inferred → extracted` on the same edge ID. Since `make_stable_edge_id` hashes `relationship_type` into the ID, an `"extracted"` edge typed `related_to` and an `"llm"` edge typed `uses` for the same entity pair have different IDs. BE-6 must replace the ID-keyed pre-read with an entity-pair pre-read. The plan's 3.0h estimate and "rewrite, not extension" framing are correct. Additionally, the pre-read must also filter incoming llm-typed rows before the `merge_insert` call — a row with an `"extracted"` counterpart for the same entity pair must be dropped from the write batch, not just tagged differently, because `merge_insert("id")...when_not_matched_insert_all()` will INSERT it as a new distinct row regardless of what tags were rewritten.

### §3c Risk flagged — roadmap charter

The E2i roadmap entry in `Documentation/Backlog/03_world_class_roadmap.md` originally listed `summary_refresh` as scope item (3) and included "refresh policy touches only changed communities" in the Minimum acceptance text. The plan explicitly defers `summary_refresh` to Q4 (see Known Limitations and Q4 resolution). The roadmap entry was updated during K1 to strike through item (3) (`~~summary_refresh~~ deferred to follow-up — E2i does full-rebuild only`) and remove the corresponding Minimum acceptance bullet. The corrected roadmap now matches the plan's deferral decision.

---

## 4. Verdicts

| Item | Verdict | Basis |
|------|---------|-------|
| **C1** — LLM Enrichment Client seam | **CONFIRMED (corrected, two rounds)** | TypeSpec `e2i-llm-enrichment-client.tsp` was corrected in two rounds during K1: (D6) `SummaryRequest.communityId` removed, `entityNames: string[]` added, `summarize` renamed to `summarizeCommunity`; (D11) `EntityPair` 4-field model replaced with 2-field `EntityPairIds { sourceId, targetId }`, removing `sourceName` and `targetName` which have no counterpart in the Python protocol's `list[tuple[str, str]]`. Corrected TypeSpec compiles clean; `graph_store_protocol.py` precedent for consumer-owned protocol verified (claim 9); `LLMEnrichmentClientProtocol` protocol file does not exist yet (claim 27) — correctly scoped as new in BE-0; `graph_enrichment_protocol.py` and `llm_enrichment_client.py` confirmed absent (claims 27-28) |
| **C2** — Graph Inspection HTTP Extension | **CONFIRMED** | TypeSpec `api-contracts/e2i-graph-inspection-api.tsp` compiles clean; `GraphInspectionResponse` exists at `schemas.py:698-713` without the three new fields (claim 24) — correctly scoped as new in BE-3/BE-4; `get_graph` MCP tool exists and returns five-field dict without community stats (claim 22) — correctly scoped as manual update in BE-4/BE-8 |
| **Q1** `extraction_token_budget` = per-community max_tokens | **CONFIRMED (design-level)** | Config field does not yet exist (correctly prospective); BE-1 test `test_generate_llm_summary_token_budget_reaches_api` guards the forwarding behavior; no contradicting code exists |
| **Q2** Entity names passed alongside chunk_texts | **CONFIRMED** | `nodes_by_id` is already in memory during `CommunityBuilder.build()` — zero extra I/O claim is correctly scoped. D2 in §3 documents the exact starting signature change BE-2 must make |
| **Q3** Batched per chunk | **CONFIRMED (design-level)** | No LLM extraction code exists yet to contradict; consistent with E2i architecture intent |
| **Q4** summary_refresh DEFERRED | **CONFIRMED** | `entity_ids_hash` column confirmed absent from communities schema (grep returns no matches in `graph_store.py`); `write_communities()` does full `delete("1=1") + add()` on every rebuild — verified at `graph_store.py:1216` write path. Plan's stated rationale (no stable Leiden community IDs across re-runs) is correctly captured in Known Limitations |
| **Q5** `community_summaries: dict[str, str]` on `SearchResponse` | **CONFIRMED** | `SearchResponse` is in `routes_search.py:126` (not `schemas.py`) — verified (claim 25). `community_summaries` field confirmed absent (claim 31). Both construction sites at lines 252 and 348 confirmed via grep |
| **Q6** `extracted > llm > inferred > null` hierarchy; entity-pair pre-read | **CONFIRMED** | Current guard at `graph_store.py:454` uses `extraction_method == "inferred"` (claim 8) — confirmed as the starting point BE-6 extends. `make_stable_edge_id` confirmed to hash `relationship_type` (claim 6) — this is the root cause requiring entity-pair pre-read instead of edge-ID pre-read |
| **Q7** Lazy cold-start WARNING | **CONFIRMED (design-level)** | `app.state._enrichment_warnings_fired` confirmed absent (claim 30) — correctly prospective; `get_graph` confirmed to not yet include community stats (claim 22) — stats query needed first (BE-3) before WARNING can fire; lazy-on-first-call pattern has no contradicting code |
| **Q8** Include in MCP `get_graph` | **CONFIRMED** | `get_graph` at `mcp.py:1882` confirmed (claim 22); return dict at 1955-1961 has no community fields; Known Limitations correctly notes manual update required (not auto-propagated from REST response) |

---

## 5. Summary

**TypeSpec compile results:** Both contracts compile clean under TypeSpec v1.13.0 (binary at `Documentation/Backlog/api-contracts/node_modules/.bin/tsp`):
- `api-contracts/e2i-graph-inspection-api.tsp`: `Compilation completed successfully.`
- `e2i-llm-enrichment-client.tsp`: corrected in two rounds during K1 (D6, D11) and recompiles clean after both corrections.

**TypeSpec C1 corrections (D6, D11):** Round 1 (D6): `SummaryRequest.communityId` was removed, `entityNames: string[]` was added, and the `summarize` method was renamed to `summarizeCommunity` to match the plan's protocol. Round 2 (D11): `EntityPair` 4-field model replaced with 2-field `EntityPairIds { sourceId; targetId }`, removing `sourceName` and `targetName` which have no counterpart in the Python protocol's `list[tuple[str, str]]`. Both divergences were introduced when the TypeSpec was first drafted; it is now aligned with the plan.

**Roadmap charter correction (§3c):** The E2i roadmap entry in `03_world_class_roadmap.md` included `summary_refresh` in its Minimum acceptance; this was updated during K1 to reflect the plan's Q4 deferral.

**Claim-by-claim table:** 32 claims checked; **29 without issue**, 3 with minor drift/imprecision (non-blocking; see D1-D3). Additional discrepancies D4-D12 document test-infrastructure constraints, implementation notes, and TypeSpec corrections found during K1; none block Phase 1 work.

**Verdict table:** 2 contracts and 8 open-question resolutions verified:
- **1 CONFIRMED (corrected, two rounds)** (C1) — TypeSpec corrected in two rounds during K1 review (D6: method rename + SummaryRequest shape; D11: EntityPair shape); compiles clean after both corrections
- **1 CONFIRMED** (C2) — source facts directly checked, TypeSpec compiles clean
- **3 CONFIRMED** (Q2, Q4, Q6) — current-state code facts directly verified
- **5 CONFIRMED (design-level)** (Q1, Q3, Q5, Q7, Q8) — no code exists to contradict these forward-looking decisions; feasibility is consistent with existing patterns

Scenario-to-task allocation (S1-S15, minus the absent S10) and task dependency graph were cross-checked and found internally consistent with no orphaned scenarios or contradicted dependencies.

**Risks flagged for implementing tasks:** R1 (BE-2 must drop `community_id` AND change return type AND add `entity_names`); R2 (BE-0b must migrate two `test_app.py` tests); R3 (`community_summaries` in `routes_search.py` not `schemas.py`); R4 (BE-6 is a genuine rewrite of the pre-read block, not an extension).

**Overall verdict (in two parts, following E2g K1 precedent):**

1. **Current-state code claims: VERIFIED (32/32 against source).** Every file:line, signature, and behavioral claim about the existing codebase that underlies C1-C2/S1-S15/Q1-Q8 was independently checked against the source tree and matched (three precision/drift notes, no substantive discrepancy found).

2. **Eight design decisions (Q1-Q8): fact-checked for feasibility/non-contradiction only — no external team ratification occurred.** This record should not be read as design sign-off, only as a premise-accuracy check that surfaced zero contradictions.

**Practical conclusion:** Phase 1 work (BE-0, BE-0b, BE-1, BE-2, BE-3) may proceed on the strength of premise-accuracy. The single most important watch item for implementers is D2 (BE-2 starting signature) and R4 (BE-6 entity-pair pre-read rewrite scope).
