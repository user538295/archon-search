## Bug: Wizard Step 2 prints a different 'Models (...)' header than documented

**ID**: S953-models_header_text
**Scenario**: S953
**Severity**: low
**Version**: archon-search, version 26.10.2285

### What happened
The picker prints '  Models (download = embedder + reranker weights from the Hugging Face Hub):' instead of the documented header.

### What should happen
20_wizard.md:81: 'Models (weights are downloaded from Hugging Face at pinned revisions):'

### Steps to reproduce
1. printf "1
n
y
n
n
n


n
n
n
" | archon-search wizard --dry-run --no-multilingual --disable-gpu --skip-preload --config $TMP/c.toml --db-path $TMP/s

### Evidence
```
Observed header: Models (download = embedder + reranker weights from the Hugging Face Hub):
```
