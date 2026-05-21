# Feature Brief: A5 — Ingest Hardening (Input Safety + SQL Builder Defense-in-Depth)

> **Sequencing**: Ships LAST in the sequence (A1→A2→A3→A4→A5). Depends on A2 for the shared SQL-quoting helper (`_sql_quote_str` in `archon_search/store_filters.py`).
>
> Roadmap reference: `Documentation/Backlog/03_world_class_roadmap.md` Phase A item A5. Two independent hardening tasks share this brief: **A5a** (path-input safety on ingest entry points) and **A5b** (replace f-string SQL builders in `store.py` with defense-in-depth quoting/binding). Plan-maker should produce two independent task lists (one per half) from this brief — they share no code, no tests, no risk profile, and either can ship alone.

## Problem

Two robustness gaps in the ingest and storage layer:

1. **A5a — Path safety.** HTTP and MCP ingest endpoints (`POST /collections`, `POST /jobs/ingest`, MCP `ingest_file`, MCP `ingest_directory`) accept arbitrary path strings with no validation. A request whose `path` contains `..` segments is followed silently — indexing files the user did not intend. **Scope clarity:** A5a only blocks `..` traversal (plus trivial junk inputs: empty, whitespace-only, NUL byte). It does NOT prevent `POST /ingest {"path":"/etc/passwd"}`, and it does NOT independently check symlinks — the existing `pipeline.py` (~line 208) / `sync.py` (~line 458) symlink-skip during filesystem walks remains the only symlink defence. Symlink-escape proper requires an `allowed_dirs` root to be defined against, which is out of scope here (see Future Iterations). The roadmap's "symlink-escape" wording is honestly narrowed in this brief.
2. **A5b — SQL builder defense-in-depth.** `archon_search/store.py` builds five `where()` / `delete()` / `count_rows()` clauses via f-strings interpolating identifiers: in `delete_collection_meta` (around line 291), `update_collection_meta` (around line 380), `delete_document` (count + delete, around lines 538 and 541), and `fetch_adjacent_chunks` (the IN-clause around line 633). Every interpolated value is already regex-gated upstream — `name` by `_COLLECTION_RE` (`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`), `namespace` by `_validate_namespace`/`_NAMESPACE_RE` (same shape), `doc_id` by `_DOC_ID_RE` (`^[a-f0-9]{64}$`), and `chunk_id` in `fetch_adjacent_chunks` constructed from a validated `doc_id` plus `i:06d` integer formatting. **This is not a correctness bug today**: the regex gates make all five sites injection-safe by construction. The risk is structural — a future contributor who relaxes one of these regexes (e.g. to allow `.` in collection names, or to expand namespace charset) silently introduces SQL injection because the safety lives at the call site, not at the SQL boundary. A5b makes the SQL boundary itself safe (belt-and-braces with the existing regex gates), and documents the regex as the security boundary inline at each site.

## Goal

- **A5a:** HTTP and MCP ingest endpoints reject unsafe paths before any filesystem read.
  - HTTP returns `HTTP 400` with `{"detail": "path is unsafe: <reason>"}` via `HTTPException(400, detail=...)`, conforming to the existing `ErrorDetail` model used across routes.
  - MCP returns `McpErrorResponse(error="path is unsafe: <LLM-readable phrase>", code="path_unsafe")` — the existing MCP error envelope shape (`{"error": str, "code": str}` from `McpErrorResponse` in `server/mcp.py`). The `error` field must be LLM-readable prose, not a terse code (e.g. `"path is unsafe: input contains '..' segment — use an absolute path without traversal"`).
- **A5b:** No f-string-interpolated `where()` / `delete()` / `count_rows()` builders remain in `store.py`. Each remediated site documents the regex gate inline (`# safe: name validated by _COLLECTION_RE`) and uses either LanceDB's native parameter binding (if available in the pinned version) or a single private quoting helper in `store.py`. **The upstream regex gates are NOT being relaxed** — existing tests like `test_store_delete_document_injection_safe` must continue to pass unchanged because `_DOC_ID_RE` still rejects the malicious doc_id at the validator level.

Observable success: malicious-path integration tests return 400 with the conforming envelope; legitimate path tests (including deep absolute paths with spaces and unicode such as `/home/user/My Documents/notes.md`) still ingest successfully; existing store-injection tests stay green; coverage stays ≥ 85%.

## Users & Context

- **Operators / power users** running scripted ingest pipelines — A5a catches typos and accidental traversal.
- **MCP clients** (e.g. Claude Desktop) where an LLM constructs paths — A5a is a guardrail against model mistakes.
- **Future contributors** maintaining `store.py` — A5b ensures that relaxing an identifier regex never silently re-enables SQL injection.
- **Threat model.** Today the deployment model is local single-user (`Documentation/Architecture/150_security_and_privacy_architecture.md`). A5a is defense-in-depth against accidental misuse, not a CVE patch. A5b is structural future-proofing, not a bug fix.

## Core Flow

### Process (TDD — mandatory)

Per project `CLAUDE.md`, tests-first is required. For each half, plan-maker must order tasks so a failing test exists **and runs red** before the implementation it covers (happy path first, then edge cases). Tests are authored and committed strictly before the code change they exercise — bundling test and implementation in one commit defeats the red-green-refactor discipline.

### A5a — Path validation

1. Introduce a shared callable `validate_ingest_path(raw: str) -> Path` in a small validation module (suggested: `archon_search/_path_safety.py` — top-level package, importable by both `server/` and any future caller without circular-import risk). This is the single source of truth invoked by **both** HTTP Pydantic validators (for `POST /collections`, `POST /jobs/ingest` request bodies) **and** MCP tool bodies (`ingest_file`, `ingest_directory`) which take `path: str` as plain function arguments and have no Pydantic model.
2. **Error contract:** the validator **raises** a typed `PathUnsafeError(reason: str)` on rejection. HTTP callers catch and translate to `HTTPException(400, detail=f"path is unsafe: {e.reason}")`. MCP callers catch and translate to `McpErrorResponse(error="path is unsafe: <LLM-readable phrase>", code="path_unsafe")`. Choosing raise-and-catch (rather than `Path | None`) keeps every callsite's accept path one line.
3. **Return value (accept path):** the validator returns the **resolved** absolute `Path` (`Path(raw).expanduser().resolve(strict=False)`). All callers MUST use this returned value rather than re-resolving from `raw`, to ensure the value validated is the value used.
4. Validator behaviour:
   a. Reject if `Path(raw).parts` contains any element equal to `..` (literal traversal segment — not a substring match, so `..backup` as a real directory name is accepted).
   b. Reject empty string, whitespace-only, or a path containing a NUL byte (`\x00`).
   c. **Reject non-absolute paths** after `expanduser()`. The server runs as a daemon with unpredictable CWD (often `/` under launchd/systemd); accepting relative paths produces silently inconsistent behaviour. Require `Path(raw).expanduser().is_absolute()`.
   d. Non-existent paths pass through to the existing "not found" handling — the validator does not pre-check existence.
   e. **Symlinks are NOT independently checked by A5a.** Without an `allowed_dirs` root (out of scope — see Future Iterations), there is no defensible boundary against which a "symlink escape" can be defined. A `..`-free path that resolves through a symlink to anywhere on the filesystem is no worse than the same absolute path passed directly (which A5a does NOT block, by design — see Problem). The existing symlink-skip in `pipeline.py` and `sync.py` continues to apply during the filesystem walk; A5a leaves it untouched. Roadmap text mentions "symlink-escape" — this brief honestly narrows that to "the `..` half" and defers symlink-escape proper to the `allowed_dirs` feature.
5. **Auth ordering:** a 400 `path_unsafe` fires **after** authentication. A 401 always takes precedence; integration tests must include valid `auth_headers` (use the conftest fixture).
6. **OpenAPI:** each affected route's `responses=` map gains a `400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"}` entry. Adding (not overwriting) the entry preserves existing `401`/`409`/etc. declarations. Per Doc 520 this is required for the change to be considered shipped.
7. **MCP return annotations.** The existing MCP tool functions in `server/mcp.py` are annotated `-> dict[str, Any]` (or `list[...]`). Adding a `McpErrorResponse` return on the error path is consistent with this loose annotation. Plan-maker should NOT tighten the annotation to a union as part of A5a — that is a separate cleanup.
8. On acceptance: ingest proceeds unchanged using the validator's returned resolved `Path`.

### A5b — SQL builder defense-in-depth

1. Probe LanceDB's pinned Python API for parameterised `where()` / `delete()` (bind, `?`, `@name`, or equivalent).
2. **If supported:** migrate all five f-string sites to native binds.
3. **If not supported:** introduce private helpers in `store.py` that **return fully-quoted SQL fragments as plain strings** — at minimum:
   - `_where_eq(col: str, value: str) -> str` → `"col = 'escaped_value'"` (covers lines 291, 380, 538, 541 — also composed with ` AND ` for the compound `name = '...' AND namespace = '...'` at line 291; callers concatenate with a literal `" AND "` string, no f-strings).
   - `_where_in(col: str, values: Iterable[str]) -> str` → `"col IN ('v1', 'v2', ...)"` (covers the IN-clause at line 633 in `fetch_adjacent_chunks`).
   Both use single-quote doubling for escaping. Callsites read `.where(_where_eq("name", name) + " AND " + _where_eq("namespace", ns))` — **no f-string ever wraps a SQL method call**, which keeps the CI guard's job simple. A bare `_quote_literal` returning just the quoted value would still tempt `f".where(f"name = {_quote_literal(name)}")"`-style regressions; helpers must return full predicates.
4. At each remediated site, add an inline comment naming the regex gate that is the primary security boundary (e.g. `# name validated by _COLLECTION_RE; quoting is defense-in-depth`).
5. **CI guard.** Add a grep-based pre-commit hook (or `pytest` test-time scan) that fails if `archon_search/store.py` contains any of the patterns `\.where\(\s*f["']`, `\.delete\(\s*f["']`, `\.count_rows\(\s*f["']` — i.e. an f-string passed **directly** as the first argument to one of those methods. Scope is restricted to `store.py` (or anywhere `lancedb` is imported, if plan-maker prefers AST-based scoping) to avoid false positives on `@router.delete(...)` decorators in `server/routes_*.py` and on unrelated f-strings elsewhere. The guard is what prevents regression; without it the structural fix erodes.

## In Scope

- A5a `validate_ingest_path` callable, wired into HTTP `POST /collections`, HTTP `POST /jobs/ingest`, MCP `ingest_file`, MCP `ingest_directory`.
- HTTP error path conforming to `ErrorDetail` (`{"detail": str}`) with `HTTPException(400, ...)` and `responses={400: {"model": ErrorDetail}, ...}` on each affected route.
- MCP error path returning `McpErrorResponse(error=<phrase>, code="path_unsafe")`.
- A5b: replace f-string SQL builders at the five sites listed in the Problem section. Plan-maker re-greps `store.py` to catch any drift before implementation.
- Inline comments at each remediated site naming the existing regex gate.
- CI/static guard preventing reintroduction of f-string SQL builders.
- Unit tests for the validator and the quoting/binding layer.
- Integration tests with adversarial payloads at each HTTP/MCP entry point.
- A `BREAKING.md` Changelog entry under the current `### [next release]` heading, labelled as a behaviour change (matching the file's existing format — no new top-level section), noting that MCP `ingest_file`/`ingest_directory` calls with `..`-containing paths, which previously succeeded silently, now return `McpErrorResponse(code="path_unsafe")`.

## Out of Scope

- **Debt-register entries** (`VAL-*`, `RP-*`, `SEC-*`). Born-resolved entries in `530_technical_debt_refactoring_roadmap.md` are noise. PR description, commit message, and roadmap checkmarks provide sufficient traceability. **Note:** `03_world_class_roadmap.md` text for A5 references `VAL-1` and `RP-5` as forward IDs. This brief supersedes those references with checkmark-only tracking; the roadmap text should be amended (in the same PR as A5a or A5b) to drop the forward IDs or note that they were never created.
- **CLI `archon-search ingest`** — local trusted user; consistent with the existing threat model.
- **`allowed_dirs` config knob** — limiting ingest to configured roots is a separate, larger feature (see Future Iterations). A5a explicitly does NOT close that gap.
- **File size / batch size limits** — orthogonal hardening item.
- **Encoding/MIME validation** — out of scope; current `errors="replace"` behaviour is intentional.
- **Rate limiting on ingest endpoints** — orthogonal; gateway concern.
- **Glob-pattern depth caps** for `ingest_directory` — separate DoS concern.

## Key Decisions

- **Two halves in one brief, two independent task lists.** A5a and A5b share no code, no tests, no risk profile. Plan-maker must split them.
- **A5b is reframed as defense-in-depth, not a bug fix.** The existing regex gates already make every interpolated value injection-safe. The change is structural: move the safety to the SQL boundary so a future regex relaxation cannot reintroduce the vulnerability.
- **HTTP 400 + `ErrorDetail` shape**, matching every other route's error envelope. No invented `{"error":"path_unsafe","reason":...}` shape — that contradicts Docs 140 and 520.
- **MCP uses the existing `McpErrorResponse` envelope** (`error` + `code`), with `error` as LLM-readable prose.
- **One shared `validate_ingest_path` callable** for HTTP and MCP. MCP tools take plain `str` args (no Pydantic model), so a Pydantic-only validator would miss them.
- **CI guard for the f-string regression** is part of the acceptance criteria. Without it the structural fix erodes silently.
- **No new debt-register IDs.** Roadmap checkmark + PR is enough.

## Edge Cases & Constraints

- **Absolute paths under `/tmp`, `~/`, or user data dirs** — accepted; legitimate.
- **Legitimate deep absolute paths with spaces and unicode** (e.g. `/home/user/My Documents/notes.md`) — must still ingest. This is an explicit acceptance test, not an aspiration.
- **`..` as a substring of a real directory name** (`/data/..backup/file.md`) — accepted; the validator checks `Path.parts` element equality, not substring.
- **Symlinks** — A5a does NOT check symlinks (see Core Flow §4d). Any `..`-free path that resolves through a symlink is accepted by the validator; the existing `pipeline.py` / `sync.py` symlink-skip during walks is the only existing defence and remains in place. Symlink-escape proper is deferred to the `allowed_dirs` feature.
- **Non-existent paths** — pass-through to the existing ingest "not found" error.
- **Windows path separators** — `Path.parts` normalises; checks operate on parts, not the raw string.
- **MCP behaviour change.** Existing MCP clients that pass `..`-containing paths currently succeed silently and now receive an error dict. This is a behaviour change (not a contract break — the dict type is unchanged) and must be recorded in `BREAKING.md` under "Behaviour Changes".
- **HTTP behaviour.** A new `400` response that did not previously exist; additive, not breaking.
- **Performance.** Validator runs once per request. Quoting/binding adds at most a per-site dict lookup. Neither is on the hot path.
- **LanceDB SQL dialect.** Single-quote escaping convention must be verified against the pinned version before shipping the quoting helper.
- **Existing tests.** `tests/test_store.py::test_store_delete_document_injection_safe` and similar rely on the upstream regex gate rejecting `' OR '1'='1`. A5b must not relax those gates, so these tests remain green unchanged.

## Test matrix (validator)

Plan-maker must produce tests covering at minimum:

- Reject: literal `..` standalone; `..` mid-path (`/foo/../bar`); `..` at start (`../foo`); empty string; whitespace-only; NUL byte in input; **relative paths** (`./foo`, `foo/bar`, just `.`).
- Accept (must still ingest, do NOT regress): legitimate deep absolute path with spaces and unicode (`/home/user/My Documents/notes.md`); path where `..backup` is a real directory-name substring; `~/foo` (expands to an absolute path via `expanduser()`); any symlinked path that contains no `..` segments (A5a does not check symlinks — see Core Flow §4e); non-existent absolute path (passes validator, fails later in ingest); trailing slash; very long path (within OS limits).
- A5b SQL helpers — call the helpers **directly** in unit tests, bypassing the upstream regex gates. Pass adversarial values (`O'Brien`, `' OR 1=1 --`, `\x00`, `"; DROP TABLE--`, multi-byte unicode, empty string). Assert the helper produces a well-formed SQL fragment with the value safely quoted. This documents the belt-and-braces layering even though the regex gates make injection unreachable in production.
- **Test layer assignment:** validator unit tests live in `tests/test_path_safety.py` (default tier, no marker). HTTP/MCP integration tests live alongside existing route tests and use FastAPI `TestClient` (default tier). A5b helper unit tests live in `tests/test_store.py` (default tier). Any test that touches real LanceDB stays behind `-m integration`.

Path-validator integration tests use FastAPI `TestClient` (default tier, not behind a marker). Tests that touch real LanceDB stay behind `-m integration`.

## Open Questions

- **Does LanceDB expose parameterised `where()` in the pinned version?** Probe at plan time; this decides A5b's mechanism (binds vs. internal predicate-builder helper).
- **Form of the CI guard.** Pre-commit grep hook, `pytest` test-time scan, or AST-based check — plan-maker picks based on existing tooling. Acceptance criterion (the three patterns above, scoped to `store.py`) is fixed; mechanism is not.
- **Roadmap completion semantics.** A5 in `03_world_class_roadmap.md` is one item with two halves. Plan-maker proposes whether to split the roadmap entry into A5a/A5b (recommended) or check A5 only when both PRs merge.
- **Symlink-escape beyond A5a.** Out of scope here; tracked under the future `allowed_dirs` feature. No open question for this brief.

## Future Iterations

- `allowed_dirs` config knob constraining ingest to a configured root set.
- File-size and batch-size caps on ingest endpoints.
- Glob-pattern depth and breadth limits for `ingest_directory`.
- Dedicated `-m security` pytest marker once adversarial test count justifies it.
- Extending path validation to the CLI if it becomes a remote-exec surface.

## Recommendation

Ship as two independent PRs (A5a, A5b — either order). A5a is the higher-user-value half: it converts a silent footgun into an explicit, LLM-readable rejection at the surface most likely to mis-construct paths (MCP). A5b is lower-urgency but cheap, and its CI-guard half is what makes the fix durable. Frame A5b honestly to reviewers as defense-in-depth — the regex gates already prevent injection today, and the value is preventing a regression when a future contributor expands an identifier charset. Do not skip the integration tests at each HTTP/MCP entry point; unit tests alone will miss wiring mistakes, which are the most likely failure mode here.
