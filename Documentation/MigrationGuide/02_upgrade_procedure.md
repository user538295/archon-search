**Purpose**: Provide a generic, repeatable upgrade procedure for `archon-search`, including the rollback path.
**Audience**: Operators upgrading an installed copy of `archon-search`; maintainers writing release notes that link here.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-05-20

# Upgrade Procedure

Upgrades are a five-step loop: read, back up, stop, install, verify. The same loop is used for forward upgrades and rollbacks; only the install step differs.

## Principles

1. **Read `BREAKING.md` first.** Skipping this step is the most common cause of post-upgrade outages.
2. **Back up `~/.archon-search/` before anything else.** All durable state lives there; a single tarball is a complete recovery point.
3. **Stop the server before swapping the wheel.** Running processes hold open file handles into the state directory.
4. **Verify with `GET /ready` (and `GET /status` with a key) and a smoke search**, not by reading the version string alone.

## Step 1 — Read `BREAKING.md` for your range

Identify the version you have and the version you want:

```bash
archon-search --version              # current installed version (see 01_versioning_and_release_model.md)
pip index versions archon-search     # available versions on PyPI
```

Open [`/BREAKING.md`](../../BREAKING.md) and read every entry strictly between your current version and the target. If you are upgrading to a release that has just been tagged, also re-read the `[next release]` entries that were rolled into it. #Unverified (existence of `[next release]` section in `BREAKING.md` not confirmed against current source) See [`03_breaking_changes_index.md`](./03_breaking_changes_index.md) for a curated index.

## Step 2 — Back up `~/.archon-search/`

The state directory holds **everything** that persists across restarts: config, the API key, the LanceDB vector + FTS indexes, indexing-state JSON, and (if enabled) telemetry logs. See [`Architecture/130_data_architecture_and_persistence.md`](../Architecture/130_data_architecture_and_persistence.md) for the full layout.

```bash
tar -czf ~/archon-search-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C ~ .archon-search
```

Verify the tarball is non-empty before continuing:

```bash
tar -tzf ~/archon-search-backup-*.tar.gz | head
```

## Step 3 — Stop the server

```bash
archon-search stop
```

If you installed the server as an OS service via `archon-search install`, `stop` is still the correct command — it routes through the platform service manager. See `archon_search/platform/` for the per-OS implementations. #Unverified (the exact stop-via-service-manager routing path in `cli/_helpers.py` was not traced end-to-end)

Confirm there is no process bound to the configured port (default `127.0.0.1:8765`):

```bash
archon-search status
```

`status` is purely a client-side probe; it does not start the server. A "not running" report is the expected state before installing the new wheel.

## Step 4 — Install the new wheel

Pick **one** install path; do not mix `pip` and `uv pip` against the same environment.

```bash
# pip
pip install -U archon-search

# uv (preferred when the install is managed by uv)
uv pip install -U archon-search

# Pin to a specific version (rollback or staged upgrade).
# The version below is illustrative — replace with a real tag from `pip index versions archon-search`.
# Releases follow CalVer `YY.M.<rev-count>` (see release.sh).
pip install archon-search==26.5.123
```

After install:

1. Diff your `<data-dir>/archon-search.toml` (default `~/.archon-search/archon-search.toml`) against the current template `archon-search.toml.example` at the repo root for the version you just installed. `load_config` in `archon_search/config.py` only reads an explicit allowlist of known keys per section, so unknown or stale keys are skipped (not raised) — but a renamed key silently reverts to its default. Three removed keys are the exception and are refused: `[database].providers`, `[database].reranker_providers` and `[graph].providers` make `serve`, `start` and `install` exit `1` (`archon-search wizard` drops them); see [`04_config_migration.md`](./04_config_migration.md). The one **explicit** silent coercion in the loader is `telemetry.export_enabled = true`, which is logged as a warning and forced back to `false` (reserved for a future release; see `config.py` around the `export_enabled` branch).
2. If `BREAKING.md` for your range names any config key changes, apply them now. For the 2026-10-25 security and operations entries that means, at least: every API key at least 32 characters (`ARCHON_SEARCH_API_KEY` lowercase hex; `[namespaces]` tokens printable ASCII), `[ingest].allowed_roots` if non-admin keys send paths, and `[server].host` / `cors_origins` / `docs_enabled` if you relied on the old network defaults. A key shorter than 32 characters, or an invalid key file, stops the server at start with `Error: …` and exit `1`.
3. Admin-only routes (`/sync`, `/backup/trigger`, `/maintenance/trigger`, `/telemetry/*`, `/keys/rotate`, `POST /collections/`) now need the default key or an `admin: true` managed key; point cron jobs and CI at the right key.
4. Coming from the fastembed / ONNX build: stop the service **before** installing (launchd or systemd would crash-loop on the old keys), then follow the PyTorch entry's Migration in `BREAKING.md` (remove the three keys, set `device`, `archon-search start`, check `/ready`, then free the orphaned ONNX files with `hf cache rm`, never `rm -rf` on `models--*`). Existing indexes need no re-index.

## Step 5 — Restart and verify

```bash
archon-search start
```

Wait for the bind to complete, then run the two-part verification:

```bash
# 5a. Readiness probe — unauthenticated. `/health` answers only {"status": "running"}; the version is in GET /status.
curl -sf http://127.0.0.1:8765/ready | jq .

# 5b. Smoke search — uses the auth key from <data-dir>/.search.env.
KEY=$(grep -E '^ARCHON_SEARCH_API_KEY=' ~/.archon-search/.search.env | cut -d= -f2-)
curl -sf -H "Authorization: Bearer $KEY" http://127.0.0.1:8765/status | jq .version
curl -sf -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection": "<one of your collections>", "query": "smoke test"}' | jq .
```

Expected outcomes:

- `GET /ready` returns 200 with `"ready": true` (a replica whose default model cannot load answers `503` `checks.models: "fail"` and retries in the background; first starts download model weights and can take minutes). `GET /status` reports the version that matches the wheel you just installed.
- `POST /search` returns 200 with a `{"results": [...], "acl_filtered": <bool>}` payload. Even an empty `results` array is a passing smoke test — it confirms auth, routing, and the response schema are intact.

If either probe fails, jump to **Rollback** below; do not attempt to debug in place against a partially-upgraded install.

## Rollback procedure

The same five-step loop, with two changes: the install pins to the previous version, and the state restore is conditional.

```bash
# 1. Stop the server.
archon-search stop

# 2. Back up the *current* (failed-upgrade) state before overwriting it — principle 2 still applies.
#    This snapshot is what you would attach to a bug report.
tar -czf ~/archon-search-failed-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C ~ .archon-search

# 3. Restore state ONLY if the failed upgrade is known to have mutated on-disk data.
#    Today no upgrade requires schema migration (see 05_data_migration.md, roadmap item D3 #Unverified),
#    so this step is normally skipped — the existing state directory is reused.
tar -xzf ~/archon-search-backup-<timestamp>.tar.gz -C ~

# 4. Pin to the previous version.
pip install archon-search==<previous-version>

# 5. Start and re-verify with the same /health + smoke-search probes.
archon-search start
```

If the previous version had a different config schema and you applied schema-level changes in Step 4 of the forward upgrade, undo those edits in `~/.archon-search/archon-search.toml` before starting.

## What this procedure does **not** cover

- **Schema migrations of the LanceDB store.** There are no automated migrations today; this gap is tracked as roadmap item D3 in [`Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md). If a future release requires a reindex, its `BREAKING.md` entry will say so explicitly.
- **Service-manager-level changes.** If a release changes how `archon-search install` registers itself with launchd/systemd/Windows Services, the `BREAKING.md` entry will call that out and you may need to run `archon-search uninstall && archon-search install` as part of Step 4.
- **External integrations.** Updating the REST/MCP clients in your own codebase is out of scope for this doc; see [`06_client_migration_examples.md`](./06_client_migration_examples.md) for diffs.

## Related documents

- [`01_versioning_and_release_model.md`](./01_versioning_and_release_model.md) — how to identify the version you are on.
- [`03_breaking_changes_index.md`](./03_breaking_changes_index.md) — what changed in your range.
- [`04_config_migration.md`](./04_config_migration.md) — config keys and the silent-coerce quirk.
- [`05_data_migration.md`](./05_data_migration.md) — on-disk layout and reindex commands.
- [`Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) — `/health`, `/status`, runbooks.
