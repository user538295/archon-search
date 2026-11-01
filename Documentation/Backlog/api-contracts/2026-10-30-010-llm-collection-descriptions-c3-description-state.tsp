// C3 — Collection description state seam (internal logical seam).
// Revised after implementation (2026-11-01): the port is realised on `SearchStore` as `write_description(collection, namespace, fields, guard)`, `list_chunk_ids`, `read_excerpts` and `description_version`; reads raise `StoreReadError`. `DescriptionFields` has SIX fields: the routing vector (`description_embedding`) is the sixth. The guard (`ExpectedState`) compares description, source, last-described AND the stored status, as written below. `description_version(collection)` is new: an in-memory counter that grows whenever the description text, source or status changes (or the meta row is deleted); the lifecycle remembers a failure the busy store refused against it and reads the collection as `failed` until the version changes. The overlay lives in `collection_meta.visible_description_status`, and it is applied on GET, list and PATCH only: a manual PUT or DELETE answers the stored state, never `pending`.
//
//
// Layer edge: Use Cases (pipeline, description generation, lifecycle, manual set/clear, import) -> the
// consumer-owned `DescriptionStore` port below, implemented by the persistence adapter (`SearchStore`,
// Frameworks & Drivers), over the Entities (`CollectionMeta`). Use cases never call a concrete
// `SearchStore` method for description work: the port carries everything they need (stored state,
// every collection's state for the backfill, the guarded write, the chunk ids and excerpts the sample
// is drawn from).
//
// Stored: `description_source` (`generated` | `manual`, or empty) and `description_status`
// (`ok` | `failed`, or empty). Two new nullable columns on the collection metadata table, added by
// the startup migration at every server start, with no schema version bump and no migration spec.
// `pending` is NEVER stored and the PORT never reports it: it is held in memory by the lifecycle
// (C5), and the API edge (REST, MCP, CLI) overlays it with the lifecycle's `overlayStatus` while a
// generation is in flight. The wire field `description_status` is therefore a DERIVED read value
// (`DescriptionStatus`: the stored `DescriptionStoredStatus` plus the overlay); only `ok` and `failed`
// are ever persisted. A restart loses `pending`; the document-count baseline is saved only on
// success, so the 20% rule fires again at the next qualifying ingest. (An earlier draft had the
// store's `read` apply the overlay; the store does not own `pending`, so the overlay moved out.)
// Legacy and defaults: a collection that already has a description reads as `generated` / `ok`; an
// empty description with no failed attempt reads as no source and no status. A failed attempt on a
// collection with no description stores status `failed` and leaves the source empty. A
// never-attempted collection and a disabled feature both read empty; the disabled state is shown
// once in `/status` (C7).
// Manual wins: generation never replaces a description whose source is `manual`, and the check is
// made INSIDE the collection lock at write time (see `write`), not from an earlier read.
// Set manual: source `manual`, status `ok`, last-described now, baseline untouched.
// Clear: description, source, status empty; last-described and the baseline back to never. No
// immediate generation. Next triggers: a qualifying ingest (an unset baseline always regenerates),
// startup backfill if enabled, or `collection describe`.
// The write reports its outcome: a lock timeout is a failed description, never swallowed.
// Lost updates: any writer that changes the description fields (generate, set, clear, import,
// record failure) goes through `write`, which re-reads the row inside the lock and changes only the
// fields it owns; every field in `DescriptionFields` says explicitly whether it is kept, set or
// cleared, so "unchanged" is never confused with "empty". A generated write and a recorded failure
// carry the state the generation started from (`ExpectedState`) and are refused when the row differs
// at write time: a manual set (`refusedManual`) or a clear or any other change (`refusedStale`) that
// landed during the model call or the embedding is never overwritten, and a failure landing after a
// clear does not leave a `failed` status that the backfill would then skip forever.
// Every OTHER whole-row writer (the metadata recompute, centroid maintenance, reindex, collection
// creation) must carry the description fields AND the stored description vector as read inside the
// lock at write time, never from a read made before a slow step (the recompute scans every vector
// between its read and its write); a routing vector computed from description text that changed in
// between is dropped and left to the shared write operation (C11).
// Locking: `write` takes the collection lock itself and the lock is not reentrant, so a route that
// already holds it self-deadlocks until the lock timeout. The set and clear routes (and import) MUST
// call the shared write WITHOUT holding the lock; PATCH and DELETE are not a pattern to copy here:
// they hold it through `locked_collection_meta` (routes_collections.py line 903) and
// `acquire_collection_lock_or_503` (line 614) and pass `_locked_by_caller=True`.
// Every place that rebuilds the metadata field by field must carry both new fields or they silently
// reset. The rebuild sites are the 15 `CollectionMeta(` constructor sites (`grep -rn --include=*.py
// "CollectionMeta(" archon_search`): in `store.py` ten (the row reader, the description write, five in
// `_do_update_meta_on_add`, two in `_do_subtract_meta_on_delete`, one in `_do_claim_collection`),
// `routes_collections.py` and `routes_jobs.py` (collection creation), the two recompute constructors
// in `pipeline.py`, and the router's routing-field filter, plus both `merge_insert` writer
// dictionaries and the metadata table schema.
// Prefer copy-with (`dataclasses.replace`) over field-by-field construction, and add a test that
// builds a metadata row with every field non-default and fails if any writer drops one.
// Rolling back: an older server's writers omit the new keys. That LanceDB then leaves the stored
// values untouched is an ASSUMPTION, not something checked in the repository (lancedb 0.41.0's
// `_do_merge` passes `allow_subschema=True`, table.py line 7038; what the merge does with a column the
// source omits is decided in the Rust core, which is not read here), and S78 verifies it. If it holds,
// the store still opens, and the stored source and status can be stale relative to text the older
// server wrote (see the plan's Known limitations).
//
// Logical only.

namespace DescriptionStateContract;

enum DescriptionSource {
  generated,
  manual,
}

/// Stored values.
enum DescriptionStoredStatus {
  ok,
  failed,
}

/// What readers see: stored values plus the in-memory overlay (derived at the API edge, never stored).
enum DescriptionStatus {
  ok,
  failed,
  pending,
}

/// What the port reads: stored values only. `source` and `status` are absent for a collection with
/// no description and no failed attempt.
model DescriptionState {
  description: string | null;
  source?: DescriptionSource;
  status?: DescriptionStoredStatus;
  lastDescribed?: utcDateTime;
  /// Document count at the last successful generation; unset after a clear.
  describedAtDocCount?: int32;
  /// Current document count of the collection (input to the 20% rule and to a generated write).
  docCount: int32;
}

/// One collection's state, for the backfill's walk across every namespace.
model DescriptionStateEntry {
  collection: string;
  collectionNamespace: string;
  state: DescriptionState;
}

enum DescriptionWriteOutcome {
  written,
  /// The collection lock did not become free in time. Reported to the caller, never swallowed.
  lockTimeout,
  collectionMissing,
  /// The row's source is `manual` inside the lock: nothing was written.
  refusedManual,
  /// The row changed since the generation began (cleared, replaced, set) inside the lock: nothing was
  /// written and the generation's text or failure is discarded.
  refusedStale,
}

enum FieldOp {
  /// Leave the stored value as just read inside the lock.
  keep,
  /// Write `value`.
  set,
  /// Reset to empty (never, for a timestamp or the baseline).
  clear,
}

/// `value` is present exactly when `action` is `set`.
model FieldChange<T> {
  action: FieldOp;
  value?: T;
}

/// The fields one description write owns. All five are required so that a writer states each one:
/// recording a failure is description `keep`, source `keep`, status `set` failed, the rest `keep`;
/// clear is every field `clear`; a generated write sets description, source, status, last-described
/// and the baseline.
model DescriptionFields {
  description: FieldChange<string>;
  source: FieldChange<DescriptionSource>;
  status: FieldChange<DescriptionStoredStatus>;
  lastDescribed: FieldChange<utcDateTime>;
  describedAtDocCount: FieldChange<int32>;
}

/// The description state a generation started from, read after it acquired its slot.
model ExpectedState {
  description: string | null;
  source?: DescriptionSource;
  lastDescribed?: utcDateTime;
  /// The STORED status (never the `pending` overlay); a change of status alone makes the write stale.
  status?: DescriptionStoredStatus;
}

/// Compared inside the lock against the freshly read row.
union WriteGuard {
  /// Set, clear and import: the caller is the authority.
  unconditional: "unconditional",
  /// Generated write and recorded failure: refuse with `refusedManual` when the row's source is
  /// `manual`, and with `refusedStale` when its description, source, last-described or stored status
  /// differ from `expected`.
  refuseIfChanged: ExpectedState,
}

/// Raised by the read calls on any store error; never an empty result (see C2 `sampleReadFailed`).
@error
model StoreReadFailure {
  detail: string;
}

interface DescriptionStore {
  /// Stored state without the `pending` overlay; null when the collection does not exist.
  read(collection: string, collectionNamespace: string): DescriptionState | null | StoreReadFailure;

  /// Every collection of every namespace (the backfill's walk).
  readAll(): DescriptionStateEntry[] | StoreReadFailure;

  /// One atomic step under the collection lock: re-read the row, apply the guard, change only the
  /// description fields as `fields` states them, write the row once. Every other field stays as
  /// just read. The caller must NOT hold the collection lock.
  write(collection: string, collectionNamespace: string, fields: DescriptionFields, guard: WriteGuard): DescriptionWriteOutcome;

  /// Chunk ids of the open rows (restricted rows too when `includeRestricted`), sorted by `chunk_id`
  /// ascending (C2). Empty means the read worked and found none.
  listChunkIds(collection: string, collectionNamespace: string, includeRestricted: boolean): string[] | StoreReadFailure;

  /// Excerpts of the given chunk ids, in the order given.
  readExcerpts(collection: string, collectionNamespace: string, chunkIds: string[]): string[] | StoreReadFailure;
}
