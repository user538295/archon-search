# Graph operations

Purpose: operate the graph subsystem in production — enable it, rebuild communities, inspect the graph, and keep it healthy.
Audience: operators running `archon-search serve`.
Status: current.
Last reviewed: 2026-09-07.

The graph subsystem builds an entity/relationship graph from ingested content and powers the graph search modes (`naive`, `local`, `global`, `ppr`). This guide covers the operational surface: enabling it, the extras and NER model it needs, rebuilding Leiden communities, inspecting the graph, garbage collection, and optional LLM enrichment. For how end users query the graph, see [`../UserManual/65_graph_search.md`](../UserManual/65_graph_search.md) and [`../UserManual/70_code_graph_and_impact.md`](../UserManual/70_code_graph_and_impact.md).

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
| `archon-search[graph]` | `gliner` (+ torch/transformers) — prose entity **and** relation extraction | `graph.enabled = true` | **Startup fails** with `ConfigError` (`ensure_graph_engine_importable`, `graph_extractor.py`, called from `_check_graph_deps` in `server/app.py` and from `pipeline.create_pipeline`): `graph.enabled=true but gliner is not installed. Install the graph extras: pip install 'archon-search[graph]'`. |
| `knowledgator/gliner-relex-multi-v1.0` checkpoint | the actual weights `gliner` runs (multilingual, relation-capable) | prose entity/relation extraction | **Degrades** — server starts and ingest still succeeds; prose extraction is skipped for the document, code-symbol nodes are unaffected, one WARNING is logged per process, and a sanitized notice rides on `IngestResult.warnings`. See [Provisioning the prose extraction model](#provisioning-the-prose-extraction-model). |
| `archon-search[code]` | tree-sitter code parsers (def/ref edges, impact) | Code graphs / `GET /graph/{col}/impact/{symbol}` | Server still starts; logs a WARNING once per unsupported extension and surfaces a per-file warning in `IngestResult.warnings`. Prose graphing still works. |
| `leidenalg` + `igraph` | Leiden community detection | `local` / `global` search modes | **Lazy** — imported only inside `_run_leiden_partition_sync` (`community_builder.py`). A missing install does **not** block startup; it fails the rebuild *job* (`FAILED`) with an actionable message. |

Install everything for a full graph deployment:

```bash
pip install 'archon-search[graph,code]'
archon-search wizard   # pre-warms the prose extraction checkpoint (~1.2 GB)
```

Because `leidenalg`/`igraph` are lazy, a graph-enabled server boots fine without them — you only discover the gap when a community rebuild runs. Install them proactively if you use `local`/`global` search.

---

## Provisioning the prose extraction model

The `archon-search[graph]` extra installs the `gliner` *library* (plus torch and transformers); the *checkpoint* it runs is a separate ~1.2 GB artifact hosted on HuggingFace. Both the repo id and the revision are pinned to one owner in `paths.py`, so they cannot drift between the pre-warm and the runtime load:

```python
GRAPH_NER_MODEL_NAME     = "knowledgator/gliner-relex-multi-v1.0"
GRAPH_NER_MODEL_REVISION = "e990d9ba6f471b846f7d78bf7e4b4dab11761ada"
```

**The runtime downloads it lazily on first use.** `ProseExtractionBackend.load()` (`prose_extraction_backend.py`) calls `GLiNER.from_pretrained(GRAPH_NER_MODEL_NAME, revision=GRAPH_NER_MODEL_REVISION, cache_dir=get_graph_models_dir())` off the event loop, once per process, and `huggingface_hub` populates the cache if it is empty. So a networked host needs no manual provisioning step at all — the first prose ingest after enabling the graph pays the download, and every later one is warm.

The cache directory is derived, never hand-written (`paths.get_graph_models_dir()`):

```
<data-dir>/models/graph/knowledgator--gliner-relex-multi-v1.0-e990d9ba6f471b846f7d78bf7e4b4dab11761ada/
```

`<data-dir>` is `~/.archon-search` unless `ARCHON_SEARCH_DATA_DIR` relocates it. The `/` of the HuggingFace repo id becomes `--` so the id stays a single path segment, and the revision is part of the directory name — a revision bump lands in a new directory rather than overwriting the old one.

**Degraded behavior is not an ingest failure.** Chunks still embed and persist, code-symbol graph nodes and edges are still written, prose extraction is skipped for the affected document, one WARNING is logged per process (not once per file), and the matching sanitized notice rides on `IngestResult.warnings`. There are three such notices (`graph_extractor.py`), and none of them ever carries exception text:

- `graph prose extraction model is unavailable; prose entity extraction is disabled for this ingest (code-symbol extraction is unaffected).` — the checkpoint could not be loaded (no network on a cold cache, corrupt cache, torch/gliner failure). A genuine load failure **latches once per process**: it is not retried until restart.
- `graph prose extraction model is still loading for another document; prose entity extraction is disabled for this ingest (code-symbol extraction is unaffected).` — this caller waited on another document's in-flight first load and timed out. This one is **not** latched; the next document gets a fresh wait. It never returns 503.
- `graph prose entity/relation extraction failed; prose entity extraction was skipped for this document (code-symbol extraction is unaffected).` — the model loaded but the forward pass raised.

Only a missing `gliner` *package* is fatal, and that is caught before any ingest runs, by `ensure_graph_engine_importable`.

### Remedy: re-run the wizard

```bash
archon-search wizard
```

Enabling code indexing installs `[code]` and `[graph]` as one bundle and pre-warms the checkpoint (`_prewarm_graph_model`, `install/prewarm.py`) into the same `get_graph_models_dir()` cache the runtime uses, so the pre-warm and the first ingest share one copy rather than two. The wizard discloses the two download costs separately before fetching anything — about 18 MB for the tree-sitter parsers and an estimated 1218 MB for the prose model, licensed Apache-2.0 — and that same estimate feeds the install-time disk guard. The figure is a declared estimate, not a verified byte count: the checkpoint is fetched by `GLiNER.from_pretrained`, never through the digest-pinned provisioning seam, so it is neither digest- nor size-verified on arrival.

A failed pre-warm is **non-fatal**: it logs a WARNING and the wizard continues, leaving the model to download on first use instead. After the pre-warm the wizard runs its two-stage device probe and writes `[graph].providers` — it offers an accelerator only when the host has one *and* the pre-warmed model actually landed on it, and otherwise settles on CPU without prompting.

### Remedy: seed the cache by hand

For an air-gapped or scripted install, populate the cache directory on a networked host that has the `[graph]` extra installed, then copy it across. Do exactly what the pre-warm does:

```bash
# On a networked host, with the same [graph] extra installed:
python - <<'PY'
from gliner import GLiNER
from archon_search.paths import (
    GRAPH_NER_MODEL_NAME, GRAPH_NER_MODEL_REVISION, get_graph_models_dir,
)
dest = get_graph_models_dir()
print("cache dir:", dest)
GLiNER.from_pretrained(
    GRAPH_NER_MODEL_NAME, revision=GRAPH_NER_MODEL_REVISION, cache_dir=str(dest)
)
PY

# Then copy the whole directory to the same path on the air-gapped host:
rsync -a ~/.archon-search/models/graph/ target-host:~/.archon-search/models/graph/
```

Run that with the same interpreter the server uses (the one that has the `[graph]` extra installed) — for a `uv tool install`, that is the tool's managed venv, not your system `python`. Copy the **whole** `models/graph/` tree, not a hand-picked subset: the layout inside it is `huggingface_hub`'s own cache layout, and the loader reads it through `huggingface_hub`, not by globbing. Re-running the same snippet on the air-gapped host is the verification — a clean exit means the server will load it too.

Do **not** set `HF_HUB_OFFLINE=1` process-wide on the server to force this: it is global to the process and blocks fastembed's embedder and reranker downloads as well, which turns a cold cache into a *failed* ingest instead of a degraded one. Scope it to the verification command if you want it at all.

### Checking from the outside

When `[graph].enabled = true`, startup validation probes the engine *package* the same way `GraphExtractor` construction does and reports a missing package in `GET /status` → `model_validation.provider_warnings`, so this never hides in per-file ingest logs (`graph_ner_status`, `model_validation.py`). It emits exactly two messages:

```
graph prose entity extraction is disabled: gliner is not installed. Install
the graph extras: pip install 'archon-search[graph]'
```

```
graph NER model presence could not be determined
```

The second means the probe itself failed unexpectedly — it never raises, so a surprise reads as "cannot determine" rather than failing startup.

**One component, several spellings.** This guide calls it the **prose extraction engine** (and the artifact it loads the prose extraction model, or checkpoint). The probe function is named `graph_ner_status`, its second message says "graph NER model", and the pinned constants are `GRAPH_NER_MODEL_NAME` / `GRAPH_NER_MODEL_REVISION` — all naming holdovers from the engine this one replaced. They refer to the same `gliner` engine and the same checkpoint, not to a second component.

**The startup probe checks the package, not the checkpoint.** Unlike the previous engine, there is no "installed but wrong model artifact" state to probe here: the checkpoint is fetched lazily and any failure to load it degrades per-ingest, not at startup. A process that has latched a failed checkpoint load is therefore invisible on `/status` — grep the server log for the load WARNING instead.

`checks.models` reaches `"warn"` only when the embedder and reranker probes both already passed and `provider_warnings` is non-empty — `"fail"` (a probe failed) and `"pending"` (validation still running) outrank it. When those probes are clean, a missing `[graph]` extra does flip `checks.models` to `"warn"` on `GET /ready` (still HTTP 200 — it never gates readiness).

**Multilingual by design.** `knowledgator/gliner-relex-multi-v1.0` is a multilingual checkpoint: prose in any corpus language produces entity nodes, mentions and typed relations, and there is no English-only restriction and no disclosure to surface for it. Output *quality* still varies by language, and a language may carry a recorded quality gap — see the corpus-language vs interface-language section in [`../Architecture/220_accessibility_and_internationalization.md`](../Architecture/220_accessibility_and_internationalization.md) for the distinction between the corpus (unrestricted) and the operator-facing interface (English by design).

There is also no `provider_notes` field on `GET /status` → `model_validation` any more, and nothing replaced it. Everything reported through `provider_warnings` is operator-actionable by construction, because `checks.models` grades that field alone: anything permanent and unactionable placed there would pin the check to `warn` for the life of the deployment.

**Chunks longer than the model window are truncated, not split.** `GRAPH_NER_TOKEN_WINDOW_WORDS` (2048 words, `prose_extraction_backend.py`) is `gliner`'s own enforced limit; a longer chunk is truncated before extraction and the event is logged once per process. Extraction runs in sub-batches of `GRAPH_NER_SUB_BATCH_SIZE` (8) texts per call, so a large document is never one unbounded forward pass.

**Extraction model probe.** When `[graph].enabled = true` AND `extraction_model` is set (any of `anthropic`, `openai`, `ollama`, `llama_cpp` as `provider`), startup also runs a one-shot probe of `extraction_model` and adds a warning to the same `provider_warnings` field if the model does not respond within the probe's time budget (the primary "this looks like a reasoning model" signal) or the call otherwise fails (misconfiguration — a separately worded warning). That budget is `min(extraction_timeout_seconds, 75.0s)` — the probe is capped at this fixed 75-second economic ceiling (`_EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS`) regardless of how high an operator raises their own `extraction_timeout_seconds`, so raising the timeout to accommodate a slow reasoning model can never defeat detection. The probe is skipped when `provider == "llama_cpp"` and the separate llama-server reachability probe already found the server unreachable, to avoid a second, confusing warning for the same root cause. This probe issues one real API call — potentially billed, for `anthropic`/`openai` — on every process start/restart; see the next section for why a reasoning model needs it.

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

> **An empty graph on a collection ingested before 2026-09-06 is expected.** The prose NER
> prompt shipped in a form that extracted nothing (GRAPH-3 in
> [530](../Architecture/530_technical_debt_refactoring_roadmap.md)): ingests reported success
> and wrote no entities. The fix applies at extraction time only, so it does not backfill —
> a collection ingested under the broken prompt keeps its empty graph until its documents are
> re-ingested. Check with `GET /graph/{collection}`; if it returns no nodes and the collection
> has content, re-ingest it.

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

GC does two sweeps in one pass (`delete_orphan_nodes_and_edges`, `graph_store.py`):

- **Orphan nodes** — entity IDs with no remaining mention row — are removed, plus every edge touching them. Endpoint/code-symbol nodes that are not mention-derived are exempted so they survive.
- **Unsupported relationships** (2026-08-20-010) — mention-derived edges whose two entities no longer appear together in any chunk. The sweep covers **four** relationship types (`_MENTION_DERIVED_RELATIONSHIP_TYPES`, `graph_store.py`): `related_to` from the co-occurrence loop, plus `uses`, `implements` and `depends_on` from the prose engine's relation output. Prose extraction is their only producer. `write_graph` upserts by stable edge ID and never removes, so editing a document so two entities stop co-occurring left the edge asserting a relationship nothing supports. Both endpoints usually remain mentioned elsewhere, so the orphan-node sweep never reached these. Edges of the four def/ref types (`calls`/`imports`/`defines`/`inherits`) and synonym edges are not judged by co-mention. Note what exempts the first four: `_edge_is_defref` (`graph_store.py`) classifies them by `relationship_type` **alone**, regardless of `extraction_method` — and the prose engine's own label set includes all four (`prose_extraction_backend.py`), so a prose-asserted edge of one of those types is GC-exempt too, not just the AST-derived ones. Synonym edges are the dictionary/embedding-derived case.

  The support test is co-mention of the two endpoints, and that is weaker than "the model still asserts this relation". A typed edge whose endpoints still share a chunk, but whose relation the model no longer asserts after a text edit, a `relation_confidence` change or a checkpoint revision, is **not** collectable and stays in place. There is no per-relation support ledger.

If the mentions table is absent or empty, GC skips both sweeps rather than risk deleting live rows. When GC removes **either** nodes or edges and `gc_rebuild_communities = true` (default), it triggers a community rebuild at CPU priority `gc_rebuild_cpu_priority` (`low`/`normal`/`high`; per-thread nice is Linux-only) — Leiden partitions over the edge list, so a deleted relationship makes the stored groupings stale even when every member node survives.

**Timing:** a stale relationship disappears at the next maintenance GC pass, not at re-ingest. Ingest never deletes edges, deliberately — edge rows are shared between every document that asserts the same pair, so a document-scoped delete would erase relationships other documents still support.

**After upgrading the prose extraction engine, run three steps in this order.** Changing an entity's type re-keys its node and the write path is a pure upsert, so re-ingest alone leaves the old nodes behind:

1. **Re-ingest** every collection whose graph you want rebuilt.
2. **Run the GC pass** — `POST /maintenance/trigger` (or `archon-search maintenance run`) — to sweep the now-orphaned nodes and unsupported edges.
3. **Rebuild communities**, which step 2 triggers for you when it removed anything.

Two settings silently make that a no-op, so check both before concluding the pass did nothing: `[maintenance] graph_gc` must be `true` (default) for step 2 to sweep at all, and `[graph] gc_rebuild_communities` must be `true` (default) for step 3 to follow it. With either off, stale nodes or stale community groupings survive the upgrade indefinitely.

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

Enrichment is **community summarisation only**. Typed edges (`uses`, `implements`, `depends_on`) are **not** an enrichment feature: the prose extraction engine produces them locally, with no provider configured and no network access, so they are present in the default configuration. Without a provider, only community `summary_text` is empty.

Setting `[graph] provider` — a **discrete** field, not a `"provider:model"` string — turns summarisation on:

```toml
[graph]
enabled = true
provider = "anthropic"                              # anthropic | openai | ollama | llama_cpp
extraction_model = "claude-haiku-4-5-20251001"       # bare model name, never "provider:model"
```

`provider` defaults to `null` (enrichment disabled) and is itself the enrichment enable gate — unlike `[hyde]`/`[rag_fusion]`, there is no separate `[graph].enrichment_enabled`. `claude_cli` is a valid provider name elsewhere in the config but has no v1 enrichment client (no HTTP endpoint; deferred post-v1) — setting `provider = "claude_cli"` logs a WARNING and enrichment stays disabled. For `llama_cpp`, also set `[graph] llama_cpp_base_url` (default `http://localhost:8080`).

Use a small, direct-response instruct model for `extraction_model`, on any provider — a reasoning model burns the whole `extraction_token_budget` on hidden chain-of-thought and, even given an adequate budget, needs far longer per community (111s-286s measured) than the default `extraction_timeout_seconds` (30.0s) allows, so `community_builder.py` swallows the failure and silently falls back to an unsummarised community. This is not only silent: startup runs a one-shot probe against `extraction_model` (see "Checking from the outside" above) that catches exactly this — a call that does not complete within the probe's time budget warns distinctly from any other probe failure (missing credentials, unreachable server, wrong model name), via `provider_warnings` (`GET /status`) — the reachable, observable signal that per-community enrichment would otherwise swallow. That budget is capped at a fixed 75-second economic ceiling even if you raise `extraction_timeout_seconds` past it — raising your own timeout to give a reasoning model more room cannot defeat this probe.

**Community summaries** — each community gets an LLM-written summary (`summary_text`), surfaced in `local`/`global` search and in the inspection endpoint. That is the whole of what a provider adds.

Operational properties:

- **Byte-identical default:** with `provider` unset (`null`), no LLM call, no token cost, no API dependency — and the graph still carries typed prose edges, which the local engine produces either way.
- **Silent fallback:** any LLM failure (timeout, quota, missing key, network) logs a WARNING and proceeds. Communities are still built (unsummarised), entities and typed relations are still extracted locally, and **no ingest ever fails**.
- **Typed edges are additive, not overriding:** the prose engine's directed `uses`/`implements`/`depends_on` edges are merged in alongside the normalised, undirected `related_to` co-occurrence edges (distinct `relationship_type` values produce distinct stable edge IDs) — they never replace or downgrade an existing edge. This is a separate mechanism from the def/ref extractor's `"extracted"`-always-wins-over-`"inferred"` precedence rule for code-symbol edges.
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
