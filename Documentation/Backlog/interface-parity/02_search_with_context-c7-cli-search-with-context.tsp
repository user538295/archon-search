// C7 — `archon-search search-with-context` command (Presentation <-> REST over HTTP).
// The CLI is an HTTP client of REST: it imports nothing from services/ or server/ (ADR 20 rule 4), builds the REST request body itself and renders the REST body. Follows 01_search-c8-cli-search.tsp. Lives in its own module `cli/search_with_context.py`, registered in cli/main.py.
namespace CliSearchWithContextContract;

model CliSearchWithContextOptions {
  // Positional argument.
  query: string;
  // Single-valued and required through Click (a usage error otherwise, as in `search`). The server still validates a blank value.
  collection: string;
  // Omitted from the body when not given, so the server default (1) applies; the CLI never hard-codes the range (the server validates, like `--top-k`).
  context_window?: int32;
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
  scope_filter?: string;
  acl_context: boolean;
  // Transport options (exempt in C10).
  json: boolean;
  api_url: string;
  api_key?: string;
  // No `--graph-mode` and no multi-collection option exist on this command.
}

// Human output on stdout: each hit as `search` prints it (rank, score, collection, source path, excerpt: whitespace collapsed, cut at the shared 200-character constant), then its neighbours as indented excerpt lines marked before or after: the before lines first, then the after lines, each side in the response's ascending chunk order (UserManual/60_searching.md line 156). The chunk id is not printed and a neighbour's excerpt uses the same shared helper as a hit's. The exact indent width and marker words are the developer's choice and are pinned by the unit test of S36. Every server-supplied string printed is cleaned (non-printable characters replaced by spaces, `_clean` in cli/_helpers.py).
model NeighbourLine {
  side: NeighbourSide;
  excerpt: string;
}

enum NeighbourSide {
  before,
  after,
}

model HitBlock {
  rank: int32;
  score: float64;
  collection: string;
  source_path: string;
  excerpt: string;
  neighbours: NeighbourLine[];
}

enum CliExit {
  ok,
  failure,
  usage_error,
}

model CliSearchWithContextOutcome {
  exit: CliExit;
  // --json: the REST body verbatim in the repo's format (indent 2, sorted keys), full text, neighbours included.
  json_body?: string;
  // Human mode, stdout. An empty result prints `No results found.` and exits 0.
  hits?: HitBlock[];
  // Stderr notes after the results, as `search`: `Note: <expansion_warning>` when present. There are no excluded-collection notes (single collection).
  notes?: string[];
  // Error text to stderr, exit 1: same renderings as `search` (connection refused, `Error: <detail>`, `Retry-After` suffix, bare 401, unexplained status, unexpected response shape). The shape check is the new module's own: it checks the nested item (`result`, `context_before`, `context_after`) with the shared primitives, because the shared flat check of `search` reads `results[].score` and would pass a nested item vacuously.
  stderr?: string;
}

// The request timeout reuses CLI_SEARCH_REQUEST_TIMEOUT_SECONDS (constants.py line 28: twice SEARCH_TIMEOUT_SECONDS plus a margin, 65 s), which exceeds the 30 s server budget, so the CLI reports the server's 504 rather than its own timeout. No new constant.
interface CliSearchWithContext {
  run(options: CliSearchWithContextOptions): CliSearchWithContextOutcome;
}
