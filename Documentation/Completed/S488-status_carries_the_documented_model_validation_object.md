## Bug: `reranker_providers` TOML override is accepted (the "custom reranker listed in `/status`" echo is UNDOCUMENTED)

**ID**: S488-status_carries_the_documented_model_validation_object
**Scenario**: S488
**Severity**: medium
**Version**: archon-search, version 26.10.2181

### What happened
AssertionError: `model_validation` is missing documented keys ['embedder_ok', 'reranker_ok', 'provider_warnings', 'validated_at'] (OperatorGuide/20_monitoring_and_alerts.md:76); model_validation=None
assert not ['embedder_ok', 'reranker_ok', 'provider_warnings', 'validated_at']

### What should happen
- **The bullet's claim — "custom reranker listed in `/status`" — is UNDOCUMENTED.** `reranker_providers` is mentioned in `./docs/` only as the TOML key (`UserManual/30_configuration.md:63`, `UserManual/20_wizard.md:149`, `:756`, `UserManual/10_installation.md:135`) or as a provider source of `provider_warnings` (`OperatorGuide/90_incident_runbook.md:117`, `UserManual/160_troubleshooting.md:212`). No document states that `GET /status` echoes the configured reranker model or its execution providers; the documented `/status` inventory (`OperatorGuide/20_monitoring_and_alerts.md:24`) names no such field, and the only provider-related field it does name is `model_validation.provider_warnings` (`:76`). No assertion is invented for the echo.
- **Documented, exercisable assertions (what IS specified):**
  - The instance starts and stays healthy with `reranker_providers = []` present — the key is a documented, accepted `[database]` key (`30_configuration.md:63`).
  - `GET /status` returns **HTTP 200** and carries the documented `model_validation` sub-object with the four documented keys `embedder_ok`, `reranker_ok`, `provider_warnings`, `validated_at` (`OperatorGuide/20_monitoring_and_alerts.md:76`).
  - Once validation is no longer pending (`validated_at` non-null, `:76`), `model_validation.reranker_ok` is **not `false`** — `reranker_providers = []` forces the reranker onto CPU (`30_configuration.md:63`), which cannot be an unavailable ONNX provider, so the documented "reranker misconfiguration" alert condition (`20_monitoring_and_alerts.md:82`, `:117`) must not fire.
  - `POST /explain` with `rerank: true` returns **HTTP 200** and every result's `breakdown` (`ExplainScoreBreakdown`, `80_explain_and_debugging.md:93`) carries a **non-null `reranker_score`** — the cross-encoder is `null` only when `rerank=false` (`:102`), so the CPU-forced reranker demonstrably still runs.
- **Doc-gap reopening gate:** no sentence anywhere in `./docs/` mentions both `reranker_providers` and `/status`. If a future doc ever ties the key to `/status` output, the paired test flips **red** so S488 is re-implemented against the then-documented field. No bug is filed — a missing doc is not an app defect.

### Steps to reproduce
1. Start an isolated instance with `reranker_providers = []` in its `[database]` block.
2. `GET <base>/status` with `Authorization: Bearer <key>`.
3. `POST <base>/explain {"collection": "<seeded>", "query": "quick brown fox", "rerank": true}` with the same header.
4. `grep -rn reranker_providers ./docs/`.

### Evidence
```
E   AssertionError: `model_validation` is missing documented keys ['embedder_ok', 'reranker_ok', 'provider_warnings', 'validated_at'] (OperatorGuide/20_monitoring_and_alerts.md:76); model_validation=None
E   assert not ['embedder_ok', 'reranker_ok', 'provider_warnings', 'validated_at']
```
