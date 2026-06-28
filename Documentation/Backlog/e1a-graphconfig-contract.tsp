// Internal logical seam — GraphConfig TOML section to Python consumers.
// Validated with: tsp compile e1a-graphconfig-contract.tsp --no-emit
namespace GraphConfigContract;

model GraphConfig {
  enabled: boolean;
  extractionModel: string | null;      // optional LLM model string; null = spaCy-only
  backendThresholdEdges: int32;        // default 10 000; triggers Kuzu migration hint
}
