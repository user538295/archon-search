// Internal logical seam — GraphExpander sits between query receipt and hybrid
// search in pipeline.search() / search_many() when graph_mode="naive".
// Validated with: tsp compile e1a-graphexpander-contract.tsp --no-emit
namespace GraphExpanderContract;

model ExpandedQuery {
  expandedText: string;            // original query + neighbour entity names appended
  originalQuery: string;
  entityNamesFound: string[];      // entity names matched against node table (exact, case-insensitive; NOT a second spaCy NER pass)
  neighbourNamesAdded: string[];   // entity names from first-degree graph neighbours
  expansionApplied: boolean;       // false when no entities found or graph empty
}

interface GraphExpander {
  expand(query: string, collection: string): ExpandedQuery;
}
