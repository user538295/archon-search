## Bug: Runbook 90:162 documents a 409 for POST /keys/rotate but never shows the required request body — a plain curl returns 422

**ID**: S186-keys_rotate_409_doc_omits_request_body
**Scenario**: S186
**Severity**: low
**Version**: archon-search, version 26.8.1751

### What happened
OperatorGuide/90_incident_runbook.md:162 states that with ARCHON_SEARCH_API_KEY set, POST /keys/rotate returns 409 with 'Cannot rotate: ARCHON_SEARCH_API_KEY env var is set...'. The line gives no request body and no Content-Type, so an operator following it with a plain curl (no -d) gets 422 instead: {'type': 'missing', 'loc': ['body'], 'msg': 'Field required'}. Body validation runs BEFORE the env-var check, so the documented 409 is never reached and the response text gives no hint that a body is the missing piece. Sending -d '{}' with Content-Type: application/json produces the documented 409 immediately (verified 2026-08-01 against an isolated instance with pin_key_env=True). This is a DOCUMENTATION defect, not a product defect: 90:162's own sibling line at 90:155 documents that POST /keys/rotate takes an integer grace_seconds, so the endpoint having a body schema is correct behaviour. Only the incident-runbook invocation is incomplete. Operational cost: an operator working an incident at 3am hits an unexplained validation error on the one command the runbook told them to run.

### What should happen
The runbook's env-var-set paragraph (90:162) should show a copy-paste-ready invocation that actually produces the documented 409 — i.e. include the request body and content type, e.g. curl -X POST http://127.0.0.1:8765/keys/rotate -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{}'. Alternatively, note explicitly that the body is required and that omitting it returns 422 before the env-var check.

### Steps to reproduce
1. Start a server whose process environment has ARCHON_SEARCH_API_KEY set (pinned key).
2. Follow 90_incident_runbook.md:162 literally, with no request body:
   curl -i -X POST http://127.0.0.1:8765/keys/rotate -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"
3. Observe 422, not the documented 409.
4. Repeat with the body added:
   curl -i -X POST http://127.0.0.1:8765/keys/rotate -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{}'
5. Observe the documented 409.

### Evidence
```
Without body -> HTTP 422
{'detail': [{'type': 'missing', 'loc': ['body'], 'msg': 'Field required'}]}

With -d '{}' -> HTTP 409
detail mentions ARCHON_SEARCH_API_KEY env var is set

Doc lines:
  docs/OperatorGuide/90_incident_runbook.md:155 — 'POST /keys/rotate takes integer grace_seconds' (body schema exists — product behaviour correct)
  docs/OperatorGuide/90_incident_runbook.md:162 — documents the 409 with no body shown (the gap)

Test coverage: tests/test_s186_keys_rotate_409_env_set.py (3 passed) sends -d '{}' explicitly; scenarios/s186_keys_rotate_409_env_set.md step 4 was corrected to match.
```
