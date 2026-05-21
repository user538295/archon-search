# Review: Architecture/130_data_architecture_and_persistence.md

## Summary

The document is broadly accurate. The on-disk layout, LanceDB chunk-table and
collection-metadata schemas, FTS/RRF behavior, ACL precedence, telemetry
limits, and job-state location all match the source. A handful of small
inaccuracies cluster around (a) the reindex section in `sync.py` (the
"per-document checkpoint" framing and the cited line ranges are wrong; the
checkpoint is per-collection and a chunk-size change forces a full collection
reindex, not a per-document re-chunk), and (b) one wording slip in the
ingest-flow diagram (parser does not extract front matter; pipeline does).

## Inaccuracies (numbered: quoted claim, ground truth, file:line, severity)

1. **Claim**: "`sync.py` records `indexed_chunk_size` per document checkpoint."
   **Ground truth**: `indexed_chunk_size` is stored on the per-collection
   checkpoint (`state.collections.get(name)`), not per document. Each entry
   in `state.collections` is keyed by collection name and carries one
   `indexed_chunk_size` for the whole collection.
   **Source**: `archon_search/sync.py:228`, `:275`, `:364`, `:401`.
   **Severity**: medium (misleads anyone trying to find/modify the field).

2. **Claim**: "If `[database].auto_reindex_on_chunk_size_change = true` (default)
   → the document is re-chunked, re-embedded, and replaces the existing chunks
   (`sync.py:401–406`)."
   **Ground truth**: On a chunk-size change the code sets
   `force_full_reindex = True`, which causes `_diff_eligible` to return every
   eligible file as "to add" (see `sync.py:425–426`). The entire collection is
   reindexed, not just a single document. The cited line range is also off
   slightly — the true branch spans lines 402–409.
   **Source**: `archon_search/sync.py:402–409`, `:425–426`.
   **Severity**: medium (operational expectation differs from reality).

3. **Claim**: "If `false` → the document is left as-is with a logged mismatch
   warning (`sync.py:414–415`)."
   **Ground truth**: Behaviorally correct that no reindex is triggered, but
   "the document" should be "all documents" / "the collection". The warning
   message in code is collection-scoped (`"Chunk size mismatch for '%s' …"`).
   The cited line range is also off — the false branch with the warning spans
   `sync.py:411–416`.
   **Source**: `archon_search/sync.py:411–416`.
   **Severity**: low (line range + scope phrasing).

4. **Claim** (ingest flowchart): "`parser.py: read + front-matter`".
   **Ground truth**: `parser.py` does not parse front matter — it only reads
   plain text / HTML / PDF / office / image content. Front-matter extraction
   lives in `pipeline.py::_extract_front_matter` and is invoked from
   `SearchPipeline.ingest_file` after parsing.
   **Source**: `archon_search/parser.py` (no front-matter logic),
   `archon_search/pipeline.py:57` (`_extract_front_matter`), `:152–158`
   (invocation).
   **Severity**: low (diagram boundary off by one stage).

5. **Claim**: "`Pruner` (`telemetry/pruner.py`) runs once per 24 hours and
   deletes `*.jsonl` files older than `[telemetry].retention_days` (default
   30)."
   **Ground truth**: The comparison is `file_date < cutoff` where `cutoff = now
   - timedelta(days=retention_days)`, i.e. a file exactly `retention_days` old
   is deleted (strict less-than against cutoff date). Calling this "older
   than retention_days" is approximately right but slightly off-by-one.
   **Source**: `archon_search/telemetry/pruner.py:31, 47`.
   **Severity**: low (precision nit; user-visible behavior is essentially
   "older than the retention window").

## Verified claims

- Root directory `~/.archon-search/` and per-file ownership table:
  - `archon-search.toml` / `config.py::load_config` defaults — verified
    (`config.py` defines defaults; the file is optional).
  - `.search.env` auto-generated with mode `0o600` — verified
    (`key_manager.py:18`, `:89`, `:140`).
  - `search/` as LanceDB path; configurable via `[database].db_path`; default
    `~/.archon-search/search` — verified (`config.py:33`).
  - `search-logs/` per-UTC-day `YYYY-MM-DD.jsonl` only if `telemetry.enabled` —
    verified (`config.py:24`, `telemetry/writer.py:149`).
  - `logs/archon-search.log` controlled by `[logging].log_file` — verified
    (`config.py:51`).
  - `archon-search-jobs.json` and `JOBS_FILE` constant in `jobs/model.py` —
    verified (`jobs/model.py:8`).
- Env-var overrides `ARCHON_SEARCH_KEY_FILE`, `ARCHON_SEARCH_API_KEY`,
  `ARCHON_SEARCH_CONFIG` — verified (`key_manager.py:14`, `:20`;
  `config.py:83`).
- Collection-name regex `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` and `_archon_`
  reserved prefix; user collections filtered out of `list_collections` —
  verified (`store.py:25, 26, 217`).
- Chunk-table schema fields (`doc_id`, `chunk_id`, `text`, `vector`,
  `source_path`, `indexed_at`, `file_type`, `language`, `metadata`,
  `custom_score`, `ingested_by`, `updated_at`, `acl`) and types — verified
  (`store.py:120–140`).
- Metadata bounds 50 fields / 256 char key / 4096 char value — verified
  (`store.py:31–33, 40–55`).
- Collection-meta schema fields and types incl. `described_at_doc_count = -1`
  sentinel — verified (`store.py:142–159`, `:382–385`).
- `_COLLECTION_RE`, `_DOC_ID_RE`, `_CHUNK_ID_RE` enforcement — verified
  (`store.py:23–25`, `:412–413`, `:532–533`).
- FTS rebuild via `lancedb.index.FTS` on `text` column — verified
  (`store.py:445–451`).
- Hybrid search uses RRF with `_RRF_K = 60`, fetches `max(top_k*3, 20)` per
  leg, and gracefully falls back to vector-only when FTS index is absent —
  verified (`store.py:29, 471, 484–489`).
- `migrate_namespace` adds `namespace` column to `_archon_collection_meta` —
  verified (`store.py:302–319`).
- `migrate_acl` adds nullable `acl` list<utf8> column to chunk tables that
  lack it — verified (`store.py:321–352`).
- `update_collection_meta` refuses cross-namespace reassignment with
  `ValueError` — verified (`store.py:368–379`).
- `centroid_json` is `""` when centroid is `None`; JSON list of floats when
  set — verified (`store.py:382`, `:244`).
- ACL precedence: front-matter `_acl` overrides sidecar; sidecar is
  `<file>.acl` — verified (`acl.py:217–244`, `pipeline.py:155–161`).
- `_acl` keys extracted only for text extensions
  (`{.md, .txt, .rst, .html}`) — verified (`pipeline.py:54, 153–158`).
- `doc_id = sha256(str(path.resolve()).encode()).hexdigest()` and
  `chunk_id = f"{doc_id}-{idx:06d}"` — verified
  (`pipeline.py:140`, `:170`; `store.py:552`).
- Telemetry: `MAX_ENTRY_BYTES = 8192`, default `queue_size = 1024`, per-UTC-day
  filename, `truncated=true` set on `result_doc_ids` truncation — verified
  (`telemetry/writer.py:33, 46, 149, 177, 188–190`).
- `telemetry.export_enabled = true` forced to `False` with a warning at
  `config.py:213–215` — verified (cited line range is exact).
- `DOCUMENTED_SCHEMA_FIELDS` is the closed field set and contains no `query`
  field; `TelemetryEntry` uses `extra="forbid"` — verified
  (`telemetry/entry.py:39–54, 57–74`).
- Job state file at `~/.archon-search/archon-search-jobs.json`; statuses
  PENDING/RUNNING/DONE/FAILED/CANCELLED/CANCELLING; `IngestJob` defined in
  `types.py` — verified (`jobs/model.py:8`; `jobs/__init__.py:2`;
  `types.py::IngestJob`).
- `delete_by_source_path` recomputes `doc_id` from `Path(source_path).resolve()`
  — verified (`store.py:544–553`).
- "No reindex is triggered for embedding-model changes; that requires a manual
  `collection reindex`" — verified at the sync layer (a separate
  `force_full_reindex = True` is set when the configured embedding model
  differs, `sync.py:398`), and reindex is exposed as a job-returning route.

## Unverifiable / ambiguous

- "Reassigning a collection to a different namespace is refused
  (`update_collection_meta` raises `ValueError`)" — correct for the *write*
  path; the doc could be read as a global invariant. There is no namespace
  immutability check on the *read* path or in `migrate_namespace` (which
  blindly defaults existing rows to `default`). Not strictly inaccurate, but
  worth noting if a reader expects a hard system-wide invariant.
- "No external transmission occurs in v1" — verified by absence: no HTTP/SMTP
  client code touches telemetry outputs in the searched modules. This is a
  negative claim and cannot be fully proven from a single review pass, only
  by the structural fact that `export_enabled` is forced to `False`.
- The diagram's `H. update_collection_meta` step claims it updates `centroid`
  and `last_indexed` on every ingest. Verified for `ingest_directory`
  (`pipeline.py:277–289`) but `ingest_file` alone does not update
  collection meta — readers expecting per-file centroid updates from
  `ingest_file` would be surprised. Mild ambiguity in the diagram.
