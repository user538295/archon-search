# Manual Test: LCD T-2 — Real Ollama, llama-server and claude windows and ratios

Plan task T-2 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-tasks.md`; completes S8, S11, S68 and S79 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-team-plan.md` (C1, the shared LLM client). The automated tests drive the same code against `tests/_fake_model_server.py` and `tests/_fake_claude.py`; none of them can show what a real tokenizer, a real server or a real login does. Each item below is non-automatable for the reason stated in it.

**Not yet executed.** The commands were written against the code as implemented (`archon_search/llm_client/`, `archon_search/server/llm_composition.py`, `archon_search/constants.py`). Record the outcome of every item in the Results section at the end.

## Prerequisites

- `uv sync --dev` (the default sync pulls the `ollama`, `openai-provider` and `hyde` extras).
- Read the server log in the terminal where you run `uv run archon-search serve`; every WARNING quoted below appears there.
- Item 1: `llama-server` (llama.cpp) and any GGUF model. Items 2 and 3: `ollama serve` running on `http://localhost:11434` and at least one pulled model (the commands use `llama3.1:8b`; repeat with every model family you intend to support). Item 4: a `claude` binary logged in to the account that should be billed (`claude --version`; verified flags below against 2.1.308).
- Config: before each scenario that starts the server, replace the **entire** contents of `~/.archon-search/archon-search.toml` with the block shown (back up the existing file first).
- Every scenario that starts the server needs `[hyde] enabled = true`, because HyDE is the only feature wired to the shared client so far (`server/app.py`, `app.state.hyde_client` and the `_llm_window_task` background task); the other sections gain the same behaviour with their own tasks.

---

## 1 — Real llama-server window (S8, S11)

**Why manual:** needs a real `llama-server`. The `/props` and `/tokenize` endpoints are exercised nowhere else in the repository except against `FakeModelServer`, so only a real server proves the field names and the exact counts.

**Steps:**
- [ ] Start a server with a known window: `llama-server -m /path/to/model.gguf -c 4096 --port 8080` (the config default is `llama_cpp_base_url = "http://localhost:8080"`; the llama-server default port differs, so pass `--port 8080` or set the URL below).
- [ ] Read the window the code reads: `curl -s http://localhost:8080/props | jq '.default_generation_settings.n_ctx'`. Expected: an integer equal to the `-c` value (note it as `N_CTX`). If the server was started with several slots (`-np`) or a unified KV pool, `n_ctx` here is the per-slot value the code will trust; record what it reports.
- [ ] Count a text exactly the way the client does: `curl -s -X POST http://localhost:8080/tokenize -H 'Content-Type: application/json' -d '{"content": "The quick brown fox jumps over the lazy dog."}' | jq '.tokens | length'`. Expected: an integer, and `jq '.tokens'` is a list of token ids (the code reads `body["tokens"]` and takes its length).
- [ ] Run the client against the server (replace the text with a prompt template of your own to see a bigger count):

  ```bash
  uv run python - <<'PY'
  import asyncio
  from archon_search.llm_client.llama_cpp_client import LlamaCppClient

  async def main() -> None:
      client = LlamaCppClient(model="local", base_url="http://localhost:8080")
      window = await client.probe_context_window(5.0)
      print("window:", window.size, window.source.value)
      template = "The quick brown fox jumps over the lazy dog."
      budget = await client.input_budget(template)
      print("budget:", budget.tokens)

  asyncio.run(main())
  PY
  ```

  Expected: `window: <N_CTX> probed`, and `budget` equals `N_CTX - 1024 - <the /tokenize count of the template>` (`ANSWER_RESERVE_TOKENS = 1024` in `archon_search/constants.py`; `exact_budget` in `archon_search/llm_client/budget.py`).
- [ ] Start Archon with the window read at startup. Config:

  ```toml
  [hyde]
  enabled = true
  provider = "llama_cpp"
  model = "local"
  llama_cpp_base_url = "http://localhost:8080"
  ```

  Run `uv run archon-search serve`. Expected: the server boots and logs **no** `llama_cpp: cannot read the context window` line.
- [ ] Failure arm (S8): stop `llama-server`, restart Archon with the same config. Expected: it still boots and logs exactly one WARNING of the form `llama_cpp: cannot read the context window from localhost:8080/props (<reason>); assuming 4096 tokens` (`ASSUMED_CONTEXT_TOKENS = 4096`; host and port only, no query text).

**Pass criterion:** the `/props` value equals the window the client reports (`probed`), the client budget matches the arithmetic above to the token, and the failed probe logs the single WARNING and assumes 4096.

---

## 2 — Estimate margin on real text (S11)

**Why manual:** needs a real Ollama tokenizer. The unit tests stub the gpt2 counter and the fake Ollama server reports invented counts, so only a real model shows whether the 20% margin covers the difference between the gpt2 estimate and what Ollama actually counts.

**Rule under test:** for an `ollama` feature the budget is `(window - 1024) // 1.2 - gpt2_template` in gpt2 tokens (`OLLAMA_ESTIMATE_MARGIN = 0.20` in `archon_search/constants.py`, `estimated_budget` in `archon_search/llm_client/budget.py`). The margin is sufficient exactly when the real count of any text is at most 1.2 times its gpt2 count.

**Steps:**
- [ ] Prepare one file per text class under `/tmp/lcd-t2/`, each long (about 3000 gpt2 tokens: a few thousand words; short samples distort the ratio): `english.txt` (prose), `python.txt` (source code, for example a few concatenated modules of this repository), `hungarian.txt`, `chinese.txt`, `japanese.txt`, `emoji.txt` (emoji-dense), `digits.txt` (digit-heavy: tables, hashes, numbers).
- [ ] For each file get the gpt2 count the server would use (`TokenCounter.count`, `archon_search/llm_client/token_counter.py`; the pinned gpt2 `tokenizer.json` downloads on first use):

  ```bash
  uv run python -c "import sys; from archon_search.llm_client.token_counter import TokenCounter; print(TokenCounter().count(open(sys.argv[1], encoding='utf-8').read()))" /tmp/lcd-t2/english.txt
  ```
- [ ] For each file get the real Ollama count with a raw prompt (no chat template) and a one-token answer, in a context large enough for the file (use `num_ctx` of at least 8192 so the prompt is not truncated):

  ```bash
  jq -n --rawfile p /tmp/lcd-t2/english.txt '{model:"llama3.1:8b", prompt:$p, raw:true, stream:false, keep_alive:0, options:{num_predict:1, num_ctx:16384}}' | curl -s localhost:11434/api/generate -d @- | jq '.prompt_eval_count'
  ```

  The first token (BOS) may add 0 or 1 to the count; ignore it. `prompt_eval_count` can be missing or low when Ollama reuses a cached prefix, so run each file once per model with a fresh load (`keep_alive:0` above) and, if the value is absent, repeat the call.
- [ ] Compute `ratio = ollama_count / gpt2_count` for every file and record it per model family. Repeat for each Ollama model family you support (for example llama, qwen, gemma, mistral).

**Pass criterion:** for every text class and every model tried, `ollama_count <= 1.2 * gpt2_count` (ratio at most 1.20). A ratio above 1.20 for any class is a failure: record the class, the model and the ratio, and the margin or the documented model support must change before release. A ratio below 1.0 only means the estimate is conservative for that class and is not a failure.

---

## 3 — Real Ollama model maximum (S68)

**Why manual:** needs a real Ollama model. The fake server answers `/api/show` with an invented `model_info`; only a real model proves the key names the code reads (`general.architecture` and `<architecture>.context_length`).

**Steps:**
- [ ] Read the model maximum the code reads: `curl -s localhost:11434/api/show -d '{"model":"llama3.1:8b"}' | jq '.model_info["general.architecture"], .model_info[(.model_info["general.architecture"] + ".context_length")]'`. Expected: the architecture name (for `llama3.1:8b` it was `"llama"`) and a positive integer (it was `131072`). Note it as `MODEL_MAX`.
- [ ] Oversized arm. Set a window above the maximum and boot Archon:

  ```toml
  [hyde]
  enabled = true
  provider = "ollama"
  model = "llama3.1:8b"
  ollama_base_url = "http://localhost:11434"
  ollama_context_tokens = 1000000
  ```

  (use any value above `MODEL_MAX` for a different model). Run `uv run archon-search serve`. Expected: the server boots and logs one WARNING: `[hyde].ollama_context_tokens = 1000000 is above the model's maximum of <MODEL_MAX> tokens (read from Ollama's model details); Ollama lowers the window to that maximum without saying so. Set [hyde].ollama_context_tokens to <MODEL_MAX> or less.`
- [ ] Within-limit arm: set `ollama_context_tokens = 8192`, restart. Expected: no oversized WARNING and no unset WARNING.
- [ ] Unset arm: delete the `ollama_context_tokens` line, restart. Expected: one WARNING `[hyde].ollama_context_tokens is not set: assuming 4096 tokens and sending no num_ctx, so the window Ollama picked for itself applies. Set [hyde].ollama_context_tokens in archon-search.toml.`
- [ ] Unreachable arm: stop Ollama, set `ollama_context_tokens = 1000000`, restart Archon. Expected: it boots; an INFO line says the model's maximum could not be read and the value is not checked; no WARNING.

**Pass criterion:** the three warnings fire exactly once per process with the wording above, carry the real `MODEL_MAX`, and the server boots in every arm.

---

## 4 — Real claude login (S79)

**Why manual:** needs a real `claude` login. The automated tests run a fake `claude` that records its argv, stdin and environment; only the real binary shows that the flags are accepted and that the call is billed to the login. Each real call is billed.

**Steps:**
- [ ] Confirm the binary and login: `claude --version` and `claude -p "Reply with one word: ok"` (a logged-out binary prints `Not logged in`).
- [ ] Run the exact argv of `archon_search/llm_client/claude_cli_client.py` (`_LOCKDOWN_ARGV`, the prompt on standard input, never in the argv), from an empty directory: `cd "$(mktemp -d)" && claude -p --output-format text --tools "" --no-session-persistence --disable-slash-commands --strict-mcp-config --max-turns 1 --safe-mode <<< "Reply with the single word: ok"`. Expected: exit status 0 and the answer `ok` on standard output. `--safe-mode` and `--max-turns 1` must not be rejected as unknown options (`--max-turns` is accepted though `claude --help` does not list it).
- [ ] Add `--model <model>` (the client adds it unless the model is blank or the default fast model): `... --safe-mode --model claude-haiku-4-5-20251001 <<< "Reply with the single word: ok"`. Expected: the same success.
- [ ] Large prompt on standard input: `cd "$(mktemp -d)" && yes "lorem ipsum dolor sit amet" | head -n 20000 | claude -p --output-format text --tools "" --no-session-persistence --disable-slash-commands --strict-mcp-config --max-turns 1 --safe-mode`. While it runs, `ps -Ao args= | grep "[c]laude -p"` shows no prompt text (about 540 KB passed without an argv-size error).
- [ ] No session saved: compare the session store of the logged-in account before and after the calls (for example `ls -R ~/.claude/projects | wc -l`); nothing new for the empty directory.
- [ ] Billing: after the calls, open the usage page of the logged-in account; the calls appear there. With `ANTHROPIC_API_KEY` unset in the shell, nothing is billed to an API key.
- [ ] Through Archon. Config:

  ```toml
  [hyde]
  enabled = true
  provider = "claude_cli"
  model = "claude-haiku-4-5-20251001"
  ```

  Run `uv run archon-search serve` with `ANTHROPIC_API_KEY` and `CLAUDECODE` unset, then POST `/search` with `{"query": "test", "hyde": true}` and a bearer token (`grep ARCHON_SEARCH_API_KEY ~/.archon-search/.search.env | cut -d= -f2`). Expected: `hyde_applied=true` and non-empty `results`.

**Pass criterion:** every real call succeeds with the full argv, the prompt never appears in the process list, no session is saved, the usage lands on the login, and the HyDE search applies on `claude_cli`.

---

## Results

Record per item: date, tool versions (`claude --version`, `ollama --version`, `llama-server --version`), models, measured values, pass or fail.

| Item | Date | Versions and models | Measured | Pass/fail |
|------|------|---------------------|----------|-----------|
| 1 Real llama-server window | | | | |
| 2 Estimate margin (ratio per class and model) | | | | |
| 3 Real Ollama model maximum | | | | |
| 4 Real claude login | | | | |

## Acceptance

All four items passed: the `llama_cpp` window and counts are exact, the 20% margin covers every measured text class on every supported model family, the oversized-window WARNING names the real model maximum, and the real `claude` accepts the lockdown argv and bills the login.
