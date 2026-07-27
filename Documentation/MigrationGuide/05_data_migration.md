**Purpose**: Describe the on-disk layout under `~/.archon-search/`, what survives an upgrade unchanged, and how to force a reindex when a future release requires one.
**Audience**: Operators planning an upgrade; maintainers introducing changes that affect persistence.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Data Migration

All durable state for `archon-search` lives under `~/.archon-search/`. The server is single-process and owns this directory exclusively. There is no remote database, no shared cache, and no "staging" vs "prod" split.

## Principles

1. **One state directory per install.** Backup is `tar -czf` of `~/.archon-search/`; restore is the inverse.
2. **No automated schema migrations today.** Every release reuses the existing LanceDB layout. Schema-migration tooling is roadmap item **D3** in [`Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) and is not yet implemented.
3. **Re-ingest is a fallback, not a routine step.** No release to date requires a full reindex. If one does, it will say so in its [`BREAKING.md`](../../BREAKING.md) entry.

## On-disk layout

The canonical layout, as described in [`Architecture/130_data_architecture_and_persistence.md`](../Architecture/130_data_architecture_and_persistence.md) and [`Architecture/510_release_and_environment_strategy.md`](../Architecture/510_release_and_environment_strategy.md):

```
~/.archon-search/
├── archon-search.toml          # User config (see 04_config_migration.md)
├── .search.env                 # Bearer auth key, mode 600 (key_manager.py)
├── search/                     # LanceDB root (db_path)
│   ├── .indexing_state.json    # Per-collection mtimes / indexed-model fingerprint (progress.py)
│   └── <per-collection tables, vector + FTS indexes>
├── logs/
│   └── archon-search.log       # Server log (logging.log_file)
└── search-logs/                # Telemetry JSONL — only when [telemetry].enabled = true
    └── YYYY-MM-DD.jsonl
```

The auth key file `.search.env` is the canonical location written by `archon_search/key_manager.py` (the only override is the `ARCHON_SEARCH_KEY_FILE` env var). The layout under `search/` is an implementation detail of the LanceDB driver — treat that subtree as **opaque** and never edit files inside `search/` by hand.

## What survives an upgrade unchanged

The default expectation, for **every release shipped so far**:

| Item | Survives upgrade? | Notes |
| --- | --- | --- |
| `archon-search.toml` | Yes | Keys that no longer exist in the new release are silently ignored (see [`04_config_migration.md`](./04_config_migration.md)). |
| `.search.env` (API key) | Yes | The key file is not rewritten on upgrade. `ARCHON_SEARCH_API_KEY` env var still overrides it. |
| `search/` (LanceDB tables) | Yes | Vector + FTS indexes are read in place. No schema migration is performed. The `.indexing_state.json` file inside `search/` (per-collection mtimes + indexed-model fingerprint, see `archon_search/progress.py`) also survives and is what lets the sync layer skip unchanged inputs. |
| `logs/archon-search.log` | Yes | Appended to; not rotated by the server itself. #Unverified — no `RotatingFileHandler` was found in the source, but rotation absence was not directly verified against the logger setup. |
| `search-logs/*.jsonl` | Yes | Subject to `[telemetry].retention_days` pruning by `pruner.py`. |

If a future release **requires** any of these to change, the `BREAKING.md` entry for that release will spell it out and link a migration procedure. Until then, an upgrade is a wheel swap with no data touch.

## What requires a reindex

**Today: nothing automatic.** No release in [`/BREAKING.md`](../../BREAKING.md) has required a reindex.

There are two situations where reindexing is relevant:

1. **You changed `[database].chunk_size`** in `archon-search.toml`. By default `auto_reindex_on_chunk_size_change = true`, so affected collections are reindexed automatically the next time `sync.py`'s `_sync_collection` runs for them — that includes server start, watcher-triggered re-syncs, and explicit `archon-search sync` invocations, not just process start. Set the flag to `false` if you want to defer and trigger reindex manually later.
2. **You changed `[database].embedding_model`.** This is a model-identity change; existing vectors are no longer comparable to new queries. `sync.py` detects this automatically by comparing the configured embedding model against the per-collection indexed-model fingerprint and forces a full reindex unconditionally on the next sync pass — no manual action is required, and there is no opt-out flag. Changing `[database].reranker_model` does **not** require a reindex: the reranker operates on retrieved candidates at query time and is not persisted per-collection.

The schema-migration gap is tracked as **D3** in [`Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md): a future release will introduce a migration job kind so model or schema changes do not require operators to wipe `search/` by hand. Until D3 ships, the manual reindex below is the only tool.

## Manual reindex procedure

The CLI exposes a per-collection reindex command (see `archon_search/cli/collection.py` `reindex`):

```bash
# List collections you can reindex.
archon-search collection list

# Force a full reindex of one collection.
archon-search collection reindex <collection-name>
```

`reindex` **clears** the `IndexingStateStore` entry for the collection and **drops** the LanceDB collection before re-walking its source path. Unlike a normal `sync`, an interrupted `reindex` does not resume incrementally — the next `reindex` run starts from scratch because state was deliberately wiped. (Normal `sync` operations, by contrast, do resume incrementally.)

There is no global "reindex all" subcommand; loop in the shell if you need it. Note that `archon-search collection list` does **not** print a header row — it prints either `No collections found.` or one line per collection of the form `{name}  docs={N}  chunks={M}`:

```bash
archon-search collection list | awk '{print $1}' | \
  grep -v '^No$' | \
  while read -r c; do archon-search collection reindex "$c"; done
```

All `collection` subcommands (`list`, `info`, `add`, `remove`, `reindex`, `reindex-metadata`) are HTTP proxies — they route through the running archon-search server. Ensure `archon-search serve` is running before invoking any collection command.

## Backup and restore

```bash
# Backup — stop the server first to ensure consistency.
archon-search stop
tar -czf ~/archon-search-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C ~ .archon-search

# Restore — replaces the live state directory in full.
archon-search stop
rm -rf ~/.archon-search
tar -xzf ~/archon-search-backup-<timestamp>.tar.gz -C ~
chmod 600 ~/.archon-search/.search.env   # ensure auth key permissions are tight after restore
archon-search start
```

After restore, verify that `~/.archon-search/.search.env` is mode `600`. `key_manager.py` enforces `600` when it writes the file, but `tar -x` will recreate it with whatever your umask produces; until the next key rotation, an over-permissive restore leaves the bearer secret readable.

A live backup (without stopping the server) is **not** safe: LanceDB writes are not guaranteed atomic across the whole tree #Unverified — this is an upstream LanceDB property and was not verified against the pinned library version, and an in-flight ingest can leave the tar inconsistent. If you need online backup, that gap is tracked alongside D3 as **D2 (Export / import / backup / restore)** in [`Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md).

## Telemetry data across upgrades

If `[telemetry].enabled = true`, `~/.archon-search/search-logs/*.jsonl` accumulates one JSON line per `/search` and `/route` call #Unverified — call-site coverage of `telemetry/writer.py` (e.g. whether `search_with_context` or MCP variants also emit entries) was not traced exhaustively. Across an upgrade:

- The file format is structurally stable (`archon_search/telemetry/entry.py`).
- The "no raw query" invariant is structural: `entry.py` factory methods do not accept a `query` argument. A future upgrade will **not** silently start writing raw query strings; that would be a `BREAKING.md`-class change to the privacy contract.
- `doc_id`s in telemetry are path-derived and may leak filesystem paths. This is documented as accepted risk in [`Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md); the risk does not change across upgrades.

There is no telemetry-format migration today. If one is ever needed, expect a `BREAKING.md` entry plus a one-shot tool — old JSONL will not be silently rewritten.

## Related documents

- [`02_upgrade_procedure.md`](./02_upgrade_procedure.md) — where backup and restore fit in the upgrade flow.
- [`04_config_migration.md`](./04_config_migration.md) — config keys that interact with the data layer (`db_path`, `chunk_size`, models).
- [`Architecture/130_data_architecture_and_persistence.md`](../Architecture/130_data_architecture_and_persistence.md) — LanceDB schema and persistence rules.
- [`Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) — D2 (export / import / backup) and D3 (schema migration tooling).
- [`Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — telemetry privacy invariants.
