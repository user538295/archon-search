// C8 — Shared CLI request and render helpers (Presentation, internal to cli/).
// ADR 20 rule 3 (tests/test_layer_import_boundary.py): a cli/ file imports no other cli/ module except cli/_helpers. The new command therefore cannot import from cli/search.py, so the pieces both commands use move to cli/_helpers.py. Follows 00_service_layer_foundation-c5-cli-shared-helpers.tsp.
namespace CliSharedHelpersContract;

// Move from cli/search.py into cli/_helpers.py (names as they are today; the leading underscore convention of _helpers stays). These helpers work on one result dict, one response or one value, so they fit the nested item of the new route as well as the flat `search` body.
enum MovedHelper {
  retry_after_note,
  error_message,
  is_list_of_objects,
  is_printable_score,
  excerpt,
  result_line,
  echo_notes,
  given,
}

// Stay in cli/search.py: `_shape_problem`. It reads the flat `results[]` with `score` and `excluded_collections` of `search`; run on the nested `{result, context_before, context_after}` item it would pass vacuously (the score defaults to 0.0), so the new module writes its own nested shape check from the moved primitives (C7). `_echo_notes` moves because it tolerates an absent `excluded_collections` and prints the `expansion_warning` note both commands need.
// Constants that move with them: the excerpt cut (200 characters), the bare-401 message, the unexplained-error message.
// Not moved: the command functions, their options and the `httpx.post` call. The POST stays in each command module: the four unit-test files tests/cli/test_search_fe3.py to fe6.py patch `archon_search.cli.search.httpx.post` and keep working; the integration tests reach the command through the `cli_against_app` bridge (tests/_cli_testclient_bridge.py) and the smoke tests run a subprocess, so none of those patches it. The new module gets its own target `archon_search.cli.search_with_context.httpx.post`. The request body is built per module (the new body adds `context_window` and has no `collections` or `graph_mode`); only `_given` is shared.
// Importers: the tests import the `search` command from cli.search and one test reads a private name that moves: tests/cli/test_search_fe3.py line 78 reads `search_mod._EXCERPT_MAX_CHARS` (the module is imported at line 15), so that test is re-pointed to cli/_helpers when the constant moves (tests/smoke/test_cli_search.py keeps its own copy, line 31). No other test reads a moved name; grep again before moving.
interface CliSharedHelpers {
  render_error(status: int32, body_is_json_object: boolean): string;
  render_hit_line(rank: int32, score: float64, collection: string, source_path: string, text: string): string;
}
