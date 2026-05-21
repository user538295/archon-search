# Review: SecurityGuide/05_network_exposure_and_tls.md

Verified against `archon_search/server/app.py`, `archon_search/config.py`, `archon_search/constants.py`, `archon_search/cli/start.py`, `archon_search/server/middleware_auth.py`, `tests/server/test_openapi_schema.py`, and `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`.

## Summary

The doc's substantive technical claims about defaults and posture are accurate: the server defaults to `127.0.0.1:8765`, has no TLS, uses wildcard CORS, and CORS is mounted after `APIKeyMiddleware` such that preflight `OPTIONS` bypass auth. Two inaccuracies are notable: (1) the doc claims `CORS-1` is not in the debt register, but it is present at `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md:39`; (2) the verification curl example for the preflight is missing the `Access-Control-Request-Method` header that Starlette's `CORSMiddleware` requires to recognize the request as a preflight, so the documented expected response (`200` with `Access-Control-Allow-Origin: *`) will not occur — the request will fall through to the API-key middleware and return `401`. Several minor citation imprecisions also exist.

## Inaccuracies (numbered)

1. **Line 38 — `CORS-1` claimed absent from debt register.** "This risk is not yet tracked in the debt register … `grep -n CORS` … returns no entry. The recommended tracking ID is `CORS-1`; raise it as a debt entry on the next register update." False. `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md:39` already contains a `CORS-1` row that even back-references this file. The "Related documents" bullet at line 127 ("propose new `CORS-1` entry on next register update") is also stale for the same reason.

2. **Lines 112–116 — preflight verification curl is malformed.** The example `curl -i -H "Origin: https://evil.example" -X OPTIONS http://127.0.0.1:8765/search` does not include `Access-Control-Request-Method`. Starlette's `CORSMiddleware` only treats an `OPTIONS` request as a CORS preflight (and short-circuits it with 200 + CORS headers) when both `Origin` *and* `Access-Control-Request-Method` are present. Without the latter the request passes to `APIKeyMiddleware` and returns `401 Unauthorized` (no bearer token), not `200` with `Access-Control-Allow-Origin: *`. The matching repo test (`tests/server/test_openapi_schema.py::test_cors_preflight_to_protected_endpoint_not_blocked`) sets `Access-Control-Request-Method: POST` precisely to make the preflight fire.

3. **Line 28 — wrong line citation for the preflight test.** Doc cites `tests/server/test_openapi_schema.py:80`. The relevant test `test_cors_preflight_to_protected_endpoint_not_blocked` begins at line 79; line 80 is the docstring. Minor, but the rule said "verify each".

4. **Line 26 — `run_server` line not cited.** The claim itself ("`run_server` calls `uvicorn.run(app, host=host, port=port)` with no `ssl_certfile`/`ssl_keyfile`") is correct (verified at `archon_search/server/app.py:156`), but the doc gives no line number while doing so for the CORS entry, which is an inconsistency rather than an inaccuracy. The actual call is `uvicorn.run(app, host=config.host, port=config.port)` (it reads `config.host`/`config.port`, not local `host`/`port` variables).

## Verified claims

- **Line 9 / Line 15 / Table row 1**: default `[server].host = "127.0.0.1"`. Verified at `archon_search/config.py:30` (`host: str = "127.0.0.1"`).
- **Table row 2**: default `[server].port = 8765`. Verified at `archon_search/config.py:31` (`port: int = 8765`).
- **Lines 9, 16, Table row 3**: no native TLS; `uvicorn.run` called without `ssl_certfile`/`ssl_keyfile`. Verified at `archon_search/server/app.py:156`.
- **Lines 9, 27, Table row 4**: wildcard CORS via `app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])`. Verified verbatim at `archon_search/server/app.py:122`.
- **Lines 28, 30**: `CORSMiddleware` added after `APIKeyMiddleware` (`app.py:121` then `:122`), so CORS runs first on the request path and OPTIONS preflight bypasses bearer auth. Verified at `archon_search/server/app.py:121-122` and corroborated by `tests/server/test_openapi_schema.py::test_cors_preflight_to_protected_endpoint_not_blocked`.
- **Line 40**: "no config knob for CORS in v1". Verified — `config.py` has no CORS-related fields and the values in `app.py:122` are literal.
- **Port-range validation referenced indirectly**: `config.py:136-137` enforces `1 <= port <= 65535`. Not a doc claim but supports the table row.

## Unverifiable / ambiguous

- **Line 38** also points readers at "Security surprises callouts in the project review" — that artifact is not in `Documentation/` and cannot be verified from source.
- **Line 98** references `01_threat_model.md` "Out of scope" — exists per `ls`, but the content cross-reference is out of scope for this code-grounded review.
- **Lines 51–60 (proxy recommendations)** are configuration guidance, not factual claims about `archon-search` behavior; treated as advisory and not assessed against source.
- **Line 59** "Server-side access logs are uvicorn's default and do not include the request body" — uvicorn's stock access log behavior is a third-party fact not verified against this repo.
