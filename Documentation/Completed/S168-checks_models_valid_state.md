## Bug: docs contradict themselves on /ready checks casing: lowercase on the wire, uppercase in the prose

**ID**: S168-checks_models_valid_state
**Scenario**: S168
**Severity**: low
**Version**: archon-search, version 26.8.1751

### What happened
The documentation states the `GET /ready` `checks.storage` / `checks.models` values in two different casings, and readers following the uppercase pages write assertions that can never pass.

LOWERCASE (matches the actual wire format):
- `docs/OperatorGuide/20_monitoring_and_alerts.md:23` — literal body `{ready:true, checks:{storage:"ok", models:...}}`
- `docs/OperatorGuide/20_monitoring_and_alerts.md:73` — "`pending` while validation runs, `ok` when both models load cleanly, `warn` ..., `fail` when a model could not load"

UPPERCASE (contradicts the wire format):
- `docs/UserManual/160_troubleshooting.md:147` — "`checks.storage` — `OK` / `FAIL`"
- `docs/UserManual/160_troubleshooting.md:148` — "`checks.models` — `OK` / `WARN` / `FAIL` / `PENDING`"
- `docs/UserManual/160_troubleshooting.md:154`
- `docs/OperatorGuide/90_incident_runbook.md:108` — "reports `checks.models: \"FAIL\"` or `\"WARN\"`" (in quotes, i.e. presented as the literal value)
- `docs/OperatorGuide/90_incident_runbook.md:113` — "**FAIL** ... > **WARN** ... > **OK**. `PENDING` means ..."

The server returns LOWERCASE. This is a documentation defect, not a server defect.

CONCRETE COST: this inconsistency directly produced three false bug reports against a correct server (S168 x2, S183) — tests written against the uppercase pages asserted `"OK"` and failed on the correct lowercase `"ok"`. Filing it so the next person does not repeat it.

### What should happen
One casing, used consistently across all four pages, matching the wire format. The literal JSON body at `docs/OperatorGuide/20_monitoring_and_alerts.md:23` and the explicit value list at :73 are lowercase and agree with observed behaviour, so the uppercase occurrences in `docs/UserManual/160_troubleshooting.md:147,148,154` and `docs/OperatorGuide/90_incident_runbook.md:108,113` should be lowercased.

Where uppercase is intended purely as prose emphasis for a state name (e.g. the priority ordering at 90_incident_runbook.md:113), it should not be presented in backticks or quotes as if it were the literal wire value — 90_incident_runbook.md:108 puts `"FAIL"` in quotes, which reads unambiguously as the literal payload value.

### Steps to reproduce
1. `curl -s http://127.0.0.1:8765/ready` (no auth required) and read `checks.storage` / `checks.models`
2. Observe lowercase values (`ok` / `warn` / `fail` / `pending`)
3. Read `docs/UserManual/160_troubleshooting.md:147-148` and `docs/OperatorGuide/90_incident_runbook.md:108,113` — both give the values in UPPERCASE
4. Read `docs/OperatorGuide/20_monitoring_and_alerts.md:23,73` — lowercase, agreeing with step 2

### Evidence
```
Observed wire format from GET /ready (200, no auth):
  checks.storage == "ok"
  checks.models  in {"pending", "ok", "warn", "fail"}   (lowercase)

Doc lines, verbatim:

OperatorGuide/20_monitoring_and_alerts.md:23
  | `GET /ready` | None | ... `200` `{ready:true, checks:{storage:"ok", models:...}}` ...

OperatorGuide/20_monitoring_and_alerts.md:73
  - **`GET /ready`** — the informational `checks.models` field: `pending` while validation
    runs, `ok` when both models load cleanly, `warn` when a provider fallback occurred ...,
    `fail` when a model could not load ...

UserManual/160_troubleshooting.md:147
  - `checks.storage` — `OK` / `FAIL`. `FAIL` -> HTTP 503; fix the datastore ...

UserManual/160_troubleshooting.md:148
  - `checks.models` — `OK` / `WARN` / `FAIL` / `PENDING`. `PENDING` means the background
    model probe has not finished yet ...

OperatorGuide/90_incident_runbook.md:108
  **Symptoms**: `GET /ready` reports `checks.models: "FAIL"` or `"WARN"`; ...

OperatorGuide/90_incident_runbook.md:113
  - `checks.models` priority is strict: **FAIL** ... > **WARN** ... > **OK**. `PENDING`
    means the probe has not produced a result yet.

Tests now assert the lowercase wire format (the literal body at 20_monitoring_and_alerts.md:23
being authoritative for wire format) and pass:
  tests/test_s168_ready_checks_storage_models.py  — 4 passed
  tests/test_s183_ready_auth_exempt_model_probe.py — 4 passed
False reports this inconsistency caused, since withdrawn: S168 x2, S183.
```
