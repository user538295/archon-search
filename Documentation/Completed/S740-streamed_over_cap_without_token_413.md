## Bug: A streamed (chunked) body above `max_request_body_mb` gets 413 `request_body_too_large`

**ID**: S740-streamed_over_cap_without_token_413
**Scenario**: S740
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S740-streamed_over_cap_without_token_413.md — streamed 3 MiB (cap 1 MiB, NO token) returned 401 body=b'', expected 413 request_body_too_large before authentication (30_configuration.md:54; 01_threat_model.md:61)

### What should happen
- Step 1: HTTP 413 with code `request_body_too_large`.
- Step 2: HTTP 413 with code `request_body_too_large` — the check runs before authentication.
- Step 3: not 413 (under the cap; the handler answers with a validation error).

### Steps to reproduce
1. `POST /ingest` with `Transfer-Encoding: chunked` and three 1 MiB chunks, `Authorization: Bearer $KEY`.
2. Same, without an `Authorization` header.
3. `POST /ingest` with `Transfer-Encoding: chunked` and one tiny chunk `{}`, with the token.

### Evidence
```
E   Failed: bug: bugs/S740-streamed_over_cap_without_token_413.md — streamed 3 MiB (cap 1 MiB, NO token) returned 401 body=b'', expected 413 request_body_too_large before authentication (30_configuration.md:54; 01_threat_model.md:61)
```
