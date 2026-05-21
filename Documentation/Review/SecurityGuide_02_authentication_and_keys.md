# Review: SecurityGuide/02_authentication_and_keys.md

Reviewed against: `archon_search/key_manager.py`, `archon_search/server/middleware_auth.py`, `archon_search/server/app.py`, `archon_search/config.py`, `archon_search/constants.py`.

## Summary

The document is largely accurate. The authentication model, bootstrap order, file-permission handling, env-var validation regex, exempt paths, and middleware behavior all match source. Most cited line numbers are within 1–2 of the actual locations. A few minor inaccuracies and one ambiguous claim are flagged below; no statement was found that misrepresents behavior in a security-relevant way.

## Inaccuracies (numbered)

1. **Line `middleware_auth.py:41–43` for the per-namespace loop is off-by-one on the start.** The loop spans lines 40–43 (line 40 is `for key_hex, ns in self._namespaces.items():`). Trivial drift.

2. **Line `key_manager.py:54–59` for the re-tighten block is off-by-one on the start.** The `try:` opening the stat/chmod block is line 54, but the actual `mode = os.stat(...)` check begins at line 55 and the chmod call is line 57. The cited range is acceptable but the doc says the loader chmods back on mismatch — confirmed; failure is swallowed in the bare `except OSError: pass` (line 58–59), which the doc calls "logged, not fatal." **Not logged.** It is silently swallowed (`except OSError: pass`). The doc’s "Failure is logged, not fatal" is inaccurate — failures are silently ignored, with no log emission.

3. **Line `key_manager.py:89` for creation mode is correct**, but the doc's description "`0600` enforced via `O_EXCL` open with explicit mode" is slightly misleading: `O_EXCL` enforces exclusivity, not the mode. The `0o600` argument to `os.open` enforces the mode (subject to umask). The doc conflates the two flags. The final `_chmod_600(KEY_FILE)` at line 131 is what guarantees mode after `os.replace`, since `os.replace` does not re-apply the temp-file mode in a way independent of umask. The doc omits this final chmod.

4. **`app.py:65–66` for OpenAPI exempt-path filtering is incorrect.** The actual block is lines 65–73 (the `for path, path_item in schema.get("paths", {}).items()` loop, with the `if path in _EXEMPT_PATHS: continue` at line 68). Line 65 is a comment line.

5. **The claim that `ARCHON_SEARCH_KEY_FILE` "redirects" / "can be overridden" implies dynamic resolution.** In source, `ARCHON_SEARCH_KEY_FILE` is read at module import time (`key_manager.py:14`) and frozen into the module-global `KEY_FILE`. Setting it after import has no effect. Minor but worth noting for ops who set it mid-process.

6. **"`ARCHON_SEARCH_API_KEY` … Bypasses the file entirely."** Accurate as written for the load path, but the doc earlier in §"Key bootstrap" item 1 says "Invalid values are logged as a warning and ignored; the loader falls through." Falls through to the *file*, then to *auto-generation* — confirmed by `load_or_generate_key()` lines 25–36. Doc table cell "Invalid → warning and fall-through" is correct but the prose elsewhere ("Bypasses the file entirely") only applies to *valid* env values. Minor wording inconsistency, not a behavior error.

7. **"Keys are validated only for type at config load (string-to-string); the format check (`^[0-9a-f]+$`) is not currently re-applied at this layer."** Verified — `config.py:227–232` only checks `isinstance(k, str) and isinstance(v, str)`. The further claim "invalid hex will simply never match a token, with no early diagnostic" is correct: there is no log line on non-hex namespace keys.

8. **"`middleware_auth.py:62` is only on success"** — verified accurate; the `logger.debug("auth ok: ...")` at line 62 fires only on the success path. The doc claim "401s are not separately recorded" is also accurate: the two `return Response(status_code=401, ...)` paths emit no log.

9. **Doc says `_chmod_600` "skips chmod on `win32` and logs 'permission check skipped'".** Verified — `logger.info("permission check skipped on Windows")` at line 137. Cited range `135–138` is correct.

10. **§"Manual rotation procedure" step 3 claim: "both keys remain valid only if both entries remain in the file"** — verified by middleware loop logic (any token matching any `[namespaces]` entry is accepted), but note that the default key from `~/.archon-search/.search.env` is *also* always valid as long as it is the loaded default; the doc's procedure does not address concurrent validity of old + new *default* keys (the file holds only one default at a time).

11. **§"Verifying authentication is working" uses port `8765`.** Not verified here; this is a config/default value (likely from `config.py` defaults) — the user should verify against `SearchConfig` defaults if accuracy matters. Flagging as unverified rather than incorrect.

## Verified claims

- `_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}` — `middleware_auth.py:16`. Confirmed.
- 401 returned with `WWW-Authenticate: Bearer` header and empty body — `middleware_auth.py:32–35, 49–53`. Confirmed.
- `secrets.compare_digest` used for both per-namespace and default comparison — `middleware_auth.py:42, 46`. Confirmed.
- Per-namespace loop iterates without early `break` — `middleware_auth.py:43` comment `# no break`. Confirmed.
- Default-key match stamps `DEFAULT_NAMESPACE` ("default") — `middleware_auth.py:47`, `constants.py:12`. Confirmed.
- `_validate_namespace` called at request time, returns 500 on invalid — `middleware_auth.py:55–59`. Confirmed.
- Regex `^[0-9a-f]+$` for env-var key validation — `key_manager.py:22, 79`. Confirmed.
- Env-var invalid → warning + fall-through — `key_manager.py:43–45`. Confirmed (`logger.warning`).
- Resolution order: env → file → auto-generate — `key_manager.py:25–36`. Confirmed.
- Key file path: `~/.archon-search/.search.env`, overridable via `ARCHON_SEARCH_KEY_FILE` — `key_manager.py:14–19`. Confirmed.
- File line format `ARCHON_SEARCH_API_KEY=<hex>` — `key_manager.py:67`. Confirmed.
- `secrets.token_hex(32)` → 64 hex chars — `key_manager.py:85`. Confirmed.
- `os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)` for atomic create — `key_manager.py:89`. Confirmed.
- Temp file + `os.replace` atomic rename — `key_manager.py:84, 122`. Confirmed.
- Concurrent-write retry via `_load_from_file()` — `key_manager.py:91–114`. Confirmed.
- `os.makedirs(..., exist_ok=True)` for parent dir — `key_manager.py:83`. Confirmed.
- Startup log `"API key authentication enabled (source: %s)"` — `app.py:123`. Confirmed.
- `[namespaces]` config block parsed at `config.py:225–233`. Confirmed (type-only validation).
- Middleware wired in `app.add_middleware(APIKeyMiddleware, api_key=..., namespaces=...)` — `app.py:121`. Confirmed.
- ENV_VAR name: `ARCHON_SEARCH_API_KEY` — `key_manager.py:20`. Confirmed.
- Header parsed as `Authorization: Bearer <token>` with `split(" ", 1)` — `middleware_auth.py:29–31`. Confirmed.

## Unverifiable / ambiguous

- **"Tracked as `SEC-1` in `../Architecture/530_technical_debt_refactoring_roadmap.md` and as item **D7** in `../Backlog/03_world_class_roadmap.md`."** Not verified — these doc files were not opened.
- **Port `8765` in the curl examples** — not verified against `SearchConfig` defaults.
- **Claim that `/health` "exposes runtime data" while `/docs`, `/openapi.json`, `/redoc` are "schema only"** — semantically reasonable but not verified against `routes_health.py`.
- **"The directory `~/.archon-search/` is created with `os.makedirs(..., exist_ok=True)` and inherits the user's umask"** — first half verified (`key_manager.py:83`); the umask claim is OS-level behavior and is true by default for `os.makedirs` without an explicit `mode`, but `os.makedirs` here is called without a mode arg, so the directory ends up at `0o777 & ~umask`. Accurate.
- **"Per-namespace map adds another bearer token"** — verified semantically: the middleware accepts the per-namespace key as a token in the `Authorization: Bearer` header. Confirmed.
- **Comment at `middleware_auth.py:43` says "no break"** — the doc cites the no-early-break behavior as deliberate timing-leakage prevention. The code comment supports this; intent is corroborated.
