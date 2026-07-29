**Purpose**: Table of contents and reading order for the Operator Guide.
**Audience**: SREs and sysadmins running `archon-search` in production.
**Status**: Draft
**Last reviewed**: 2026-07-29
**Next review**: 2027-07-29

# Operator Guide

This guide is for operators running `archon-search` in production. `archon-search`
is a single-process Python server — no clustering, no leader election, no native
TLS — so operating it well means getting the fundamentals right: pin the bind
address and supervise the process (deployment), watch a narrow signal surface
(monitoring, logging), protect and recover the data directory (backup/DR), let the
maintenance loop self-heal known degradation, run the graph subsystem, rotate API
keys without downtime, size the single-host capacity envelope, triage the failures
that actually occur, and upgrade safely across CalVer releases. Each page is
task-oriented and cross-links to the Architecture, Security, and Migration guides
rather than duplicating them.

## Documents

| Doc | What it covers |
|---|---|
| [10_deployment_topologies.md](10_deployment_topologies.md) | Supported single-host topologies — bind address, supervision (launchd/systemd), reverse-proxy and TLS termination. |
| [20_monitoring_and_alerts.md](20_monitoring_and_alerts.md) | What each endpoint (`/health`, `/ready`, `/status`, `/indexing-state`, `/telemetry/*`) tells you, operator thresholds, and known signal gaps. |
| [30_logging.md](30_logging.md) | The `[logging]` section — file rotation, log level, text vs JSON output, and shipping structured logs to an aggregator. |
| [40_backup_restore_disaster_recovery.md](40_backup_restore_disaster_recovery.md) | What to back up, the file-system snapshot vs scheduled `.tar.gz` backup loop, restore steps, and which DR scenarios are (and are not) supported. |
| [50_maintenance_and_jobs.md](50_maintenance_and_jobs.md) | The in-process `MaintenanceLoop` — its `[maintenance]` policies, the manual trigger, and where per-collection health surfaces. |
| [60_graph_operations.md](60_graph_operations.md) | Enabling the graph subsystem, required extras, rebuilding Leiden communities, inspection, GC, and optional LLM enrichment. |
| [70_key_management_and_rotation.md](70_key_management_and_rotation.md) | Issuing, listing, revoking, and rotating API keys against a running server with no restart, via the `KeyStore`. |
| [80_capacity_and_performance.md](80_capacity_and_performance.md) | The single-process capacity envelope, the cost surfaces that bend it, and sizing heuristics that work today. |
| [90_incident_runbook.md](90_incident_runbook.md) | Triage steps for the failure modes the codebase produces today, using only the existing endpoints, logs, and CLI. |
| [100_upgrading.md](100_upgrading.md) | Upgrade and rollback — reading CalVer, running startup vs job-based schema migrations, and where breaking changes and release notes live. |

## Related guides

- [`../UserManual/00_index.md`](../UserManual/00_index.md) — day-to-day use (install, ingest, search, jobs).
- [`../SecurityGuide/01_threat_model.md`](../SecurityGuide/01_threat_model.md) — threat model, auth, ACL, network exposure, hardening.
- [`../MigrationGuide/02_upgrade_procedure.md`](../MigrationGuide/02_upgrade_procedure.md) — the full upgrade and schema-migration reference.
- [`../Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) — the operational surface (endpoints, service install, runbooks) this guide builds on.
- [`../Architecture/990_documentation_index_and_contribution_guide.md`](../Architecture/990_documentation_index_and_contribution_guide.md) — index of every doc with review cadence.
