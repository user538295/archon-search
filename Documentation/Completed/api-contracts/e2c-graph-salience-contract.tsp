import "@typespec/http";
using Http;

@service(#{ title: "E2C Graph Salience API" })
namespace E2cGraphSalience;

/** Salience scoring mode. E2c adds "tfidf"; "frequency" is the existing default. */
union SalienceMode {
  frequency: "frequency",
  tfidf: "tfidf",
}

/** Graph export format. */
union GraphFormat {
  json: "json",
  graphml: "graphml",
}

/** A single graph node with derived salience metrics. */
model GraphNodeResponse {
  entity_id: string;
  entity_name: string;
  /** Number of distinct chunks in which this entity is mentioned. */
  chunk_count: int32;
  /**
   * Salience score. Semantics depend on salience_mode:
   * - "frequency": chunk_count / total_chunks_in_collection, clamped [0, 1].
   * - "tfidf": (chunk_count / total_chunks_in_collection) × log((N+1) / df),
   *   where N = total namespace collections, df = collections containing this entity.
   */
  salience: float32;
}

/** A single graph edge with co-occurrence weight. */
model GraphEdgeResponse {
  edge_id: string;
  source_entity_id: string;
  target_entity_id: string;
  weight: int32;
  source_chunk_ids: string[];
}

/** E2c addition: salience mode echoed in both response types. */
model GraphInspectionResponse {
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  node_count: int32;
  edge_count: int32;
  truncated: boolean;
  /** E2c — echoes the effective salience mode used. */
  salience_mode: SalienceMode;
}

/** E2c addition: salience mode echoed in cross-collection response. */
model CrossCollectionGraphInspectionResponse {
  collections: string[];
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  node_count: int32;
  edge_count: int32;
  truncated: boolean;
  /** E2c — echoes the effective salience mode used. */
  salience_mode: SalienceMode;
}

model GraphDisabledError {
  detail: "graph inspection requires [graph] enabled=true in server config";
}

model CollectionNotFoundError {
  detail: "collection not found";
}

model CrossCollectionMinimumError {
  detail: string;
}

model InvalidSalienceError {
  detail: string;
}

@route("/graph")
interface GraphInspection {
  /**
   * Return node and edge list for a single collection, scored by the chosen salience mode.
   * Defaults to "frequency" (existing behaviour) when ?salience= is omitted.
   * Returns 422 for unknown salience values.
   */
  @get
  @route("/{collection}")
  getSingleCollection(
    @path collection: string,
    @query format?: GraphFormat,
    @query salience?: SalienceMode,
  ): GraphInspectionResponse | {
    @statusCode statusCode: 422;
    @body body: GraphDisabledError | InvalidSalienceError;
  } | {
    @statusCode statusCode: 404;
    @body body: CollectionNotFoundError;
  };

  /**
   * Return merged node and edge list across ≥2 collections, scored by the chosen salience mode.
   * For tfidf mode, IDF denominator is the full namespace collection count (not just the listed ones).
   * Defaults to "frequency" when ?salience= is omitted.
   */
  @get
  @route("/cross-collection")
  getCrossCollection(
    @query collections: string,
    @query format?: GraphFormat,
    @query salience?: SalienceMode,
  ): CrossCollectionGraphInspectionResponse | {
    @statusCode statusCode: 422;
    @body body: GraphDisabledError | CrossCollectionMinimumError | InvalidSalienceError;
  } | {
    @statusCode statusCode: 404;
    @body body: CollectionNotFoundError;
  };
}
