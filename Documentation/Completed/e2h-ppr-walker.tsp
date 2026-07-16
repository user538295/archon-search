// C3 — PPRWalker interface (internal logical seam)
// Contract between pipeline.py and the new ppr_walker.py Use-Case module.
namespace E2h.PPRWalker;

/** Result returned by a single PPR walk for one query. */
model PPRWalkResult {
  /** IDs of top-K entities by PPR score. */
  entityIds: string[];

  /** Chunk IDs associated with those entities (via mentions table). */
  chunkIds: string[];

  /** Number of seed entities matched from query n-grams; 0 = no match → fallback. */
  entitiesMatched: int32;
}

/** In-process interface implemented by PPRWalker. */
interface PPRWalkerProtocol {
  /** Run Personalized PageRank for the given query in the given collection.
      Returns empty entityIds/chunkIds and entitiesMatched=0 on no entity match.
      Must run in asyncio.to_thread (CPU-bound igraph call). */
  walk(
    collection: string,
    query: string,
    damping: float64,
    topEntities: int32,
    /** LanceDB namespace; never named "namespace" (TypeSpec reserved word). */
    collectionNs: string
  ): PPRWalkResult;
}
