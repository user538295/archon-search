# Manual Test: LCD T-6 — Real data directory upgrade and clean install without claude-agent-sdk

Plan task T-6 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-tasks.md`; covers the real-data arm of S42 and the clean-environment arm of S49 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-team-plan.md`. The automated tests in `tests/integration/test_lcd_t6_old_store_e2e.py` (`test_e2e_old_store_migrates_columns_and_default_ttl_at_startup`, `test_e2e_old_store_lacking_only_default_ttl_keeps_description_state`) boot the real app on a synthetic old-schema store; they cannot show what a store written by a real older release looks like, and an install test is forbidden in the default and unlocked-dependency lanes.

**Executed 2026-11-01** (see Results). Deviations from the text below, all recorded in the Results table: scratch paths were `/tmp/lcd-manual/t6/...` and ports 18711-18714 instead of `/tmp/lcd-t6` and 18766-18768; item 1 used a data directory built with the previous release (tag `26.10.2296` lineage, commit `822e096e^`) in a scratch dir because `~/.archon-search` holds no store; the old release could not generate descriptions (it needs `ANTHROPIC_API_KEY`, absent), so two descriptions were written through that release's own `SearchStore.update_description` writer.

**State observed while writing this checklist (2026-10-31, development machine):** `~/.archon-search` holds only `logs/` and `models/` (no `search` store directory, no `.search.env`, no `archon-search.toml`), so there was no real collection data to dry-run against and nothing was copied or started. In the repository's `.venv`, `.venv/bin/python -c "import claude_agent_sdk"` fails with `ModuleNotFoundError` and `uv pip show claude-agent-sdk` reports the package not found, so BE-28's removal is visible there; an older venv elsewhere may still hold it (item 2 must use a fresh one).

## Prerequisites

- `curl` and `jq`; `uv`. Run from the repository root.
- Item 1 needs a data directory written by an OLDER release (collections that were described before this feature). Without one the item cannot be run: say so in the Results table rather than building a synthetic store (the synthetic case is the automated test).
- Never point the new version at the original: all commands below work on a copy. Stop the installed service first (`archon-search stop`) and keep it stopped through the copy, so the LanceDB files are copied in a consistent state. If the installation's config sets `[database] db_path` elsewhere, copy that store into `/tmp/lcd-t6/data/search` too (`cp -R ~/.archon-search` alone misses it). `models/` can be several GB: `cp -R` then takes a while.
- Make sure `ARCHON_SEARCH_DATA_DIR`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_KEY_FILE`, `ARCHON_SEARCH_API_KEY`, `ARCHON_SEARCH_CONFIG` and `ARCHON_SEARCH_CONTAINER` are unset in the shell before the steps set their own values (they override the config keys; the container flag binds 0.0.0.0).

---

## 1 — Real data directory upgrade (S42)

**Why manual:** the automated test builds the old schema from the current one by dropping columns; only a store written by a real older release proves that its real rows (real embeddings, centroids, ACL columns) open and read as `generated` / `ok`.

**Setup:**

```sh
mkdir -p /tmp/lcd-t6   # a leftover /tmp/lcd-t6 from an earlier run: move it to the trash first (`trash /tmp/lcd-t6`)
touch /tmp/lcd-t6/marker
cp -R ~/.archon-search /tmp/lcd-t6/data
export ARCHON_SEARCH_DATA_DIR=/tmp/lcd-t6/data
```

With `ARCHON_SEARCH_DATA_DIR` set the store is `/tmp/lcd-t6/data/search` and the API key file is `/tmp/lcd-t6/data/.search.env` (the copy's own, never the original). Write `/tmp/lcd-t6/archon.toml` with at least:

```toml
[server]
port = 18766

[logging]
level = "INFO"
```

Take a baseline of the copied log before the first start: `BASE_LINES=$(wc -l < /tmp/lcd-t6/data/logs/archon-search.log); echo $BASE_LINES > /tmp/lcd-t6/base_lines`.

Add the real installation's `[collections] collections = [...]` list from its config file if you want the startup sync to run on those paths (the collections stay readable through `GET /collections/{name}` without it, because the metadata row makes them visible). With `ARCHON_SEARCH_DATA_DIR` set, `[database] db_path` and `[logging] log_file` are overridden by the environment: the store is `data/search` and the log is `data/logs/archon-search.log`.

**Steps:**
- [ ] Record the old state before starting. Save as `/tmp/lcd-t6/schema.py` and run `uv run python /tmp/lcd-t6/schema.py`:

```python
import asyncio

import lancedb


async def main() -> None:
    db = await lancedb.connect_async("/tmp/lcd-t6/data/search")
    table = await db.open_table("_archon_collection_meta")
    print((await table.schema()).names)
    print([(r["name"], bool(r["description"])) for r in await table.query().to_list()])


asyncio.run(main())
```

  Expected: the column list has no `description_source`, `description_status` (and possibly no `default_ttl_seconds`); note which collections have a non-empty description.
- [ ] Start the new version on the copy: `uv run archon-search serve --config /tmp/lcd-t6/archon.toml` (a second terminal runs the commands below). Wait for `curl -s http://127.0.0.1:18766/ready | jq .ready` to print `true`.
- [ ] `API=http://127.0.0.1:18766; TOKEN=$(grep ARCHON_SEARCH_API_KEY /tmp/lcd-t6/data/.search.env | cut -d= -f2)`. List: `curl -s $API/collections/ -H "Authorization: Bearer $TOKEN" | jq '.[] | {name, description, description_source, description_status}'`. Expected: every collection opens (no 5xx); each one that had a non-empty description in the first step reads `description_source: "generated"`, `description_status: "ok"`; each one with an empty description reads both as `null`.
- [ ] Detail and CLI for one described collection: `curl -s $API/collections/<name> -H "Authorization: Bearer $TOKEN" | jq .` and `uv run archon-search collection info <name> --api-url $API --api-key $TOKEN`. Expected: the description text is unchanged, the CLI prints `description_source: generated` and `description_status: ok`.
- [ ] A metadata write: `curl -s -w '\nHTTP %{http_code}\n' -X PATCH $API/collections/<name> -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"default_ttl_seconds": 3600}'`. Expected: `HTTP 200`, `"default_ttl_seconds":3600` in the body, source and status unchanged. The PATCH answers 404 unless the collection's name equals the name derived from a path in `[collections] collections` (the last path component, as `path_to_collection_name` builds it); choose such a collection, adding its path and restarting if needed. A collection created with a custom `--collection` name cannot pass this step: say so in the Results table instead of marking it failed.
- [ ] Search still works on an old collection: `curl -s -X POST $API/search -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"query": "<a word from a document>", "collections": ["<name>"]}' | jq '.results | length'`. Expected: greater than 0.
- [ ] Stop the server (Ctrl-C), repeat the schema read of the first step. Expected: the columns `description_source`, `description_status` and `default_ttl_seconds` are present. Start the server again and repeat the list call. Expected: identical output (the migration is idempotent).
- [ ] Log check: the server's log is `/tmp/lcd-t6/data/logs/archon-search.log` (`ARCHON_SEARCH_DATA_DIR` forces it there, so `[logging] log_file` in the scratch file is ignored) and it already holds the original installation's history from the copy: use only lines after the baseline line count taken before the first start (the shell that runs `serve` blocks, so read the number from the file written in Setup: `BASE_LINES=$(cat /tmp/lcd-t6/base_lines)`). Do not run this check across 00:00 UTC (the log rotates at midnight UTC and the offset is lost). `tail -n +$((BASE_LINES+1)) /tmp/lcd-t6/data/logs/archon-search.log | grep -cE "ERROR|Traceback"` prints `0`; every WARNING in that range is read and explained in the Results table (an orphan-table WARNING is unrelated). The log file holds only the `archon_search` loggers: also read the `serve` terminal for uvicorn errors such as `Exception in ASGI application` (none expected).
- [ ] Clean up: `unset ARCHON_SEARCH_DATA_DIR`. The original was never started against: `find ~/.archon-search/search -newer /tmp/lcd-t6/marker | head` prints nothing (the marker is the file touched right before the copy, see Setup).

**Pass criterion:** the copy opens with no error, described collections read `generated` / `ok`, empty ones read null, a metadata write succeeds and sticks, search still returns results, and a second start changes nothing.

---

## 2 — Clean-environment install without claude-agent-sdk (S49)

**Why manual:** an install test is forbidden in the default and unlocked-dependency lanes, and a stale development environment still holds the package, so only a fresh environment proves that nothing needs it. In this repository's `.venv` the package is already absent; that shows the lock-file removal, not a fresh install.

**Steps:**
- [ ] Build a fresh environment from the lock file outside the repository's `.venv`: `UV_PROJECT_ENVIRONMENT=/tmp/lcd-t6/venv uv sync --locked` (run from the repository root; `--locked` fails when `uv.lock` does not match `pyproject.toml`). Expected: completes; `grep -c claude-agent-sdk uv.lock pyproject.toml` prints `uv.lock:0` and `pyproject.toml:0`.
- [ ] The package is not installed or importable: `/tmp/lcd-t6/venv/bin/python -c "import claude_agent_sdk"` prints `ModuleNotFoundError: No module named 'claude_agent_sdk'`, and `uv pip list --python /tmp/lcd-t6/venv/bin/python | grep -ic claude` prints `0`. Positive control for the check: `uv pip list --python /tmp/lcd-t6/venv/bin/python | grep -ic click` prints at least `1`.
- [ ] The CLI works: `/tmp/lcd-t6/venv/bin/archon-search --version` and `/tmp/lcd-t6/venv/bin/archon-search config show` exit 0 (lightweight start; the SDK was never imported).
- [ ] The server works with descriptions enabled on a provider that needs no SDK. This step needs network access or a copy of the models: a fresh data directory has an empty `models/`, so the first start downloads the embedder and reranker (and, for ollama, the token-counter tokenizer) and `/ready` stays `false` until then; to skip the download run `mkdir -p /tmp/lcd-t6/clean-data && cp -R ~/.archon-search/models /tmp/lcd-t6/clean-data/models`. Use a data directory of its own: `export ARCHON_SEARCH_DATA_DIR=/tmp/lcd-t6/clean-data; mkdir -p $ARCHON_SEARCH_DATA_DIR` (with it set the store is `clean-data/search`, the log `clean-data/logs/archon-search.log` and the key file `clean-data/.search.env`; never leave it unset, or the server writes into the real `~/.archon-search`). Write `/tmp/lcd-t6/clean.toml` (not the item 1 file, which may list real collection paths) with `[server] port = 18767` and a `[description]` section with `enabled = true`, `provider = "ollama"` (or `claude_cli` with a logged-in `claude`), `model` and `ollama_context_tokens = 8192` (plus `ollama_base_url` for a non-default host). Start `/tmp/lcd-t6/venv/bin/archon-search serve --config /tmp/lcd-t6/clean.toml` in one terminal (its output must show no `ModuleNotFoundError` or traceback) and use another for the rest; wait for `curl -s http://127.0.0.1:18767/ready | jq .ready` to print `true`. Expected: `grep -c ModuleNotFoundError /tmp/lcd-t6/clean-data/logs/archon-search.log` prints `0`. Ingest a small folder that is NOT under the data directory (ingest refuses paths below it): `KEY=$(grep ARCHON_SEARCH_API_KEY /tmp/lcd-t6/clean-data/.search.env | cut -d= -f2)` and `/tmp/lcd-t6/venv/bin/archon-search ingest --path <dir> --collection lcd-t6-clean --wait --api-url http://127.0.0.1:18767 --api-key $KEY`. The description is written after the job ends (the status reads `pending` meanwhile): poll `curl -s http://127.0.0.1:18767/collections/lcd-t6-clean -H "Authorization: Bearer $KEY" | jq '{description, description_source, description_status}'` until the status is no longer `pending`. Expected: `description_source: "generated"`, `description_status: "ok"` when the provider answers. Stop the server (Ctrl-C).
- [ ] Without `[description]`: stop the first server, `export ARCHON_SEARCH_DATA_DIR=/tmp/lcd-t6/clean-data2` (copy `models/` as above or allow the download), write `/tmp/lcd-t6/clean2.toml` with only `[server] port = 18768`, start `/tmp/lcd-t6/venv/bin/archon-search serve --config /tmp/lcd-t6/clean2.toml`, read the new key from `/tmp/lcd-t6/clean-data2/.search.env`, ingest a small folder as collection `lcd-t6-clean2` against `http://127.0.0.1:18768`. Expected: the server starts, the ingest job ends `DONE` and the collection reads `description_status: null`. Stop the server and `unset ARCHON_SEARCH_DATA_DIR`.

**Pass criterion:** a fresh environment built from the lock file has no `claude-agent-sdk`, the package is not importable, the CLI and the server start and work, and descriptions are generated without it.

---

## Results

Record per item: date, installed versions (`archon-search --version`), the old release that wrote the data directory, collections checked, pass or fail.

| Item | Date | Versions and data | Measured | Pass/fail |
|------|------|-------------------|----------|-----------|
| 1 Real data directory upgrade | 2026-11-01 | New: `archon-search 26.10.2296.post1.dev41` (this checkout, venv from `uv sync --locked`). Old: worktree `822e096e^` (`26.10.2296-4-g2866eddc`) in its own `uv sync --locked` venv; store written by that release's server on port 18711: collections `alpha` (2 docs, 64 chunks), `beta` (2 docs, 41 chunks), `gamma` (1 doc, 15 chunks), data dir copied with `cp -cR` before the new version touched it. `alpha`/`beta` descriptions written via the old `SearchStore.update_description` (NOT old-release auto-generation: that needs `ANTHROPIC_API_KEY`); `gamma` left empty. Not the user's real data. | Before: meta columns had no `description_source`/`description_status` (had `default_ttl_seconds`); `alpha`, `beta` described, `gamma` not. New version on the copy (port 18712, `[collections]` listing the three paths): `/ready` true; list: `alpha` and `beta` `generated`/`ok` with descriptions unchanged, `gamma` description `""` and both fields `null`; `collection info alpha` printed `description_source: generated`, `description_status: ok`; PATCH `default_ttl_seconds=3600` HTTP 200 body `default_ttl_seconds:3600`, source/status unchanged; search `alpha` returned 5 results. After stop: columns `description_source`, `description_status` present. Second start: list output byte-identical to the first (`diff` empty); `default_ttl_seconds` still 3600. Log lines after baseline: 0 ERROR/Traceback, 0 WARNING; no `Exception in ASGI application` in the serve output. Caveat: `find ~/.archon-search -newer marker` printed `~/.archon-search/logs/archon-search.log` (a log file only, no store); I started no server against it, the writer is a CLI call without a data dir (my `--version`/`config show` runs or another agent's), not attributed. | PASS |
| 2 Clean install without claude-agent-sdk | 2026-11-01 | Fresh venv `UV_PROJECT_ENVIRONMENT=/tmp/lcd-manual/t6/venv uv sync --locked` (exit 0), `archon-search 26.10.2296.post1.dev41`, Python 3.13.14; models cloned into scratch data dirs with `cp -c` (no downloads). | `grep -c claude-agent-sdk uv.lock pyproject.toml`: 0 and 0. `import claude_agent_sdk`: `ModuleNotFoundError: No module named 'claude_agent_sdk'`; `uv pip list | grep -ic claude` 0, `grep -ic click` 1. `--version` and `config show` exit 0. Server with `[description] provider=ollama model=llama3.1:8b ollama_context_tokens=8192` (port 18713): `/ready` true, `/health` `{"status":"running"}`, 0 `ModuleNotFoundError`/Traceback in the log, no WARNING/ERROR; ingest of 2 files as `lcd-t6-clean` `--wait` complete; after the job the collection read `description_source: generated`, `description_status: ok` with a 2-sentence description from the local Ollama model (no `claude` calls). Without `[description]` (port 18714): server started, ingest of the same folder as `lcd-t6-clean2` completed, collection read `description_source: null`, `description_status: null` (`status` field reads `not_yet_indexed` because the collection has no configured path; not an ingest failure). Servers stopped; no leftover processes. | PASS |

## Acceptance

Accept when item 1 passes on a data directory written by a real older release (described collections read `generated` / `ok`, writes succeed, restart is idempotent) and item 2 passes on a fresh environment (package absent, CLI and server work).
