// C2 — GraphConfig PPR additions (internal logical seam)
// New fields added to archon_search/config.py GraphConfig dataclass for E2h.
namespace E2h.GraphConfig;

/** PPR and naive-cap fields added to [graph] TOML section. */
model GraphConfigPprAdditions {
  /** Damping factor for Personalized PageRank walk; range (0, 1). Default: 0.85. */
  pprDamping: float64 = 0.85;

  /** Top-K entity count whose associated chunks are blended into results. */
  pprTopEntities: int32 = 20;

  /** Cap on neighbour names appended for naive mode expansion.
      Separate from pprTopEntities — they are unrelated concepts. */
  naiveMaxExpansionTerms: int32 = 20;
}
