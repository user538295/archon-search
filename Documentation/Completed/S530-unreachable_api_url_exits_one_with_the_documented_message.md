## Bug: with a service live on the default port, an unreachable `--api-url` reports the LOCAL server's state instead of the target's

**ID**: S530-unreachable_api_url_exits_one_with_the_documented_message
**Scenario**: S530 (canonical — the same defect fails S323, S531, S532, S533, S534, S537)
**Severity**: medium
**Version**: archon-search, version 26.8.1980

### What happened
Every CLI write command pointed at a port nothing is listening on exits `1` (correct) but
prints, on stderr:

```
archon-search is starting up. Please wait for it to finish loading models, then retry.
```

The string appears nowhere in `docs/` (grepped over the whole tree). It is also false twice
over: nothing is listening on the target port, and the *local* server it is evidently
describing is fully up — `GET /ready` returns `200 {"ready":true,"checks":{"storage":"ok","models":"ok","sync":"ok"}}`
at the same moment. No component is loading models.

### Trigger — reproduced by hand, 2026-08-18
The message depends on **local service state, not on the target named by `--api-url`**:

| Local service on `:8765` | `--api-url` target | Message |
|---|---|---|
| running | dead port `19999` | `archon-search is starting up. Please wait…` ← **wrong** |
| stopped | dead port `19999` | `archon-search serve is not running. Start it first with: archon-search serve` |
| stopped | default (no flag) | `archon-search serve is not running. Start it first with: archon-search serve` |
| no `~/.archon-search/` at all | dead port `19999` | `archon-search serve is not running. Start it first with: archon-search serve` |

So the CLI answers for the *default/local* instance while the operator asked about the
instance named by `--api-url`. That is the contract `40_running_the_server.md:122` rules
out: the flags select the target server and "there is no in-process fallback".

Confirmed from the other direction by S13
(`tests/test_050_s07_s14_ingestion.py::TestS13WriteWithoutServer::test_error_mentions_server`),
which stops the managed service and passes — the documented message is produced correctly
whenever no service is live on the default port.

**Operational impact** (why this is more than wording): in the multi-instance topologies of
`150_multi_instance_setup.md`, an operator who typos a port or targets a down instance is
told to wait for a startup that will never finish, while the instance they actually asked
about stays broken and undiagnosed.

### What should happen
`docs/UserManual/100_jobs_and_async_operations.md:11` — "On connection refused they print
`archon-search serve is not running. Start it first with: archon-search serve`
(`cli/_helpers.py:_SERVER_NOT_RUNNING_MSG`)".

`docs/UserManual/50_ingestion_and_collections.md:16` — write commands "exit `1` with
`archon-search serve is not running. Start it first with: archon-search serve`".

`docs/UserManual/40_running_the_server.md:122` — "They accept `--api-url` / `--api-key` and
print a friendly message on connection refused; there is no in-process fallback."

The exit code (`1`) is already correct. The message must describe the server that
`--api-url` named.

### Affected commands
All seven write commands from `docs/UserManual/40_running_the_server.md:112-120` that the
suite exercises with an unreachable `--api-url` — every one reproduces identically:

| Scenario | Command |
|---|---|
| S530 | `collection migrate <name>` |
| S531 | `collection reindex <name>` |
| S532 | `collection reindex-metadata <name>` |
| S533 | `collection remove <name>` |
| S534 | `ingest --path <path>` |
| S537 | `sync` |
| S323 | `graph build-communities <collection>` |

### Steps to reproduce
```bash
# 1. Wizard-installed instance running on the default port
archon-search wizard --profile minimal --non-interactive --skip-preload
curl -s http://127.0.0.1:8765/ready     # {"ready":true, checks all "ok"}

# 2. Any write command at a port nothing is listening on
archon-search collection migrate foo --api-url http://127.0.0.1:19999 --api-key dummy
# exit 1, stderr: archon-search is starting up. Please wait for it to finish loading models, then retry.

# 3. Contrast — stop the local service and repeat step 2
archon-search stop
archon-search collection migrate foo --api-url http://127.0.0.1:19999 --api-key dummy
# exit 1, stderr: archon-search serve is not running. Start it first with: archon-search serve
```

Substitute any command from the table above — all seven behave identically in both states.

### Evidence
```
E   AssertionError: output missing the documented connection-refused message
E     (UserManual/100_jobs_and_async_operations.md:11)
E     stdout:
E     stderr: archon-search is starting up. Please wait for it to finish loading models, then retry.
E
E   assert 'archon-search serve is not running. Start it first with: archon-search serve' in
E     ('' + 'archon-search is starting up. Please wait for it to finish loading models, then retry.\n')
```

Identical assertion text in all seven. Run 2026-08-18 against v26.8.1980 on a freshly
wizard-installed instance: S530/S531/S532/S533/S534/S537/S323 each `1 failed`, with every
other test in those seven files passing.
