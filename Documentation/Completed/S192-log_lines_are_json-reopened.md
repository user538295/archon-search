## Bug: `--log-format json` wizard flag writes JSON-formatted log entries

**ID**: S192-log_lines_are_json
**Scenario**: S192
**Severity**: medium
**Version**: archon-search, version 26.10.2181

### What happened
AssertionError: expected exactly one log_file line in /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-so47f8qu/archon-search.toml, found 0:
[server]
host = "127.0.0.1"

[database]
embedding_model = "BAAI/bge-small-en-v1.5"
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
chunk_size = 512
profile = "minimal"
multilingual = false
db_path = "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-so47f8qu/data/search"
providers = ["CoreMLExecutionProvider"]

[logging]
format = "json"

assert 0 == 1

### What should happen
- Wizard exits 0.
- Each line of the log file is a valid JSON object (parseable, starts with `{`).

### Steps to reproduce
1. `archon-search wizard --profile minimal --non-interactive --skip-preload --log-format json`
2. `head -5 ~/.archon-search/logs/archon-search.log`

### Evidence
```
E   AssertionError: expected exactly one log_file line in /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-so47f8qu/archon-search.toml, found 0:
E     [server]
E     host = "127.0.0.1"
E     
E     [database]
E     embedding_model = "BAAI/bge-small-en-v1.5"
E     reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
E     chunk_size = 512
E     profile = "minimal"
E     multilingual = false
E     db_path = "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-so47f8qu/data/search"
E     providers = ["CoreMLExecutionProvider"]
E     
E     [logging]
E     format = "json"
E     
E   assert 0 == 1
```
