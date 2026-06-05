**Purpose**: Procedure for upgrading and rolling back an `archon-search` deployment, including how to read CalVer and where breaking changes are recorded.
**Audience**: SREs and sysadmins responsible for the lifecycle of an `archon-search` install.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Upgrading

`archon-search` is published to PyPI from tagged releases (`release.sh` + the `archon-search-release.yml` workflow). CalVer in the version string encodes time only; it does **not** signal compatibility. The compatibility contract is `BREAKING.md` at the repo root. There is no in-place migration tool today (`D3` on the roadmap); upgrades that change persistent data layout require a re-ingest.

## Principles

1. **CalVer encodes time, not compatibility.** Versions look like `YY.M.<rev-count>` (e.g. `26.5.314`). A higher version is not automatically safer to roll forward to — read `BREAKING.md`.
2. **`BREAKING.md` is the contract.** Every release that changes an existing REST/MCP/CLI/config contract adds an entry there. If a release has no entry, it makes no contract changes.
3. **Backup before upgrade.** Stop the service, snapshot `~/.archon-search/`, then upgrade. See `OperatorGuide/03_backup_restore_disaster_recovery.md`.
4. **`MigrationGuide/` is the long-form companion.** See `Documentation/MigrationGuide/02_upgrade_procedure.md` and `03_breaking_changes_index.md` for narrative detail; this doc is the operator-facing summary. Schema-migration tooling (roadmap `D3`) is not yet shipped — until then, upgrades that change on-disk layout require backup + re-ingest.

## CalVer at a glance

Versions are produced by `hatch-vcs` from the latest git tag (`pyproject.toml`). Format: `YY.M.<commit-count-since-anchor>`.

- `YY` = two-digit year of the tag.
- `M` = month of the tag (1–12, no leading zero).
- `<commit-count>` = monotonic across the repo's history.

Two consequences for operators:

- You **cannot** tell from the version whether a bump removes or changes an API surface. Read `BREAKING.md`.
- Releases are manual (`bash release.sh`) and gated on the eval harness in CI (`Architecture/510_release_and_environment_strategy.md`). Plain pushes to `main` do not publish.

## Pre-upgrade checklist

1. Read the diff in `BREAKING.md` between your current version and the target. Each entry lists the surface, the change, and the migration path.
2. Note any new entries under the relevant headings (REST `/search`, MCP `search`, config keys, etc.) — `BREAKING.md` is small enough to read end-to-end.
3. Verify the service is healthy before touching it: `curl -fsS http://127.0.0.1:8765/health` and `curl -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" http://127.0.0.1:8765/status`.
4. Take a cold backup (`OperatorGuide/03_backup_restore_disaster_recovery.md`).
5. Pin the current version somewhere you can reach it without internet: `pip download archon-search==<current>` into a side directory. PyPI does not guarantee permanent availability of yanked releases. #Unverified — PyPI yank semantics are not enforced or tested by this repo.

## Upgrade procedure

```bash
# 1. Stop the service.
archon-search stop

# 2. Cold backup (mandatory).
DEST="/backups/archon-search/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$DEST" && cp -a ~/.archon-search "$DEST/"

# 3. Upgrade the package. Use the same installer you used originally:
#    - uv tool:        uv tool upgrade archon-search
#    - pipx:           pipx upgrade archon-search
#    - plain pip:      pip install --upgrade archon-search
#    - pinned version: pip install archon-search==<target>

# 4. Re-run install if BREAKING.md mentions service-definition changes
#    (the plist / unit template is rewritten by archon-search install).
#    #Unverified — the rewrite behaviour of `archon-search install` on
#    upgrade was not confirmed against `cli/install_cmd.py` here.
archon-search install

# 5. Start and verify.
archon-search start
curl -fsS http://127.0.0.1:8765/health | jq -r .version
archon-search --version    # confirms the new version is live
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     http://127.0.0.1:8765/status | jq '.version, .collections[].status'
```

Verification:

- `/health` reports the new version string.
- `/status` lists the expected collections without new `error_count` entries.
- A known-good search query returns hits (pipeline failures now surface as HTTP 500/504, not silent empty results — `CON-5` resolved in A3).
- If telemetry is on, `/telemetry/stats` returns numbers, not `{"enabled": false}`.

## Configuration drift

The config loader in `archon_search/config.py` ignores unknown keys silently and uses defaults for missing ones. Two implications:

- Adding new keys to your `archon-search.toml` is safe across upgrades; old versions ignore them.
- Renamed keys may stop applying without warning. Check `BREAKING.md` for any `[section].key` renames and update before restart.
- Diff `archon-search.toml.example` against your live config after every upgrade to pick up new keys and comments. #Unverified — there is no CI step that regenerates the example on each release; treat it as hand-maintained.

Note the documented mismatch on `[telemetry].export_enabled` tracked as `TEL-1`: `archon-search.toml.example` already states the v1 behaviour (logs a warning and silently coerces to `false`), and the code matches it. The mismatch is between this behaviour and what `CLAUDE.md` / ADR-05 describe (which suggest a `ConfigError`). The runtime warning on load is intentional today; do not treat it as an upgrade failure.

## Rolling back

Roll back is **pip install + restore backup**. There is no schema migration to undo, but the on-disk LanceDB tables can be touched by the newer version in ways that an older version may refuse to open. #Unverified — this claim is not validated against `store.py` migration behaviour; treat the backup as authoritative.

```bash
# 1. Stop the service.
archon-search stop

# 2. Reinstall the previous version.
pip install archon-search==<previous-version>

# 3. Restore ~/.archon-search/ from the pre-upgrade backup. This is mandatory
#    when BREAKING.md announced any on-disk format change between the two
#    versions.
mv ~/.archon-search ~/.archon-search.failed-upgrade
cp -a /backups/archon-search/<timestamp>/.archon-search ~/

# 4. Re-run install (rewrites the service definition for the older version).
archon-search install

# 5. Start and verify as in the upgrade procedure.
archon-search start
```

If the data shape did not change between versions (the relevant `BREAKING.md` entries do not mention storage), you may roll back the package only and leave `~/.archon-search/` in place. When in doubt, restore.

## Major upgrade patterns

### MCP `search` response shape

The `[next release]` entry in `BREAKING.md` changes the MCP `search` tool from a bare list to `{"results": [...], "acl_filtered": bool}`. Operator action: notify MCP consumers; no server-side migration needed.

### REST `/search` per-request `top_k` ignored

Same release: the `top_k` field in `POST /search` is ignored at the route. Set `[database].top_k_return` in `archon-search.toml` to the desired value before upgrading. (Note: `BREAKING.md` currently refers to this key as `[search] top_k_return`, but the live key read by `archon_search/config.py` is `[database].top_k_return`; follow the code. Tracked as a `BREAKING.md` fix-up.)

### C2 — Multilingual retrieval upgrade notes

Upgrading to the C2 release activates language tagging at ingest time (when `multilingual=True`). Key considerations:

1. **`language` field type change**: `SearchResult.language`, `ScoredSearchCandidate.language`, `ExplainResult.language`, and `ExplainNearMiss.language` now return `""` instead of `None` for untagged chunks. Update any `if result.language is None` guards to `if result.language == ""`. See `BREAKING.md`.
2. **REST/JSON**: The `language` field serializes as `""` (empty string) instead of `null`. Update OpenAPI client type stubs.
3. **`language` filter previously rejected**: before C2, any non-empty `language` value raised 422. After C2, valid ISO codes and `"unknown"` are accepted. Clients that sent `language` values expecting 422 will now receive results.
4. **Re-ingest for language tags**: existing collections ingested before C2 will have `language=""` on all chunks. Use `GET /status` to see a per-collection warning when `multilingual=True` and untagged chunks are present. Re-ingest to populate language tags.
5. **Enabling multilingual**: if upgrading from an English-only install to multilingual, run `archon-search install --multilingual [--accept-fasttext-license]` to download `lid.176.ftz`, then set `multilingual = true` in `archon-search.toml` and restart.
6. **Profile switch with language detection**: switching from an English profile to a multilingual profile still requires `--force --delete-db` (unchanged pre-C2 behavior). All data must be re-ingested.

### Future schema migrations

When/if `D3` ships, schema changes will move from "stop, backup, restore on failure" to a managed migration job kind with documented rollback. Until then, treat any `BREAKING.md` entry mentioning `db_path`, LanceDB tables, or `.indexing_state.json` as requiring a backup + re-ingest plan.

## Version probing

```bash
# Server version (from the running process).
curl -fsS http://127.0.0.1:8765/health | jq -r .version

# Installed package version (from the CLI host).
archon-search --version

# Available versions on PyPI.
pip index versions archon-search
```

If the `/health` version and `archon-search --version` disagree, the supervisor is still running the old binary — restart the service.

## Related documents

- `BREAKING.md` — compatibility contract; read on every upgrade.
- `MigrationGuide/` — long-form versioning, upgrade, config, data, and client-migration guides.
- `Architecture/510_release_and_environment_strategy.md` — how releases are cut; what CI gates them.
- `OperatorGuide/03_backup_restore_disaster_recovery.md` — backup and restore procedure referenced above.
- `OperatorGuide/05_incident_runbook.md` — what to do when verification fails.
- `Backlog/03_world_class_roadmap.md` `D3` — planned schema-migration tooling.
