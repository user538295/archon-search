## Bug: `config show` displays all four `[logging]` keys

**ID**: S104-all_four_logging_keys_present
**Scenario**: S104
**Severity**: medium
**Version**: archon-search, version 26.8.1916

### What happened
AssertionError: logging key 'level' missing from config show:
[collections]
collections = ["/private/tmp/archon-test-docs", "/private/tmp/archon-multitype", "/private/tmp/s051_cli_col", "/private/tmp/s052_col", "/private/tmp/s054_col"]
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


assert 'level' in '[collections]

### What should happen
- Output contains all four logging keys: `level`, `log_file`, `format`,
  `backup_count`.
- Default values match the doc: `level` is `INFO`, `log_file` ends with
  `.archon-search/logs/archon-search.log`, `format` is `text`, `backup_count`
  is `7`.
- Exit code 0.

### Steps to reproduce
1. `archon-search config show`

### Evidence
```
E   AssertionError: logging key 'level' missing from config show:
E     [collections]
E     collections = ["/private/tmp/archon-test-docs", "/private/tmp/archon-multitype", "/private/tmp/s051_cli_col", "/private/tmp/s052_col", "/private/tmp/s054_col"]
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
E   assert 'level' in '[collections]
collections = ["/private/tmp/archon-test-docs", "/private/tmp/archon-multitype", "/private/tmp/s051_cl...ng_strategy = "centroid"

[logging]
format = "text"

[hyde]
enabled = false

[rag_fusion]
enabled = false

'
```
