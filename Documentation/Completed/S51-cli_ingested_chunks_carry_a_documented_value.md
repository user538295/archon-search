## Bug: documented ingested_by value "cli" is unreachable — every CLI path records "http"

**ID**: S51-cli_ingested_chunks_carry_a_documented_value
**Scenario**: S51
**Severity**: low
**Version**: archon-search, version 26.8.1751

### What happened
No CLI path produces `ingested_by: "cli"`. Both documented CLI ingestion entry points record `"http"` instead:

- `archon-search ingest --collection <name> --path <dir>` -> chunks carry `ingested_by: "http"`
- `archon-search collection add <dir> --wait` -> chunks carry `ingested_by: "http"`

The mechanism is consistent with the documented CLI architecture: the CLI reaches the server over HTTP rather than touching storage directly (`docs/UserManual/65_graph_search.md:67`, "the CLI (an HTTP proxy to the running server)"; `docs/OperatorGuide/60_graph_operations.md:70`, "a pure HTTP proxy to the route above"; and per-command at `docs/UserManual/50_ingestion_and_collections.md:143`, "Proxies `POST /collections/{name}/reindex-metadata`") — so from the server's point of view every CLI ingest arrives over HTTP and is attributed as such.

Either way the documentation is wrong as written: the worked example at `55_chunk_metadata_and_enrichment.md:104` shows output a user cannot produce.

### What should happen
`docs/UserManual/55_chunk_metadata_and_enrichment.md:28` lists `cli` as a valid `ingested_by` value, and :104 shows `"ingested_by": "cli"` in a worked example. Either:

(a) the attribution is wrong — a CLI-initiated ingest should be recorded as `cli` even though it reaches the server over HTTP, so the field distinguishes entry points as documented; or
(b) the value is genuinely unreachable and the docs are misleading — `cli` should be removed from the list at :28 and the example at :104 corrected to `http`.

We cannot tell which from the outside; the maintainers know the intent. What we can state is that the documented example cannot be reproduced.

### Steps to reproduce
1. `archon-search collection add /tmp/s051_cli_col --wait`
2. `POST /search` for collection `s051_cli_col` and read `ingested_by` on the returned chunks
3. Observe `http`, not `cli`
4. Repeat with `archon-search ingest --collection s051_cli_col --path /tmp/s051_cli_col` — same result

### Evidence
```
Distinct ingested_by values observed across chunks, by ingestion entry point:

  collection add <dir> --wait          -> {'http'}
  ingest --collection X --path <dir>   -> {'http'}
  POST /ingest (HTTP directly)         -> {'http'}

No invocation of any kind produced 'cli'.

Doc references:
  docs/UserManual/55_chunk_metadata_and_enrichment.md:28   lists 'cli' as a valid value
  docs/UserManual/55_chunk_metadata_and_enrichment.md:104  worked example shows "ingested_by": "cli"
  docs/UserManual/65_graph_search.md:67                    CLI is an HTTP proxy to the running server

The test now asserts only that values are a SUBSET of the documented set
{cli, http, watcher, reindex}, which passes — the unreachability of 'cli' is
reported here rather than asserted, since the docs do not promise that a
specific CLI command yields a specific value.
```
