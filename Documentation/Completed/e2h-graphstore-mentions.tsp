// C4 — GraphStore.get_mentions_for_entity_ids (internal logical seam)
// New method added to archon_search/graph_store.py for targeted mention lookup.
namespace E2h.GraphStore;

/** Row from the _archon_graph_{ns}__{col}_mentions LanceDB table. */
model GraphMention {
  entityId: string;
  chunkId: string;
  docId: string;
}

/** Extension to GraphStoreProtocol for PPR entity-to-chunk resolution. */
interface GraphStoreMentionsExtension {
  /** Return all mention rows for the given entity IDs.
      Callers aggregate by entityId to derive mention counts.
      Empty entityIds returns [].
      collectionNs replaces "namespace" (TypeSpec reserved word). */
  getMentionsForEntityIds(
    collection: string,
    entityIds: string[],
    collectionNs: string
  ): GraphMention[];
}
