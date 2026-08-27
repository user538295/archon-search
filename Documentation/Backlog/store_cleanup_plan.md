**Purpose**: Remediation plan and execution order for `archon_search/store.py` clean-code-review findings not covered by the parallel quick-fix pass.
**Audience**: Maintainers doing the follow-on cleanup work.
**Status**: Draft — unowned, unscheduled
**Owner**: unassigned
**Last reviewed**: 2026-08-27
**Next review**: 2026-11-27

# `store.py` clean-code remediation plan

> **How to read the sequencing below**: this is a *proposed* dependency order, not a committed schedule. No slice has an owner, an acceptance-criteria list, or a target release. Whoever picks up a slice writes its acceptance criteria first — the ordering here only tells you which slices must not be interleaved.

> **On citations**: the `store.py` line numbers throughout this plan are a *snapshot* taken during the `/clean-code-review` run cited below and will drift with the next edit to the file. Where a line number and a symbol name disagree, **the symbol name is authoritative** (per the repo convention established in commit `c3821f01`, "cite contract text by symbol, not by line number"). Re-locate by symbol before acting on any slice.

> Source: `/clean-code-review @archon_search/store.py` run on 2026-08-27 (97 findings, 3 Critical / 38 Major / 32 Moderate / 24 Minor). The 3 Critical, the 3 `safety-06` blocking-I/O findings, and a batch of purely cosmetic Minor/Moderate items (`arch-10`, `clarity-01`, `clarity-07`, `clarity-13`, `smells-18`, `clarity-08`, `clarity-09`) are handled separately via `/bugfix`. This plan covers everything else.

## Scope and risk

`archon_search/store.py` is the central persistence layer (`SearchStore`, ~3100 lines) sitting under `pipeline.py`, `router.py`, `watcher.py`, `sync.py`, every `server/routes_*.py`, and `jobs/`. Almost every finding here touches a method with external callers, so **signature-changing fixes carry real blast radius**; comment/naming fixes do not.

Test coverage check (`tests/test_store.py` + dedicated files) going in: `hybrid_search`/`_hybrid_search_with_trace` are heavily covered (~15+ tests), `reindex_metadata` has three dedicated files (`test_store_reindex_metadata.py`, `test_store_reindex_metadata_fts.py`, CLI/integration tests), `ping` has `test_store_ping.py`, `sample_chunk_texts` has `test_e0c_be1_sample_shuffle.py`, `prune_expired_chunks` has `test_e2a_be6_store_prune.py`. Coverage is **broad but not deep** on these paths. What the existing suites pin is *observable outcomes* — result counts, score ordering, FTS behavior, dry-run vs apply semantics — not *how the SQL predicate that produces them is constructed*. The proof: the `where=f"chunk_id = '{chunk_id}'"` raw-interpolation bug in `reindex_metadata` shipped straight through `test_store_reindex_metadata.py`, `test_store_reindex_metadata_fts.py`, and the CLI/integration suites, all green. So: these suites are an adequate regression net for behavior-preserving restructuring, but every slice must additionally assert the *invariants* it could silently break (quoting helpers, log lines, raised exceptions) — the existing tests will not do it for you.

The one genuine gap: `CollectionMeta` construction (ddd-09) and the God-class split (solid-01) have no test that pins the *current* full-field construction pattern — extend coverage before touching those, since factory-method refactors of a 20-field dataclass are easy to get subtly wrong (a dropped or defaulted field silently changes stored metadata).

## Work slices

### Slice 1 — `reindex_metadata` overhaul
**Resolves**: clarity-10, clarity-14 (1935), clarity-15 (1935), clarity-16 (1935), smells-03 (1935)
**What**: Split into a preview command (`dry_run`) and an apply command per clarity-14/smells-03; extract low-level mtime/suffix detail into a private helper (clarity-10); reduce nesting with guard clauses (clarity-15); the branch-count drop (clarity-16) falls out once the above land — no separate pass needed.
**Guarded invariant that must survive the split**: the per-chunk `table.update(where=_where_eq("chunk_id", chunk_id), …)` call inside `reindex_metadata` must keep going through `_where_eq` — never an f-string or manual quoting. `tests/test_no_fstring_sql.py` fails the build on the f-string form, and this exact call site is what that guard was added for (see `Documentation/Architecture/150_security_and_privacy_architecture.md` → "SQL boundary defense-in-depth (A5b)"). Whichever half of the split ends up issuing the update inherits the obligation.
**Effort**: L · **Risk**: Med (one function, well-tested, but touches CLI `reindex` command and REST `/collections/{name}/migrate`-adjacent job path — check `tests/cli/test_reindex_metadata_cli.py` and `tests/integration/test_s52_reindex_metadata_dry_run_resolvable.py` for the current dry-run contract before splitting the signature).

### Slice 2a — FTS-fallback misclassification (`smells-19`) — **has a driver, keep it**
**Resolves**: smells-19 (2104, 3026)
**Why now**: this is not merely "exception-as-control-flow" — it silently degrades unrelated failures. The FTS arm of `hybrid_search` catches broad `Exception`, lowercases the message, and treats the failure as "no FTS index" whenever the text contains the substring `"index"` or `"fts"`, re-raising only otherwise (`exc_str = str(exc).lower()` inside `hybrid_search`; mirrored in `_hybrid_search_with_trace`). Any unrelated LanceDB/Arrow error whose message happens to contain `index` is therefore logged as `"FTS index not available for collection …"` and the request quietly returns vector-only results — real recall loss behind a misleading log line. Replace the message sniff with an explicit index-presence check and let every other exception propagate. **Reuse the check that already exists** — `optimize_fts` (store.py:1896-1897) does `indices = await table.list_indices()` then `[idx for idx in indices if getattr(idx, "index_type", "") == "FTS"]`; extract that into a shared private helper rather than re-deriving it, so the codebase does not gain a second copy.
**Effort**: S · **Risk**: Med — deliberate behavior change on the hottest path: failures that degrade silently today will start propagating. Add a test asserting that a non-FTS exception whose message contains `"index"` now raises instead of falling back, and keep a test for the genuine no-index fallback.

### Slice 2b — `hybrid_search` / `_hybrid_search_with_trace` restructuring — **DEFERRED**
**Why deferred**: no user-facing requirement drives it. It is XL effort at High risk on the hottest request path in the codebase (every `/search` call), and its entire payoff is readability metrics (`clarity-16`/`17`, `smells-02`/`11`) — nothing a user or operator can observe. Per the YAGNI rule in the root `CLAUDE.md` ("don't refactor things that aren't broken"), that is not a trade worth making on speculation. **Trigger to un-defer**: the next time a real change has to be made inside `hybrid_search` (a new ranking mode, a new filter phase, a latency fix) and the current shape demonstrably obstructs it — then do the extraction *as part of* that change, so the restructuring rides on a diff that has a reason to exist and a behavioral test to prove it. Do not schedule it standalone.
**Resolves**: clarity-16 (2062, 2950), clarity-17 (2950), smells-02 (2062, 2183, 2950), smells-11 (2095, 3013)
**What**: Extract the FTS-fetch/rank block into `_do_fts_search()`/`_do_fts_search_trace()` (smells-11) — this alone shrinks both functions enough to materially help clarity-16/17; wrap the 5-6 positional params in a `HybridSearchQuery` param object (smells-02) once the split is stable, not before — don't rename the signature while restructuring the body.
**Effort**: XL · **Risk**: High — ~15+ existing tests pin score ordering, tie-break determinism, filter behavior, and trace field mapping. Run the full `hybrid_search*`/`hybrid_search_trace*` test subset after every intermediate commit, not just at the end.

### Slice 3 — `ingest_chunks` / `delete_document` / `delete_by_source_path` command-query + boolean-param split
**Resolves**: clarity-14 (1129, 1481, 1727, 1796, 2233, 2536), smells-03 (1727, 2233, 2301)
**What**: `ingest_chunks`'s `_locked_by_caller`/`_is_continuation` flags and `delete_document`/`delete_by_source_path`'s `skip_fts_optimize` flag each become two explicit call paths. `apply_rewrite_migration`, `_do_update_meta_on_add`, `_do_ingest`, `prune_expired_chunks` get their command/query halves separated per clarity-14.
**Effort**: L · **Risk**: Med-High — `ingest_chunks`'s locked/continuation flags are load-bearing for the batch-ingest retry path (`pipeline.py`); grep every caller before splitting, this is the kind of change that silently breaks a retry loop if a caller still expects the old flag semantics under the new name.

### Slice 4 — `CollectionMeta` factory + duplication cleanup
**Resolves**: ddd-09 (1327, 1516, 1548, 1579, 1608, 1617, 1660, 1698), smells-04 (1262, 1327)
**What**: Add `CollectionMeta.create(...)`/reuse `dataclasses.replace(existing, ...)` (imported as `replace`; already used twice, in `apply_in_place_migrations` and `apply_rewrite_migration`, both to bump `schema_version`) instead of hand-copying ~20 fields at 8 call sites; dedupe the `update_collection_meta` merge_insert row-dict against the identical block in `_do_write_meta_unlocked` (store.py:1428).
**Effort**: M · **Risk**: Med — mechanical once a characterization test locks in current field-by-field behavior (add one first, per the coverage gap noted above: assert a `CollectionMeta` round-trip through `update_description`/`_do_update_meta_on_add`/`_do_subtract_meta_on_delete` preserves every field before refactoring).

### Slice 5 — Swallowed exceptions (mostly already resolved)
**Resolves**: smells-12 (340, 495, 1927) — **two of its three call sites no longer hold.** Re-verified against the current source:
- `list_collections` (store.py:495-496): **not a finding.** It already catches a narrow `(RuntimeError, ValueError, OSError)` and logs `logger.warning("Could not inspect collection %s: %s", name, exc)`. Nothing to do — skipping one broken table while naming it in the log is the intended behavior.
- `sample_chunk_texts` (store.py:1927-1928): **partially holds.** The catch is broad `Exception`, but it is not silent — it logs at DEBUG with `exc_info=True`. The only open question is whether DEBUG is the right level for a retrieval-path failure that returns `[]` to the caller (invisible in default operation), and whether the catch should narrow to the exceptions `open_table` / `to_list` can actually raise.
- `ping` (`SearchStore.ping`, its `except Exception:` arm at store.py:340-342): **holds in full.** Broad `except Exception:` with no log line at all — just a `(now, False)` cache write and `return False`. It correctly re-raises `CancelledError` from the preceding arm (store.py:338-339), but a failed ping is cached as `False` with zero diagnostic trace.

**What**: add a log line to `ping`'s except arm (the only genuinely silent swallow); decide whether `sample_chunk_texts` should log at WARNING and narrow its catch. Drop `list_collections` from the slice.
**Effort**: XS · **Risk**: Low — additive logging. `test_store_ping.py` and `test_e0c_be1_sample_shuffle.py` already pin the swallow behavior; extend them to assert the new log line.

### Slice 6 — Data clump, param objects, parameter-object sweep (remaining)
**Resolves**: smells-02 (168, 1128, 1298, 1481, 1631, 2437, 2635 — everything not folded into Slices 1-3), smells-14 (2437)
**What**: Wrap `(collection, namespace)` in a `CollectionRef` used by `get_collection_meta`, `delete_collection_meta`, `count_chunks`, `query_expiring_chunks`, `prune_expired_chunks`, `count_expired_chunks`, `list_chunks_raw`; wrap remaining >3-param functions in dedicated param objects.
**Effort**: M · **Risk**: Med — `CollectionRef` changes a widely-shared 2-arg pair into a 1-arg object across 7+ methods; do this **after** Slices 1-4 land, since those slices touch several of the same call sites and a param-object change under them would create merge conflicts, not correctness risk.

### Slice 7 — `migrate_expires_at_and_scopes` / `migrate_acl_provenance` / `_row_to_meta` / `migrate_per_collection_model` nesting + complexity
**Resolves**: clarity-15 (811, 912), clarity-16 (504, 694)
**What**: Guard clauses / early returns; extract branch groups into named helpers. These are one-time migration functions, lower call frequency than Slices 1-2.
**Effort**: M · **Risk**: Low-Med — migration code paths, exercised by `tests/test_store_reindex_metadata*.py` and migration-specific tests; STORE_SCHEMA_VERSION invariant (CLAUDE.md) means these functions must keep producing byte-identical output for existing migration specs — refactor internals only, do not change the migration contract or bump `STORE_SCHEMA_VERSION`.

### Slice 8 — `safety-11` clock/RNG injection — **OPTIONAL, needs a named consumer first**
**Resolves**: safety-11 (330, 1650, 1925, 2474, 2573, 2627)
**What**: Inject a clock (`Callable[[], datetime]` or similar) and RNG source into `SearchStore.__init__` (or the affected methods), replacing direct `time.monotonic()`, `datetime.now(timezone.utc)`, `random.shuffle` calls.
**Blocked on justification**: the only consumer of these seams is test convenience, and the suites already pin the behavior without them — `tests/test_store_ping.py` shrinks the TTL with `patch("archon_search.store.PING_TTL_SECONDS", 60.0)` and reads the cache timestamp off `s._ping_cache[0]` (test_store_ping.py:70, 102-103), and `tests/test_e0c_be1_sample_shuffle.py` asserts the shuffle statistically rather than by seeding the RNG (there is no `freezegun` or equivalent time-faking dependency anywhere in the repo). Adding a constructor seam with no production caller is speculative generality (root `CLAUDE.md`, "no abstractions for single-use code"). **Do not start this slice until one of these is true**: (a) a test that cannot be written any other way is actually blocked on it, or (b) a production consumer appears (e.g. an operator-settable clock skew, or a deterministic-sampling config knob). Otherwise close it as won't-do. The one real but non-blocking cost of the status quo is that the ping tests reach into the private `_ping_cache` tuple.
**Effort**: M · **Risk**: Med — changes `SearchStore.__init__`'s signature and touches 6 call sites across ping/TTL/centroid/sample paths; update every constructor call site in the same commit rather than relying on defaults to paper over the change. **If it does get scheduled, do it before Slice 10** — if the God-class split happens first, the same clock/RNG dependency has to be threaded through N new classes instead of one.

### Slice 9 — `ddd-01`, `ddd-04` (small, standalone)
**Resolves**: ddd-01 (`ChunkIngestResult`, store.py:45-46 — the finding cited 42, which is a field of the preceding dataclass), ddd-04 (`_do_update_meta_on_add`, store.py:1574)
**What**: `ChunkIngestResult` → `frozen=True`; move centroid weighted-average maintenance out of `_do_update_meta_on_add` into a `CollectionMeta` domain method.
**Effort**: S (ddd-01) / M (ddd-04) · **Risk**: Low (ddd-01) / Med (ddd-04 — touches the same centroid-maintenance code Slice 4's factory work also touches; sequence directly after Slice 4).

### Slice 10 — `solid-01` God-class split
**Resolves**: solid-01 (268)
**What**: Split `SearchStore` into cohesion groups: connection/migrations, ingest, search, delete/TTL, centroid+metadata bookkeeping. Largest, highest-risk item in this plan.
**Effort**: XL · **Risk**: High.

### Slice 11 — `solid-07` primitive obsession (deferred, optional)
**Resolves**: solid-07 (408, 2236)
**What**: Wrap `doc_id`/`chunk_id`/job-id fields in value objects. Not planned for near-term execution — flagged here so it isn't lost, but every other slice touches these fields as raw strings, and wrapping them mid-cleanup would multiply the diff size of every other slice. Revisit only after Slices 1-10 are settled.
**Effort**: L · **Risk**: Med (wide surface, low semantic risk — it's a type wrapper, not a behavior change).

## Execution order

1. **Slice 5** (swallowed exceptions) — now down to one log line in `ping` plus a level/narrowing decision in `sample_chunk_texts`, since `list_collections` was never a defect — its narrow catch and warning log predate this review, so there is no fix commit to look for. It is no longer "clearing easy Major findings" — it is a ten-minute warm-up on the file. Do it first only because it costs nothing and touches nothing else; if a slice needs to be dropped for capacity, drop this one.
2. **Slice 9a** (`ddd-01` frozen dataclass) — standalone, zero-risk, do alongside Slice 5.
3. **Slice 4** (`CollectionMeta` factory) → **Slice 9b** (`ddd-04` centroid domain method) — these touch the same centroid-maintenance code; do the factory extraction first so `ddd-04`'s domain method has a clean construction path to call into, not the old hand-copied version.
4. **Slice 7** (migration nesting/complexity) — isolated, low call frequency, no dependency on anything above; can run in parallel with step 3 if using separate branches, but land before Slice 10 so the God-class split doesn't have to carry unrefactored migration code into whichever new class it lands in.
5. **Slice 8** (clock/RNG injection) — **optional; skip unless its justification gate is met** (see the slice entry). If it is ever scheduled, it **must precede Slice 10.** Reasoning: injecting a clock/RNG into `SearchStore.__init__` is a small, well-scoped change against the current single-class shape. If Slice 10 (the split) happens first, the same dependency has to be threaded through however many new classes centroid/TTL/sample logic ends up in — same work, done twice, against a moving target the second time.
6. **Slice 1** (`reindex_metadata`) and **Slice 3** (`ingest_chunks`/`delete_document`/`delete_by_source_path`) — independent of each other, both depend on nothing above being in-flight simultaneously (different code regions), but land **before** Slice 6 since Slice 6's `CollectionRef` param object touches call sites inside these same functions.
7. **Slice 2a** (FTS-fallback misclassification) — schedulable at any point, independent of every other slice; it touches only the FTS `except` arm in both hybrid-search functions. Being a deliberate behavior change, it must land as its own commit with its own test, never folded into a restructuring diff. **Slice 2b** (`hybrid_search` restructuring) is **deferred, not scheduled** (see the slice entry for the un-defer trigger). If a real change to `hybrid_search` ever pulls 2b in, do it on its own, not interleaved with 1/3/6 — isolating it avoids a merge conflict with every other slice's incidental touches to shared helpers (`_where_eq`, filter builders).
8. **Slice 6** (`CollectionRef` + remaining param objects) — last of the "mechanical" slices, deliberately after 1/3/4/7 land, since it touches call sites inside all of them; doing it earlier means rebasing every other slice's diff against a renamed parameter list.
9. **Slice 10** (God-class split) — **last.** Reasoning for last-not-first: every other slice is a targeted, reviewable diff against the current single-class file. Doing the split first means every subsequent slice's diff is against a moving target (which new class does `reindex_metadata` live in?), multiplying review cost and rebase churn across the whole plan. Splitting last means the split absorbs already-cleaned-up methods and only has to solve "where does this method go," not "where does this method go, and also here's an unrelated behavior change in the same diff."
10. **Slice 11** (`solid-07` value objects) — deferred past the end of this plan; revisit as a separate follow-up once 1-10 are shipped and stable, per the reasoning in its slice entry above.

## Documentation checklist

Slices 1, 3, 4, 6, 8, 9b and 10 add or change public methods documented outside the source, and Slice 2a changes documented behavior. A fix in the code but not in the Decisions/doc surfaces is this project's top recorded plan defect (`learnings.md`, "plan/task authoring"), so each of those slices must close out the surfaces below **in the same PR**, in this order (per `learnings.md`, "doc close-out"). Slices 5, 7 and 9a change no documented surface — they need only the `530` and "this file" rows.

| Surface | When it applies | What to update |
|---|---|---|
| `Documentation/Architecture/600_api_reference_or_public_interface.md` | Slice 1 only, and only if the CLI `reindex` flags or the REST reindex/migrate request shape change | The CLI and REST reindex entries. If the wire shape changes at all, `GET /openapi.json` is authoritative (root `CLAUDE.md`) — regenerate the OpenAPI snapshot. No other slice in this plan is expected to reach the wire: they are all internal to `store.py`, so `openapi.json` stays untouched |
| `/BREAKING.md` | **Slice 1** (only if the wire shape changes) and **Slice 2a** (unconditionally) | Slice 2a's behavior change is operator-visible — errors that used to degrade silently to vector-only now propagate as a 500 — so it needs an entry even though no schema moves |
| `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` | **Slices 1, 3, 6, 8, 10** (the `store.py` row) and **Slices 4, 9b** (the `collection_meta.py` row); plus Slice 2b and Slice 11 if either is ever un-deferred | The `store.py` row's public-symbols column and Purpose text; for Slices 4 and 9b, the `collection_meta.py` row — Slice 4 adds `CollectionMeta.create(...)` and Slice 9b adds the centroid-maintenance domain method, both public methods on a class whose row currently documents fields only. Slice 10 additionally needs new rows for whatever classes the split creates, and a layer assignment for each |
| `Documentation/Architecture/130_data_architecture_and_persistence.md` | Any slice that alters filter/predicate construction or migration behavior, plus **Slice 2a** | "Filter execution and over-fetch" (§`### Filter execution and over-fetch`) and "Migrations" (§`### Migrations (idempotent, run at startup)`). For Slice 2a, tighten the FTS paragraph's claim that hybrid search "gracefully falls back to vector-only if no FTS index exists" — after 2a that is true *only* for a genuinely absent index |
| `Documentation/Architecture/150_security_and_privacy_architecture.md` | **Slices 1, 3, 6, 7, 10** — verified against the symbols 150 currently names: Slice 1 moves `reindex_metadata`'s `where=` site; Slice 3 splits `prune_expired_chunks` and `ingest_chunks`; Slice 6 re-signatures `query_expiring_chunks`, `prune_expired_chunks` and `count_expired_chunks` behind `CollectionRef`; Slice 7 refactors `migrate_per_collection_model`; Slice 10 relocates all of them | "SQL boundary defense-in-depth (A5b)" — it enumerates the helper, `+`-concat and `add_columns` sites **by symbol** across both `store.py` and `graph_store.py`, so a rename, a signature change or a relocation makes it stale. Re-check the enumeration against the code, not just the moved symbol's new name |
| `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` | Every slice | Mark the slice's findings resolved; a slice closed here but still open there is a false debt entry |
| Root `CLAUDE.md` | Slice 10 only | The "Architecture at a glance" line naming `store.py` as the persistence layer |
| This file | Every slice | Strike the completed slice and re-check the Execution order for slices that were only ordered relative to it |

Grep the whole tree (including `README.md` and `archon-search.toml.example`) for any renamed symbol before declaring a slice done — per-file scope orphans siblings.

## Notes

- No slice in this plan touches `STORE_SCHEMA_VERSION` or the shared `_schema()`/`_meta_schema()` structural definitions — all changes here are internal restructuring of existing behavior, not schema changes. If any slice's implementation turns out to require a schema change, stop and re-scope: that is out of bounds for a clean-code pass.
- Every slice should land as its own PR/session with its own test run — do not batch multiple slices into one commit; the whole point of the slicing above is to keep each diff reviewable and independently revertible on a file this central.
