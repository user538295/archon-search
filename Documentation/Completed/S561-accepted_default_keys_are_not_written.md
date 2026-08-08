## Bug: `wizard --routing-strategy centroid`: which key the wizard writes when a flag repeats the default

**ID**: S561-accepted_default_keys_are_not_written
**Scenario**: S561
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: UserManual/20_wizard.md:665 states that a question whose default is accepted has its key NOT written to the file, naming the `text` log format as its own example. No --log-format, --watch or --telemetry flag was passed, yet the wizard wrote: [('[logging].format', 'text'), ('[collections].watch', False), ('[telemetry].enabled', False)] (doc refs: ['20_wizard.md:506 log format text / :650 (json only)', '20_wizard.md:502 watcher Disabled / :631', '20_wizard.md:503 telemetry Disabled / :637']). The wizard exited 0 and materialised every default as an explicit key.
--- /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-7dm2y23q/archon-search.toml ---
[server]
host = "127.0.0.1"
port = 8765

[database]
db_path = "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-7dm2y23q/data/search"
embedding_model = "BAAI/bge-small-en-v1.5"
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
chunk_size = 512
auto_reindex_on_chunk_size_change = true
profile = "minimal"
multilingual = false
eager_load_embedders = false
providers = ["CoreMLExecutionProvider"]

[routing]
routing_shortlist_size = 8
routing_confidence_threshold = 0.3
routing_strategy = "centroid"

[collections]
pinned_collections = []
collections = []
watch = false

[logging]
level = "INFO"
log_file = "~/.archon-search/logs/archon-search.log"
format = "text"
backup_count = 7

[telemetry]
enabled = false

[hyde]
enabled = false

[rag_fusion]
enabled = false

assert not [('[logging].format', 'text', '20_wizard.md:506 log format text / :650 (json only)'), ('[collections].watch', False, '20_wizard.md:502 watcher Disabled / :631'), ('[telemetry].enabled', False, '20_wizard.md:503 telemetry Disabled / :637')]

### What should happen
- Step 1: exit code `0`.
- Step 2 (flag half, `:665` sentence 3): `[routing].routing_strategy` is present and is exactly the string `"centroid"` — the value passed. It is never `"hybrid"`, the only value `:644` names, and never any other value. Under the row's original reading the key would be absent instead; that reading and this one agree that the key can hold no value other than `centroid`, which is why the value is asserted whenever the key exists.
- Step 2 (accepted-default half, `:665` sentences 1–2): `[logging].format` is **absent** from the written config. No `--log-format` flag was passed, `text` is the accepted non-interactive default (`:506`), and `:665` names that exact case — "keep … `text` log format … that key is not written to the file". `[collections].watch` and `[telemetry].enabled` are absent for the same reason (`:502`, `:503`, `:631`, `:637`).
- A failure of the accepted-default half is a **bug** against `20_wizard.md:665`, not a doc gap: the sentence is explicit and names its own example. It is reported with the observed config as evidence.

### Steps to reproduce
1. `ARCHON_SEARCH_CONFIG="$TMP/archon-search.toml" ARCHON_SEARCH_DATA_DIR="$TMP/data" ARCHON_SEARCH_KEY_FILE="$TMP/data/.search.env" archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/data/search" --profile minimal --non-interactive --skip-preload --routing-strategy centroid`
2. Read the TOML at `$TMP/archon-search.toml`.

### Evidence
```
E   AssertionError: UserManual/20_wizard.md:665 states that a question whose default is accepted has its key NOT written to the file, naming the `text` log format as its own example. No --log-format, --watch or --telemetry flag was passed, yet the wizard wrote: [('[logging].format', 'text'), ('[collections].watch', False), ('[telemetry].enabled', False)] (doc refs: ['20_wizard.md:506 log format text / :650 (json only)', '20_wizard.md:502 watcher Disabled / :631', '20_wizard.md:503 telemetry Disabled / :637']). The wizard exited 0 and materialised every default as an explicit key.
E     --- /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-7dm2y23q/archon-search.toml ---
E     [server]
E     host = "127.0.0.1"
E     port = 8765
E     
E     [database]
E     db_path = "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-7dm2y23q/data/search"
E     embedding_model = "BAAI/bge-small-en-v1.5"
E     reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
E     chunk_size = 512
E     auto_reindex_on_chunk_size_change = true
E     profile = "minimal"
E     multilingual = false
E     eager_load_embedders = false
E     providers = ["CoreMLExecutionProvider"]
E     
E     [routing]
E     routing_shortlist_size = 8
E     routing_confidence_threshold = 0.3
E     routing_strategy = "centroid"
E     
E     [collections]
E     pinned_collections = []
E     collections = []
E     watch = false
E     
E     [logging]
E     level = "INFO"
E     log_file = "~/.archon-search/logs/archon-search.log"
E     format = "text"
E     backup_count = 7
E     
E     [telemetry]
E     enabled = false
E     
E     [hyde]
E     enabled = false
E     
E     [rag_fusion]
E     enabled = false
E     
E   assert not [('[logging].format', 'text', '20_wizard.md:506 log format text / :650 (json only)'), ('[collections].watch', False, '20_wizard.md:502 watcher Disabled / :631'), ('[telemetry].enabled', False, '20_wizard.md:503 telemetry Disabled / :637')]
```
