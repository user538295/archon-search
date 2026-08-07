# Graph operations

Purpose: operate the graph subsystem in production — enable it, rebuild communities, inspect the graph, and keep it healthy.
Audience: operators running `archon-search serve`.
Status: current.
Last reviewed: 2026-07-29.

The graph subsystem builds an entity/relationship graph from ingested content and powers the graph search modes (`naive`, `local`, `global`, `ppr`). This guide covers the operational surface: enabling it, the extras it needs, rebuilding Leiden communities, inspecting the graph, garbage collection, and optional LLM enrichment. For how end users query the graph, see [`../UserManual/65_graph_search.md`](../UserManual/65_graph_search.md) and [`../UserManual/70_code_graph_and_impact.md`](../UserManual/70_code_graph_and_impact.md).

---

## Enabling the graph subsystem

The graph is **off by default**. Turn it on in `~/.archon-search/archon-search.toml`:

```toml
[graph]
enabled = true
```

Enabling requires the optional extras — the base install does not pull them in.

| Extra | Provides | Required for | Missing-install behavior |
|---|---|---|---|
| `archon-search[graph]` | spaCy NER (prose entity extraction) | `graph.enabled = true` | **Startup fails** with `ConfigError` (`_check_graph_deps`, `server/app.py`): `graph.enabled=true but spacy is not installed; run: pip install archon-search[graph]`. |
| `archon-search[code]` | tree-sitter code parsers (def/ref edges, impact) | Code graphs / `GET /graph/{col}/impact/{symbol}` | Server still starts; logs a WARNING once per unsupported extension and surfaces a per-file warning in `IngestResult.warnings`. Prose graphing still works. |
| `leidenalg` + `igraph` | Leiden community detection | `local` / `global` search modes | **Lazy** — imported only inside `_run_leiden_partition_sync` (`community_builder.py`). A missing install does **not** block startup; it fails the rebuild *job* (`FAILED`) with an actionable message. |

Install everything for a full graph deployment:

```bash
pip install 'archon-search[graph,code]'
python -m spacy download en_core_web_sm   # spaCy model, if not already present
```

Because `leidenalg`/`igraph` are lazy, a graph-enabled server boots fine without them — you only discover the gap when a community rebuild runs. Install them proactively if you use `local`/`global` search.

---

## Rebuilding communities

`local` and `global` search modes need Leiden communities. They are built by an **async, trackable job** — never synchronously in the request path.

### REST

```bash
curl -X POST http://localhost:8765/graph/mydocs/rebuild-communities \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"
```

Returns `202` with a `JobResponse` body already in `RUNNING` state (the route transitions QUEUED→RUNNING before returning, so you never see QUEUED). Poll `GET /jobs/{id}` for the terminal status; on `DONE` the result carries `{"communities_built": N}`.

Route: `POST /graph/{collection}/rebuild-communities` (`server/routes_graph.py`). Status codes:

| Code | Meaning |
|---|---|
| `202` | Job accepted, running. |
| `404` | Collection not found in the caller's namespace. |
| `409` | A rebuild is already in progress for this collection (`community_rebuild_job_id` guard). |
| `422` | `graph.enabled=false`, graph store unavailable, or a `?namespace=` mismatch. |

Concurrent rebuilds on the same `(namespace, collection)` — including a `MaintenanceLoop` GC-triggered rebuild — serialize through a module-level lock in `community_builder.py`, so a rebuild never blocks ingest.

### CLI

```bash
archon-search graph build-communities mydocs --namespace default --wait
```

This is a pure HTTP proxy to the route above (`cli/graph_cmd.py`) — the server must be running. Flags: `--namespace`/`-n` (default `default`), `--wait` (poll to terminal), `--api-url`, `--api-key`.

**Namespace guard:** the `--namespace` value is forwarded as `?namespace=`. It must match the namespace the Bearer token authorizes; a mismatch returns `422`:

```
namespace mismatch: token authorises '<ns>', but ?namespace='<other>' was requested
```

Use the API key for the namespace you intend to target.

### Leiden knobs

All under `[graph]` (`config.py`). Defaults shown:

| Key | Default | Effect |
|---|---|---|
| `leiden_resolution` | `1.0` | Higher → more, smaller communities. |
| `max_community_size` | `10` | Max entities per community; oversized ones are split by re-running Leiden at higher resolution (up to 5 levels). |
| `community_summary_chunks` | `3` | Representative chunks per community (also the LLM summary context window). |
| `max_global_candidates` | `100` | Cap on community-representative chunks fed to the reranker in `global` mode. |
| `backend_threshold_edges` | `10000` | Edge count above which the heavier graph backend engages. |

---

## Inspecting the graph

Two read-only endpoints (`server/routes_graph.py`) expose the graph as JSON or GraphML.

`GET /graph/{collection}` — single collection.
`GET /graph/cross-collection?collections=a,b` — merge across ≥2 collections (deduped; `422` if fewer than 2 remain).

Shared query parameters:

| Param | Values | Notes |
|---|---|---|
| `format` | `json` (default), `graphml` | GraphML returns `application/xml`. |
| `salience` | `frequency` (default), `tfidf`, `importance` | `frequency` = chunk ratio [0,1]; `tfidf` = TF×IDF across all namespace collections; `importance` = persisted PageRank over code-symbol edges (nulls-last). |

Examples:

```bash
# Top entities by TF-IDF salience
curl -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  "http://localhost:8765/graph/mydocs?salience=tfidf"

# Export a code graph ordered by PageRank importance
curl -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  "http://localhost:8765/graph/mycode?salience=importance&format=graphml" -o mycode.graphml
```

Responses are capped and truncated deterministically: nodes by highest salience first (`max_inspection_nodes`, default `5000`), edges by highest weight first (`max_inspection_edges`, default `25000`). The `truncated` flag in the JSON response tells you when a cap was hit — raise the caps in `[graph]` if you need the full graph. For the exhaustive field list, see `GET /openapi.json`.

There is also a browser graph viewer at `GET /graph/{collection}/view` (HTML, auth via `Authorization` header or `?token=`). It is documented for users in [`../UserManual/65_graph_search.md`](../UserManual/65_graph_search.md).

---

## Lifecycle hygiene and garbage collection

Deleting documents leaves behind stale graph state. The `MaintenanceLoop` reclaims it when `[maintenance] graph_gc = true` (default) and `interval_hours > 0`.

GC finds **orphan nodes** (entity IDs with no remaining mention row) and removes them plus every edge touching them (`delete_orphan_nodes_and_edges`, `graph_store.py`). Endpoint/code-symbol nodes that are not mention-derived are exempted so they survive the sweep. If the mentions table is absent or empty, GC skips rather than risk deleting live nodes. When GC removes nodes and `gc_rebuild_communities = true` (default), it triggers a community rebuild at CPU priority `gc_rebuild_cpu_priority` (`low`/`normal`/`high`; per-thread nice is Linux-only).

### Monitoring GC

`GET /status` surfaces two signals (`server/routes_status.py`, `jobs/maintenance_loop.py`):

- `graph.stale_mention_count` — aggregate stale mention rows awaiting cleanup.
- `maintenance.last_graph_gc_at` — timestamp of the last GC pass (`null` until the first pass).

`archon-search status` prints both when set. A steadily rising `stale_mention_count` with a stale `last_graph_gc_at` means GC is not keeping up — check `[maintenance] interval_hours` and that `graph_gc` is enabled. Force an immediate pass with `POST /maintenance/trigger` (or `archon-search maintenance run`). See [`50_maintenance_and_jobs.md`](50_maintenance_and_jobs.md) and [`20_monitoring_and_alerts.md`](20_monitoring_and_alerts.md).

### Orphan tables from before namespacing

Graph tables are named `_archon_graph_{ns}__{col}_{nodes|edges|communities|mentions}` — a **double** underscore separates the namespace from the collection. Tables from the pre-namespacing scheme (`_archon_graph_{col}_*`, single underscore, no namespace) are no longer read after upgrade. On **every startup**, `check_and_warn_legacy_graph_tables` (`graph_store.py`) scans for them and logs a WARNING listing the exact table names:

```
Legacy graph tables detected from a pre-E2d schema (missing namespace separator): [...].
These tables are no longer read by archon-search and should be deleted manually from the
LanceDB data directory to reclaim disk space. No automatic migration is performed.
```

There is no automatic migration. Delete the listed tables manually from the LanceDB data directory to reclaim disk space. Namespaces are isolated: a rebuild, inspection, or GC in one namespace never touches another's tables.

---

## Opt-in LLM enrichment

By default the graph is built from statistical co-occurrence only: community summaries are empty and edges are generic. Setting `[graph] provider` — a **discrete** field, not a `"provider:model"` string — turns on two enrichments automatically:

```toml
[graph]
enabled = true
provider = "anthropic"                              # anthropic | openai | ollama | llama_cpp
extraction_model = "claude-haiku-4-5-20251001"       # bare model name, never "provider:model"
```

`provider` defaults to `null` (enrichment disabled) and is itself the enrichment enable gate — unlike `[hyde]`/`[rag_fusion]`, there is no separate `[graph].enrichment_enabled`. `claude_cli` is a valid provider name elsewhere in the config but has no v1 enrichment client (no HTTP endpoint; deferred post-v1) — setting `provider = "claude_cli"` logs a WARNING and enrichment stays disabled. For `llama_cpp`, also set `[graph] llama_cpp_base_url` (default `http://localhost:8080`); use a small, direct-response instruct model — a reasoning model burns the whole `extraction_token_budget` on hidden chain-of-thought and enrichment silently produces nothing.

1. **Community summaries** — each community gets an LLM-written summary (`summary_text`), surfaced in `local`/`global` search and in the inspection endpoint.
2. **Typed edges** — relationships are labeled (`uses`, `implements`, `depends_on`) instead of generic `related_to`.

Operational properties:

- **Byte-identical default:** with `provider` unset (`null`), behavior is exactly as before — no LLM call, no token cost, no API dependency.
- **Silent fallback:** any LLM failure (timeout, quota, missing key, network) logs a WARNING and proceeds. Communities are still built, entities still extracted via spaCy, and **no ingest ever fails**.
- **LLM-typed edges are additive, not overriding:** relationship-labeling edges are merged in alongside the `related_to` co-occurrence edges (distinct `relationship_type` values produce distinct edge IDs) — they never replace or downgrade an existing edge. This is a separate mechanism from the def/ref extractor's `"extracted"`-always-wins-over-`"inferred"` precedence rule for code-symbol edges.
- **Refresh:** the maintenance loop re-summarizes only communities whose membership changed since the last build.
- **No rate limiting for `llama_cpp`:** `extraction_rate_limit_rpm` is honored by `anthropic` but ignored by the `llama_cpp` enrichment client (local inference, unthrottled) — parity with the query-expansion adapters.

Set the provider credential (e.g. `ANTHROPIC_API_KEY`) in the environment or via the wizard-managed `~/.archon-search/.secrets.env`. `ollama` and `llama_cpp` need no credential. Community texts and LLM prompts are never logged — the no-raw-query telemetry guarantee extends to enrichment.

---

## Related documents

- [`00_index.md`](00_index.md) — Operator Guide table of contents.
- [`50_maintenance_and_jobs.md`](50_maintenance_and_jobs.md) — maintenance loop, GC scheduling, async jobs.
- [`20_monitoring_and_alerts.md`](20_monitoring_and_alerts.md) — `/status` signals and alerting.
- [`../UserManual/65_graph_search.md`](../UserManual/65_graph_search.md) — graph search modes and the browser graph viewer.
- [`../UserManual/70_code_graph_and_impact.md`](../UserManual/70_code_graph_and_impact.md) — code graphs and impact analysis.
