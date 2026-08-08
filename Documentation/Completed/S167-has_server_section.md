## Bug: `archon-search config show` prints TOML

**ID**: S167-has_server_section
**Scenario**: S167
**Severity**: medium
**Version**: archon-search, version 26.8.1916

### What happened
AssertionError: no [server] section in TOML; top-level keys=['collections', 'database', 'telemetry', 'routing', 'logging', 'hyde', 'rag_fusion']
stdout=[collections]
collections = ["/private/tmp/archon-test-docs", "/private/tmp/archon-multitype", "/private/tmp/s051_cli_col", "/private/tmp/s052_col", "/private/tmp/s054_col", "/private/tmp/ttl-test-docs"]
watch = false
pinned_collections = []

[database]
embedding_model = "BAAI/bge-small-en-v1.5"
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
chunk_size = 512
profile = "minimal"
multilingual = false
eager_load_embedders = false
providers = ["CoreMLExecutionProvider"]

[telemetry]
enabled = false

[routing]
routing_strategy = "centroid"

[logging]
format = "text"

[hyde]
enabled = false

[rag_fusion]
enabled = false


assert 'server' in {'collections': {'collections': ['/private/tmp/archon-test-docs', '/private/tmp/archon-multitype', '/private/tmp/s051_... 'BAAI/bge-small-en-v1.5', 'multilingual': False, ...}, 'hyde': {'enabled': False}, 'logging': {'format': 'text'}, ...}

### What should happen
- Exits 0.
- Output is valid TOML containing at least the `[server]` section.
- Output contains `port` key (e.g. `port = 8765`).

### Steps to reproduce
1. `archon-search config show`

### Evidence
```
E   AssertionError: no [server] section in TOML; top-level keys=['collections', 'database', 'telemetry', 'routing', 'logging', 'hyde', 'rag_fusion']
E     stdout=[collections]
E     collections = ["/private/tmp/archon-test-docs", "/private/tmp/archon-multitype", "/private/tmp/s051_cli_col", "/private/tmp/s052_col", "/private/tmp/s054_col", "/private/tmp/ttl-test-docs"]
E     watch = false
E     pinned_collections = []
E     
E     [database]
E     embedding_model = "BAAI/bge-small-en-v1.5"
E     reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
E     chunk_size = 512
E     profile = "minimal"
E     multilingual = false
E     eager_load_embedders = false
E     providers = ["CoreMLExecutionProvider"]
E     
E     [telemetry]
E     enabled = false
E     
E     [routing]
E     routing_strategy = "centroid"
E     
E     [logging]
E     format = "text"
E     
E     [hyde]
E     enabled = false
E     
E     [rag_fusion]
E     enabled = false
E     
E     
E   assert 'server' in {'collections': {'collections': ['/private/tmp/archon-test-docs', '/private/tmp/archon-multitype', '/private/tmp/s051_... 'BAAI/bge-small-en-v1.5', 'multilingual': False, ...}, 'hyde': {'enabled': False}, 'logging': {'format': 'text'}, ...}
```
