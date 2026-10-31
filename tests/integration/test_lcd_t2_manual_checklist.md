# Manual Test: LCD T-2 — Real Ollama, llama-server and claude windows and ratios

Plan task T-2 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-tasks.md`; covers S8, S11, S68 and S79 of `Documentation/Backlog/2026-10-30-010-llm-collection-descriptions-team-plan.md` (C1, the shared LLM client). The automated tests drive the same code against `tests/_fake_model_server.py`, `httpx.MockTransport` and `tests/_fake_claude.py`; none of them can show what a real tokenizer, a real server or a real login does. Each item below is non-automatable for the reason stated in it.

**Not yet executed.** The commands were written against the code as implemented (`archon_search/llm_client/`, `archon_search/server/llm_composition.py`, `archon_search/constants.py`). S8, S11, S68 and S79 stay unproven for their real-backend arms until the Results table at the end is filled in.

## Prerequisites

- `uv sync --dev` (the default sync pulls the `ollama`, `openai-provider` and `hyde` extras), plus `curl` and `jq`.
- Item 1: `llama-server` (llama.cpp) and any GGUF model. Items 2 and 3: `ollama serve` on `http://localhost:11434` and at least the models named in the items. Item 4: a `claude` binary logged in to the account that should be billed (`claude --version`; the flags below were checked against 2.1.308).
- Isolate from the installed service: `mkdir -p /tmp/lcd-t2`, run `archon-search stop` first if the installed service is running, and write each scenario's TOML block to `/tmp/lcd-t2/archon.toml` with these extra keys so the scratch server shares nothing with the real store or port: `[server] port = 18765`, `[database] db_path = "/tmp/lcd-t2/db"`, `[logging] log_file = "/tmp/lcd-t2/server.log"` and `level = "INFO"` (the server logs to that file, not to the terminal; do not set `ARCHON_SEARCH_CONTAINER=1`, it binds 0.0.0.0; make sure `ARCHON_SEARCH_DATA_DIR` and `ARCHON_SEARCH_PORT` are unset, they override these keys). Before every start truncate the log with `: > /tmp/lcd-t2/server.log` (it is appended across restarts) and start with `uv run archon-search serve --config /tmp/lcd-t2/archon.toml`. Do not call `POST /sync` against the scratch config: it drops collections the config does not list.
- Scenarios that call `/search` need, against the scratch server (`API=http://127.0.0.1:18765`): the bearer token `TOKEN=$(grep ARCHON_SEARCH_API_KEY ~/.archon-search/.search.env | cut -d= -f2)` (the file appears at the first server start), an ingested collection (`uv run archon-search ingest --path /path/to/some/docs --collection lcd-t2 --wait --api-url $API --api-key $TOKEN`; `COLLECTION=lcd-t2`), and a finished warm-up (`curl -s $API/ready | jq .ready` prints `true`; HyDE is skipped while the embedder is cold). The search call is `curl -s -X POST $API/search -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"query": "test", "collection": "lcd-t2", "hyde": true}' | jq '{hyde_applied, n: (.results | length)}'`; the search budget caps one HyDE call at 30 s whatever `[hyde].timeout_seconds` says.
- Every scenario that starts the server needs `[hyde] enabled = true`, because HyDE is the only feature wired to the shared client so far (`server/app.py`, `app.state.hyde_client` and the `_llm_window_task` background task). The window checks therefore log shortly after startup, from that task.

---

## 1 — Real llama-server window (S8, S11)

**Why manual:** needs a real `llama-server`. `/props` and `/tokenize` are exercised elsewhere in the repository only against `FakeModelServer` and `httpx.MockTransport`, so only a real server proves the field names and the exact counts.

**Steps:**
- [ ] Start a server with a known window and one slot, using a model whose training context is at least 4096 (the reported `n_ctx` is capped at it): `llama-server -m /path/to/model.gguf -c 4096 -np 1 --port 8080` (the config default is `llama_cpp_base_url = "http://localhost:8080"`; the llama-server default port differs, so pass `--port 8080`).
- [ ] Read the window the code reads: `curl -s http://localhost:8080/props | jq '.default_generation_settings.n_ctx'`. Expected: `4096` (note it as `N_CTX`). With several slots or a unified KV pool, `n_ctx` is the per-slot value the code will trust; record what it reports.
- [ ] Count a text exactly the way the client does: `curl -s -X POST http://localhost:8080/tokenize -H 'Content-Type: application/json' -d '{"content": "The quick brown fox jumps over the lazy dog."}' | jq '.tokens | length'`. Expected: an integer; `jq '.tokens'` is a list of token ids (the code reads `body["tokens"]` and takes its length).
- [ ] Run the client against the server. Save the snippet as `/tmp/lcd-t2/llama.py` (not committed) and run `uv run python /tmp/lcd-t2/llama.py`:

```python
import asyncio
from archon_search.llm_client.llama_cpp_client import LlamaCppClient

async def main() -> None:
    client = LlamaCppClient(model="local", base_url="http://localhost:8080")
    window = await client.probe_context_window(5.0)
    print("window:", window.size, window.source.value)
    template = "The quick brown fox jumps over the lazy dog."  # stands in for the longest template form, retry reminder included
    budget = await client.input_budget(template)
    print("budget:", budget.tokens)

asyncio.run(main())
```

  Expected: `window: 4096 probed`, and `budget` equals `N_CTX - 1024 - <the /tokenize count of the template>` (`ANSWER_RESERVE_TOKENS = 1024` in `archon_search/constants.py`; `exact_budget` in `archon_search/llm_client/budget.py`).
- [ ] The budget really fits (S11, exact arm): build a prompt of `budget.tokens` tokens by tokenizing a long text with `/tokenize`, keeping the first `budget.tokens` ids and turning them back with `POST /detokenize` (`{"tokens": [...]}` answers `{"content": "..."}`), then send template plus that prompt to `POST /v1/chat/completions` with `max_tokens` 1024. Expected: HTTP 200 and `usage.prompt_tokens + 1024 <= N_CTX` in the reply (a bare 200 proves little: llama-server rejects only when the prompt alone reaches `n_ctx`). Record the difference between `usage.prompt_tokens` and `budget.tokens + template tokens`; that is the chat-template overhead the budget does not count.
- [ ] Start Archon with the window read at startup. Config (plus the isolation keys of the prerequisites; truncate the log first):

```toml
[hyde]
enabled = true
provider = "llama_cpp"
model = "local"
llama_cpp_base_url = "http://localhost:8080"
```

  Expected: the server boots, the log holds no `llama_cpp: cannot read the context window` line, and llama-server's own output shows a `GET /props` request from Archon (otherwise the probe never ran and the missing WARNING proves nothing).
- [ ] Failure arm (S8): stop `llama-server`, restart Archon with the same config. Expected: it still boots and the log holds exactly one `llama_cpp: cannot read the context window from localhost:8080/props (<reason>); assuming 4096 tokens` WARNING (`ASSUMED_CONTEXT_TOKENS = 4096`; host and port only). A second, different line from the model validation probe (`llama-server unreachable at http://localhost:8080: ...`) is expected too and does not count against the criterion.

**Pass criterion:** the `/props` value equals the window the client reports (`probed`), the client budget matches the arithmetic above to the token, a prompt of exactly the budget is accepted, and the failed probe logs the one `cannot read the context window` WARNING and assumes 4096.

---

## 2 — Estimate margin on real text (S11)

**Why manual:** needs a real Ollama tokenizer. The unit tests stub the gpt2 counter and the fake Ollama server reports no token counts at all, so only a real model shows whether the 20% margin covers the difference between the gpt2 estimate and what Ollama actually counts.

**Rule under test:** for an `ollama` feature the budget is `(window - 1024) // 1.2 - gpt2_template` in gpt2 tokens (`OLLAMA_ESTIMATE_MARGIN = 0.20` in `archon_search/constants.py`, `estimated_budget` in `archon_search/llm_client/budget.py`). The margin is sufficient when the real count of any input is at most 1.2 times its gpt2 count; the template is counted once in gpt2 tokens and must satisfy the same bound.

**Steps:**
- [ ] Prepare one file per text class under `/tmp/lcd-t2/`, each long (about 3000 gpt2 tokens; short samples distort the ratio): `english.txt` (prose), `python.txt` (source code, for example a few concatenated modules of this repository), `hungarian.txt`, `chinese.txt`, `japanese.txt`, `emoji.txt` (emoji-dense), `digits.txt` (digit-heavy: tables, hashes, numbers).
- [ ] Models: at least three families, for example `llama3.1:8b` (llama), `qwen3.6:35b-a3b-128k` (qwen) and one of gemma or mistral. Per-digit tokenizers are the likely worst case on `digits.txt`; that is not yet measured.
- [ ] For each file get the gpt2 count the server would use (`TokenCounter.count`, `archon_search/llm_client/token_counter.py`; the pinned gpt2 `tokenizer.json` downloads on first use):

```bash
uv run python -c "import sys; from archon_search.llm_client.token_counter import TokenCounter; print(TokenCounter().count(open(sys.argv[1], encoding='utf-8').read()))" /tmp/lcd-t2/english.txt
```

- [ ] For each file and model get the real Ollama count with a raw prompt (no chat template), a one-token answer and a context large enough for the file:

```bash
jq -n --rawfile p /tmp/lcd-t2/english.txt '{model:"llama3.1:8b", prompt:$p, raw:true, stream:false, keep_alive:0, options:{num_predict:1, num_ctx:16384}}' | curl -s localhost:11434/api/generate -d @- | jq '{prompt_eval_count, prompt_eval_cached_count: (.prompt_eval_cached_count // null)}'
```

  The first token (BOS) may add 0 or 1; ignore it. `keep_alive:0` unloads the model so no prefix is reused; if `prompt_eval_count` is missing, repeat the call. Truncation guard: if `prompt_eval_count` is close to the effective context (the smaller of `num_ctx` and the model maximum), Ollama truncated the prompt and the ratio is falsely low; shorten the file and repeat.
- [ ] Template overhead probe: send a one-word user message through `/api/chat` (`{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}],"stream":false,"options":{"num_predict":1}}`) and read `prompt_eval_count`; subtract the raw count of `hi` from `/api/generate`. This is the chat-template cost Ollama adds, which the budget does not count separately; record it.
- [ ] Compute `ratio = ollama_count / gpt2_count` per file and model.
- [ ] The budget really fits (S11, estimated arm). Save the script as `/tmp/lcd-t2/ollama_budget.py` and run `uv run python /tmp/lcd-t2/ollama_budget.py english` and again with `digits` (it needs the two files of this item; the window is 4096 so a 3000-token file is cut):

```python
import asyncio, json, sys
from archon_search.hyde import HYDE_PROMPT_TEMPLATE
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.token_counter import load_gpt2_tokenizer

MODEL, WINDOW = "llama3.1:8b", 4096
client = OllamaClient(model=MODEL, base_url="http://localhost:11434", context_tokens=WINDOW)
budget = asyncio.run(client.input_budget(HYDE_PROMPT_TEMPLATE)).tokens
tokenizer = load_gpt2_tokenizer()
ids = tokenizer.encode(open(f"/tmp/lcd-t2/{sys.argv[1]}.txt", encoding="utf-8").read(), add_special_tokens=False).ids
cut = tokenizer.decode(ids[:budget])
print("budget:", budget, "cut gpt2 tokens:", len(tokenizer.encode(cut, add_special_tokens=False).ids))
body = {"model": MODEL, "stream": False, "keep_alive": 0, "options": {"num_ctx": WINDOW, "num_predict": 1},
        "messages": [{"role": "user", "content": HYDE_PROMPT_TEMPLATE.format(query=cut)}]}
json.dump(body, open("/tmp/lcd-t2/ollama_body.json", "w"))
```

  Then `curl -s localhost:11434/api/chat -d @/tmp/lcd-t2/ollama_body.json | jq .prompt_eval_count` (the client sends `/api/chat`, so the chat template is included). Expected: `prompt_eval_count + 1024 <= 4096` and no truncation. The template ratio check: `prompt_eval_count` of the same call with an empty `query` against the gpt2 count of `HYDE_PROMPT_TEMPLATE` must also be at most 1.2.

**Pass criterion:** for every text class and every model tried, `ollama_count <= 1.2 * gpt2_count` (ratio at most 1.20), and the at-budget prompts fit. A ratio above 1.20 for any class is a failure: record the class, the model and the ratio, and report it to the plan owner, who decides between a larger `OLLAMA_ESTIMATE_MARGIN` and dropping support for that model family. A ratio below 1.0 only means the estimate is conservative and is not a failure.

---

## 3 — Real Ollama model maximum (S68)

**Why manual:** needs a real Ollama model. The fake server answers `/api/show` with an invented `model_info`; only a real model proves the key names the code reads (`general.architecture` and `<architecture>.context_length`; a model without that key makes the check silently skip).

**Steps:**
- [ ] Read the model maximum the code reads, for each model of item 2: `curl -s localhost:11434/api/show -d '{"model":"llama3.1:8b"}' | jq '.model_info["general.architecture"], .model_info[(.model_info["general.architecture"] + ".context_length")]'`. Expected: the architecture name and a positive integer (note it as `MODEL_MAX`; read on 2026-10-31 from `/api/show` and `/api/tags` of a local Ollama 0.40.13: `llama` and `131072` for `llama3.1:8b`, so the key names are right for that family).
- [ ] Oversized arm. Set a window above the maximum and boot Archon (plus the isolation keys of the prerequisites; truncate the log first):

```toml
[hyde]
enabled = true
provider = "ollama"
model = "llama3.1:8b"
ollama_base_url = "http://localhost:11434"
ollama_context_tokens = 1000000
```

  (use any value above `MODEL_MAX` for another model). Expected: the server boots and the log holds one WARNING: `[hyde].ollama_context_tokens = 1000000 is above the model's maximum of <MODEL_MAX> tokens (read from Ollama's model details); Ollama lowers the window to that maximum without saying so. Set [hyde].ollama_context_tokens to <MODEL_MAX> or less.`
- [ ] Within-limit arm: set `ollama_context_tokens = 8192`, truncate the log, restart. Expected: no oversized WARNING and no unset WARNING, and Ollama's own log (`~/.ollama/logs/server.log`) shows a `POST /api/show` from Archon (otherwise the check never ran and the silence proves nothing).
- [ ] Unset arm: delete the `ollama_context_tokens` line, truncate the log, restart. Expected: one WARNING `[hyde].ollama_context_tokens is not set: assuming 4096 tokens and sending no num_ctx, so the window Ollama picked for itself applies. Set [hyde].ollama_context_tokens in archon-search.toml.`
- [ ] Unreachable arm: stop Ollama, set `ollama_context_tokens = 1000000`, truncate the log, restart Archon. Expected: it boots; one INFO line says the model's maximum could not be read and the value is not checked; no WARNING from the window check.

**Pass criterion:** per arm: oversized gives one WARNING carrying the real `MODEL_MAX`; within-limit gives nothing; unset gives one WARNING; unreachable gives one INFO and no WARNING; the server boots in every arm; the oversized arm fires for each model family tried.

---

## 4 — Real claude login (S79)

**Why manual:** needs a real `claude` login. The automated tests run a fake `claude` that records its argv, stdin and environment; only the real binary shows that the flags are accepted, that tools are really off and that the calls count against the login. Each real call uses the login's quota (a subscription has no per-call bill; the usage page is the evidence). Direct commands below run in a subshell with `env -i` and only the variables the client's own allowlist passes (`child_environment` in `claude_cli_client.py`), so a stray `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_BASE_URL` in your shell cannot make them pass or bill elsewhere. Define once: `CE='env -i PATH="$PATH" HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}"'` and run each command with `eval "$CE claude ..."` or paste the expanded `env -i ...` prefix.

**Steps:**
- [ ] Confirm the binary and login: `claude --version` and `eval "$CE claude -p 'Reply with one word: ok'"` (a logged-out binary prints `Not logged in`). Positive control for the isolation: `ANTHROPIC_API_KEY=sk-ant-invalid claude -p hi` must fail with an authentication error; if it succeeds the invalid key is ignored and the Archon poisoned-environment step below proves nothing about the key.
- [ ] Take a session-store marker after that plain call (it saves a session): `touch /tmp/lcd-t2/marker`.
- [ ] Run the exact argv of `_LOCKDOWN_ARGV` in `archon_search/llm_client/claude_cli_client.py`, prompt on standard input, from an empty directory `D=$(mktemp -d)`: `(cd "$D" && eval "$CE claude -p --output-format text --tools '' --no-session-persistence --disable-slash-commands --strict-mcp-config --max-turns 1 --safe-mode" <<< "Reply with the single word: ok"; echo "exit=$?")`. Expected: `ok` and `exit=0`; `--safe-mode` and `--max-turns 1` are accepted (`--max-turns` is accepted although the local `claude --help` of 2.1.308 does not list it).
- [ ] Add a model that is not the default fast model (the client adds `--model` unless the model is blank or equals `DEFAULT_FAST_MODEL`, `claude-haiku-4-5-20251001`): the same command plus `--model claude-sonnet-4-6`. Expected: the same success.
- [ ] Tools are off: `touch "$D/SENTINEL_lcd_t2"` and ask `Run the shell command ls and show its output.` with the same command. Expected: the answer states it cannot run commands and never shows `SENTINEL_lcd_t2` (with Bash enabled it would list it; an empty directory could not tell the two cases apart).
- [ ] Large prompt on standard input, in a second terminal run `while true; do ps -Ao args= | grep -c "[l]orem"; sleep 0.5; done` while the first runs `(cd "$D" && yes "lorem ipsum dolor sit amet" | head -n 5000 | eval "$CE claude -p --output-format text --tools '' --no-session-persistence --disable-slash-commands --strict-mcp-config --max-turns 1 --safe-mode --model claude-sonnet-4-6"; echo "exit=$?")`. Expected: the counter stays `0` (the prompt is never in an argv) and the call ends with `exit=0` or a model-side length answer, not an argv error. 135 KB is below the macOS argument limit of about 1 MiB, so the `ps` counter, not the absence of an error, is the evidence.
- [ ] No session saved: `find ~/.claude/projects -newer /tmp/lcd-t2/marker -iname "*$(basename "$D")*"` prints nothing (the directory slug carries the temp directory name; other live Claude Code sessions do not match it). Repeat after the Archon steps below.
- [ ] Usage: after the calls, open the usage page of the logged-in account; the calls appear there. With the isolation above nothing goes to an API key.
- [ ] Through Archon with a poisoned environment. Config (plus the isolation keys of the prerequisites):

```toml
[hyde]
enabled = true
provider = "claude_cli"
model = "claude-sonnet-4-6"
timeout_seconds = 60
```

  Start with `CLAUDECODE=1 ANTHROPIC_API_KEY=sk-ant-invalid uv run archon-search serve --config /tmp/lcd-t2/archon.toml`, wait for `/ready`, then run the search call of the prerequisites. Expected: `hyde_applied` is `true` and `n` is above 0; with the positive control above this shows the invalid key did not reach the child (the allowlist drops `ANTHROPIC_API_KEY` and `CLAUDECODE`; that `CLAUDECODE` itself is stripped is proven only by the fake-`claude` environment test).
- [ ] Kill on timeout: set `timeout_seconds = 1`, truncate the log, restart, and send one search call while `pgrep -P <server pid>` in a second terminal shows the `claude` child (the server pid: `pgrep -f "archon-search serve --config /tmp/lcd-t2"`, the Python process is a child of `uv run`). Expected: `hyde_applied` is `false`, one WARNING in the log, and `pgrep -P <server pid>` prints nothing within 2 s afterwards.
- [ ] Kill on cancellation: restore `timeout_seconds = 60`. Control first: one search left to finish keeps a `claude` child visible via `pgrep -P <server pid>` for several seconds. Then send the search again and abort the client (Ctrl-C of `curl`) about 1 s in, while the child is visible. Expected: the child is gone within 2 s. If the child instead survives to its natural end, cancellation does not propagate; record that as a failure.

**Pass criterion:** every real call succeeds with the full argv, tools are off (the sentinel never appears), the prompt never appears in the process list, the lockdown runs save no session, the usage lands on the login, the HyDE search applies on `claude_cli` despite the invalid key in the server's environment, and no `claude` child outlives a timeout or a cancelled search.

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

Accept when all four items pass: the `llama_cpp` window and counts are exact and an at-budget prompt is accepted, the 20% margin covers every measured text class on every model family tried, the oversized-window WARNING names the real model maximum, and the real `claude` accepts the lockdown argv, keeps tools off, bills the login and leaves no child behind.
