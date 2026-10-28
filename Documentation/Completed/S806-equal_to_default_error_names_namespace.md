## Bug: [namespaces] token equal to the default key: startup error does not name the namespace

**ID**: S806-equal_to_default_error_names_namespace
**Scenario**: S806
**Severity**: low
**Version**: archon-search, version 26.10.2267

### What happened
A `[namespaces]` token equal to the default API key refuses startup (exit 1, correct) but the error line does not name the namespace: `Error: a [namespaces] token equals the default API key; give every namespace its own token`. For the short/whitespace cases the server prints `Error: [namespaces] the token for namespace '<ns>' is not usable: …`. Observed 26.10.2267, native install, isolated data dir.

### What should happen
`OperatorGuide/90_incident_runbook.md:220` — "`[namespaces]` token problems (shorter than 32 printable ASCII characters, whitespace, or equal to the default key): the error names the namespace, never the token." Also `SecurityGuide/02_authentication_and_keys.md:82`. The error for a token equal to the default key should name the namespace (here `team-token-ns`), like the short/whitespace cases do.

### Steps to reproduce
1. Create a throwaway config whose `[namespaces]` table maps a token identical to the default key to the namespace `team-token-ns`.
2. `ARCHON_SEARCH_API_KEY=<that same 64-hex value> archon-search serve`
3. Read stderr.

### Evidence
```
exit status: 1
stderr: Error: a [namespaces] token equals the default API key; give every namespace its own token
Contrast (31-char token): Error: [namespaces] the token for namespace 'team-token-ns' is not usable: it must be at least 32 characters long (got 31)
```
