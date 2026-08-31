# Graph operations

Purpose: operate the graph subsystem in production — enable it, rebuild communities, inspect the graph, and keep it healthy.
Audience: operators running `archon-search serve`.
Status: current.
Last reviewed: 2026-08-20.

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
| `archon-search[graph]` | spaCy NER (prose entity extraction) | `graph.enabled = true` | **Startup fails** with `ConfigError` (`_check_graph_deps`, `server/app.py`): `graph.enabled=true but spacy is not installed; run: pip install archon-search[graph]`. |
| `en_core_web_sm` model | the actual NER weights spaCy runs | prose entity extraction | **Degrades** — server starts and ingest still succeeds; prose NER is skipped, code-symbol nodes are unaffected, one WARNING is logged per process, and the warning rides on `IngestResult.warnings`. See [Provisioning the spaCy NER model](#provisioning-the-spacy-ner-model). |
| `archon-search[code]` | tree-sitter code parsers (def/ref edges, impact) | Code graphs / `GET /graph/{col}/impact/{symbol}` | Server still starts; logs a WARNING once per unsupported extension and surfaces a per-file warning in `IngestResult.warnings`. Prose graphing still works. |
| `leidenalg` + `igraph` | Leiden community detection | `local` / `global` search modes | **Lazy** — imported only inside `_run_leiden_partition_sync` (`community_builder.py`). A missing install does **not** block startup; it fails the rebuild *job* (`FAILED`) with an actionable message. |

Install everything for a full graph deployment:

```bash
pip install 'archon-search[graph,code]'
archon-search wizard   # provisions the en_core_web_sm NER model
```

Because `leidenalg`/`igraph` are lazy, a graph-enabled server boots fine without them — you only discover the gap when a community rebuild runs. Install them proactively if you use `local`/`global` search.

---

## Provisioning the spaCy NER model

The `archon-search[graph]` extra installs the spaCy *library*; the `en_core_web_sm` *model* is a separate artifact. PyPI rejects the direct-URL dependency that would ship it, so the published wheel has no package channel for the model at all — it is provisioned out of band.

**The runtime never downloads or installs it.** `GraphExtractor` resolves the model on first use and nothing more (`find_spacy_model`, `graph_extractor.py`):

1. the installed `en_core_web_sm` package, if a pip-style install already carries it;
2. `<data-dir>/models/spacy/en_core_web_sm-<version>/` — `spacy.load()` accepts a filesystem path;
3. neither present → degrade.

`<data-dir>` is `~/.archon-search` unless `ARCHON_SEARCH_DATA_DIR` relocates it (`paths.get_spacy_models_dir()`).

**Degraded behavior is not an ingest failure.** Chunks still embed and persist, code-symbol graph nodes and edges are still written, prose NER is skipped for every document, one WARNING is logged once per process (not once per file), and each affected `IngestResult.warnings` carries the notice. Only a missing spaCy *library* is fatal, and that is caught at startup by `_check_graph_deps`.

### Remedy: re-run the wizard

```bash
archon-search wizard
```

The wizard's graph step pins the model version against the installed spaCy (via the published `compatibility.json`), fetches that release's wheel, places the model under `<data-dir>/models/spacy/`, and smoke-loads it before reporting success. A failed fetch is non-fatal — the wizard warns and continues, leaving graph ingest in the degraded state above.

### Remedy: place the model by hand

For air-gapped or scripted installs, do what the wizard does. Pick the `en_core_web_sm` version listed for your installed spaCy in [`compatibility.json`](https://github.com/explosion/spacy-models/blob/master/compatibility.json), then:

```bash
VER=3.8.0                                   # must match your spaCy minor (3.8.x -> 3.8.x)
DEST=~/.archon-search/models/spacy          # or $ARCHON_SEARCH_DATA_DIR/models/spacy
curl -fL -o /tmp/en_core_web_sm.whl \
  "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-$VER/en_core_web_sm-$VER-py3-none-any.whl"
unzip -q /tmp/en_core_web_sm.whl -d /tmp/en_core_web_sm-whl
mkdir -p "$DEST"
mv "/tmp/en_core_web_sm-whl/en_core_web_sm/en_core_web_sm-$VER" "$DEST/"
```

The wheel is a zip whose inner `en_core_web_sm/en_core_web_sm-<version>/` directory **is** the model — it is the directory holding `config.cfg`, and that is what must land at `<data-dir>/models/spacy/en_core_web_sm-<version>/`. The resolver ignores any candidate directory without a `config.cfg` at its top level, filters out versions incompatible with the installed spaCy (per each candidate's `meta.json` `spacy_version` specifier), and picks the newest **version-sorted** (not lexicographic — `3.9.0` would otherwise rank above `3.10.0`) compatible candidate when several are present. The compatibility filter is deliberately permissive: it rejects a candidate only when `meta.json` *unambiguously* rules the installed spaCy out. A missing, unreadable, or unparseable `meta.json`, or one with no `spacy_version` field, reads as compatible — so a hand-placed directory that omits it will be resolved and only fail later, at load time, degrading that ingest. Keep the wheel's own `meta.json` alongside `config.cfg` rather than copying out `config.cfg` alone. Verify before restarting:

```bash
python -c "import spacy; spacy.load('$DEST/en_core_web_sm-$VER')"
```

Run that with the same interpreter the server uses (the one that has the `[graph]` extra installed) — for a `uv tool install`, that is the tool's managed venv, not your system `python`. A clean exit means the server will resolve it too.

`python -m spacy download en_core_web_sm` is **not** a supported remedy for a `uv tool install` deployment: that venv has no package installer, so the download exits with "No package installer found". It still works in environments that do have pip (the Docker image, a dev checkout), and a model installed that way is picked up by resolution step 1.

### Checking from the outside

When `[graph].enabled = true`, startup validation probes the model the same way the extractor does and reports a miss in `GET /status` → `model_validation.provider_warnings`, so this never hides in per-file ingest logs:

```
graph prose entity extraction is disabled: spaCy model 'en_core_web_sm' is
neither installed nor provisioned under the data directory — re-run
`archon-search wizard` to provision it
```

`checks.models` reaches `"warn"` only when the embedder and reranker probes both already passed and `provider_warnings` is non-empty — `"fail"` (a probe failed) and `"pending"` (validation still running) outrank it. When those probes are clean, a missing NER model does flip `checks.models` to `"warn"` on `GET /ready` (still HTTP 200 — it never gates readiness).

**English only.** `en_core_web_sm` reads English prose. When the graph is enabled with `multilingual = true`, the wizard summary discloses this, and `model_validation.provider_notes` carries it too — deliberately a separate field from `provider_warnings`, so this permanent, non-operator-actionable disclosure never pins `checks.models` to `"warn"` forever. The two notes are independent: a missing or incompatible model reports the miss (in `provider_warnings`), and `multilingual = true` adds the English-only disclosure (in `provider_notes`) *alongside* it — so both can appear together. Non-English documents contribute code-symbol entities only. A multilingual successor engine is tracked separately.

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
- **Unsupported relationships** (2026-08-20-010) — `related_to` edges whose two entities no longer appear together in any chunk. These are the co-occurrence edges spaCy produces at ingest; `write_graph` upserts by stable edge ID and never removes, so editing a document so two entities stop co-occurring left the edge asserting a relationship nothing supports. Both endpoints usually remain mentioned elsewhere, so the orphan-node sweep never reached these. Def/ref edges (`calls`/`imports`/`defines`/`inherits`) and synonym edges are file- and dictionary-derived rather than mention-derived, and are not judged by co-mention.

If the mentions table is absent or empty, GC skips both sweeps rather than risk deleting live rows. When GC removes **either** nodes or edges and `gc_rebuild_communities = true` (default), it triggers a community rebuild at CPU priority `gc_rebuild_cpu_priority` (`low`/`normal`/`high`; per-thread nice is Linux-only) — Leiden partitions over the edge list, so a deleted relationship makes the stored groupings stale even when every member node survives.

**Timing:** a stale relationship disappears at the next maintenance GC pass, not at re-ingest. Ingest never deletes edges, deliberately — edge rows are shared between every document that asserts the same pair, so a document-scoped delete would erase relationships other documents still support.

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

`provider` defaults to `null` (enrichment disabled) and is itself the enrichment enable gate — unlike `[hyde]`/`[rag_fusion]`, there is no separate `[graph].enrichment_enabled`. `claude_cli` is a valid provider name elsewhere in the config but has no v1 enrichment client (no HTTP endpoint; deferred post-v1) — setting `provider = "claude_cli"` logs a WARNING and enrichment stays disabled. For `llama_cpp`, also set `[graph] llama_cpp_base_url` (default `http://localhost:8080`).

Use a small, direct-response instruct model for `extraction_model`, on any provider — a reasoning model burns the whole `extraction_token_budget` on hidden chain-of-thought and, even given an adequate budget, needs far longer per chunk (111s-286s measured) than the default `extraction_timeout_seconds` (30.0s) allows, so enrichment silently falls back to co-occurrence-only edges. This is not only silent: startup runs a one-shot probe against `extraction_model` (see "Checking from the outside" above) that catches exactly this — a call that does not complete within the probe's time budget warns distinctly from any other probe failure (missing credentials, unreachable server, wrong model name), via `provider_warnings` (`GET /status`) — the reachable, observable signal that per-chunk enrichment would otherwise swallow. That budget is capped at a fixed 75-second economic ceiling even if you raise `extraction_timeout_seconds` past it — raising your own timeout to give a reasoning model more room cannot defeat this probe.

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
