# Manual Test: LCD T-4 — Large community on a real claude login

Plan task T-4 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-tasks.md`; covers the real-login arms of S64 and S4 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-team-plan.md` (graph enrichment on `claude_cli` through the shared client: prompt on standard input, `extraction_rate_limit_rpm` applied; a failing provider leaves communities without a summary). The automated tests `test_e2e_graph_build_on_claude_cli_stdin_prompt_and_rpm_throttle` and `test_e2e_graph_provider_down_communities_without_summary` in `tests/integration/test_lcd_t4_graph_claude_cli_e2e.py` drive the same build through the real app against `tests/_fake_claude.py`; they cannot show what the real `claude` does with a prompt of several hundred kilobytes, how long it takes, or that the calls land on the login.

**Executed on 2026-11-01** (results at the end). The commands were written against the code as implemented (`archon_search/community_builder.py`, `archon_search/llm_client/claude_cli_client.py`, `archon_search/llm_client/rate_limit.py`, `archon_search/server/routes_graph.py`, `archon_search/graph_store.py`). Corrections found by the run, applied below: (a) the original corpus yielded one entity, because the pinned extractor labels only person, concept, system and event (organisations and cities are not extracted) and the repeated sentence gave it no second entity: the corpus now names people and systems; (b) two documents are enough for two communities (the third document only added a billed call per arm); (c) the run used `ARCHON_SEARCH_DATA_DIR=/tmp/lcd-manual/t4/data` (a copy of `~/.archon-search/models` seeded into it with `cp -cR` so nothing is downloaded or written under the real data dir): with that variable set, the key file is `$ARCHON_SEARCH_DATA_DIR/.search.env`, the database is `$ARCHON_SEARCH_DATA_DIR/search` (not `[database] db_path`) and the log is `$ARCHON_SEARCH_DATA_DIR/logs/archon-search.log` (not `[logging] log_file`); (d) the positive control for the leak watcher must not be `sh -c 'sleep 20' 'storage replication findings'` (the shell execs the lone `sleep` and the argument vanishes from the process list): use `sh -c 'sleep 20; true' 'storage replication findings'`.

## Prerequisites

- `uv sync --dev --extra graph` (the `graph` extra of `pyproject.toml`) plus `curl` and `jq`. The first graph extraction downloads the pinned GLiNER checkpoint (see the `[graph]` notes in `archon-search.toml.example`), so the first ingest needs network access and time.
- A `claude` binary logged in to the account that should be billed (`claude --version`; the flags were checked against 2.1.308). The calls use the login's quota (a subscription has no per-call bill; the account's usage page is the evidence). One summarised community is a prompt of about 80,000 tokens, so every summarised community costs that much quota: record how many communities the build makes (step 4) before running the other arms.
- Isolate the store and the port from the installed service: `mkdir -p /tmp/lcd-t4`, run `archon-search stop` first if the installed service is running, and write the scenario's TOML to `/tmp/lcd-t4/archon.toml`. Besides the blocks below it MUST hold `[server] port = 18765`, `[database] db_path = "/tmp/lcd-t4/db"` (the block below adds `chunk_size` to the same section), `[logging] log_file = "/tmp/lcd-t4/server.log"` and `level = "INFO"`. The server logs to that file, not to the terminal. Do not set `ARCHON_SEARCH_CONTAINER=1` (it binds 0.0.0.0) and make sure `ARCHON_SEARCH_DATA_DIR`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_KEY_FILE` and `ARCHON_SEARCH_API_KEY` are unset (they override these keys, and an exported API key is persisted to `~/.archon-search/.search.env`, which the installed service shares). The database, the log and the port are isolated; the API key file, the jobs file and the downloaded models still live under `~/.archon-search` and are shared with the installed service. Truncate the log before every start with `: > /tmp/lcd-t4/server.log` (it is appended across restarts) and start in the foreground with `uv run archon-search serve --config /tmp/lcd-t4/archon.toml`; stop it with Ctrl-C of that command (`archon-search stop` acts only on the installed service). After every start wait for `/ready`.
- Against the scratch server: `API=http://127.0.0.1:18765`, `TOKEN=$(grep ARCHON_SEARCH_API_KEY ~/.archon-search/.search.env | cut -d= -f2)` (the file appears at the first server start) and a finished warm-up (`curl -s $API/ready | jq .ready` prints `true`; a rebuild posted earlier answers 503).
- Direct `claude` commands run with only the variables the client's own allowlist passes, so a stray `ANTHROPIC_API_KEY` in your shell cannot make them pass or bill elsewhere. Define once: `CE='env -i PATH="$PATH" HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}"'` and run them as `eval "$CE claude ..."`.

---

## 1 — Very large community on a real claude login (S64, S4)

**Why manual:** needs a real `claude` login and real GLiNER extraction. The automated tests build the large community from a stubbed extractor and answer from a fake binary; only the real binary shows that a prompt far above any argument limit is accepted on standard input, that the call fits the timeout, and that it is billed to the login.

**Setup of the corpus (real prose with named people and systems, two documents of about 330 KB each, so each community's prompt is above 300 KB, roughly 80,000 tokens, well inside the model's window):** save the snippet as `make_corpus.py` in the scratch directory (not committed) and run it with `uv run python`; it writes `corpus/*.md`.

```python
from pathlib import Path

A = ("Alice Johnson maintains the Borealis system, which uses PostgreSQL and Kafka. Borealis depends on Kafka for the event stream, "
     "and Alice Johnson reviewed the storage replication findings with Bruno Keller. Bruno Keller operates PostgreSQL and the Borealis dashboard. ")
B = ("Bob Martinez maintains the Meridian platform, which uses Cassandra and RabbitMQ. Meridian depends on RabbitMQ for the job queue, "
     "and Bob Martinez reviewed the storage replication findings with Carla Souza. Carla Souza operates Cassandra and the Meridian gateway. ")
TARGET_CHARS = 330_000

out = Path("/tmp/lcd-t4/corpus")
out.mkdir(parents=True, exist_ok=True)
for name, para in {"aardvark": A, "barnacle": B}.items():
    paragraph = para * 6
    text = "\n\n".join([paragraph] * (TARGET_CHARS // len(paragraph)))
    (out / f"{name}.md").write_text(text, encoding="utf-8")
    print(name, len(text))
```

**Steps:**
- [ ] Work from `cd /tmp/lcd-t4` for the direct `claude` calls (run from the repository, `claude` would load its project files and save a session there). Confirm the binary and login: `claude --version` and `eval "$CE claude -p 'Reply with one word: ok'"` (a logged-out binary prints `Not logged in`). Positive control for the isolation: `ANTHROPIC_API_KEY=sk-ant-invalid claude -p hi` must fail with an authentication error; if it succeeds the invalid key is ignored, so a pass of the plain call proves nothing about which credential was used. Take a marker after these calls: `touch /tmp/lcd-t4/marker`.
- [ ] Write `/tmp/lcd-t4/archon.toml` with the isolation keys of the prerequisites and:

```toml
[database]
chunk_size = 4096

[graph]
enabled = true
provider = "claude_cli"
extraction_timeout_seconds = 300.0
community_summary_chunks = 10000
extraction_rate_limit_rpm = 60
```

  A blank `extraction_model` is the claude default model (`load_config` fills `DEFAULT_FAST_MODEL`; the client then sends no `--model`). `chunk_size = 4096` (tokens) keeps the community's passage selection cheap: with the default 512 the selection over a community's chunks is cubic in their number and runs on the server's event loop, so the server looks hung (`/jobs` and `/ready` stall) for a long time. Truncate the log and start the server; wait for `/ready`.
- [ ] Ingest the corpus: `uv run archon-search ingest --path /tmp/lcd-t4/corpus --collection lcd-t4 --wait --api-url $API --api-key $TOKEN`. Then check the graph has at least two entities (a build over fewer than two nodes writes one community and never calls the model): `curl -s $API/status -H "Authorization: Bearer $TOKEN" | jq '.graph.collections[] | select(.collection=="lcd-t4") | .node_count'`. Expected: 2 or more. If it prints 0 or 1, first look in `/tmp/lcd-t4/server.log` for a skipped prose extraction (an unavailable GLiNER checkpoint skips it without failing the ingest; fix the download and re-ingest); only if the model ran, the prose is too thin: use more entity-rich prose and re-ingest before going on.
- [ ] Watch the process list while the build runs. Positive control first: `sh -c 'sleep 20' 'storage replication findings' &` and the watcher below must print a `LEAK` line while it sleeps. In a second terminal: `while true; do ps -Aww -o args= | grep -q "[s]torage replication findings" && echo "LEAK $(date +%T)"; sleep 0.5; done` (`-ww` stops `ps` from cutting the line at the terminal width). Expected for the whole build: no `LEAK` line (the passages are never in an argv). In a third terminal watch the server's children: `SERVER=$(pgrep -n -f "archon-search serve --config /tmp/lcd-t4")` and `while true; do pgrep -P $SERVER -fl claude; sleep 1; done`. Expected: during a summary call one `claude` child is listed (the positive control for the later no-child checks) and none between calls.
- [ ] Start the build: `JOB=$(curl -s -X POST $API/graph/lcd-t4/rebuild-communities -H "Authorization: Bearer $TOKEN" | jq -r .job_id)` and poll `curl -s --max-time 30 $API/jobs/$JOB -H "Authorization: Bearer $TOKEN" | jq '{status, result, error}'` until the status is terminal (a large prompt can take a minute or more per community). Expected: `DONE` and `result.communities_built` of 2 or more. Note `communities_built`: the arms below need at least 2 communities (connected entities can merge into one), and every arm costs one prompt per summarised community.
- [ ] Read back the stored communities and the prompt size (Ctrl-C the server first; the data is in `/tmp/lcd-t4/db`). Save as `/tmp/lcd-t4/read.py` and run `uv run python /tmp/lcd-t4/read.py`:

```python
import asyncio

from archon_search.graph_store import GraphStore
from archon_search.store import SearchStore

COLLECTION, NAMESPACE, DB = "lcd-t4", "default", "/tmp/lcd-t4/db"


async def main() -> None:
    graph, search = GraphStore(DB), SearchStore(DB)
    await graph.connect()
    await search.connect()
    try:
        nodes = {n.id: n for n in await graph.get_all_nodes(COLLECTION, ns=NAMESPACE)}
        for community in await graph.list_community_representatives(COLLECTION, NAMESPACE):
            docs = sorted({nodes[e].source_doc_id for e in community.entity_ids if e in nodes})
            picked = set(community.representative_chunk_ids)
            passage_bytes = 0
            for doc in docs:
                for chunk in await search.get_chunks_for_doc(COLLECTION, doc):
                    if chunk["chunk_id"] in picked:
                        passage_bytes += len(chunk["text"].encode())
            text = community.summary_text
            names = [nodes[e].entity_name for e in community.entity_ids if e in nodes]
            print(community.community_id[:8], names, "passage bytes:", passage_bytes, "summary:", None if text is None else text[:120])
    finally:
        await search.disconnect()
        await graph.disconnect()


asyncio.run(main())
```

  Expected: every community has a non-empty summary that talks about its own entities (Acme, Globex or Initech material, not another community's) and at least one community has `passage bytes` above 300000 (the prompt carries the passages plus about 700 characters of template, so the prompt is that large). The log (`/tmp/lcd-t4/server.log`) holds no `LLM summary failed` line and no passage text (`grep -c "storage replication findings" /tmp/lcd-t4/server.log` prints `0`).
- [ ] Throttle arm (S64): set `extraction_rate_limit_rpm = 1`, truncate the log, start the server and wait for `/ready`, then wait another 60 s (the limiter's window opens when the server builds it and a new one opens at the first call after it has run out, so the first call of the build must find a fresh window), POST the rebuild again (a new `JOB`) and read back. Expected: the job is `DONE`; exactly one community has a summary and the others show `summary: None`, and the log holds exactly one WARNING containing `rate limit exhausted (1 rpm); retry later`. If the first call took longer than 60 s, a later community can legitimately find a new window and be summarised; the arm is then inconclusive: repeat with the call durations noted from the log timestamps.
- [ ] Failure arm (S4): set `extraction_timeout_seconds = 1.0` and `extraction_rate_limit_rpm = 60`, truncate the log, restart, wait for `/ready`, rebuild and read back. Expected: the job is `DONE`, every community has `summary: None`, the log holds exactly one `LLM summary failed` WARNING, and the third terminal's watcher lists no `claude` child within 2 s of the job ending (restart the watcher with the new `SERVER` pid).
- [ ] No session saved: the client runs `claude` in a fresh empty temp directory named `archon-claude-cli-*` under `$TMPDIR`, so a saved session would leave a project directory whose name carries it: `find ~/.claude/projects -maxdepth 1 -newer /tmp/lcd-t4/marker -iname "*archon-claude-cli*"`. Expected: prints nothing (`--no-session-persistence`). Positive control for the check: from a fresh `D=$(mktemp -d -t archon-claude-cli-XXXXXX)` run `(cd "$D" && eval "$CE claude -p 'say ok'")` (this one saves a session) and the same `find` must now list the directory; if it does not (a `CLAUDE_CONFIG_DIR` elsewhere, or a different slug scheme), the empty result above proves nothing.
- [ ] Usage: after the runs, open the usage page of the logged-in account; the summary calls appear there.

**Pass criterion:** the build over prompts above 300 KB ends `DONE` with real summaries, the passages never appear in the process list or the log (the counter showed `1` for its positive control), the `rpm = 1` arm summarises one community and logs the single `rate limit exhausted` WARNING, the one-second timeout arm leaves every community without a summary with one WARNING and no `claude` child behind, no session is saved, and the usage lands on the login.

---

## Results

Record per item: date, `claude --version`, model, measured values (`communities_built`, passage bytes per community, seconds per call, number of summarised communities per arm), pass or fail.

Run 2026-11-01, `claude` 2.1.308 (real login), scratch dir `/tmp/lcd-manual/t4`, port 18740, two-document corpus (329,186 and 330,218 bytes), `[graph] provider = "claude_cli"` with a blank model (`extraction_model='claude-haiku-4-5-20251001'` in the log, no `--model` on the argv). Real `claude` calls billed to the login: about 10 (1 plain control, 2 healthy build, 1 throttle arm, up to 2 one-second-timeout attempts, 1 session control, 2 in the cancel-by-DELETE probe, 1 in the shutdown probe), plus 1 call with an invalid key that the CLI rejected (no billing).

| Item | Date | Versions and models | Measured | Pass/fail |
|------|------|---------------------|----------|-----------|
| 1a Auth isolation controls | 2026-11-01 | claude 2.1.308 | `env -i ... claude -p 'Reply with one word: ok'` printed `ok`; `ANTHROPIC_API_KEY=sk-ant-invalid claude -p hi` printed `Invalid API key · Fix external API key` (positive control: an invalid key is not ignored). | PASS |
| 1b Ingest and graph | 2026-11-01 | GLiNER pinned checkpoint | 9 entities, 16 edges (4 + 5 entities per document); the original corpus gave 1 entity (see corrections). | PASS |
| 1c Healthy build, passages > 300 KB on stdin | 2026-11-01 | claude 2.1.308, default model | `communities_built` 2; job DONE in about 15 s (calls about 6 s and 7 s); passage bytes 330,218 and 329,186; both summaries non-empty and about their own entities (Meridian/Cassandra/RabbitMQ/Bob Martinez/Carla Souza; Borealis/PostgreSQL/Kafka/Alice Johnson); one `claude` child per call, listed with `-p --output-format text --tools  --no-session-persistence --disable-slash-commands --strict-mcp-config --max-turns 1 --safe-mode` (the real binary accepted every flag); leak watcher printed 0 `LEAK` lines (positive control printed 7); `grep -c "storage replication findings"` on the log 0; no `LLM summary failed` line. | PASS |
| 1d Throttle arm (S64), `extraction_rate_limit_rpm = 1` | 2026-11-01 | claude 2.1.308 | Fresh window (server ready + 62 s): job DONE, `communities_built` 2, exactly 1 summary and 1 `summary: None`; 1 `claude` call; exactly one WARNING `LLM summary failed ... claude_cli: rate limit exhausted (1 rpm); retry later`; whole build 6 s so the new-window caveat did not apply. | PASS |
| 1e Failure arm (S4), `extraction_timeout_seconds = 1.0` | 2026-11-01 | claude 2.1.308 | Job DONE in 2 s, both communities `summary: None`, one WARNING `LLM summary failed ... claude_cli: no answer within 1s`; `pgrep` found no `claude` process 4 s after the job ended. The 1 s child watcher printed no line at all (a child that lives at most 1 s can fall between two polls), so this arm has no in-run positive control for the child; the positive control is item 1c. | PASS |
| 1f No session saved | 2026-11-01 | claude 2.1.308 | `find ~/.claude/projects -maxdepth 1 -newer marker -iname "*archon-claude-cli*"` printed nothing after the whole run; positive control (`claude -p 'say ok'` from a fresh `archon-claude-cli-*` temp dir) made the same `find` list that project directory (removed afterwards with `trash`). | PASS |
| 1g Usage page of the login | 2026-11-01 | | The usage page of the account cannot be opened from this environment. | BLOCKED: account usage page not reachable by the agent; the calls ran on the login (real answers returned), but billing to it is not independently observed |
| 1h Extra: kill on cancel (not in the original steps) | 2026-11-01 | claude 2.1.308 | `DELETE /jobs/<rebuild>` moved the job to CANCELLING but the rebuild kept running and finished `DONE` with `communities_built` 2 after about 10 s (documented behaviour: a rebuild job is not stopped by DELETE, see the comment above `_REBUILD_ACTIVE_STATUSES` in `routes_graph.py`; not an LCD defect). Server shutdown (SIGINT) during an in-flight summary call: the `claude` child existed at t-0 and 0 `claude` processes and 0 server processes remained from t+1 s on. | PASS (shutdown kill); DELETE does not cancel a rebuild (existing, documented) |

Defects found: none in production code; no code was changed. Processes left running: none (servers and watchers stopped, `ps` empty).

## Acceptance

Accept when item 1 passes: a community prompt above 300 KB reaches the real `claude` on standard input and returns a summary, the request cap throttles the build with one WARNING, a failing or slow call leaves communities without a summary and without a leftover child, and the usage is billed to the login. Status after the 2026-11-01 run: every criterion is observed except the usage page (1g, BLOCKED), so the "billed to the login" clause stays unproven.
