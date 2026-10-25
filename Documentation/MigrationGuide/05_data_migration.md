**Purpose**: Describe the on-disk layout under `~/.archon-search/`, what survives an upgrade unchanged, and how to force a reindex when a future release requires one.
**Audience**: Operators planning an upgrade; maintainers introducing changes that affect persistence.
**Status**: Draft
**Last reviewed**: 2026-10-25
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
~/.archon-search/                 # mode 0700; files the server writes are 0600
├── archon-search.toml          # User config (see 04_config_migration.md)
├── .search.env                 # Bearer auth key, mode 600 (key_manager.py)
├── keys.json                   # Managed keys (hashes only), carries each key's `admin` flag
├── search/                     # LanceDB root (db_path)
│   ├── .indexing_state.json    # Per-collection mtimes / indexed-model fingerprint (progress.py)
│   └── <per-collection tables, vector + FTS indexes>
├── models/                     # PyTorch weights (Hugging Face hub layout), language-ID model, graph/ (GLiNER)
├── exports/<namespace>/        # Export archives, one directory per namespace
├── backups/<namespace>/        # Scheduled backups ([backup].output_dir/<namespace>)
├── logs/
│   └── archon-search.log       # Server log (logging.log_file)
└── search-logs/                # Telemetry JSONL — only when [telemetry].enabled = true
    └── YYYY-MM-DD.jsonl
```

The auth key file `.search.env` is the canonical location written by `archon_search/key_manager.py` (the only override is the `ARCHON_SEARCH_KEY_FILE` env var). The layout under `search/` is an implementation detail of the LanceDB driver — treat that subtree as **opaque** and never edit files inside `search/` by hand. `ARCHON_SEARCH_DATA_DIR` moves the whole tree (default config path, models, exports and backups follow it).

## What survives an upgrade unchanged

The default expectation, for **every release shipped so far**:

| Item | Survives upgrade? | Notes |
| --- | --- | --- |
| `archon-search.toml` | Yes | Keys that no longer exist are silently ignored, except the removed `providers` / `reranker_providers` keys, which stop `serve` and `start` until they are deleted (see [`04_config_migration.md`](./04_config_migration.md)). New keys fall back to their defaults — which now include a stricter `[ingest].max_file_mb = 100` and loopback-only binding. |
| `.search.env` (API key) | Yes, if valid | The key file is not rewritten on upgrade. A key that is not lowercase hex of at least 32 characters is refused at start (the file is left untouched; fix or delete it). `ARCHON_SEARCH_API_KEY` still overrides it and must meet the same rule. |
| `search/` (LanceDB tables) | Yes | Vector + FTS indexes are read in place. No schema migration is performed, and moving from the fastembed / ONNX build to the PyTorch build needs no re-index (stored vectors are unchanged within ADR 18's thresholds). Collections that have rows but no collection-meta row are treated as owned by the `default` namespace and listed in one startup WARNING. Superseded Lance table versions are deleted by maintenance passes after `[maintenance].version_retention_hours` (24). The `.indexing_state.json` file inside `search/` (per-collection mtimes + indexed-model fingerprint, see `archon_search/progress.py`) also survives and is what lets the sync layer skip unchanged inputs. |
| `logs/archon-search.log` | Yes | Rotated by a timed rotating handler (`[logging].backup_count`, default 7); the file and rotated copies are written `0600`. |
| `search-logs/*.jsonl` | Yes | Subject to `[telemetry].retention_days` pruning by `pruner.py`. New files are written `0600` in a `0700` directory. |
| `models/` | Partly | The PyTorch build downloads its own snapshots next to the old fastembed directories; the ONNX files are unused and can be removed with `hf cache rm` (see the PyTorch entry in [`/BREAKING.md`](../../BREAKING.md); `rm -rf` on `models--*` frees little because the blobs are shared). |
| `exports/`, backups | Re-file | Archives are importable only from the caller's own `exports/<namespace>/` or `backups/<namespace>/` directory; see "Export archives and per-namespace restore" below. |

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
chmod -R go-rwx ~/.archon-search         # tar -x applies your umask; the server expects 0700 dirs and 0600 files
archon-search start
```

After restore, verify that `~/.archon-search/.search.env` is mode `600` and that the directory is not group- or world-readable: `tar -x` recreates files with whatever your umask produces, and indexed text lives in `search/`. `key_manager.py` enforces `600` only when it writes the file.

A live backup (without stopping the server) is **not** safe: LanceDB writes are not guaranteed atomic across the whole tree #Unverified — this is an upstream LanceDB property and was not verified against the pinned library version, and an in-flight ingest can leave the tar inconsistent. If you need online backup, that gap is tracked alongside D3 as **D2 (Export / import / backup / restore)** in [`Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md).

## Export archives and per-namespace restore

Each namespace has its own export directory, `<data-dir>/exports/<namespace>/`, and scheduled backups go to `[backup].output_dir/<namespace>/` (default `<data-dir>/backups/<namespace>/`). `POST /collections/{name}/export` (and MCP `export_collection`, `archon-search export`) takes no path any more: the archive lands in the caller's directory, and the job's `output_path` names the file. An import accepts only a file in the caller's own two directories, for every key, admin included; any other path is `400` `path_unsafe` / `outside_allowed_dirs`, whether or not a file exists there. The manifest records the exporting `namespace`: another namespace's archive is `422` `archive_namespace_mismatch`.

Archives and backups made before this change have no `namespace` in the manifest. To restore one as namespace `<ns>`: copy it into `<data-dir>/exports/<ns>/` (or `[backup].output_dir/<ns>/`, where scheduled backups already are) and import it from there:

```bash
mkdir -p ~/.archon-search/exports/<ns>
cp /path/to/old-archive.tar.gz ~/.archon-search/exports/<ns>/
# run with an API key of namespace <ns>; the collection name is the one to create
archon-search import <collection> ~/.archon-search/exports/<ns>/old-archive.tar.gz --wait
```

The `default` namespace may also import an old file that sits directly in `<data-dir>/exports/` (not in a subdirectory). Importing needs the collection name to be free or owned by the caller's namespace; another namespace's collection is refused with `collection name is not available to this namespace`, even with `force_overwrite`.

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
