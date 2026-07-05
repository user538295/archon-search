/** Internal logical seam: GraphStore entity presence method — E2c addition.
 * Boundary: routes_graph.py (Presentation) → graph_store.py (Frameworks & Drivers),
 * via pragmatic exception — see team-plan Key Decisions (layer-assignment note).
 * New method: given a list of collection names, return a map of entity_id → number of
 * collections in which the entity appears. Called directly by route handlers to build IDF denominators.
 * Realised by: BE-1.  Verified by: BE-1 (unit), BE-3 (integration).
 */
namespace E2cGraphStorePresenceSeam;

interface GraphStorePresence {
  /**
   * Scan the node table of each named collection; return entity_id → collection count.
   * One LanceDB read per collection; no per-entity fan-out (O(N_collections × M_nodes)).
   * Empty collectionNames returns {}. Absent tables are skipped (contribute count 0).
   * GraphStore has no namespace concept — the caller resolves namespace → collection names.
   */
  getEntityPresenceAcrossCollections(
    collectionNames: string[],
  ): Record<int32>;
}
