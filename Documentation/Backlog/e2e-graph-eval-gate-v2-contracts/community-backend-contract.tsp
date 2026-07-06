namespace CommunityBackendContract;

/** Minimal community representation for the eval backend protocol.
    Mirrors archon_search.graph_types.Community (relevant fields only). */
model Community {
  community_id: string;
  entity_ids: string[];
  representative_chunk_ids: string[];
  built_at: utcDateTime;
  summary_text: string | null;
}

/** Minimal graph-node representation for the eval backend protocol.
    Mirrors archon_search.graph_types.GraphNode (relevant fields only). */
model GraphNode {
  id: string;
  entity_name: string;
  entity_type: string;
  source_doc_id: string;
  collection_name: string;
}

/** Protocol both CommunityStoreStub (existing) and RealCommunityEvalBackend (new)
    must implement.  Called by SearchPipeline._search_graph_mode() and
    ._search_local_mode() at eval query time. */
interface CommunityBackend {
  communities_table_exists(collection: string, ns: string): boolean;
  list_community_representatives(collection: string, ns: string): Community[];
  find_nodes_by_name(collection: string, names: string[], ns: string): GraphNode[];
  get_communities_for_entities(
    collection: string,
    entity_ids: string[],
    ns: string
  ): Community[];
}
