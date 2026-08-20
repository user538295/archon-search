# Bug Brief: co-occurrence edges from a previous version of a document survive every re-ingest

**ID:** 2026-08-20-010 · **Severity:** P2 · **Status:** Fixed 2026-08-21 — GC-side co-mention sweep. The originally-filed premise and candidate fix were wrong; see "Root cause — corrected".
**Found during:** review of [[2026-08-19-030-graph-spacy-download-retry-and-ingest-abort-brief.md]] — pre-existing, explicitly kept out of that change set.

## Problem

`SearchPipeline.ingest_file`'s graph block never deletes the document's previous
co-occurrence edges on the normal (non-degraded) path. `GraphStore.write_graph` upserts by
stable edge ID (`graph_store.py:375`), so re-ingesting a document only ever *adds*. The only
deletion on that path is the def/ref one (`pipeline.py:810`) and, since 2026-08-19-030, the
degraded-path `delete_graph_by_doc` call.

Consequence: an edge produced by an earlier version of a document survives indefinitely as
long as its endpoint nodes stay mentioned by anything at all. Edit a document so that
"Alice" and "Acme Corp" no longer co-occur, re-ingest, and the `related_to` edge asserting
they do remains in the graph — with no mention supporting it.

## Why this is not the 030 degrade path

2026-08-19-030 C2-I-2 fixed the *degraded* case: when prose NER cannot run, the document's
graph rows are deleted so its contribution is consistently "code symbols only". That fix
deliberately did not touch the healthy path, which is where this bug lives.

## Failing repro — written

`tests/test_graph_store_gc.py::test_gc_removes_related_to_edge_whose_pair_is_no_longer_co_mentioned`
(real LanceDB in `tmp_path`). Confirmed to fail against unmodified code. The sketch below was
the original plan; the test that landed asserts the same thing at the GC layer rather than
through a full re-ingest, because the sweep is where the fix lives.

Original sketch:

1. Ingest a document whose text co-occurs entities A and B, with a working NER model.
2. Assert an edge exists with `source_doc_id == doc_id`.
3. Rewrite the file so A and B no longer appear in the same chunk. Re-ingest.
4. The stale A–B edge is still present.

`tests/integration/test_bug030_graph_spacy_latch_ingest.py::test_reingest_under_degradation_leaves_no_orphan_prose_edges`
is the shape to copy; it pins the degraded half of the same question.

## Root cause — as originally filed (SUPERSEDED, kept for the record)

- `archon_search/pipeline.py:736-743` — `write_graph` is upsert-only, and no
  `delete_graph_by_doc` runs before it on the healthy path.
- `archon_search/graph_store.py:375` — `merge_insert` on the stable edge ID means an edge is
  never removed by writing a set that omits it.

Both facts are true. The conclusion drawn from them — that the fix belongs on the write side —
was not. See below.

## Root cause — corrected 2026-08-21

The premise this brief was filed on was wrong, and the correction changes the fix.

`make_stable_edge_id` (`graph_types.py:94-110`) hashes `source_id:target_id:relationship_type`
— **no `doc_id`**. Two documents that both co-occur the same entity pair therefore share ONE
edge row, and `source_doc_id` is a last-writer stamp (`graph_store.py:538`: "source_doc_id
always takes the incoming value"). Edge lifetime was never document-scoped, and the schema
cannot express a document-scoped edge without a stored-shape change.

So the original "Candidate fix" — `delete_graph_by_doc` before `write_graph` on the healthy
path — was **actively harmful**: it deletes edges other documents still assert, and they do
not come back until one of those documents happens to be re-ingested. It trades a visible
wrong answer for a silent one. Do not do it.

`delete_orphan_nodes_and_edges` was likewise not failing a job it had been given: it asks
"does this ENTITY still have a mention?", never "does this RELATIONSHIP still hold?". A gap,
not a defect.

## Fix as implemented

A GC-side co-mention sweep (`graph_store.py`, Step 3b). The mentions table is the accurate
collection-wide ledger — replaced per-document on every ingest, pruned for dead chunks — so
the sweep rebuilds `entity -> chunks` from it in one pass and deletes `related_to` edges whose
two endpoints no longer share any chunk. Cross-document-correct: an edge survives while ANY
document supports it and dies when none does.

- Scoped by `relationship_type == "related_to"`, which the NER co-occurrence loop is the only
  producer of, plus the existing `_edge_is_defref` guard. Def/ref and synonym edges are file-
  and dictionary-derived, not mention-backed.
- The ingest path is untouched, so the def/ref ordering hazard (`delete_graph_by_doc` also
  removes def/ref rows that `pipeline.py:810` writes later) never arises.
- `GcPassResult.communities_invalidated` now fires on edges as well as nodes: Leiden partitions
  over the edge list, so a deleted relationship makes stored groupings stale even when every
  member node survives.
- C1-B-2's "healthy graph does not open the edges table" optimisation is reversed. That early
  return is exactly what made this bug unreachable — the common shape is "no node orphaned, one
  relationship unsupported".

**Trade-off accepted:** a stale relationship disappears at the next maintenance pass, not at
re-ingest.

## The long-term alternative, rejected for a P2

Putting `doc_id` into the edge ID gives each document its own rows and makes a write-side
delete correct. It is the right answer if per-document parallel edges are ever wanted, but it
is a stored-shape change plus a migration plus a rewrite of every traversal, ranking and
community consumer to handle parallel edges. Out of proportion here.

## References

- [[archon_search/pipeline.py]] — graph write block (:731-745), def/ref delete (:810)
- [[archon_search/graph_store.py]] — `write_graph` upsert (:375), `delete_graph_by_doc` (:1415)
