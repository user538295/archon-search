## Bug: 60_searching.md:235 promises a request-level 422 for hyde=true without the provider package, but the server refuses to boot instead

**ID**: S455-hyde_enabled_without_the_provider_package_hits_the_documented_boot_guard
**Scenario**: S455
**Severity**: low
**Version**: archon-search, version 26.8.1848

### What happened
Starting an isolated `archon-search serve` with `[hyde] enabled = true` while the `anthropic` package is absent makes the server EXIT AT STARTUP with `archon_search.config.ConfigError: [hyde] enabled=true with provider='anthropic' but the 'anthropic' package is not installed; run: pip install archon-search[hyde]`. No process ever binds, so the documented request-level 422 for `hyde=true` with the provider package uninstalled cannot be observed in that configuration. Observed on 26.8.1848, 2026-08-06.

### What should happen
The two shipped statements contradict each other and only one can hold. UserManual/60_searching.md:235: 'The one non-silent case is `hyde=true` with the provider package uninstalled - that returns `422` (a config error, not a runtime fallback).' UserManual/160_troubleshooting.md:135-137: 'Symptom: server refuses to start ... The server exits with a ConfigError naming [hyde] or [rag_fusion], e.g. [hyde] enabled=true with provider='anthropic' but the 'anthropic' package is not installed'. The application implements the 160_troubleshooting.md behaviour verbatim, so 60_searching.md:235 (and the ':50' 422 bullet, to the extent it covers a missing HyDE/RAG-Fusion provider package) describes an unreachable path and should be corrected to point at the startup ConfigError.

### Steps to reproduce
1. Write a config with [server]/[database]/[logging] plus:
   [hyde]
   enabled = true
2. ARCHON_SEARCH_CONFIG=<that file> archon-search serve
3. Observe the process exit and read the ConfigError on stdout/stderr.
4. Note that no POST /explain or POST /search request with hyde=true can be issued, so the documented 422 is unobservable.

### Evidence
```
serve.log tail:
  File ".../archon_search/server/app.py", line 289, in create_app
    _check_provider_deps(config)
  File ".../archon_search/server/app.py", line 179, in _check_provider_deps
    raise ConfigError(
archon_search.config.ConfigError: [hyde] enabled=true with provider='anthropic' but the 'anthropic' package is not installed; run: pip install archon-search[hyde]
```
