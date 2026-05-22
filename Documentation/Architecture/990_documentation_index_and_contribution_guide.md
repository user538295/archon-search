Purpose: Single entry point to all `archon-search` documentation, plus the rules for adding and maintaining docs.
Audience: Medior engineers contributing to or navigating the `archon-search` documentation set.
Status: Draft
Last reviewed: 2026-05-20 / Next review: 2026-08-20

# Documentation Index and Contribution Guide

## Guiding principles

1. **One index, one source of navigation.** Every doc under `/Documentation/` is listed here. If a doc is not in this index, it does not exist as far as readers are concerned.
2. **Numbered prefixes encode reading order.** `000`–`099` are foundations, `100`–`299` are architecture (including testing/perf/a11y at `200`–`220`), `300`–`499` are unallocated, `500`–`599` are workflows and roadmap-style technical reference, `600` is API reference, `990` is meta. The cadence table below groups `500`–`699` together as "Technical reference"; the workflows label on `500`–`599` is a narrative gloss, not a strict taxonomy. #Unverified
3. **Source of truth is code.** Docs explain intent and trade-offs; they never replace `/openapi.json`, `BREAKING.md`, or the test suite.
4. **Cross-link liberally.** Every doc should point readers to neighbours; orphan docs are a smell.
5. **Review on a cadence.** Architecture quarterly, technical reference bi-annual, process and meta annual, ADRs only when superseded (never edited after acceptance) — see [Maintenance and review cadence](#maintenance-and-review-cadence).

## Architecture

| File | Purpose |
| --- | --- |
| [`Architecture/000_introduction_and_guiding_principles.md`](./000_introduction_and_guiding_principles.md) | Project introduction, scope, and top-level guiding principles. |
| [`Architecture/010_engineering_principles_and_constraints.md`](./010_engineering_principles_and_constraints.md) | Engineering values, hard constraints, non-goals. |
| [`Architecture/100_system_architecture_overview.md`](./100_system_architecture_overview.md) | High-level architecture and pipeline overview. |
| [`Architecture/110_component_catalog_and_layer_breakdown.md`](./110_component_catalog_and_layer_breakdown.md) | Per-module catalog: parser, chunker, embedder, store, reranker, pipeline, router. |
| [`Architecture/120_services_and_integration_architecture.md`](./120_services_and_integration_architecture.md) | FastAPI server, MCP endpoint, OS service integration, external interfaces. |
| [`Architecture/130_data_architecture_and_persistence.md`](./130_data_architecture_and_persistence.md) | LanceDB layout, FTS index, indexing state, on-disk paths under `~/.archon-search/`. |
| [`Architecture/140_error_handling_strategy.md`](./140_error_handling_strategy.md) | Status code conventions, retry semantics, failure modes. |
| [`Architecture/150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md) | Bearer auth, namespaces, ACL, telemetry privacy invariants. |
| [`Architecture/160_operational_readiness_monitoring_and_reliability.md`](./160_operational_readiness_monitoring_and_reliability.md) | Health, status, telemetry, alerts, on-call. |
| [`Architecture/200_testing_strategy.md`](./200_testing_strategy.md) | Test pyramid, markers (`live`, `eval`, `integration`, `benchmark`), coverage gate. |
| [`Architecture/210_performance_and_scalability.md`](./210_performance_and_scalability.md) | Latency budgets, eval baselines, scaling considerations. |
| [`Architecture/220_accessibility_and_internationalization.md`](./220_accessibility_and_internationalization.md) | CLI accessibility posture and internationalization scope (English-only by design). |
| [`Architecture/500_development_workflows_and_conventions.md`](./500_development_workflows_and_conventions.md) | `uv`, pytest, lint, commit and branch conventions. |
| [`Architecture/510_release_and_environment_strategy.md`](./510_release_and_environment_strategy.md) | `release.sh`, CalVer tagging, CI/CD, PyPI OIDC. |
| [`Architecture/520_api_design_and_contracts.md`](./520_api_design_and_contracts.md) | Design rules for REST, MCP, and CLI surfaces. |
| [`Architecture/530_technical_debt_refactoring_roadmap.md`](./530_technical_debt_refactoring_roadmap.md) | Grounded register of known debt, severity, triggers, and planned refactors. |
| [`Architecture/600_api_reference_or_public_interface.md`](./600_api_reference_or_public_interface.md) | Endpoint-by-endpoint REST, MCP, CLI reference. |
| [`Architecture/990_documentation_index_and_contribution_guide.md`](./990_documentation_index_and_contribution_guide.md) | This file. |

## User Manual

End-user / operator documentation. Audience is medior-technical operators running `archon-search` on their own machine; contributors should read `contributing.md` instead.

| File | Purpose |
| --- | --- |
| [`UserManual/01_installation.md`](../UserManual/01_installation.md) | Install from PyPI or a checkout; ONNX provider tips; optional service install. |
| [`UserManual/02_configuration.md`](../UserManual/02_configuration.md) | `archon-search.toml` sections, `ARCHON_SEARCH_CONFIG`, auth key resolution. |
| [`UserManual/03_running_the_server.md`](../UserManual/03_running_the_server.md) | `start`/`stop`/`status`, exposed endpoints, Bearer-auth examples. |
| [`UserManual/04_ingestion_and_collections.md`](../UserManual/04_ingestion_and_collections.md) | `ingest`/`sync`/`collection` CLI and REST equivalents; watcher and reindex triggers. |
| [`UserManual/05_searching.md`](../UserManual/05_searching.md) | `POST /search`, `POST /route`, and the nine MCP tools. |
| [`UserManual/06_telemetry.md`](../UserManual/06_telemetry.md) | Opt-in local telemetry, no-raw-query invariant, read-back endpoints. |
| [`UserManual/07_troubleshooting.md`](../UserManual/07_troubleshooting.md) | Common failure modes: auth, empty results, stuck reindex, install hangs. |

## Migration Guide

Versioning, upgrade procedure, and the contract-change index. Audience: maintainers and operators upgrading between CalVer releases.

| File | Purpose |
| --- | --- |
| [`MigrationGuide/01_versioning_and_release_model.md`](../MigrationGuide/01_versioning_and_release_model.md) | CalVer scheme, how to read `YY.M.<rev-count>`, where the version comes from (`hatch-vcs` + tags), how to read `BREAKING.md`. |
| [`MigrationGuide/02_upgrade_procedure.md`](../MigrationGuide/02_upgrade_procedure.md) | Generic upgrade flow: read `BREAKING.md`, back up `~/.archon-search/`, stop, install, verify with `/health` + smoke search; rollback procedure. |
| [`MigrationGuide/03_breaking_changes_index.md`](../MigrationGuide/03_breaking_changes_index.md) | Chronological index of `BREAKING.md` entries with one-line migrations and links to the authoritative source. |
| [`MigrationGuide/04_config_migration.md`](../MigrationGuide/04_config_migration.md) | `archon-search.toml` keys, the `export_enabled` silent-coerce quirk (TEL-1), validation via `archon-search config show`. |
| [`MigrationGuide/05_data_migration.md`](../MigrationGuide/05_data_migration.md) | On-disk layout under `~/.archon-search/`, what survives upgrades, manual reindex via `archon-search collection reindex`, gap to roadmap D3. |
| [`MigrationGuide/06_client_migration_examples.md`](../MigrationGuide/06_client_migration_examples.md) | Python / TypeScript / MCP diffs for the queued NR-1 (MCP response shape) and NR-2 (`top_k` ignored) breaking changes. |

## Developer Guide

Integration-side documentation for engineers consuming `archon-search` from another application (Python/TypeScript clients, MCP-aware tooling). End-user / operator material lives in [User Manual](#user-manual); these docs assume you are *calling* the server, not running it.

| File | Purpose |
| --- | --- |
| [`DeveloperGuide/01_overview.md`](../DeveloperGuide/01_overview.md) | Scope, non-goals, REST/MCP duality, auth model in one paragraph. |
| [`DeveloperGuide/02_authentication.md`](../DeveloperGuide/02_authentication.md) | API-key resolution, Bearer header, per-namespace keys. |
| [`DeveloperGuide/03_rest_client_python.md`](../DeveloperGuide/03_rest_client_python.md) | `httpx` examples for search, collections, jobs, route, health. |
| [`DeveloperGuide/04_rest_client_typescript.md`](../DeveloperGuide/04_rest_client_typescript.md) | Same flows in TypeScript with hand-written types matching `schemas.py`. |
| [`DeveloperGuide/05_mcp_integration.md`](../DeveloperGuide/05_mcp_integration.md) | The 9 MCP tools, Claude Code wiring, SDK usage. |
| [`DeveloperGuide/06_error_handling.md`](../DeveloperGuide/06_error_handling.md) | REST status codes, MCP `McpErrorResponse`, retry guidance, `CON-5` quirk. |
| [`DeveloperGuide/07_versioning_and_breaking_changes.md`](../DeveloperGuide/07_versioning_and_breaking_changes.md) | CalVer scheme, `BREAKING.md` reading guide, client pinning. |

## Operator Guide

Production-grade operations documentation for SREs and sysadmins running `archon-search` on a single host. Complements [`160_operational_readiness_monitoring_and_reliability.md`](./160_operational_readiness_monitoring_and_reliability.md) with concrete topology, alerting, backup, capacity, runbook, and upgrade procedures.

| File | Purpose |
| --- | --- |
| [`OperatorGuide/01_deployment_topologies.md`](../OperatorGuide/01_deployment_topologies.md) | Foreground, `launchd`, `systemd --user`; bind/firewall guidance; nginx and Caddy reverse-proxy patterns. |
| [`OperatorGuide/02_monitoring_and_alerts.md`](../OperatorGuide/02_monitoring_and_alerts.md) | `/health`, `/status`, `/indexing-state`, `/telemetry/*` — what each reports, gaps, suggested alert rules. |
| [`OperatorGuide/03_backup_restore_disaster_recovery.md`](../OperatorGuide/03_backup_restore_disaster_recovery.md) | Backing up `~/.archon-search/`, restore steps, disaster scenarios, no-export-API gap. |
| [`OperatorGuide/04_capacity_and_performance.md`](../OperatorGuide/04_capacity_and_performance.md) | Single-process limits, ingest cost surfaces (`CON-4`, `C6`), router-cache caveats (`CON-2`), sizing heuristics. |
| [`OperatorGuide/05_incident_runbook.md`](../OperatorGuide/05_incident_runbook.md) | Triage for stuck jobs, watcher churn, key loss, LanceDB locks, search pipeline failures / timeouts (`CON-5` resolved in A3), telemetry log explosion. |
| [`OperatorGuide/06_upgrading.md`](../OperatorGuide/06_upgrading.md) | Reading CalVer + `BREAKING.md`, pre-upgrade backup, upgrade and rollback procedure; links to `MigrationGuide/` for detail. |

## Architecture Decision Records (ADRs)

| File | Purpose |
| --- | --- |
| [`ADRs/01_lancedb_as_local_vector_store.md`](../ADRs/01_lancedb_as_local_vector_store.md) | Why LanceDB for vectors + FTS. |
| [`ADRs/02_fastembed_for_dense_embeddings.md`](../ADRs/02_fastembed_for_dense_embeddings.md) | Why fastembed for local dense embeddings. |
| [`ADRs/03_cross_encoder_reranker_second_stage.md`](../ADRs/03_cross_encoder_reranker_second_stage.md) | Why a cross-encoder second stage. |
| [`ADRs/04_multi_collection_router_with_centroid_preranking.md`](../ADRs/04_multi_collection_router_with_centroid_preranking.md) | Why centroid pre-ranking for multi-collection routing. |
| [`ADRs/05_opt_in_local_telemetry_no_raw_query.md`](../ADRs/05_opt_in_local_telemetry_no_raw_query.md) | Telemetry: opt-in, local-only, no raw queries. |

## Security Guide

Security-focused documentation for security engineers, IT admins, and reviewers signing off a deployment.

| File | Purpose |
| --- | --- |
| [`SecurityGuide/01_threat_model.md`](../SecurityGuide/01_threat_model.md) | Assets, trust boundaries, in-scope and out-of-scope threats for v1. |
| [`SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md) | Bearer auth, default + per-namespace keys, key file permissions, manual rotation. |
| [`SecurityGuide/03_authorization_and_acl.md`](../SecurityGuide/03_authorization_and_acl.md) | Namespace scoping, per-chunk ACL semantics, what ACL does not do. |
| [`SecurityGuide/04_telemetry_privacy.md`](../SecurityGuide/04_telemetry_privacy.md) | Opt-in telemetry, no-raw-query invariant, retention, `doc_id` leak risk. |
| [`SecurityGuide/05_network_exposure_and_tls.md`](../SecurityGuide/05_network_exposure_and_tls.md) | Loopback default, no native TLS, wildcard CORS, reverse-proxy guidance. |
| [`SecurityGuide/06_hardening_checklist.md`](../SecurityGuide/06_hardening_checklist.md) | Pre-production checklist with verification commands. |

## Backlog

| File | Purpose |
| --- | --- |
| [`Backlog/01_competitive_analysis_field.md`](../Backlog/01_competitive_analysis_field.md) | Competitive analysis (field-level). |
| [`Backlog/02_competitive_analysis_marveen.md`](../Backlog/02_competitive_analysis_marveen.md) | Competitive analysis (Marveen). |
| [`Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) | Long-form roadmap for reaching world-class retrieval quality. |

## Root-level docs

| File | Purpose |
| --- | --- |
| [`roadmap.md`](../roadmap.md) | Active roadmap and milestones. |
| [`quick_start.md`](../quick_start.md) | 5-minute onboarding for new contributors. |
| [`../../BREAKING.md`](../../BREAKING.md) | BREAKING.md — compatibility contract; every release that changes an existing API contract adds an entry here. |

## Contribution guide

### Adding a new document

1. **Pick the right bucket.** Architecture lives under `Architecture/`, decisions under `ADRs/`, backlog items under `Backlog/`. Anything else needs discussion in PR review first.
2. **Number the file.** Architecture: extend the existing numeric range with a 10-step gap (e.g. next testing-adjacent doc → `230_…`, since `220_accessibility_and_internationalization.md` is taken). ADRs: monotonically increasing two-digit prefix. Use lowercase, snake-cased filenames.
3. **Write the metadata header.** Every doc starts with a 4-line header followed by a single H1, then 3–5 principles up front:
   ```
   Purpose: <one sentence>
   Audience: <who reads this>
   Status: Draft | Active | Deprecated
   Last reviewed: YYYY-MM-DD / Next review: YYYY-MM-DD
   ```
4. **Update this index.** Add a row in the correct table with a one-line purpose and a relative link. PRs that add docs without updating this index should not be merged — this is a policy, not a CI-enforced gate (no workflow under `.github/workflows/` currently lints the index). #Unverified
5. **Cross-link contextually.** Reference neighbouring docs with relative paths in a "Related documents" section at the bottom and inline where it helps the reader.
6. **Use Mermaid for diagrams.** Embed fenced ` ```mermaid ` blocks; do not check in rendered images unless the diagram cannot be expressed in Mermaid.
7. **Trace to code.** Where the doc describes behaviour, name the module/file/function so future readers can verify. Never paraphrase code rules without a pointer.
8. **Respect existing terminology.** Collection, namespace, ACL, centroid, pipeline have specific meanings — see [`110_component_catalog_and_layer_breakdown.md`](./110_component_catalog_and_layer_breakdown.md).

### Updating an existing document

- Bump `Last reviewed` and `Next review` whenever the content changes materially.
- If a doc becomes inaccurate, prefer fixing it in the same PR as the code change. Stale docs are a release blocker only when they document a public surface (REST/MCP/CLI/config).
- Breaking surface changes also require a `BREAKING.md` entry — see [`/BREAKING.md`](../../BREAKING.md).

### Maintenance and review cadence

| Document kind | Cadence | Owner |
| --- | --- | --- |
| Architecture (`100`–`299`) | Quarterly | Maintainer of the affected module |
| Technical reference (`500`–`699`) | Bi-annual | Whoever last shipped a related change |
| Process and meta (`000`–`099`, `990`) | Annual | Project lead |
| ADRs | Reviewed only when superseded; never edited after acceptance — supersede instead | Original author |

Review means: re-read end-to-end, fix what is wrong, bump the header dates, and open a PR even if the diff is small. A review with no diff is still a valid review — record it in the PR description.

## Related documents

- [`000_introduction_and_guiding_principles.md`](./000_introduction_and_guiding_principles.md) — start here if you are new.
- [`600_api_reference_or_public_interface.md`](./600_api_reference_or_public_interface.md) — public surface reference.
- [`/BREAKING.md`](../../BREAKING.md) — compatibility contract.
- [`/README.md`](../../README.md) — project-level readme.
