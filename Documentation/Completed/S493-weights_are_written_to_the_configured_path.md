## Bug: `FASTEMBED_CACHE_PATH` relocates fastembed's weight cache on a native install

**ID**: S493-weights_are_written_to_the_configured_path
**Scenario**: S493
**Severity**: medium
**Version**: archon-search, version 26.10.2181

### What happened
AssertionError: FASTEMBED_CACHE_PATH=/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/pytest-of-manczg/pytest-400/s493-fastembed-cache0 is still empty after an ingest forced the embedder to load — no fastembed weights were written to the configured directory (UserManual/30_configuration.md:289; 140_running_with_docker.md:149)
assert []

### What should happen
- The ingest job reaches **`DONE`** — the instance embedded the document with its weight cache redirected, so the relocation does not break model loading.
- The directory named by `FASTEMBED_CACHE_PATH`, empty before the run, now **exists and contains fastembed-downloaded model weights** — the variable is "fastembed's own weight-cache directory" (`30_configuration.md:289`) and it "persists fastembed-downloaded model weights" at that path (`140_running_with_docker.md:149`). Concretely: at least one regular file is present and the tree holds more than a megabyte of weight data, which no bookkeeping stub would account for.
- The relocation works with the directory placed **outside** `ARCHON_SEARCH_DATA_DIR` — `FASTEMBED_CACHE_PATH` is "a fastembed-native env var … independent of `ARCHON_SEARCH_DATA_DIR`" (`150_multi_instance_setup.md:47`, `:530`).

### Steps to reproduce
1. Start an isolated instance.
2. Point its environment at an empty `FASTEMBED_CACHE_PATH` directory **outside** the instance's `ARCHON_SEARCH_DATA_DIR`, and restart the process.
3. `POST <base>/ingest {"path": "<one small doc>", "collection": "s493_fastembed"}` and poll `GET <base>/jobs/<id>` to `DONE` — this forces the embedder to load.
4. Inspect the directory named by `FASTEMBED_CACHE_PATH`.

### Evidence
```
E   AssertionError: FASTEMBED_CACHE_PATH=/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/pytest-of-manczg/pytest-400/s493-fastembed-cache0 is still empty after an ingest forced the embedder to load — no fastembed weights were written to the configured directory (UserManual/30_configuration.md:289; 140_running_with_docker.md:149)
E   assert []
```
