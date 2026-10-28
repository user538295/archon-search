## Bug: A `[namespaces]` token under 32 characters, with whitespace/non-printable characters, or equal to the default key refuses startup

**ID**: S806-message_names_the_namespace[equals_default_key]
**Scenario**: S806
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S806-equal_to_default_error_names_namespace.md — error for case equals_default_key does not name the namespace 'team-token-ns' (90_incident_runbook.md:220): 'Error: a [namespaces] token equals the default API key; give every namespace its own token'

### What should happen
- Steps 1–6 each **exit with status 1** and never serve (`30_configuration.md:295`; `90_incident_runbook.md:216`).
- stderr carries exactly **one** `Error:` line and **no traceback** (`90_incident_runbook.md:216`) that **names the namespace `team-token-ns`** (`02_authentication_and_keys.md:82`; `90_incident_runbook.md:220` — this includes the equal-to-default case, which the runbook lists in the same sentence) and **never prints the token** (`02_authentication_and_keys.md:82`).
- Steps 1–5 name the rule (any natural wording — the docs fix the rule, not the text, so the test matches loosely on `32`/`short`, `whitespace`/`printable`/`ASCII`): the 32-character minimum (step 1) or the printable-ASCII/no-whitespace rule (steps 2–5) (`02_authentication_and_keys.md:82`); step 6 says the token equals the default key (`02_authentication_and_keys.md:82`).
- Step 7: the server **starts** (`GET /health` is `200`) and `GET /status` with the token returns **`200`** — a token of exactly 32 printable ASCII characters is valid (`30_configuration.md:295`).

### Steps to reproduce
1. Config `[namespaces]` with a 31-character token mapped to `team-token-ns`; run `archon-search serve`.
2. Repeat with a 40-character token containing a space.
3. Repeat with a 40-character token containing a tab.
4. Repeat with a 40-character token containing a non-ASCII character (`é`).
5. Repeat with a 40-character token containing a control character (`U+0001`).
6. Repeat with a token equal to the default key.
7. Start with a token of **exactly 32** printable ASCII characters including punctuation (`!#$%&()*+,-./:;<=>?@[]^_{|}~`), then `GET /status` with `Authorization: Bearer <token>`.

### Evidence
```
E   Failed: bug: bugs/S806-equal_to_default_error_names_namespace.md — error for case equals_default_key does not name the namespace 'team-token-ns' (90_incident_runbook.md:220): 'Error: a [namespaces] token equals the default API key; give every namespace its own token'
```
