## Bug: [maintenance] graph_gc = false is ignored: maintenance passes still run graph GC and sweep stale nodes/edges

**ID**: S652-unsupported_edge_survives
**Scenario**: S652
**Severity**: medium
**Version**: archon-search, version 26.10.2150

### What happened
With [maintenance] graph_gc = false (interval_hours = 0, orphan_cleanup = true), two POST /maintenance/trigger passes after deleting pair.md removed the unsupported Falcon Service/Nimbus Platform related_to edge AND the orphaned 'Zephyr Connector' node. GET /status maintenance.last_graph_gc_at was set and advanced on both passes (2026-10-08T12:36:13.267522+00:00 then 2026-10-08T12:36:15.301697+00:00), i.e. graph GC ran although disabled. Deleting the file alone did not change the graph (checked before the first pass).

### What should happen
- Step 6: a swept-type edge still connects `Falcon Service` and `Nimbus Platform` — with `graph_gc = false` the GC pass does not "sweep at all" (60:250), so the unsupported relationship survives (contrast S614, where GC removes it — 60:236).
- Step 6: the `Zephyr Connector` node is still present — it has no remaining mention, but with `graph_gc` off "stale nodes … survive … indefinitely" (60:250).

### Steps to reproduce
1. Start the isolated instance. Ingest three documents from `<iso-data-dir>/seed-s652_docs/` into collection `s652_docs` (S614's corpus):
   - `pair.md`: `Falcon Service depends on Nimbus Platform via the Zephyr Connector for storage.`
   - `alone_a.md`: `Falcon Service is a critical system.`
   - `alone_b.md`: `Nimbus Platform hosts the Titan Cluster workloads.`
2. `GET /graph/s652_docs` — record the node ids of `Falcon Service`, `Nimbus Platform` and `Zephyr Connector`. Confirm (setup): a swept-type edge connects `Falcon Service` and `Nimbus Platform`.
3. Delete `<iso-data-dir>/seed-s652_docs/pair.md` — the only document where `Falcon Service`/`Nimbus Platform` co-occur, and `Zephyr Connector`'s only mention. `GET /graph/s652_docs` and confirm (setup) the Falcon/Nimbus edge is still present — deleting the file alone does not touch the graph (60:242: "a stale relationship disappears at the next maintenance GC pass, not at re-ingest").
4. `curl -s -X POST -H "Authorization: Bearer <iso-key>" http://127.0.0.1:<iso-port>/maintenance/trigger` and poll `GET /status` until `maintenance.last_run_at` changes. Confirm (setup): the `s652_docs` entry of `maintenance.collection_health[]` reports `orphans_removed_last_run >= 1` — orphan cleanup deleted `pair.md`'s document, so its mentions are gone.
5. Trigger and wait for a second pass the same way — so a GC sweep, had it been enabled, would see the already-deleted mentions regardless of policy order within one pass (S614's two-pass rationale).
6. `GET /graph/s652_docs`.

### Evidence
```
After passes: edges=[{relationship_type: related_to, Titan Cluster<->Nimbus Platform}] only; nodes=[Falcon Service, Titan Cluster, Nimbus Platform] (Zephyr Connector gone). last_graph_gc_at={'first': '2026-10-08T12:36:13.267522+00:00', 'second': '2026-10-08T12:36:15.301697+00:00'}. Reproduced on two separate runs. Test: tests/test_s652_graph_gc_false_no_sweep.py
```
