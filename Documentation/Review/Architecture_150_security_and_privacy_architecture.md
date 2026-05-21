# Review: Architecture/150_security_and_privacy_architecture.md

## Summary

The document is largely accurate. Most security/privacy claims match the source. Two notable issues: the doc says the no-early-break in middleware is "labelled in the source as timing-leak mitigation (`middleware_auth.py:42`)" — the comment is at line 39, not 42, and the doc's "Empty namespace fails closed against a protected chunk" claim slightly mis-states semantics (it fails closed against any non-None ACL, including `None`? no — it correctly fails closed only when acl is not None). A few cited line numbers are imprecise. ACL precedence section omits that `parse_acl_value` has nuanced deny-all handling not summarized. Overall, the doc is well-grounded.

## Inaccuracies (numbered: quoted claim, ground truth, file:line, severity)

1. **Claim**: "this is deliberate and labelled in the source as timing-leak mitigation (`middleware_auth.py:42`)."
   **Ground truth**: The "no early exit — prevents timing leakage" comment is on line 39, not line 42. Line 42 is `if secrets.compare_digest(token, key_hex):`.
   **File**: `archon_search/server/middleware_auth.py:39`
   **Severity**: Low (line number off)

2. **Claim**: "`config.py:209–217`: `[telemetry].export_enabled = true` is logged as a warning and forced to `false`."
   **Ground truth**: The line range is approximately correct (209–217 in config.py). The warning text is "telemetry: export_enabled is reserved for a future release and will be ignored" — not exactly described, but the doc's summary is faithful.
   **File**: `archon_search/config.py:209–217`
   **Severity**: None (verified)

3. **Claim**: "On Windows, the chmod step is skipped (`key_manager.py:136–138`)."
   **Ground truth**: Correct — `_chmod_600` returns early on `sys.platform == "win32"` at lines 136–138.
   **File**: `archon_search/key_manager.py:135–138`
   **Severity**: None (verified)

4. **Claim**: "Empty namespace (`""`) always fails closed against a protected chunk."
   **Ground truth**: `is_acl_allowed` returns `False` for empty namespace only when `acl is not None`. If `acl is None` (open chunk), an empty namespace returns `True`. The doc says "against a protected chunk" which is technically correct but the wording could mislead.
   **File**: `archon_search/acl.py:194–198`
   **Severity**: None (technically correct)

5. **Claim**: "ACL is **per-chunk**, stored as a nullable `list<utf8>` column on every chunk table (added by `store.py::migrate_acl`)."
   **Ground truth**: Verified — `migrate_acl` exists at `store.py:321`, and schema field `acl` is part of `_schema`.
   **File**: `archon_search/store.py:120, 321`
   **Severity**: None (verified)

6. **Claim**: Front-matter `_acl` "highest precedence" with "If both exist, front-matter wins and a warning is logged."
   **Ground truth**: Verified — `resolve_acl` checks `front_matter_acl is not None` first, warns if a sidecar also exists, and returns the parsed FM value. Sidecar is consulted only when front-matter is absent.
   **File**: `archon_search/acl.py:217–244`
   **Severity**: None (verified)

7. **Claim**: "Invalid types, non-string list elements, invalid namespace names, ACL sidecars larger than `_ACL_SIDECAR_MAX_BYTES = 65536`, symlinked sidecars, non-UTF-8 content — all degrade to `None` (open) with a warning."
   **Ground truth**: Verified for all enumerated cases. `_ACL_SIDECAR_MAX_BYTES = 65536` confirmed at `acl.py:11`. Symlink sidecars are ignored (line 132–134), oversized sidecars ignored (137–143), non-UTF-8 ignored (146–149). The reserved word "deny-all" cannot be a namespace identifier (`is_acl_namespace_valid`, line 18).
   Minor nuance not captured: when `_acl` is a YAML list with `[]`, the function returns `[]` (deny-all sentinel) — not `None`. The doc's "[]" row in the table is correct, so this is fine.
   **File**: `archon_search/acl.py:11, 18, 119–178`
   **Severity**: None (verified)

8. **Claim**: "The three factories ... are keyword-only and **none of them accepts a `query` parameter**."
   **Ground truth**: Verified — `from_search_tool_result`, `from_route_response`, `from_error` are all keyword-only (`*,`) and accept no `query` param. `DOCUMENTED_SCHEMA_FIELDS` lacks any `query` entry.
   **File**: `archon_search/telemetry/entry.py:39–145`
   **Severity**: None (verified)

9. **Claim**: "`model_config = ConfigDict(extra="forbid", frozen=True)`."
   **Ground truth**: Verified at `entry.py:58`.
   **File**: `archon_search/telemetry/entry.py:58`
   **Severity**: None (verified)

10. **Claim**: "`telemetry/pruner.py::Pruner` deletes `*.jsonl` files older than `[telemetry].retention_days` (default 30) on a 24-hour interval. Today's file is never deleted."
    **Ground truth**: Default is 30 (`config.py:22`). Pruner explicitly skips today's file (`pruner.py:26, 44`). The "24-hour interval" claim — verified by inspecting pruner: pruner has a `prune` method that deletes files where `file_date < cutoff` and `file_date != now`. Whether it runs on a 24-hour schedule is determined by callers; not contradicted but not directly verified from the snippet read. Defer to caller for the 24h cadence claim — could be inaccurate.
    **File**: `archon_search/telemetry/pruner.py:14–48`, `archon_search/config.py:22`
    **Severity**: Low (the 24h schedule is a scheduling claim not directly confirmed in pruner.py itself)

11. **Claim**: "the directory is mode-`0700`-by-default on macOS/Linux user homes."
    **Ground truth**: This refers to the user's home directory permissions on Unix systems — environmental, not enforced by archon-search code. No code makes `~/.archon-search/` 0700 explicitly; `_generate_and_write` uses `os.makedirs(KEY_FILE.parent, exist_ok=True)` (default mode ~0777 minus umask), so the directory mode is umask-dependent, not 0700.
    **File**: `archon_search/key_manager.py:83`
    **Severity**: Low (statement is about the home dir, not archon's dir, but is misleading)

12. **Claim**: "There is no in-process rotation API."
    **Ground truth**: Verified — no rotation route exists in routes_state.py or middleware_auth.py.
    **Severity**: None (verified)

13. **Claim**: "The server binds to `127.0.0.1` by default (`[server].host`)."
    **Ground truth**: Verified at `config.py:30` (`host: str = "127.0.0.1"`).
    **Severity**: None (verified)

14. **Claim**: "ARCHON_SEARCH_API_KEY env var — must be a non-empty lowercase hex string (`^[0-9a-f]+$`)."
    **Ground truth**: Verified at `key_manager.py:22` (`_HEX_RE = re.compile(r"^[0-9a-f]+$")`) and `_validate_key` at line 78–79.
    **Severity**: None (verified)

15. **Claim**: "`secrets.token_hex(32)` writes a 64-char hex key ... with `O_EXCL` + mode `0600`, then `os.replace`s it into place."
    **Ground truth**: Verified at `key_manager.py:85, 89, 122`.
    **Severity**: None (verified)

16. **Claim**: Unauthenticated endpoints "`/docs`, `/openapi.json`, and `/redoc` are also exempted by `_EXEMPT_PATHS`".
    **Ground truth**: Verified — `_EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})` at `middleware_auth.py:16`.
    **Severity**: None (verified)

17. **Claim**: "the token is compared with `secrets.compare_digest` against (a) every entry in the per-namespace key map and (b) the default API key."
    **Ground truth**: Verified — middleware iterates `self._namespaces` first then falls back to `self._api_key`, both via `secrets.compare_digest`.
    **File**: `middleware_auth.py:41–47`
    **Severity**: None (verified)

18. **Claim**: "Failure → `401` with `WWW-Authenticate: Bearer`, empty body."
    **Ground truth**: Verified — `Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})` with no body. (`middleware_auth.py:32–35, 50–53`.)
    **Severity**: None (verified)

## Verified claims

- Trust boundary, threat model, default-open ACL semantics
- Key bootstrap priority (env var → file → auto-gen)
- Key file default path `~/.archon-search/.search.env`; override via `ARCHON_SEARCH_KEY_FILE`
- File mode 0600 with retighten attempt on read; permission errors logged not fatal
- Windows chmod skip
- Bearer auth enforcement, exempt paths, no-early-break loop, `compare_digest` use, 401 with `WWW-Authenticate: Bearer`
- ACL table semantics: None → open, `[]` → deny-all, list → allow-list, case-sensitive
- ACL sidecar size cap (65536), symlink rejection, UTF-8 validation, fail-open on parse errors
- `resolve_acl` precedence: front-matter > sidecar; both → warning + front-matter wins
- `is_acl_namespace_valid` excludes the literal `deny-all`
- Telemetry frozen Pydantic model with `extra="forbid"`
- `DOCUMENTED_SCHEMA_FIELDS` lacks a `query` field; the three factories never accept a `query` kwarg
- `export_enabled = true` coerced to `false` with warning (`config.py:209–217`)
- Pruner deletes by filename date, never deletes today's file; default retention_days = 30
- Writer sink is `~/.archon-search/search-logs/<date>.jsonl` (`config.py:24`)
- `source_path` is stored in clear in the LanceDB chunk schema (`store.py:129`)
- Server binds `127.0.0.1` by default (`config.py:30`)

## Unverifiable / ambiguous

- The "24-hour interval" pruning cadence (claim about scheduler frequency). The pruner code only defines `prune()`; its invocation cadence depends on the caller, which was not inspected here.
- "the directory is mode-`0700`-by-default on macOS/Linux user homes" — this is a claim about user home-dir permissions, not enforced by archon-search code; on many systems `~` is 0755, not 0700. Slight misleading framing.
- The doc's reference to "`middleware_auth.py:42`" — actual labelling comment is at line 39; trivial line-number drift.
- `_ACL_SIDECAR_MAX_BYTES` is a module-level constant in `acl.py` (line 11), not exposed via config; the doc correctly notes the numeric value.
- The doc reference "labelled ... as timing-leak mitigation" — comment text is "no early exit — prevents timing leakage" (not exactly "timing-leak mitigation", but the meaning matches).
