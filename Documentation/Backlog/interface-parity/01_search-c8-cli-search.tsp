// C8 — `archon-search search` command (Presentation <-> REST over HTTP).
// The CLI is an HTTP client of REST: it imports nothing from services/ or server/ and builds the REST request body itself.
// The option names below are the plan's proposal; the exact set is Q20 in the plan.
namespace CliSearchContract;

model CliSearchOptions {
  // Positional argument.
  query: string;
  // Repeatable and required. One value selects the single-collection path (REST `collection`), several select the multi path (REST `collections`).
  collection: string[];
  top_k?: int32;
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  include_metadata: boolean;
  hyde: boolean;
  rag_fusion: boolean;
  graph_mode?: string;
  scope_filter?: string;
  acl_context: boolean;
  // Machine-readable output instead of the list.
  json: boolean;
  api_url: string;
  api_key?: string;
}

// Human output: one line per result in response order. The excerpt is whitespace-collapsed, stripped of control characters and cut at one named constant (about 200 characters). There is no excerpt-length option (brief Q4).
model SearchLine {
  rank: int32;
  score: float64;
  collection: string;
  source_path: string;
  excerpt: string;
}

enum CliExit {
  ok,
  // Server error, connection failure, missing key.
  failure,
}

model CliSearchOutcome {
  exit: CliExit;
  // --json: the REST response body with the full text (not the excerpt).
  json_body?: string;
  lines?: SearchLine[];
  // Error text to stderr: connection-refused message, or the server's detail rendered from its string, list or coded shape.
  stderr?: string;
}

// The request timeout is one named CLI constant above the server's 30-second search budget, so the CLI reports the server's 504 rather than its own timeout.
interface CliSearch {
  run(options: CliSearchOptions): CliSearchOutcome;
}
