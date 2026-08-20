# Bug Brief: co-occurrence edges from a previous version of a document survive every re-ingest

**ID:** 2026-08-20-010 · **Severity:** P2 · **Status:** Filed 2026-08-20 (unowned)
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

## Failing repro (sketch — not yet written)

1. Ingest a document whose text co-occurs entities A and B, with a working NER model.
2. Assert an edge exists with `source_doc_id == doc_id`.
3. Rewrite the file so A and B no longer appear in the same chunk. Re-ingest.
4. The stale A–B edge is still present.

`tests/integration/test_bug030_graph_spacy_latch_ingest.py::test_reingest_under_degradation_leaves_no_orphan_prose_edges`
is the shape to copy; it pins the degraded half of the same question.

## Root cause

- `archon_search/pipeline.py:736-743` — `write_graph` is upsert-only, and no
  `delete_graph_by_doc` runs before it on the healthy path.
- `archon_search/graph_store.py:375` — `merge_insert` on the stable edge ID means an edge is
  never removed by writing a set that omits it.

## Candidate fix

Call `delete_graph_by_doc(collection, doc_id, ns=namespace)` before `write_graph` on every
path, not only the degraded one, making the write a replace rather than a merge. Needs care:
`delete_graph_by_doc` also removes def/ref rows (`graph_store.py:1427`), which the def/ref
pass at `:810` writes separately — check the ordering before making them symmetric, or the
two passes will delete each other's output.

## Open question

Whether mention-based orphan GC (`delete_orphan_nodes_and_edges`) was intended to cover this
and does not, or whether edge lifetime was always meant to be document-scoped. Answer that
before choosing the fix — a GC-side fix and a write-side fix are not interchangeable.

## References

- [[archon_search/pipeline.py]] — graph write block (:731-745), def/ref delete (:810)
- [[archon_search/graph_store.py]] — `write_graph` upsert (:375), `delete_graph_by_doc` (:1415)
