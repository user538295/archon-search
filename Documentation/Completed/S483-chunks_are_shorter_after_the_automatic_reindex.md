## Bug: changing `chunk_size` reindexes affected collections on the next server start

**ID**: S483-chunks_are_shorter_after_the_automatic_reindex
**Scenario**: S483
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: chunk text is not shorter after `chunk_size` went 512 -> 96 and the server restarted — the documented automatic rebuild (50_ingestion_and_collections.md:181, :14; 30:59; 40:136) did not re-chunk the collection.
config after the change:
[server]
host = "127.0.0.1"
port = 61693

[database]
db_path = "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-7mw86quf/search"
embedding_model = "BAAI/bge-small-en-v1.5"
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
chunk_size = 96
profile = "minimal"
multilingual = false
providers = ["CoreMLExecutionProvider"]
auto_reindex_on_chunk_size_change = true

[logging]
level = "INFO"
log_file = "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-7mw86quf/logs/archon-search.log"
format = "text"
backup_count = 7


[collections]
collections = ["/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-7mw86quf/corpus"]
pinned_collections = []

before (chunk_size=512): chunk_count=3, text_lengths=[2576, 2574, 2592], ingested_by=['cli']
after restart (chunk_size=96, waited 150s): chunk_count=3, text_lengths=[2576, 2574, 2592], ingested_by=['cli']
control (explicit `collection reindex`, exit 0): chunk_count=24, text_lengths=[148, 148, 148, 148, 148], ingested_by=['reindex']

assert 2592 < 2592

### What should happen
- Step 2 exits `0` and step 3 reports `chunk_count > 0` with a non-empty `path` — the precondition
  that the collection was indexed at 512 tokens (50:104).
- After step 7 the collection is **rebuilt at the new chunk size**: `chunk_count` rises above the
  512-token count, because `chunk_size` is a "Target chunk size in tokens" (30:50) and a smaller
  target over the same document yields more chunks. This is the documented automatic reindex
  (50:181; 50:14; 30:59; 40:136), and it must happen **without any explicit `collection reindex`**.
- The reindex is observable on the chunks themselves: after step 7 `POST /search` returns chunks
  whose `ingested_by` is `reindex` (55:28) rather than the `cli` value left by the `collection add`
  ingest, and whose `text` is materially shorter than the 512-token chunks.
- Step 8 (control) rebuilds to the same state — `chunk_count` above the 512-token count and
  `ingested_by = reindex` (50:133; 55:28). Its purpose is to separate the two possible failures:
  if step 8 rebuilds but step 7 did not, the rebuild machinery works and the **automatic
  chunk-size trigger** is the part that did not fire.

### Steps to reproduce
1. Start the isolated instance with `[database].chunk_size = 512`.
2. `archon-search collection add <corpus> --wait --api-url <iso> --api-key <key>` — a multi-paragraph
   document large enough to chunk differently at 512 vs 96 tokens.
3. `GET /collections/corpus` — record `chunk_count` at 512 tokens.
4. Stop the `serve` process.
5. `archon-search config set database.auto_reindex_on_chunk_size_change true`
6. `archon-search config set database.chunk_size 96`
7. Restart `archon-search serve` on the same data dir and poll `GET /collections/corpus`.
8. **Control** — `archon-search collection reindex corpus --wait`, then `GET /collections/corpus`
   again.

### Evidence
```
E   AssertionError: chunk text is not shorter after `chunk_size` went 512 -> 96 and the server restarted — the documented automatic rebuild (50_ingestion_and_collections.md:181, :14; 30:59; 40:136) did not re-chunk the collection.
E     config after the change:
E     [server]
E     host = "127.0.0.1"
E     port = 61693
E     
E     [database]
E     db_path = "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-7mw86quf/search"
E     embedding_model = "BAAI/bge-small-en-v1.5"
E     reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
E     chunk_size = 96
E     profile = "minimal"
E     multilingual = false
E     providers = ["CoreMLExecutionProvider"]
E     auto_reindex_on_chunk_size_change = true
E     
E     [logging]
E     level = "INFO"
E     log_file = "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-7mw86quf/logs/archon-search.log"
E     format = "text"
E     backup_count = 7
E     
E     
E     [collections]
E     collections = ["/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-7mw86quf/corpus"]
E     pinned_collections = []
E     
E     before (chunk_size=512): chunk_count=3, text_lengths=[2576, 2574, 2592], ingested_by=['cli']
E     after restart (chunk_size=96, waited 150s): chunk_count=3, text_lengths=[2576, 2574, 2592], ingested_by=['cli']
E     control (explicit `collection reindex`, exit 0): chunk_count=24, text_lengths=[148, 148, 148, 148, 148], ingested_by=['reindex']
E     
E   assert 2592 < 2592
E    +  where 2592 = max([2576, 2574, 2592])
E    +  and   2592 = max([2576, 2574, 2592])
```
