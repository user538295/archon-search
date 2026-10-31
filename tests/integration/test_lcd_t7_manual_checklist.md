# Manual Test: LCD T-7 — Live descriptions on every provider and real `think` behaviour

Plan task T-7 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-tasks.md`; covers the live arms of S50, S53 and S54 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-team-plan.md` (descriptions on `ollama`, `llama_cpp`, `claude_cli`, `anthropic` and `openai`; the Ollama context change; the `think` setting on a real reasoning model). The automated tests in `tests/integration/test_lcd_t7_bad_output_e2e.py` (`test_e2e_thinking_only_output_stores_failed_keeps_old_description`, `test_e2e_overlong_answer_stored_trimmed_to_one_line_within_600`, `test_e2e_cloud_retry_sends_excerpts_twice`, `test_e2e_think_setting_rides_on_every_ollama_call_retry_included`) drive the real app against `tests/_fake_model_server.py`; they cannot show what a real model, a real `claude` login or a real cloud account does with the same prompt.

**Not yet executed.** The commands were written against the code as implemented (`archon_search/description_generator.py`, `archon_search/description_lifecycle.py`, `archon_search/llm_client/*_client.py`, `archon_search/server/llm_composition.py`, `archon_search/cli/collection.py` `info`). S50 and the real-model arms of S53 and S54 stay unproven until the Results table at the end is filled in.

**What was observed while writing this checklist (2026-10-31, development machine; no server was started and nothing was ingested):** `ollama` 0.40.13 has `qwen3.6:35b-a3b-128k` (about 24 GB) and `llama3.1:8b`; `claude` 2.1.308 and `llama-server` 0.7.0 (`llama-server --version`) are installed. One direct call through `OllamaClient` (the class the server uses, no server involved) against `llama3.1:8b` with a one-sentence prompt gave: `think` on -> `LlmThinkingNotSupported` with reason `HTTP 400: "llama3.1:8b" does not support thinking`; `think` off and `default` -> a normal one-sentence answer. So a model without thinking support behaves as the contract says at the client level; the WARNING text the server logs for it was NOT observed live (the generator logs it, see item 2). `ollama ps` printed a `CONTEXT` column (4096 for that call), which item 1 uses. No cloud key, no `claude` login test and no llama-server model were exercised.

## Prerequisites

- `uv sync --dev` (it installs the `ollama`, `hyde` (for `anthropic`) and `openai-provider` extras too), `curl` and `jq`. Run from the repository root.
- Isolate the store and the port from the installed service: `mkdir -p /tmp/lcd-t7`, run `archon-search stop` first if the installed service is running, and write the scenario's TOML to `/tmp/lcd-t7/archon.toml`. Every TOML block below holds the isolation keys; the server logs to `/tmp/lcd-t7/server.log`, not to the terminal. Do not set `ARCHON_SEARCH_CONTAINER=1` (it binds 0.0.0.0) and make sure `ARCHON_SEARCH_DATA_DIR`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_KEY_FILE` and `ARCHON_SEARCH_API_KEY` are unset (they override these keys). The scratch server still shares `~/.archon-search/archon-search-jobs.json` and the key file with the installed service; `[maintenance] failed_ingest_retry = false` (in the skeleton) stops it from re-ingesting an old failed job of the real installation into the scratch store, which matters most for the cloud arms D and E. Port 18767 is also used by the T-6 checklist: do not run both at once.
- Start the scratch server from the repository root with `: > /tmp/lcd-t7/server.log; uv run archon-search serve --config /tmp/lcd-t7/archon.toml` (a second terminal; Ctrl-C stops it). Then `API=http://127.0.0.1:18767`, `TOKEN=$(grep ARCHON_SEARCH_API_KEY ~/.archon-search/.search.env | cut -d= -f2)` (the file appears at the first server start) and a finished warm-up (`curl -s $API/ready | jq .ready` prints `true`).
- Corpus: `mkdir -p /tmp/lcd-t7/corpus /tmp/lcd-t7/more && cp Documentation/UserManual/1*.md /tmp/lcd-t7/corpus/` (readable prose). The growth batch for regeneration: `cp Documentation/UserManual/0*.md Documentation/UserManual/2*.md /tmp/lcd-t7/more/`; check `ls /tmp/lcd-t7/corpus | wc -l` and `ls /tmp/lcd-t7/more | wc -l`: the second count must be at least 20% of the first (a change of 20% or more in the document count asks for a new description); trim or extend the lists until it is. Copy `more/*` into `corpus/` only when a step says so.
- Every run starts from a first ingest: stop the server, `trash /tmp/lcd-t7/db` (when it exists), take the growth files out of the corpus again (`ls /tmp/lcd-t7/more | while read f; do [ -e "/tmp/lcd-t7/corpus/$f" ] && trash "/tmp/lcd-t7/corpus/$f"; done`; a corpus that already holds them shows 0% growth and no new description is asked for), start the server again, wait for `/ready`.
- Ingest: `uv run archon-search ingest --path /tmp/lcd-t7/corpus --collection lcd-t7 --wait --api-url $API --api-key $TOKEN` (returns before the model answers).
- Read a result: `uv run archon-search collection info lcd-t7 --api-url $API --api-key $TOKEN` or `curl -s $API/collections/lcd-t7 -H "Authorization: Bearer $TOKEN" | jq '{description, description_source, description_status}'`. The status reads `pending` while the model works; poll until it is `ok` or `failed`. Length command (the stored text is overwritten by the next run, so run it per provider): `curl -s $API/collections/lcd-t7 -H "Authorization: Bearer $TOKEN" | jq -r .description | awk '{ print length($0), NR }'` prints `<characters> 1` for one line (an empty description prints nothing useful: it is a failure here).
- The scratch TOML skeleton (the `[description]` block is the part each step replaces):

```toml
[server]
port = 18767

[database]
db_path = "/tmp/lcd-t7/db"

[logging]
log_file = "/tmp/lcd-t7/server.log"
level = "INFO"

[maintenance]
failed_ingest_retry = false

[description]
# provider-specific keys of the step
```

---

## 1 — Live descriptions on every provider (S50, S53)

**Why manual:** needs real models (`ollama`, `llama-server`), a real `claude` login and cloud keys; the automated tests answer from a fake server, so they prove the plumbing and the output rules but not that real text is sensible or that a changed Ollama context really reloads the model.

**Steps (A to E and the two Ollama arms; each starts from a first ingest as described in the prerequisites; for every provider that gives text also run the length command, its output must show one line of 600 characters or fewer and no `<think>`-style tag; an over-long first answer is retried once by design and is not visible in the stored text, the automated test covers it):**
- [ ] A, `ollama`: `[description]` = `enabled = true`, `provider = "ollama"`, `model = "llama3.1:8b"`, `ollama_context_tokens = 4096`, `timeout_seconds = 120.0`. Ingest, wait, read, run the length command. Expected: status `ok`, source `generated`, a one-line description of at most 600 characters that talks about the corpus (not about the prompt), and no WARNING from `archon_search.description_generator` in `/tmp/lcd-t7/server.log`. The generator logs nothing but WARNINGs, so `grep -c "description_generator" /tmp/lcd-t7/server.log` prints `0` for a clean run; the positive control for that grep is a deliberate failure (a wrong `model` name makes the status `failed` and the grep print `1`). A run with `ollama_context_tokens` unset logs the one assumed-window WARNING from the startup composition instead (`grep -i "context" /tmp/lcd-t7/server.log`).
- [ ] A2, context change (S50): continue from A on the same collection. Stop the server, set `ollama_context_tokens = 8192`, start it and wait for `/ready`, copy `/tmp/lcd-t7/more/*` into `/tmp/lcd-t7/corpus/`. IMMEDIATELY before the ingest run `ollama ps`: it must list `llama3.1:8b` with `CONTEXT` 4096 (the reload precondition; Ollama unloads an idle model after about five minutes, and the restart may have taken that long; if the list is empty or the model shows another context, rerun A and be quicker, otherwise the later `8192` proves nothing). Ingest again (the growth asks for a new description) and run `ollama ps` right after the status leaves `pending`. Expected: `CONTEXT` reads `8192` (the model was reloaded with the new window), the description is regenerated with status `ok`, and the second call worked. A `CONTEXT` that still reads 4096 means the model was not reloaded: record it as `fail`.
- [ ] A3, small window smoke (S50; this does NOT prove the 20% estimate margin, that is measured by the T-2 checklist): start from a first ingest again with `ollama_context_tokens = 2048`. The input budget is then small (about (2048 - 1024) / 1.2 minus the template, in gpt2 tokens), so the sample is a few excerpts. Expected: status `ok`, no `failed` with a window message, and in the Ollama server log (`~/.ollama/logs/server.log` on a macOS app install; the wording differs between releases) no `truncating input prompt` line for the call; record the log evidence as `n/a` when that log is not reachable.
- [ ] B, `llama_cpp`: start `llama-server -m <a local chat .gguf> --port 8081 --ctx-size 8192` and wait for `GET http://127.0.0.1:8081/props` to answer. `[description]` = `enabled = true`, `provider = "llama_cpp"`, `model = "local"`, `llama_cpp_base_url = "http://127.0.0.1:8081"`, `timeout_seconds = 120.0`. Expected: status `ok` and sensible text; the log shows no WARNING about a failed `/props` probe (a failed probe warns once per server and keeps the assumed 4096 window).
- [ ] C, `claude_cli`: needs `claude --version` and a logged-in account. Check the login without loading this repository's project files: `cd /tmp/lcd-t7 && env -i PATH="$PATH" HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" claude -p 'Reply with one word: ok'` (a logged-out binary prints `Not logged in`). `[description]` = `enabled = true`, `provider = "claude_cli"`, `timeout_seconds = 120.0` (a blank `model` is the claude default). Expected: status `ok` and sensible text. Logged-out arm, only if you can produce a logged-out binary (for example start the server with `CLAUDE_CONFIG_DIR=/tmp/lcd-t7/empty-claude`, an empty directory the client's environment allowlist passes on): status `failed`, no earlier description, and one WARNING from `archon_search.description_generator` that carries the binary's `Not logged in` text and no excerpt.
- [ ] D, `anthropic`: `export ANTHROPIC_API_KEY=<a real key>` in the shell that starts the server; `[description]` = `enabled = true`, `provider = "anthropic"`, `model = "<a current Claude model id>"`, `timeout_seconds = 60.0`. Expected: status `ok` and sensible text. With the variable unset the generation is `failed` with a WARNING that says `ANTHROPIC_API_KEY is not set` and names no excerpt.
- [ ] E, `openai`: `export OPENAI_API_KEY=<a real key>`; `[description]` = `enabled = true`, `provider = "openai"`, `model = "<a current OpenAI model id>"`, `timeout_seconds = 60.0`. Expected: status `ok` and sensible text. A reasoning model id may need a longer `timeout_seconds`.
- [ ] Cloud retry sends the excerpts twice (S53, informational): not observable from the stored text. If a provider's dashboard lists requests, a generation whose first answer was over 600 characters shows two requests with the same excerpts. Record `n/a` when no over-long answer happened.

**Pass criterion:** each of the five providers reaches `ok` with sensible one-line text of at most 600 characters (checked per provider by the length command of the prerequisites) and no unexpected WARNING; the changed Ollama context shows the new `CONTEXT` in `ollama ps` after a verified reload and the call works; the 2048-token window stays `ok` with no truncation line.

---

## 2 — Real `think` behaviour on a reasoning model (S53, S54)

**Why manual:** needs a real reasoning model such as `qwen3.6:35b-a3b-128k` (about 24 GB; whether the thinking switch works and whether the 1024-token answer ceiling lets a thinking model finish can only be judged on a model that really thinks). The automated tests show what is sent on the Ollama wire and what happens to a thinking-only answer, not what a real model does with it.

**Steps (each arm, unless it says otherwise: a first ingest of `/tmp/lcd-t7/corpus`, `[description]` = `enabled = true`, `provider = "ollama"`, `model = "qwen3.6:35b-a3b-128k"`, `ollama_context_tokens = 8192`, `timeout_seconds = 300.0` plus the `think` line of the arm; note the seconds from the ingest until the status leaves `pending`):**
- [ ] `think = false`: Expected: status `ok`, a normal one-line description, the call is the quickest of the three arms. No WARNING from `archon_search.description_generator`.
- [ ] `think = true`: Expected: status `ok` and a clean one-line description (the thinking is returned apart from the answer or removed by the output rules); the call takes longer than the `false` arm. A status `failed` with the WARNING that points to the `think` setting ("set `think` to false") means the model spent the 1024-token answer ceiling on thinking; that contradicts S54, so record `fail` with the model and the seconds.
- [ ] `think = "default"`: Expected: status `ok`; nothing about thinking is sent, so the model's own default applies. Note which of the two arms above the duration matches. A `failed` status with the `think` WARNING here means the model thinks by default and the 1024-token ceiling cut it off: that contradicts S54 (the ceiling lets a reasoning model finish), so record `fail` with the evidence, do not excuse it.
- [ ] Unsupported model with `think = true` (S54), one process, two generations: `model = "llama3.1:8b"`, `ollama_context_tokens = 4096`, `think = true`. First ingest `/tmp/lcd-t7/corpus`, then copy `/tmp/lcd-t7/more/*` into it and ingest again (a second generation in the same server process). Expected: status `failed` after the first ingest (there is no earlier description to keep) and after the second, no retry, and exactly one WARNING per server process naming the model: `grep -c "has no thinking mode" /tmp/lcd-t7/server.log` prints `1` and the line reads `[description].think is true but the model 'llama3.1:8b' has no thinking mode; set think to "default" or false`. To also see the "earlier description is untouched" half, run the same two ingests first with `think = false` (status `ok` after the first), restart with `think = true`, copy `more/*` and ingest again: the status becomes `failed` and the earlier description is still shown.
- [ ] `think` ignored by cloud and `claude_cli` (S54): with `provider = "claude_cli"` (or `openai` / `anthropic`) and `think = true`, a description still generates (status `ok`); the value has no effect (the automated cloud test already shows that no `think` field reaches an `openai` request). The startup WARNING for an ignored `think` is part of the startup validator task BE-36 of the plan (the task file names no test for it) and does not exist at the commit this checklist was written against: record `n/a` for it.
- [ ] `llama_cpp` thinking switch (S54): with a thinking-capable `.gguf` model on `llama-server` (0.7.0 lists `--chat-template-kwargs` and `--reasoning` in `llama-server --help`; run it with the model's chat template enabled), `think = false` must give a quick direct answer and `think = true` a slower one with a clean description. Record the model; write `n/a` when no thinking-capable `.gguf` model is at hand.

**Pass criterion:** `false` answers directly; `true` and `default` end with a clean one-line `ok` description within the 1024-token answer ceiling; `true` on a model without thinking support gives status `failed` with the single WARNING naming the model. A `true` or `default` arm that ends `failed` with the `think` WARNING FAILS the S54 claim that the ceiling lets a reasoning model finish: record it as `fail` and note the model and the seconds.

---

## Results

Record per sub-item (A, A2, A3, B, C, D, E and each `think` arm), one line each: date, versions (`ollama --version`, `claude --version`, `llama-server --version`), model, measured values (status, description length, seconds from ingest to `ok`, `ollama ps` CONTEXT before and after), pass, fail or `n/a`.

| Item | Date | Versions and models | Measured | Pass/fail |
|------|------|---------------------|----------|-----------|
| 1 A `ollama` | | | | |
| 1 A2 Ollama context change | | | | |
| 1 A3 small window smoke | | | | |
| 1 B `llama_cpp` | | | | |
| 1 C `claude_cli` | | | | |
| 1 D `anthropic` | | | | |
| 1 E `openai` | | | | |
| 1 length command per provider | | | | |
| 2 `think = false` | | | | |
| 2 `think = true` | | | | |
| 2 `think = "default"` | | | | |
| 2 unsupported model, `think = true` | | | | |
| 2 ignored by cloud and `claude_cli` | | | | |
| 2 `llama_cpp` switch | | | | |

## Acceptance

Accept when item 1 passes for all five providers and item 2 passes on a real reasoning model: sensible one-line descriptions within 600 characters, a changed Ollama context reloads the model with calls still working, `think` behaves as documented for `true`, `false` and `default`, and an unsupported `think = true` fails cleanly with one WARNING.
