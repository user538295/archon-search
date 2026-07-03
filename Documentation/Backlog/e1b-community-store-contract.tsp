namespace CommunityStoreContract;

/** A Leiden community: a cluster of entities with selected representative chunks. */
model Community {
  communityId: string;
  entityIds: string[];
  representativeChunkIds: string[];
  summaryText: string | null;
  builtAt: utcDateTime;
}

/** Aggregate stats returned for GET /status and staleness checks. */
model CommunityStats {
  count: int32;
  lastBuiltAt: utcDateTime | null;
}

/** Store interface for per-collection community persistence. */
interface CommunityStore {
  ensureCommunitiesTable(collection: string): void;
  writeCommunities(collection: string, communities: Community[]): void;
  getCommunitiesForEntities(collection: string, entityIds: string[]): Community[];
  listCommunityRepresentatives(collection: string): Community[];
  getCommunityStats(collection: string): CommunityStats;
}
