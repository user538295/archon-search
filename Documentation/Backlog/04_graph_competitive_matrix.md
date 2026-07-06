**Purpose**: Feature-level comparison of archon-search's knowledge-graph capabilities against the 2026 competitive field, with before/after capability scores and the roadmap item that closes each gap.
**Audience**: Maintainers planning and reviewing the graph track (E2b–E2j).
**Status**: Snapshot
**Last reviewed**: 2026-07-06 / **Next review**: after E2f/E2g land or 2026-10-01, whichever is first

> Sources: 2026-07-03 multi-agent review — codebase audit (file:line-verified against `archon_search/`), deep-dives on 7 named competitor repos (DeusData/codebase-memory-mcp, moorcheh-ai/memanto, aksika/abmind, iternal blockify, colbymchenry/codegraph, Egonex-AI/Understand-Anything, safishamsi/graphify) plus ~15 established systems (Microsoft GraphRAG/LazyGraphRAG, LightRAG, Graphiti/Zep, Cognee, HippoRAG 1+2, KAG, Neo4j GraphRAG, R2R, Mem0, LlamaIndex PGI, txtai, MiniRAG, Youtu-GraphRAG, fast/nano-graphrag), and the 2024–2026 GraphRAG literature (arXiv IDs inline where load-bearing). Star counts and vendor benchmark numbers are self-reported by their projects and were retrieved 2026-07-03; treat as directional. Companion docs: [`01_competitive_analysis_field.md`](./01_competitive_analysis_field.md), [`02_competitive_analysis_marveen.md`](./02_competitive_analysis_marveen.md), [`03_world_class_roadmap.md`](./03_world_class_roadmap.md) (graph track), [`e2b-graph-inspection-brief.md`](../Completed/e2b-graph-inspection-brief.md).

# Graph Competitive Matrix — archon-search vs. the field

**Legend**: ✅ shipped · ⚠️ partial · ❌ missing · 🚫 deliberate non-goal (evidence says skip). **"After" = state once E2f–E2j land** (E2b–E2e shipped 2026-07; those rows now show ✓ in the Item column). Every row names the implementing roadmap item; "✓" marks already-shipped items.

## 1. Graph construction & extraction

| Feature | Bar-setter | Before | After | Item |
|---|---|---|---|---|
| No-LLM deterministic entity extraction (NER) | LazyGraphRAG (concept, unreleased) | ✅ spaCy | ✅ | E1a ✓ |
| Code-symbol entities (tree-sitter) | codebase-memory-mcp (158 langs) | ⚠️ nodes only, **zero edges** | ✅ connected | E2g |
| Typed code edges (calls/imports/defines/inherits) | codebase-memory-mcp (18 edge types), graphify | ❌ | ✅ | E2g |
| Co-occurrence prose edges | (archon/LazyGraphRAG approach) | ✅ `related_to` | ✅ | E1a ✓ |
| Typed semantic relations via LLM (uses/implements/depends_on) | MS GraphRAG, Graphiti | ❌ stub raises warning | ✅ opt-in | E2i |
| Edge confidence / extraction-method tags | graphify (EXTRACTED/INFERRED/AMBIGUOUS) | ❌ | ✅ extracted\|inferred\|llm | E2g + E2i |
| AST-aware code chunking | cAST paper, arXiv 2506.15655 (+4.3 R@5 RepoEval) | ❌ size-based | ✅ | E2g(a) |
| Chunk↔entity incidence persisted (mentions) | HippoRAG passage-node analog | ❌ computed then discarded | ✅ mentions table | E2b ✓ |
| Schema/ontology-constrained extraction | KAG, Cognee OWL | ❌ | 🚫 requires LLM-at-ingest | — |
| Multi-pass gleaning / prompt auto-tune | MS GraphRAG | ❌ | 🚫 cost, non-determinism | — |
| Multimodal extraction (audio/video/images) | graphify (whisper), RAG-Anything | ❌ | ⚠️ VLM doc enrichment only, non-graph | F3 (existing) |

## 2. Graph quality & lifecycle

| Feature | Bar-setter | Before | After | Item |
|---|---|---|---|---|
| Embedding-similarity entity dedup (synonym edges) | HippoRAG (cosine ≥0.8), Graphiti | ❌ string-normalize only | ✅ | E2f |
| Operator alias / pinned merges | Cognee (ontology URIs) | ❌ | ✅ alias file | E2f |
| Label-free graph-health metrics (dedup rate, singleton %, components) | **nobody ships this** | ❌ | ✅ differentiator | E2f |
| Graph cleanup on doc delete / re-ingest | Graphiti (invalidation), codegraph (zero-stale) | ❌ tables write-only | ✅ doc-scoped | E2d ✓ |
| TTL-expiry ↔ graph reconciliation | unique (only archon has chunk TTL) | ❌ | ✅ | E2d ✓ |
| Maintenance-loop graph GC | abmind "sleep" analog | ❌ | ✅ `graph_gc` policy | E2d ✓ |
| Namespace-scoped graph tables | (archon multi-tenant need; E1b promised, never shipped) | ❌ shared across ns | ✅ + migration | E2d ✓ |
| Staleness visibility (`stale_mention_count`, `last_graph_gc_at`) | — | ❌ | ✅ in `/status` | E2d ✓ |
| Incremental per-doc ingest (no full rebuild) | LightRAG set-merge | ⚠️ upsert but stale rows accumulate | ✅ clean | E1a ✓ + E2d ✓ |
| Watcher-driven auto-refresh | codegraph FS-events | ✅ watchdog sync exists | ✅ | shipped + E2d ✓ |
| Bi-temporal validity / edge invalidation | Graphiti/Zep | ❌ | 🚫 memory-domain; Mem0's own paper (arXiv 2504.19413): graph adds ~2% there; TTL covers the practical need | — (G-cluster later) |

## 3. Retrieval & query modes

| Feature | Bar-setter | Before | After | Item |
|---|---|---|---|---|
| Hybrid vector+FTS+RRF + cross-encoder rerank | **archon** (rare among graph tools) | ✅ | ✅ | shipped ✓ |
| `naive` expansion mode | — | ⚠️ uncapped append | ✅ capped | E1a ✓, cap in E2h |
| `local` community mode | MS GraphRAG local | ✅ rep-chunk blend | ✅ + summaries when present | E1b ✓, E2i |
| `global` sensemaking mode | MS GraphRAG (72–83% comprehensiveness win-rates, arXiv 2404.16130) | ⚠️ rep chunks, **no summaries** | ✅ opt-in LLM summaries | E1b ✓, E2i |
| PPR multi-hop retrieval | HippoRAG (2Wiki R@5 68.2→89.1, arXiv 2405.14831), fast-graphrag | ❌ | ✅ `graph_mode="ppr"` | E2h |
| Graph as additive signal (never gate) | HippoRAG-2 lesson (arXiv 2502.14802) | ✅ blend design | ✅ enforced | E1b ✓ + E2h |
| DRIFT-style dynamic LLM traversal | MS GraphRAG | ❌ | 🚫 query-time LLM cost — the path LazyGraphRAG routes around | — |
| Cypher / structured graph query | Neo4j, codebase-memory-mcp | ❌ | ⚠️ spike only | lance-graph spike (roadmap track note) |
| TF-IDF cross-collection salience | unique | ❌ | ✅ `?salience=tfidf` | E2c ✓ |
| PageRank code-symbol salience | Aider repo-map | ❌ | ✅ | E2g |
| Multi-collection graph search (`search_many`) | **unique among local tools** | ✅ | ✅ | E1b ✓ |
| Graph provenance in `/explain` | **archon leads** (vs non-Neo4j field) | ✅ | ✅ + PPR step scores | E1c ✓, E2h |

## 4. Inspection, export, visualization

| Feature | Bar-setter | Before | After | Item |
|---|---|---|---|---|
| Adjacency JSON endpoint | graphify `graph.json` | ❌ | ✅ `GET /graph/{col}` | E2b ✓ |
| Cross-collection merged graph | unique | ❌ | ✅ | E2b ✓ |
| Node salience + edge weight + chunk provenance | MS GraphRAG citations | ❌ | ✅ derived from mentions | E2b ✓ |
| GraphML export | MS GraphRAG, graphify | ❌ | ✅ `?format=graphml` | E2b ✓ |
| Neo4j/Obsidian/Mermaid/Cypher exports | graphify | ❌ | ❌ deferred | E2b future |
| Graph stats / top entities ("god nodes") | graphify, cbm `get_architecture` | ⚠️ counts in `/status` | ✅ top-20 summary | E2b ✓ |
| Deterministic truncation of oversized graphs | **nobody does this explicitly** | ❌ | ✅ | E2b ✓ |
| Interactive visual viewer | Understand-Anything dashboard, LightRAG WebUI, cbm 3D | ❌ | ✅ single-file HTML | E2j |
| Committable team graph artifact | codebase-memory-mcp `.db.zst` | ❌ | ❌ deferred (D2 export exists) | E2b future |

## 5. Agent / MCP integration

| Feature | Bar-setter | Before | After | Item |
|---|---|---|---|---|
| First-party MCP, shared auth, 17 tools | **archon leads** (only Graphiti/Neo4j/Mem0 comparable) | ✅ | ✅ | D9 ✓ |
| `graph_mode` on search/explain MCP tools | — | ✅ | ✅ | E1a–c ✓ |
| Graph summary MCP tools | graphify (7 graph tools), Graphiti (13) | ❌ | ✅ `get_graph` ×2 | E2b ✓ |
| Entity/neighbour/path MCP tools | graphify `get_node`/`shortest_path` | ❌ | ❌ deferred | E2b future |
| Blast-radius / impact tool | cbm `detect_changes`, graphify `get_pr_impact` | ❌ | ✅ `graph_impact` | E2g |
| Write-to-graph MCP (add_triplet) | Graphiti | ❌ | 🚫 ingest-derived graph stays source-of-truth | — (revisit with G16 events) |
| ADR / decision memory | cbm `manage_adr` | ❌ | ❌ non-graph | G5 (existing) |
| OpenAI-compatible context shim | R2R posture | ❌ | ❌ non-graph | G9 (existing) |

## 6. Evaluation & credibility

| Feature | Bar-setter | Before | After | Item |
|---|---|---|---|---|
| Real (non-fallback) graph retrieval gates | GraphRAG-Bench (ICLR'26, arXiv 2506.02404) | ❌ floors calibrated on fallback stub | ✅ | E2e ✓ |
| Bridge multi-hop subsets (MuSiQue/2Wiki, gold support labels) | HippoRAG methodology | ❌ | ✅ frozen in-repo | E2e ✓ |
| Negative-control gating (HotpotQA "graph must not hurt") | **nobody does this** | ❌ | ✅ two-sided gate | E2e ✓ |
| Code retrieval A/B lane (RepoBench-R) | CodeRAG-Bench | ❌ | ✅ | E2g/E2h (deferred from E2e) |
| Deterministic, no-API, CI-runnable | archon harness tradition | ⚠️ exists but graph-blind | ✅ | E2e ✓ |
| Publishable public numbers | Mem0/Zep/cbm arXiv posture | ❌ | ⚠️ enabled; publishing is a separate act | E2e ✓ (enables) |
| NER-vs-LLM extraction ablation (open gap in the literature) | **nobody published it** | ❌ | ⚠️ possible | E2e ✓ + E2i (enables) |

## 7. Platform & ops (context — archon already leads; graph inherits)

| Feature | Bar-setter | Before | After | Item |
|---|---|---|---|---|
| Single store, no graph-DB sidecar | **unique among servers** (Graphiti/KAG/Neo4j/R2R all need one) | ✅ | ✅ | — ✓ |
| Auth/ACL/namespaces/TTL/scopes on graph surface | archon | ⚠️ graph tables not ns-scoped | ✅ | E2d ✓ |
| Backup/jobs/maintenance covering graph | archon | ⚠️ graph excluded | ✅ | E2d ✓ |
| Offline/air-gapped, zero API keys, reproducible builds | codebase-memory-mcp (embeddings-in-binary) | ✅ | ✅ | C0 ✓ |
| Telemetry privacy (no raw queries) | archon | ✅ | ✅ | — ✓ |

## Capability scores (0–10; 10 = current bar-setter)

"Before" = state at 2026-07-03 baseline. "Now" = shipped state after E2b–E2e (2026-07-06). "After" = projection once E2f–E2j land.

| # | Capability (bar-setter) | Before | Now (E2b–E2e ✓) | After | Driven by |
|---|---|---|---|---|---|
| 1 | Cheap deterministic ingest (LazyGraphRAG) | **10** | **10** | **10** | E1a ✓ |
| 2 | Typed relations / extraction quality (MS GraphRAG) | 2 | 2 | 7 | E2g, E2i |
| 3 | Entity resolution (Graphiti/Cognee) | 2 | 2 | 7 | E2f |
| 4 | Community summarization (MS GraphRAG) | 3 | 3 | 8 | E2i |
| 5 | Query-mode breadth (MS/LightRAG) | 6 | 6 | 8 | E2h |
| 6 | Multi-hop accuracy (HippoRAG-2/KAG) | 3 | 3 | 7 | E2h + E2f + E2g |
| 7 | Freshness / incremental (Graphiti/codegraph) | 3 | **9** | 9 | E2d ✓ |
| 8 | Graph storage scale (Neo4j) | 4 | **5** | 5 | E2b ✓ truncation + lance-graph spike |
| 9 | Inspection / provenance / viz (Neo4j Bloom) | 4 | **8** | 9 | E2b ✓, E2j, E1c ✓ |
| 10 | MCP / agent integration (Graphiti) | 8 | **9** | 9 | E2b ✓, E2g |
| 11 | Reproducible evals (GraphRAG-Bench) | 1 | **8** | 8 | E2e ✓ |
| 12 | Local-first ops (unoccupied) | **10** | **10** | **10** | ✓ |
| | **Overall (mean)** | **4.7** | **6.3** | **8.1** | |

### Caveats on the scores

- "Now" scores are actuals (code verified); "After" scores remain planning estimates pending E2f–E2j.
- **#2, #3, #4, #5, #6 unchanged** — E2b–E2e didn't touch extraction quality, entity resolution, community summarization, PPR, or typed relations; those wait for E2f/E2g/E2h/E2i.
- **#6 caps at ~7 by design** — beating KAG/HippoRAG-2 on MuSiQue-class leaderboards requires LLM triples at ingest, which is the cost/privacy/determinism wedge archon deliberately does not trade away; E2i buys part of it back opt-in.
- **#8 stays ~5** until the lance-graph spike graduates into a roadmap item.
- **#9 stops at 8** (not 9) until E2j ships the interactive graph viewer.
- Rows marked 🚫 are evidence-based non-goals, not oversights: LLM-at-ingest default, gleaning, DRIFT, bi-temporal KG, write-to-graph MCP.
