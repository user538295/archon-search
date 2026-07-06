/** Internal logical seam: graph inspection use case — E2c additions.
 * Boundary: routes_graph.py (Presentation) → graph_inspector.py (Use Cases).
 * This seam extends the E2b inspector seam with salience_mode plumbing.
 * Realised by: BE-2, BE-4.  Verified by: BE-2 (unit), BE-3 (integration), BE-4 (unit), BE-5 (integration).
 */
namespace E2cInspectorSeam;

/** Salience scoring mode. */
union SalienceMode {
  frequency: "frequency",
  tfidf: "tfidf",
}

/** Single-collection graph view — gains salience_mode field in E2c. */
model CollectionGraphView {
  salience_mode: SalienceMode;
}

/** Cross-collection graph view — gains salience_mode field in E2c. */
model CrossCollectionGraphView {
  salience_mode: SalienceMode;
}

interface GraphInspector {
  /**
   * E2c: gains salienceMode, entityPresence, and numCollections parameters.
   * entityPresence: entity_id → count of namespace collections containing it; null in frequency mode.
   * numCollections: total namespace collection count (N in IDF formula); ignored in frequency mode.
   * Returns CollectionGraphView with salience_mode echoed.
   */
  inspect_collection(
    salienceMode: SalienceMode,
    entityPresence: Record<int32> | null,
    numCollections: int32,
  ): CollectionGraphView;

  /**
   * E2c: same additions as inspect_collection.
   * IDF is computed over the full namespace, not just the listed collections.
   * Returns CrossCollectionGraphView with salience_mode echoed.
   */
  inspect_cross_collection(
    salienceMode: SalienceMode,
    entityPresence: Record<int32> | null,
    numCollections: int32,
  ): CrossCollectionGraphView;
}
