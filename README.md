# archon-search

**A hybrid search server for your own machine — vector + full-text + reranking + routing across collections, with no external service to trust with your data by default.**

- **PyPI**: https://pypi.org/project/archon-search/
- **GitHub**: https://github.com/user538295/archon-search

Point it at a directory, ask it a question, get back ranked chunks with source paths:

```bash
curl -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection": "docs", "query": "how does the router work?"}'
```

```json
{
  "results": [
    {
      "doc_id": "docs/routing.md",
      "chunk_id": "docs/routing.md#3",
      "text": "The router scores each collection's centroid against the query embedding...",
      "score": 0.84,
      "source_path": "/Users/you/project/docs/routing.md",
      "collection": "docs"
    }
  ],
  "acl_filtered": false
}
```

No cloud call happened to produce that by default — the index, the embeddings, and the reranker all run on the box you started `archon-search` on. (HyDE and RAG Fusion, both opt-in, are the exception: they call out to an LLM provider — see [Features](#features).)

## What it's made of

- **LanceDB** as the local vector store — an embedded file format, not a service you have to run alongside it.
- **fastembed** for dense embeddings — no GPU required to get started.
- **A cross-encoder reranker** — first-stage recall from vector + FTS, second-stage precision from the reranker, RRF-fused.
- **A multi-collection router** — one query, many collections; the router scores each collection's centroid and picks which ones actually get searched instead of fanning out to all of them.
- **FastAPI** for the REST control plane, contracted via OpenAPI 3.x — `GET /openapi.json` is the source of truth, not this file.
- **An MCP endpoint** — the same tools your HTTP client uses, exposed to MCP clients (Claude Code, etc.) over the same auth.

It's one process. It persists everything under `~/.archon-search/`. There's no separate vector DB to stand up, no message queue, no second auth system for the MCP side.

## Features

**Search that gets smarter about the query, not just the index:**

- **Vague or short queries under-retrieve with plain vector search.** `hyde=true` on `/search` generates a hypothetical answer and embeds *that* instead of the raw query — closes the gap between "what you typed" and "what the answer looks like." (`archon-search[hyde]`, needs an LLM provider: Anthropic — the default, included in the `hyde` extra — or Ollama (`archon-search[ollama]`, plus a running Ollama server), OpenAI (`archon-search[openai-provider]`), `claude_cli` — uses Claude Code's own login, no extra install and no API key — or `llama_cpp` — a local llama-server instance, no extra install and no API key; use a small, direct-response instruct model, not a reasoning model, or the entire token budget goes to hidden chain-of-thought and HyDE/RAG Fusion silently stay disabled)
- **One query rarely covers what a broad question needs.** `rag_fusion=true` decomposes the query into sub-queries, searches all of them in parallel, and fuses the results with a second RRF pass. Mutually exclusive with HyDE — pick the one that fits the query shape. (`archon-search[rag_fusion]`, same provider options and extras as HyDE above)
- **Non-English corpora get silently penalized by English-tuned defaults.** Per-chunk language is detected with fastText at ingest time, and `filters.language` lets you scope a query to it.
- **Different collections need different embedding models** (code vs. prose, different languages) — `active_embedding_model` is set per collection and enforced through ingest, search, and sync; a mismatch raises loud, not silent.

**Retrieval that understands what it's indexing, not just text blobs:**

- **Code search on line-blind chunks returns half a function.** Code files get tree-sitter-aware chunking that keeps function/class boundaries intact instead of cutting on line count.
- **A hit with no location context means opening the file to find your answer.** Headings, section paths, and page numbers get extracted and attached to each chunk at ingest.
- **"Search everything" isn't the same as "search the right thing."** `filters` on `/search` narrow by file type, source-path prefix or glob, and `indexed_after`/`indexed_before` — before the query ever reaches the reranker.
- **A ranked list without a reason is a black box you can't debug.** `/explain` returns the full pipeline trace — vector score, FTS score, rerank score, routing decision — per candidate, with per-stage timings.

**Operations that don't need someone awake at 3am:**

- **API keys that never rotate are a liability, and rotating them without downtime is normally its own project.** `POST /keys/rotate` swaps in a new key while the old one stays valid through a configurable grace period.
- **A crashed backup script means you find out about data loss when you need the backup.** A background loop exports and rotates backups on a schedule — no cron job to babysit.
- **Schema changes usually mean a forced re-ingest.** Migrations run through a versioned `MigrationSpec` registry with documented rollback rules — existing collections migrate in place.
- **FTS index rebuilds that scale with corpus size make maintenance windows scale with corpus size too.** `optimize_fts()` is incremental — it updates what changed, not what already existed.
- **"Works on my machine" isn't a deployment story.** CPU and NVIDIA GPU images are published to GHCR; the tiered install wizard (`minimal` / `balanced` / `max`) picks embedding + reranker models that fit your disk budget instead of downloading everything.

## Installation

```bash
# pip
pip install archon-search
archon-search wizard

# uv (installs the CLI into an isolated managed environment)
uv tool install archon-search
archon-search wizard
```

`archon-search wizard` does the rest: pick a profile (`minimal`, `balanced`, or `max`), it pulls the matching embedding and reranker models and registers the server as a background service. Full profile comparison, flags, and disk-space requirements: [Documentation/UserManual/10_installation.md](Documentation/UserManual/10_installation.md).

Prefer a checkout over a package? Clone and sync:

```bash
git clone https://github.com/user538295/archon-search.git
cd archon-search
uv sync --dev
```

## Uninstall

Stop and unregister the service first, while the CLI is still on disk:

```bash
archon-search uninstall
```

Add `--delete-db` if you also want the search database gone — that's irreversible, it removes every indexed chunk:

```bash
archon-search uninstall --delete-db
```

Then remove the package itself:

```bash
# pip
pip uninstall archon-search

# uv tool
uv tool uninstall archon-search

# checkout / dev install — delete the cloned directory
```

Neither step touches your data. `uninstall` only stops and unregisters the OS service; removing the package only removes the CLI binary. If you want a clean wipe, these paths are still on disk and need manual deletion:

| Path | Contents |
|------|----------|
| `~/.archon-search/archon-search.toml` | Server config |
| `~/.archon-search/.search.env` | API key |
| `~/.archon-search/search/` | LanceDB vector store and FTS index |
| `~/.archon-search/logs/` | Server logs |
| `~/.archon-search/models/` | Downloaded fastText language-ID model (only if the `multilingual` extra is installed) |
| `~/.archon-search/search-logs/` | Telemetry JSONL (only if telemetry was enabled) |

Or skip the table and remove all of it in one shot:

```bash
rm -rf ~/.archon-search/
```

That does **not** remove the embedding/reranker model weights fastembed downloads — those are cached in fastembed's own default cache (outside `~/.archon-search/`), not under this directory. Clear that separately for a truly complete wipe.

## Quick start

Start the server in the foreground:

```bash
archon-search serve
```

That runs the FastAPI app on the configured host/port — `serve` defaults to `0.0.0.0:8765` (all interfaces, not just loopback), unlike the library default of `127.0.0.1:8765` used when embedding `SearchConfig` directly. Binding all interfaces means the server is reachable from other machines on the network the moment it starts; put a reverse proxy in front of it (see [Docker](#running-with-docker)) before exposing it past loopback. It blocks until you stop it (Ctrl-C). For a background service managed by launchd/systemd, use `archon-search wizard` instead — see [Installation](#installation).

Once it's up:

- `GET /health` — unauthenticated liveness probe
- `GET /ready` — unauthenticated readiness probe
- `GET /docs` — interactive Swagger UI
- `GET /openapi.json` — machine-readable OpenAPI schema

Then hit `/search` as shown at the top of this file.

## Running with Docker

Two images, CPU (`:latest`) and NVIDIA GPU (`:gpu`). Both run the foreground `archon-search serve` subcommand, bind to `0.0.0.0:8765`, persist everything under `/data`, and write logs to stderr so `docker logs` actually shows something.

Kick the tires — ephemeral, the key regenerates on every start, nothing persists:

```bash
docker run --rm -p 8765:8765 ghcr.io/user538295/archon-search:latest
```

Run it for real — pin the key, pin the volume:

```bash
docker run -d \
  --name archon-search \
  -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
  -v archon-search-data:/data \
  -p 8765:8765 \
  ghcr.io/user538295/archon-search:latest
```

**Skip the volume or the env var and every restart mints a new key** — every token you handed out stops working, silently. Mount a volume so the key persists at `/data/.search.env`, or pass `ARCHON_SEARCH_API_KEY` explicitly. Pick one; don't rely on neither.

Want a dev/test/prod stack with isolated volumes instead of one container? [`docker-compose.yml`](docker-compose.yml) and [`.env.example`](.env.example) have it. Full operator guide — compose stack, image variants, env-var reference, persistence layout: [Documentation/UserManual/140_running_with_docker.md](Documentation/UserManual/140_running_with_docker.md).

**LanceDB is single-writer.** Mount the same data volume into two running containers and the on-disk state is undefined — don't do that.

**The container speaks plaintext HTTP, nothing else.** Put a reverse proxy (nginx, Caddy, Traefik) in front of it before you expose it past loopback.

## Authentication

Every endpoint requires a `Bearer` token in the `Authorization` header, except a small unauthenticated set: `GET /health`, `GET /ready`, `GET /docs`, `GET /openapi.json`, `GET /redoc`. One route has a second, narrower exemption — `GET /graph/{collection}/view` (the HTML graph viewer) also accepts the token as a `?token=` query parameter instead of the header, since it's meant to be opened directly in a browser; the query param is still validated against the same key set (`archon_search/server/middleware_auth.py`).

First start auto-generates a key and writes it to `~/.archon-search/.search.env` at `600`. Running in Docker, CI, or across multiple hosts? Set `ARCHON_SEARCH_API_KEY` — it wins over the file every time. Need the key read from somewhere else? Set `ARCHON_SEARCH_KEY_FILE`. Need the whole runtime tree — index, logs, key file, jobs file, the fastText language-ID model cache, ingest history — under one root instead of `~/.archon-search/`? Set `ARCHON_SEARCH_DATA_DIR` (the Docker image already does this for you, pointing it at `/data`). This does not relocate fastembed's embedding/reranker weight cache, which fastembed manages in its own default cache outside this tree — see [Uninstall](#uninstall).

## Configuration

Everything server-side lives in one file: `~/.archon-search/archon-search.toml`.

- `[database]` — `db_path`, `embedding_model`, `chunk_size`, `top_k_return`, model paths, per-collection embedder pool sizing (`embedder_cache_size`, default `3`; `eager_load_embedders`, default `false`, pre-warms `embedding_model` plus every distinct per-collection model and the reranker cross-encoder at startup instead of lazily)
- `[search]` — multi-collection fan-out bounds (`max_fanout`, `fanout_timeout_seconds`)
- `[routing]` — `routing_shortlist_size`, `routing_confidence_threshold`, routing strategy
- `[collections]` — `pinned_collections`, static collection definitions, watcher settings
- `[telemetry]` — opt-in local query logging, covered below

The full annotated reference — every key, every default — is `archon-search.toml.example`. This section is the map, not the territory.

## REST API

`GET /openapi.json` is the contract — endpoint shapes, request/response types, error codes, all of it. This README is not. `GET /docs` serves the same thing as an interactive explorer.

Breaking changes to REST or MCP land in [`BREAKING.md`](BREAKING.md), not buried in a changelog entry.

## MCP tools

Your MCP client gets the same server your HTTP client does — same auth, same data, no separate integration to build. 16 tools always register; 4 more join when a key store is configured, 20 total (`archon_search/server/mcp.py`):

- `search` — hybrid vector + FTS search; returns `{"results": [...], "acl_filtered": bool}`
- `search_with_context` — same as `search` with adjacent-chunk context
- `explain` — per-stage retrieval/reranking trace plus routing decision (mirrors `POST /explain`)
- `ingest_file` — index a single file into a collection
- `ingest_directory` — recursively index a directory
- `list_collections` — list collection names
- `get_collections_meta` — metadata for all collections
- `get_collection_meta` — metadata for one collection
- `list_documents` — list documents in a collection
- `delete_document` — remove a document by `doc_id`
- `update_collection` — change a collection's embedding model (mirrors `PATCH /collections/{name}`)
- `export_collection` / `import_collection` — archive a collection out or restore it
- `get_graph` — entity-graph summary for one collection (nodes, edges, top entities by salience)
- `get_graph_cross_collection` — merged entity graph across 2+ collections
- `graph_impact` — blast-radius caller/callee analysis for a code symbol
- `create_key` / `list_keys` / `revoke_key` / `rotate_key` — API key lifecycle, mirroring the REST `/keys` endpoints (registered only when key management is configured)

## Telemetry (opt-in)

Off by default (`enabled = false`), and it stays off until you flip it. Flip it, and every `search`, `search_with_context`, `search_multi`, `POST /route`, and `/explain` call appends one JSONL line to a daily file under `~/.archon-search/search-logs/`. It never leaves the machine — no export, no phone-home, no exceptions.

### Enabling

```toml
# ~/.archon-search/archon-search.toml
[telemetry]
enabled = true
retention_days = 30          # files older than this are deleted at startup and every 24h
log_dir = "~/.archon-search/search-logs"
hash_doc_ids = false         # set true to HMAC-SHA256 result_doc_ids before writing to JSONL
```

### What is logged

`query_id` (random UUID), `timestamp` (UTC), `endpoint`, `latency_ms`, `status`, plus whatever's specific to the call — `collection`/`result_count`/`result_doc_ids` for retrieval, `collections`/`decomposer_invoked` for routing. Errors add one more field, `error_kind`, from a closed set: `empty_query | slot_out_of_range | timeout | internal_error | validation_error | other`.

### What is never logged

**The raw query string, never.** Not a policy — a structural guarantee: the factory methods that build telemetry entries have no `query` parameter to pass one in. Exception messages don't make it in either; only the coarse `error_kind` string does.

### The catch: doc_ids leak paths

doc_ids may reveal filesystem paths: `result_doc_ids` comes straight from the file path on disk — `/Users/<name>/Documents/<project>/<file>.md`. Turn telemetry on and those paths, username included, sit in your log files. Set `hash_doc_ids = true` and every `doc_id` gets HMAC-SHA256'd before it's written, opaque to anyone without the salt file at `~/.archon-search/.telemetry-salt`. Decide if that's worth doing before you turn telemetry on, not after.

### `export_enabled` does nothing yet

It's reserved for a future release. Set it to `true` today and the config loader logs a warning and quietly coerces it back to `false` (`archon_search/config.py`). Nothing gets transmitted in v1 — there's no code path that would.

### Telemetry read-back API

Both endpoints return `{"enabled": false}` when telemetry is disabled.

#### `GET /telemetry/stats`

Aggregated query statistics over an optional time window.

| Parameter | Type | Description |
|-----------|------|-------------|
| `since` | YYYY-MM-DD | Start date (inclusive, optional) |
| `until` | YYYY-MM-DD | End date (inclusive, optional) |

Response shape summary:

```json
{
  "schema_version": 1,
  "enabled": true,
  "total_queries": 42,
  "success_rate": 0.95,
  "latency_ms": {"p50": 120, "p95": 380},
  "by_endpoint": {"search": 30, "route": 12},
  "by_collection": {"docs": 25, "code": 17},
  "error_breakdown": {"timeout": 2, "internal_error": 0}
}
```

`success_rate` is `null` when no queries exist in the window.

#### `GET /telemetry/entries`

Paginated raw log entries.

| Parameter | Type | Description |
|-----------|------|-------------|
| `since` | YYYY-MM-DD | Start date (optional) |
| `until` | YYYY-MM-DD | End date (optional) |
| `collection` | string | Filter by collection name (optional) |
| `endpoint` | string | Filter by endpoint (optional) |
| `status` | string | Filter by status (optional) |
| `error_kind` | string | Filter by error kind (optional) |
| `offset` | int | Pagination offset, default 0 |
| `limit` | int | Page size, 1–200, default 50 |

Response includes `entries`, `next_offset`, and `total_in_window`. Clients should continue calling with the returned `next_offset` until `entries` is empty (equivalently, until `next_offset >= total_in_window`).

## Evaluation harness

"Retrieval quality didn't regress" is a claim, not a feeling — `tests/eval/` is what backs it: a synthetic corpus, query/label fixtures, deterministic eval backends, committed thresholds, a measured baseline. It's the sanctioned gate for any change touching retrieval, reranking, routing, or latency.

```bash
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py
```

The backends are **deterministic** — corpus-aware but label-blind, so metrics hold steady across runs without loading real model weights. Latency p50/p95 is a **regression guard, not a production SLA** — it tells you if this change got slower than the last one, not what to expect in prod.

Current baseline (recall@k, MRR, nDCG@k, reranker lift, routing accuracy, latency percentiles): [`tests/eval/baselines/baseline.md`](tests/eval/baselines/baseline.md), machine-readable twin at `tests/eval/baselines/baseline.json`. Changing a threshold or a fixture? Read [`tests/eval/README.md`](tests/eval/README.md) first — it's the maintenance guide and the waiver policy, not optional reading.

## Development

```bash
git clone https://github.com/user538295/archon-search.git
cd archon-search
uv sync --dev
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE).
