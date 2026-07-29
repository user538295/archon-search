**Purpose**: Table of contents and reading order for the archon-search User Manual.
**Audience**: End users / operators running archon-search on their own machine.
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# archon-search User Manual

`archon-search` is a standalone hybrid retrieval + routing server: a LanceDB vector
store, fastembed dense embeddings, a cross-encoder reranker, a multi-collection router,
a FastAPI HTTP control plane, and an MCP endpoint that shares the same auth — all in one
process, with runtime state under `~/.archon-search/`. This manual is written for a
**medior-technical operator** running that one process on their own workstation or server:
someone comfortable with a terminal, a TOML file, and `curl`, who wants to install it,
point it at some documents, and search them — without needing to read the source.

Each guide is task-oriented: common workflows, the flags and config keys that matter, and
worked `curl` / CLI / MCP examples. For the exhaustive request/response contract, the live
`GET /openapi.json` is always authoritative.

## Suggested reading order

If you are new, work top to bottom — each step builds on the previous one:

1. **[Install](10_installation.md)** — get the package onto your machine.
2. **[Run the wizard](20_wizard.md)** — the guided first-time setup.
3. **[Configure](30_configuration.md)** — tune `archon-search.toml` and environment variables.
4. **[Run the server](40_running_the_server.md)** — start, stop, and reach the endpoints.
5. **[Ingest documents](50_ingestion_and_collections.md)** — build collections from your corpus.
6. **[Search](60_searching.md)** — query the index over REST and MCP.
7. **Go further** — graph search, code impact, explain/debug, the OpenAI shim, TTL, Docker,
   and the operational guides below, in whatever order your use case needs.

## All guides

| Doc | What it covers |
|---|---|
| [10_installation.md](10_installation.md) | Install archon-search from PyPI or a `uv` checkout; Python 3.12+ and state layout. |
| [20_wizard.md](20_wizard.md) | The interactive `archon-search wizard` — every prompt, every flag, and what it writes. |
| [30_configuration.md](30_configuration.md) | The `archon-search.toml` sections and environment variables; where the auth key lives. |
| [40_running_the_server.md](40_running_the_server.md) | Start/stop/status, `serve` vs `start`, bind address, and reaching the endpoints. |
| [50_ingestion_and_collections.md](50_ingestion_and_collections.md) | Ingest files and directories; collections, pinned collections, sync vs ingest vs reindex. |
| [55_chunk_metadata_and_enrichment.md](55_chunk_metadata_and_enrichment.md) | What metadata each chunk carries, where it comes from, and how to backfill it. |
| [60_searching.md](60_searching.md) | Hybrid search over `/search`, `/route`, filters, HyDE/RAG-Fusion, and MCP. |
| [65_graph_search.md](65_graph_search.md) | The GraphRAG subsystem — entity graph, the four `graph_mode` paths, synonyms, graph viewer. |
| [70_code_graph_and_impact.md](70_code_graph_and_impact.md) | Index source into a def/ref graph and ask "what breaks if I change this symbol?". |
| [80_explain_and_debugging.md](80_explain_and_debugging.md) | Use `POST /explain` to see per-stage provenance and tune routing, reranking, and scoring. |
| [85_openai_compatible_api.md](85_openai_compatible_api.md) | Point OpenAI-native tools at archon-search via the `/v1` retrieval-only shim. |
| [90_export_import.md](90_export_import.md) | Package a collection as a portable `.tar.gz` and unpack it on the same or another instance. |
| [100_jobs_and_async_operations.md](100_jobs_and_async_operations.md) | The async job model; poll, resume, and manage jobs from the CLI and REST. |
| [120_telemetry.md](120_telemetry.md) | Opt-in local query telemetry — enable, inspect, and the no-raw-query guarantee. |
| [130_ttl_and_scoping.md](130_ttl_and_scoping.md) | Per-chunk `expires_at` TTL auto-pruning and `scopes` tags for multi-agent corpora. |
| [140_running_with_docker.md](140_running_with_docker.md) | Run from the published image with `docker run` / `compose`; CPU/GPU build args. |
| [150_multi_instance_setup.md](150_multi_instance_setup.md) | Run a native prod instance and a Docker dev-UAT instance side by side. |
| [160_troubleshooting.md](160_troubleshooting.md) | Diagnose common runtime failures, starting from `/health`, `/ready`, `/status`, and the logs. |

## Where to go next

This manual covers running archon-search for yourself. Sibling guides go deeper on
specific concerns:

- **Running it in production** — [../OperatorGuide/00_index.md](../OperatorGuide/00_index.md)
  (deployment topologies, monitoring, backups, maintenance, capacity, incident runbooks).
- **Calling the API from code** — [../DeveloperGuide/01_overview.md](../DeveloperGuide/01_overview.md)
  (REST/MCP clients in Python and TypeScript, auth, error handling).
- **Security & privacy** — [../SecurityGuide/01_threat_model.md](../SecurityGuide/01_threat_model.md)
  (threat model, keys, ACL, network exposure, hardening).
- **Upgrading & migrations** — [../MigrationGuide/01_versioning_and_release_model.md](../MigrationGuide/01_versioning_and_release_model.md)
  (versioning, upgrade procedure, breaking changes, config/data migration).
- **Everything, indexed** — [../Architecture/990_documentation_index_and_contribution_guide.md](../Architecture/990_documentation_index_and_contribution_guide.md)
  (the full documentation index and contribution guide).
