# Bug Brief: `GET /jobs?kind=` silently returns zero rows for `metadata_reindex` / `community_rebuild`

**ID:** 2026-08-20-020 · **Severity:** P3 (silent empty result, no data loss) · **Status:** Open
**Found during:** fix-brief-D documentation sweep, while verifying the `sync` kind added to
`_KIND_TYPE_MAP` this round (fix-brief-C/D)

## Problem

`GET /jobs?kind=<value>` filters the job list to jobs whose runtime type matches one of the
requested kind strings. When every requested kind string is **unmapped**, the endpoint returns
`200` with an **empty list** instead of an error — indistinguishable from "there really are no
jobs of that kind" to a caller who mistyped the kind or is asking about one of the two kinds the
map still omits.

`_KIND_TYPE_MAP` (`archon_search/server/routes_jobs.py:570-578`) currently covers:

```python
_KIND_TYPE_MAP: dict[str, type] = {
    "ingest": IngestJob,
    "reindex": ReindexJob,
    "delete": DeleteJob,
    "export": ExportJob,
    "import": ImportJob,
    "migration": MigrationJob,
    "sync": SyncJob,
}
```

`sync` was added in this round because `SyncJob` became user-visible (the startup-sync job and
manual `POST /sync` are both `SyncJob`s). Two job types still have no entry:

- `metadata_reindex` → `MetadataReindexJob` (`archon_search/types.py:115-118`, submitted by `POST
  /collections/{name}/reindex-metadata`)
- `community_rebuild` → `CommunityRebuildJob` (`archon_search/types.py:99-101`, submitted by `POST
  /graph/{collection}/rebuild-communities`)

## Root cause

`list_jobs` (`routes_jobs.py:610-614`):

```python
if kind:
    kind_lower = {k.lower() for k in kind}
    kind_types = {_KIND_TYPE_MAP[k] for k in kind_lower if k in _KIND_TYPE_MAP}
    jobs = [j for j in jobs if type(j) in kind_types]
```

The `if k in _KIND_TYPE_MAP` guard silently drops any kind string not in the map rather than
rejecting the request. When **every** requested kind is unmapped, `kind_types` is the empty set,
and `type(j) in kind_types` is `False` for every job — the filter step degrades to "exclude
everything" with no signal that the kind itself was the problem, not the data.

## Failing repro

```bash
curl -s -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8765/jobs?kind=community_rebuild"
# → 200 {"items": [], "next_cursor": null, "total": 0} even when a CommunityRebuildJob exists
curl -s -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8765/jobs?kind=metadata_reindex"
# → same: 200, empty, even when a MetadataReindexJob exists
```

Contrast with a genuinely unrecognized kind (e.g. `?kind=bogus`), which produces the exact same
`200` empty response — the endpoint cannot distinguish "no jobs of this kind" from "this kind
string means nothing to the server."

## Fix

Add the two missing entries to `_KIND_TYPE_MAP`:

```python
"metadata_reindex": MetadataReindexJob,
"community_rebuild": CommunityRebuildJob,
```

Neither class is currently imported into `routes_jobs.py` — the file imports `DeleteJob,
ExportJob, ImportJob, MigrationJob, ReindexJob, SyncJob` from `archon_search.types` (:25) but not
`MetadataReindexJob` or `CommunityRebuildJob`; add both to that import line. Separately,
consider whether an entirely-unmapped kind set should be a `422` instead of silently filtering to
empty — that is a wire-contract change (`GET /openapi.json` is authoritative, `BREAKING.md` entry
required) and is out of scope for the minimal fix above; call it out to the route owner rather than
deciding it here.

## Open question: should a crashed reindex arm the crash-loop guard?

`_INGEST_FAMILY_JOB_TYPES` (`archon_search/jobs/store.py:51`) currently is `{_INGEST_JOB_TYPE,
JobKind.sync.value}` — i.e. `crashed_ingest_on_load` (and therefore the sticky startup-sync
suppression) fires only for a crashed `ingest` or `sync` job, not a crashed `reindex`.

`archon_search/sync.py`'s `_check_collection_changes` (:441-463) forces a full reindex under two
conditions, both flag-gated by `self._auto_reindex_on_chunk_size_change`:

- `indexed_chunk_size is None` (prior size unknown, e.g. a collection never indexed via sync) →
  `force_full_reindex = True` **only if** `self._auto_reindex_on_chunk_size_change` is set;
  otherwise the mismatch is silently accepted (no warning — "we have no 'before' value to report").
- `self._chunk_size != indexed_chunk_size` (a real chunk-size change) → same flag gate; when the
  flag is off, it logs a WARNING instead of reindexing.

So a full reindex triggered by `sync()` is conditional on config, not automatic — but when it
*does* fire, `sync()` still routes through the same `SyncJob` (`kind = JobKind.sync`), which
**is** in `_INGEST_FAMILY_JOB_TYPES` already. `jobs/store.py`'s own comment directly above the set
(:36-50) already documents exactly this: `_INGEST_FAMILY_JOB_TYPES` is deliberately narrower than
"every job type that can ever cause a re-ingest," and explicitly calls out that a crashed
sync-triggered reindex is covered ("it crashes as a SyncJob (kind='sync'), which IS in this set").

The open question that comment does **not** answer: a **standalone** `POST
/collections/{name}/reindex` (`ReindexJob`, submitted directly, not routed through `SyncJob`) is
not in `_INGEST_FAMILY_JOB_TYPES` at all. If a `ReindexJob` crashes mid-run (process killed,
`RUNNING` → `FAILED / "process_restart"` on next load), should that also suppress the automatic
startup sync the way a crashed `ingest`/`sync` does? Arguments both ways:

- **For:** a standalone reindex re-embeds and rewrites the same LanceDB tables an ingest would —
  the failure mode (OOM, `kill -9` mid-write) is the same class of risk the crash-loop guard exists
  to catch.
- **Against:** a reindex operates on data already fully ingested; re-entering it unattended does not
  re-run the same "walk a fresh corpus" workload that caused the original incident, and widening
  `_INGEST_FAMILY_JOB_TYPES` makes the guard fire (and stick, now that suppression is sticky) for a
  strictly larger set of crash scenarios — more operator friction per incident.

Answering this decides whether `_INGEST_FAMILY_JOB_TYPES` should grow to include `"reindex"`. Not
resolved by this brief — flagging for the route/guard owner.

## Verification

- Test: `GET /jobs?kind=metadata_reindex` with a seeded `MetadataReindexJob` returns it.
- Test: `GET /jobs?kind=community_rebuild` with a seeded `CommunityRebuildJob` returns it.
- Existing kind-filter tests for `ingest`/`reindex`/`delete`/`export`/`import`/`migration`/`sync`
  continue to pass unchanged.

## References

- [[archon_search/server/routes_jobs.py]] — `_KIND_TYPE_MAP` (:570-578), `list_jobs` kind filter
  (:610-614)
- [[archon_search/types.py]] — `MetadataReindexJob` (:115-118), `CommunityRebuildJob` (:99-101),
  `JobKind` (:104-106)
- [[archon_search/jobs/store.py]] — `_INGEST_FAMILY_JOB_TYPES` (:51), the scope-tradeoff comment
  directly above it (:36-50)
- [[archon_search/sync.py]] — `_check_collection_changes` chunk-size / reindex gating (:441-463)
