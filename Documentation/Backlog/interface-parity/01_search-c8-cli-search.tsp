// C8 — `archon-search search` command (Presentation <-> REST over HTTP).
// The CLI is an HTTP client of REST: it imports nothing from services/ or server/ and builds the REST request body itself.
// Options are the command fields in kebab-case; the query is positional and `--collection` is required (a usage error otherwise).
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
  // The single include-metadata option: it fills the command's top-level include_metadata. `filters.include_metadata` has no CLI option (C9 records the waiver).
  include_metadata: boolean;
  hyde: boolean;
  rag_fusion: boolean;
  graph_mode?: string;
  scope_filter?: string;
  acl_context: boolean;
  // Transport option: maps to no command field (exempt in C9). Machine-readable output instead of the list: the response re-serialised with indent 2 and sorted keys.
  json: boolean;
  // Transport options: map to no command field (exempt in C9).
  api_url: string;
  api_key?: string;
}

// Human output on stdout: one line per result in response order. The excerpt is whitespace-collapsed and cut at one named constant (about 200 characters); every server-supplied string printed (excerpt, collection, source path, the server's detail, the notes) is stripped of control characters. There is no excerpt-length option (brief Q4).
model SearchLine {
  rank: int32;
  score: float64;
  collection: string;
  source_path: string;
  excerpt: string;
}

// Notes the human output writes to stderr after the result lines (TQ1, decided D), so stdout stays exactly one line per result and a script that counts or parses it is unaffected.
// One note carries the server's `expansion_warning` when present (a requested HyDE or RAG Fusion step did not run); one note per entry of the response's `excluded_collections` carries the collection name and its reason. No note is printed when neither is present.
// A keyword-only answer (cold embedder) prints no note: no response field reports it (an accepted limitation, plan Q2). `--json` does not print notes.
model StderrNotes {
  expansion_warning?: string;
  excluded_collections: ExcludedCollectionNote[];
}

model ExcludedCollectionNote {
  collection: string;
  reason: string;
}

enum CliExit {
  ok,
  // Server error, connection failure, missing key.
  failure,
  // A Click usage error, for example a missing `--collection`.
  usage_error,
}

model CliSearchOutcome {
  exit: CliExit;
  // --json: the response with the full text (not the excerpt), same values as the REST body. Unchanged by the notes.
  json_body?: string;
  // Human mode, stdout: one line per result; an empty result (no hits, or every collection skipped) prints `No results found.` instead and exits 0.
  lines?: SearchLine[];
  // Human mode, stderr, after the results: the notes above.
  notes?: StderrNotes;
  // Error text to stderr: connection-refused message, or the server's detail rendered from its string, list or coded shape (a bare 401 with an empty body prints the command's own fixed text).
  stderr?: string;
}

// The request timeout is one named constant in constants.py above the sum of the HyDE bound and the search budget, which are both SEARCH_TIMEOUT_SECONDS (30 s); that constant moves from services/search_budget.py to constants.py because the CLI cannot import services/. The CLI therefore reports the server's 504 rather than its own timeout.
interface CliSearch {
  run(options: CliSearchOptions): CliSearchOutcome;
}
