**Purpose**: Debug and tune retrieval with `POST /explain` — see why a result did or didn't rank where it did.
**Audience**: End users / operators / integrators tuning routing, reranking, or hybrid scoring.
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Explain and debugging

A normal `/search` returns a single opaque `score` per result. When a result is wrong, missing, or surprisingly ranked, that number tells you nothing. `POST /explain` (and the parallel `explain` MCP tool) re-runs the *same* pipeline and returns the full per-stage provenance: dense vector rank/score, FTS rank/score, the fused RRF score, the reranker score, the routing decision, near-miss candidates that just fell short, graph traversal paths when a `graph_mode` is used, and the ACL gate on every result.

Reach for `/explain` when you are asking **why** — not **what**:

- "Why is this chunk ranked #4 when it looks like the best match?"
- "Why did the expected document not come back at all?"
- "Which collection did routing pick, and how close were the others?"
- "Was this result filtered out by ACL, or did it just score low?"
- "For a graph query, what path led from my query to this chunk?"

Source: `archon_search/server/routes_explain.py`, `archon_search/server/mcp.py` (`explain` tool). The live `GET /openapi.json` is the authoritative field-by-field contract.

## The debugging workflow

`/explain` is a loop, not a one-shot report:

```
symptom  →  run /explain  →  interpret the breakdown  →  adjust a knob  →  re-run
```

The knobs you adjust live in [`30_configuration.md`](30_configuration.md) (embedding/reranker models, `top_k_*`, routing thresholds, fanout). The symptom→knob mapping is the table at the end of this page.

## Request

`/explain` takes the same shape as `/search` plus debug-only flags. Unlike `/search`, `/explain` **honours request-level `top_k`** — this is how you widen the window to see more of the pool.

REST (`ExplainRequest`, `routes_explain.py`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `query` | string | — | Required, non-empty after strip. |
| `collection` | string \| null | null | Pin one collection. |
| `collections` | list \| null | null | Multi-collection fanout. Mutually exclusive with `collection`; capped at `[search] max_fanout`. |
| `top_k` | int | 5 | `>= 1`; must be `<= [database] top_k_max`. |
| `rerank` | bool | true | Set `false` to see the raw RRF ordering before the cross-encoder. |
| `hyde` | bool | false | Run HyDE query expansion. |
| `rag_fusion` | bool | false | Run RAG-Fusion. Suppresses HyDE. |
| `graph_mode` | `naive`\|`local`\|`global`\|`ppr`\|null | null | Graph retrieval. Requires `[graph] enabled=true`. Mutually exclusive with `scope_filter` and with multi-collection fanout. |
| `scope_filter` | string \| null | null | Same semantics as `/search` (exact scope or trailing-`*` wildcard). |

When neither `collection` nor `collections` is set, `/explain` runs the router first and reports the decision (see [Routing](#routing-decision)).

### curl example

```bash
curl -sS -X POST http://127.0.0.1:8765/explain \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "query": "how does the router pick a collection?",
        "collection": "docs",
        "top_k": 5,
        "rerank": true
      }' | jq .
```

### MCP example

The `explain` MCP tool takes the same arguments and runs in the caller's authenticated namespace:

```json
{
  "name": "explain",
  "arguments": {
    "query": "how does the router pick a collection?",
    "collection": "docs",
    "top_k": 5,
    "rerank": true
  }
}
```

## Reading the response

The response (`ExplainResponse`) has two result lists plus context:

- `results` — the top `top_k` chunks, each with `text` and a full `breakdown`.
- `near_misses` — up to 20 candidates that fell just outside the top slice. **No `text` field** (structural — `ExplainNearMiss` omits it) and **no `acl_gate`** (only top-level `results` carry it).
- `routing` — the routing decision, or `null` when a collection was pinned.
- `acl_filtered` — `true` if any candidate was dropped by ACL (near-miss counts are computed *after* ACL filtering).
- `rerank`, `embedding_model`, `hyde_applied`, `rag_fusion_applied`, `graph_mode_applied`, `stage_timings_ms`, `excluded_collections`, `ppr_entities_matched`.

### The per-stage breakdown

Every result and near-miss carries an `ExplainScoreBreakdown`:

| Field | Meaning |
|---|---|
| `vector_rank` / `vector_score` | Rank and score in the dense (embedding) leg. `null` if the chunk never surfaced there. |
| `vector_score_kind` | `"distance"` for LanceDB cosine — **lower is closer**, surfaced verbatim. |
| `fts_rank` / `fts_score` | Rank and score in the full-text (BM25) leg. `null` if the chunk never surfaced there. |
| `fts_score_kind` | `"bm25"` when a raw score is present; `null` when LanceDB omits it. |
| `rrf_score` | The fused reciprocal-rank score that combines both legs. |
| `reranker_score` | The cross-encoder second-stage score; `null` when `rerank=false`. |

The top-level `score` on each result is the **final** score: the `reranker_score` when a reranker ran, otherwise the `rrf_score`.

**How to read it in practice:**

- A chunk with a strong `vector_rank` but no `fts_rank` (or vice versa) matched on only one leg — a hint that phrasing (FTS) and semantics (vector) disagree.
- A chunk with a good `rrf_score` but a low `reranker_score` was pulled *down* by the cross-encoder. Compare with `rerank=false` to confirm.
- A chunk absent from `results` but present in `near_misses` with a near-identical breakdown is a threshold/`top_k` issue, not a retrieval miss.
- A chunk absent from **both** lists never entered the retrieval pool — a chunking, ingestion, or scope/ACL problem, not a scoring one.

### Routing decision

When no collection is pinned, `routing` (`RoutingExplain`) reports which collection the router chose and how every candidate scored — **including candidates below the confidence threshold** (the gate is bypassed here so nothing is hidden):

- `chosen_collection`, `confidence_threshold`, `chosen_below_threshold`.
- `candidates[]` — `{collection, centroid_score}` for every collection in the namespace, sorted by score (`centroid_score` is `null` for collections whose embedding model doesn't match or that have no centroid).

If the wrong collection is chosen, or `chosen_below_threshold` is `true`, tune `[routing]` (`routing_confidence_threshold`, `routing_strategy`, `routing_description_weight`) in [`30_configuration.md`](30_configuration.md).

## Graph provenance

When you pass a `graph_mode`, each **graph-retrieved** result carries a `graph_provenance` object showing the traversal chain, and the response root carries `graph_mode_applied`. Chunks retrieved by ordinary hybrid search in the same request carry `graph_provenance: null`.

`graph_provenance.steps` is an ordered list (query → graph → chunk). Each `TraversalStep` has:

| Field | Meaning |
|---|---|
| `entity` / `entity_id` | Entity name and stable ID at this hop. |
| `relationship` | Typed edge (e.g. `naive` expansion relationships); `null` for community steps. |
| `community_id` | Community ID for `local`/`global` modes; `null` for naive steps. |
| `chunk_id` | Set on the terminal step — the chunk this path leads to. |

At least one of `relationship`, `community_id`, or `chunk_id` is always set. For `ppr` mode, `ppr_entities_matched` reports how many seed entities the walk matched (`0` means PPR fell back to plain hybrid — no query entities matched).

Use this to debug graph quality: wrong entities matched, unclear community boundaries, or graph chunks scoring oddly against the reranker. The graph modes themselves and how to build communities are covered in [`65_graph_search.md`](65_graph_search.md); the maintenance side is in [`../OperatorGuide/60_graph_operations.md`](../OperatorGuide/60_graph_operations.md).

## The ACL gate

`/explain` **always** populates an `acl_gate` (`AclGateSchema`) on every top-level result — no flag required (this differs from `/search`, where you must pass `acl_context=true`). Near-misses never carry it.

| Field | Meaning |
|---|---|
| `allowed_principals` | The principals allowed to see the chunk (`null` for chunks with no recorded ACL). |
| `source` | `"frontmatter"`, `"sidecar"`, `"collection_default"`, or `null`. |
| `sidecar_path` | The `.acl` sidecar file that supplied the rule, if any. |
| `warnings` | Always a list; non-empty when ACL parsing hit a recoverable problem (e.g. an oversized or malformed sidecar). |

If a document you expect is missing and `acl_filtered` is `true`, the ACL gate tells you *why*. Permission-aware search, `acl_context`, and how ACLs are resolved are documented in [`60_searching.md`](60_searching.md) and the [`../SecurityGuide/03_authorization_and_acl.md`](../SecurityGuide/03_authorization_and_acl.md).

## Symptom → interpret → adjust

| Symptom | What to look at in `/explain` | Knob to adjust |
|---|---|---|
| Expected doc missing entirely | Not in `results` **or** `near_misses` → never retrieved | Re-check ingestion/chunking; `scope_filter`; ACL gate |
| Expected doc in `near_misses`, not `results` | Compare `rrf_score`/`reranker_score` to the last `results` entry | `[database] top_k_return`, `top_k_retrieve` |
| Reranker demotes a good match | High `rrf_score`, low `reranker_score` (re-run `rerank=false`) | `[database] reranker_model`, or disable rerank |
| One leg never fires | `vector_rank` or `fts_rank` consistently `null` | `embedding_model`; query phrasing |
| Wrong collection chosen | `routing.chosen_collection`, `chosen_below_threshold`, candidate scores | `[routing] routing_confidence_threshold`, `routing_strategy` |
| Doc silently filtered | `acl_filtered: true` + result's `acl_gate` | ACL rules / sidecar (see SecurityGuide) |
| Graph result looks wrong | `graph_provenance.steps`, `graph_mode_applied`, `ppr_entities_matched` | `[graph]` params; rebuild communities |
| Slow query | `stage_timings_ms` (requires `[observability] stage_timings_enabled`) | See [`../OperatorGuide/80_capacity_and_performance.md`](../OperatorGuide/80_capacity_and_performance.md) |

## Notes and limits

- **Same pool as `/search`.** `/explain` uses the identical retrieval config, so its top-`top_k` slice matches `/search` when `rerank=true` and `top_k == [database] top_k_return`. Near-misses are drawn from the leftover pool — they do not widen retrieval.
- **The query is never logged or echoed.** Telemetry records latency/collection/count only; error responses are sanitized to stage + exception type so an FTS error can't leak the query.
- **Status codes:** `422` for invalid combinations (e.g. `graph_mode` without `[graph] enabled`, `scope_filter` + `graph_mode`, fanout over `max_fanout`); `400` for a malformed `scope_filter`; `503` for meta-lookup/router failures; `500` for pipeline-stage failures; `504` on fanout timeout.
- For the exhaustive field list, read `GET /openapi.json` (authoritative) rather than relying on this page.

## Related documents

- [`00_index.md`](00_index.md) — UserManual table of contents.
- [`60_searching.md`](60_searching.md) — `/search`, filters, `acl_context`, permission-aware results.
- [`65_graph_search.md`](65_graph_search.md) — graph modes that produce the provenance.
- [`30_configuration.md`](30_configuration.md) — the tuning knobs referenced above.
- [`160_troubleshooting.md`](160_troubleshooting.md) — broader failure diagnosis.
- [`../SecurityGuide/03_authorization_and_acl.md`](../SecurityGuide/03_authorization_and_acl.md) — how ACLs are resolved.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full REST/MCP/CLI reference.
