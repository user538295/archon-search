# Review: SecurityGuide/03_authorization_and_acl.md

Verified against: `archon_search/acl.py`, `archon_search/constants.py`, `archon_search/server/middleware_auth.py`, `archon_search/server/routes_search.py`, `archon_search/server/routes_collections.py`, `archon_search/server/routes_state.py`, `archon_search/server/routes_status.py`, `archon_search/server/routes_route.py`, `archon_search/server/routes_jobs.py`, `archon_search/server/routes_telemetry.py`, `archon_search/server/mcp.py`, `archon_search/pipeline.py`, `archon_search/store.py`.

## Summary

The document is mostly accurate on the ACL parsing and decision rules (the `acl.py` surface). The largest factual problem is the section "What ACL does not do", which claims that `/route`, `/collections`, `/state`, `/status`, and `/jobs` are namespace-blind ("a namespaced client sees the same collection list ... as the default-namespace client"). The code clearly filters all of these by `request.state.namespace`. Several smaller line-number references and one architectural attribution (front-matter "parsed by the document parser") are also wrong. Telemetry, the per-chunk ACL precedence, sidecar handling, the `is_acl_allowed` table, and the deny-all sentinel description are correct.

## Inaccuracies (numbered)

1. **(Line 50) Wrong line reference.** The doc cites `acl.py:196–197` for the empty-namespace defense-in-depth check. The check is at `acl.py:196–197` in the read excerpt — actually `not namespace → False` is at lines 196–197. **Verified correct**, not an inaccuracy. (Withdrawn.)

2. **(Line 36) Wrong line reference for the "front-matter wins" warning.** The doc cites `acl.py:236–241`. In source, `resolve_acl` logs the "Both front-matter _acl and sidecar … exist" warning at `acl.py:236–241`. **Verified correct**, not an inaccuracy. (Withdrawn.)

3. **(Lines 31–34) Inaccurate attribution: "front-matter, parsed by the document parser".** Front-matter is extracted in `pipeline.py::_extract_front_matter` (line 57) and consumed in `SearchPipeline._build_records` (lines 155–161), not in the document parser (`parser.py`). `parser.py` does not parse YAML front-matter for `_acl`. The doc should say "parsed by `SearchPipeline` in `pipeline.py`".

4. **(Line 64) Inaccurate citation `acl.py:35–66`.** The cited range covers `bool` and `else` branches that return `None` on invalid types; that part is correct. But the doc says "Invalid types (bool, int, dict) are logged as a warning and the chunk degrades to open." `int` is **not** caught as a special case — it falls through to the generic `else` branch and is logged with its type name. This is functionally equivalent, so the user-facing description is correct, but the implied list ("bool, int, dict") is misleading: any non-`None`/non-`bool`/non-`str`/non-`list` value lands in the `else` branch.

5. **(Lines 59–64) "A list of strings" omits behavior for empty list.** `parse_acl_value` at lines 48–50 returns `[]` (deny-all) for an empty list input. The decision-table row in the doc (`[]` ⇒ deny-all) covers this, but the "Front-matter" subsection should note that an empty list in front-matter becomes the deny-all sentinel, since it is a non-obvious effect of front-matter input.

6. **(Lines 59–64) Missing behavior: `"deny-all"` as a string token in front-matter.** `parse_acl_value` (lines 69–109) gives `"deny-all"` special treatment in both string and list inputs, including the "mixed with valid names → drop deny-all" and "mixed only with invalid names → fail-open" paths. The doc never mentions that `_acl: deny-all` (string) or `_acl: [deny-all]` (list) work, nor the mixing rules. This is a material omission.

7. **(Lines 70–74) Sidecar `DENY-ALL` "case-insensitive" — correct, but incomplete.** The implementation also strips a UTF-8 BOM (`acl.py:152`) before scanning lines. Not an inaccuracy per se, just unmentioned.

8. **(Lines 70–74) Sidecar: "Non-UTF-8 content → ignore with a warning." Correct.** `acl.py:147–149` matches.

9. **(Lines 70–74) Missing failure mode.** `read_acl_sidecar` returns `None` (fail-open) when every line is an invalid namespace name (`acl.py:178`, `return valid if valid else None`). The doc only describes the happy path and the deny-all path. Not strictly wrong, but the "fails open on parse trouble" principle ought to mention this concretely for sidecars too.

10. **(Line 85) MAJOR — namespace filtering on /collections, /state, /status, /route, /jobs is misrepresented.** The doc claims:
    > "`/route`, `/telemetry/*`, `/collections`, `/state`, `/status`, and `/jobs` are gated by bearer auth only; their responses are not ACL-filtered. A namespaced client sees the same collection list and the same telemetry as the default-namespace client."

    This is wrong for everything except `/telemetry/*`:
    - `routes_collections.py:80–94` filters `list_collections` by `m.namespace == ns` and skips entries belonging to other namespaces (a `default` caller can still see unregistered paths; non-default callers are strictly limited to their namespace).
    - `routes_collections.py:182–184` returns 404 for cross-namespace `DELETE /collections/{name}`.
    - `routes_collections.py:249` enforces a namespace gate (returns 404) for collection meta.
    - `routes_state.py:17–25` filters state to `ns_names`.
    - `routes_status.py:26–58` filters status to the caller's namespace.
    - `routes_route.py:86–89` filters routing candidates to the caller's namespace.
    - `routes_jobs.py:114, 134` enforce `job.namespace != request.state.namespace`.

    Only `routes_telemetry.py` is genuinely namespace-blind. The doc's framing conflates "no per-chunk ACL filtering" with "no namespace scoping at all" and ends up materially misleading.

11. **(Line 85) "namespaced client sees the same telemetry as the default-namespace client" — correct.** `routes_telemetry.py` does not reference `request.state.namespace`. Only true claim in that bullet.

12. **(Line 84) "A namespaced client can still ingest documents, list collections, and delete documents in the collections it can reach."** Misleading. A namespaced client cannot list or delete collections belonging to other namespaces (see point 10). "The collections it can reach" is technically a true qualifier, but the framing implies write authority is unrestricted, which it is not.

13. **(Line 27) Validation behavior on bad namespace.** The doc says "A namespace that fails validation returns `500`." `middleware_auth.py:56–59` confirms this. **Verified correct.**

14. **(Line 22) Implicit claim that exempt paths are absent.** The doc does not say which paths are exempt from auth, but the verification section assumes only `/health` is open. The middleware exempts `/health`, `/docs`, `/openapi.json`, and `/redoc` (`middleware_auth.py:16`). The CLAUDE.md "All endpoints except `GET /health` require a `Bearer` token" claim is also inaccurate; this doc inherits the same gap (omission, not an active error).

15. **(Line 53) "called by `SearchPipeline`" — correct, but specifically `SearchPipeline.search` (`pipeline.py:302`) and `SearchPipeline.search_with_context` (`pipeline.py:323`). The neighbor filter in `search_with_context` discards the `acl_filtered` bool (`_`), so a context call that drops neighbor chunks does **not** surface that in any field. This subtlety is unmentioned and arguably worth a note since the doc says `acl_filtered` is the "only signal" filtering occurred.

16. **(Line 101) The doc says "ingest it under any namespace, then query".** True only because ACL is stored in the row regardless of the ingesting namespace; the example reads naturally but obscures the namespace-vs-ACL distinction. Minor.

17. **(Line 96) "If a sidecar disappears, the chunk becomes open" — correct in spirit but worded ambiguously.** The chunk does not retroactively become open; the LanceDB row keeps its `acl` value (which was populated from the sidecar at ingest). It only becomes open on **reindex** after deletion. The doc later clarifies "until reindex", so this is internally consistent — flagging only because the headline sentence reads as immediate effect.

18. **(Line 50) Empty namespace defense-in-depth.** Correct; `acl.py:190, 196–197` shows `not namespace → False`. Note this only matters when ACL is non-`None`; `is_acl_allowed(None, "")` returns `True` (the `acl is None` short-circuit precedes the empty-namespace check). Doc says "If `namespace == ""` and the chunk has any non-`None` ACL, the chunk is denied" — this matches the code.

## Verified claims

- Namespace is set on `request.state.namespace` after middleware accepts a token; default key → `DEFAULT_NAMESPACE`; configured per-namespace keys → mapped namespace string. (`middleware_auth.py:40–61`)
- `DEFAULT_NAMESPACE = "default"`, `_NAMESPACE_RE = [a-zA-Z0-9][a-zA-Z0-9_-]{0,63}`. (`constants.py:12–14`)
- Token comparison uses `secrets.compare_digest`; no early exit prevents timing leakage. (`middleware_auth.py:39–47`)
- `_acl` precedence: front-matter wins over sidecar; warning logged when both exist. (`acl.py:234–242`)
- Decision table for `is_acl_allowed`: `None` → allow, `[]` → deny, list → membership check. (`acl.py:184–198`)
- Comparison is case-sensitive (no `.lower()` anywhere in `is_acl_allowed`). (`acl.py:198`)
- `is_acl_namespace_valid` rejects `"deny-all"` as a namespace identifier. (`acl.py:18`)
- `_ACL_SIDECAR_MAX_BYTES = 65536`; oversized sidecars ignored with warning. (`acl.py:11, 137–143`)
- Symlinked sidecars ignored with warning. (`acl.py:132–134`)
- Sidecar `DENY-ALL` recognized case-insensitively; trailing content warned and ignored. (`acl.py:161–167`)
- ACL column is `list<utf8>`, nullable, added by `SearchStore.migrate_acl`. (`store.py:138, 321–352`)
- `acl_filtered: bool` is on `SearchPipelineResult` and surfaced via `SearchResponse`. (`pipeline.py:32`, `routes_search.py:59, 80`)
- ACL filtering is post-retrieval in the pipeline (`pipeline.py:302`).
- `/telemetry/*` is not namespace-filtered. (`routes_telemetry.py`)
- ACL parse failures degrade to fail-open with warnings (no exceptions raised). (`acl.py:21–116`, `119–178`)
- There is no metric counter for ACL parse failures in v1 (no `prometheus`/`counter`/`metric` symbols around acl parse paths).

## Unverifiable / ambiguous

- **(Line 90) Roadmap claim "E4 placed after D7 (key rotation)".** Verifying requires reading `Documentation/Backlog/03_world_class_roadmap.md` — out of scope for this code-grounded review.
- **(Line 121) Tech-debt cross-ref `SEC-1` in `530_technical_debt_refactoring_roadmap.md`.** Same — doc-only claim.
- **(Lines 84) "There is no per-namespace write ACL today."** True in the sense that the per-chunk `acl` list is not consulted on write paths; however, write paths (POST/DELETE on `/collections`, `/jobs`) are namespace-scoped (see point 10), so the practical effect is partially what the doc denies. The literal claim is technically correct but the surrounding paragraph leaves a false impression.
- **(Line 113) "check the server log for `_acl in <path> has invalid …`".** Matches the actual log format (`acl.py:38, 61, 77`). Verified correct.
