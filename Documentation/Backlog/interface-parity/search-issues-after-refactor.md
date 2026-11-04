# Search after the refactor: what is left and what you can decide

This file explains, in plain words, what is still open after the "Search on one implementation" work (REST, MCP, the OpenAI shim and the command line). It lists what was finished, what real-world testing showed, and every open issue with your options and my recommendation. Technical details sit in parentheses for the engineers.

## What "Search" is now

Before the work, four doors into the search engine (the web API, the MCP tool for AI agents, the OpenAI-compatible endpoint and the terminal) each had their own copy of the rules. They had drifted apart. Now all four call one shared piece of code (`SearchService` in `archon_search/services/search_service.py`). All 44 planned tasks are done and committed, and the full automatic test run passes (16631 tests, ruff clean).

## What happened in the last round

| Item | Result |
|---|---|
| Live test T-2: HyDE and RAG Fusion with a real AI model | Done on this machine with the local Ollama server. No defect found. Details below. |
| Live test T-4: Open WebUI and an AI agent that chooses the search tool | Done. No defect found. Details below. |
| Contracts and the brief | Fixed in commit `e050474a`. Seven contract files now match the real code (they compile with `tsp compile`). Moving them to another folder was not done. See issue 1. |
| Flaky tests | One fixed and committed (`ddbb846a`). One could not be explained. See issues 3 and 4. |

HyDE means "ask the AI to write a fake answer first and search with that". RAG Fusion means "ask the AI to rewrite the question in several ways and merge the results". Both are optional extras that need an AI model.

### T-2 result (real model: `llama3.1:8b` through Ollama)

The command line and the MCP tool gave identical field values and identical result order in every case. Each row below is one scenario.

| Scenario | HyDE ran | RAG Fusion ran | Warning shown | Time |
|---|---|---|---|---|
| Plain search | no | no | none | under 2 s |
| `--hyde` | yes | no | none | 3 to 7.5 s |
| `--rag-fusion` | no | yes (3 queries) | none | 1.6 to 4 s |
| Both | no (RAG Fusion wins) | yes | none | about 2 to 3 s |
| Wrong model name, `--hyde` | no | no | "HyDE expansion failed" | under 1 s |
| Wrong model name, `--rag-fusion` | no | no (but "attempted" is true) | none | under 1 s |
| Closed port | same as wrong model name | same | same | under 1 s |
| Reasoning model `qwen3.6:35b-a3b-128k` | no (HyDE failed) | no | HyDE warning only | 7 to 17 s |

Only six small documents were used, so the result order never changed. This proves the wiring, the fields and the speed. It does not prove that the extras find better results. Anthropic and OpenAI providers were not tested because there are no keys on this machine.

### T-4 result (Open WebUI v0.13.0 in Docker, plus an AI agent)

- **OpenAI-compatible door:** the count of `[Source: …]` blocks equalled the configured `[openai_shim].top_k` (3 when set, 5 when unset). A `top_k` sent inside the request (1, 50 or 0) was ignored. This held for Open WebUI, the official `openai` library and `curl` streaming.
- **AI agent door:** the local model chose the `search` tool itself and picked `top_k=2`, `top_k=7` with `acl_context`, and `top_k=5`. It got exactly that many results, and the access-control details only when asked. `top_k=0` and `101` returned a `validation_error` message. `top_k=50` on a 12-chunk collection returned 12.
- **Not captured:** whether Open WebUI itself sends `top_k` to the shim. The library and `curl` rows prove the "ignored" rule directly.
- **Method:** Open WebUI was driven through its HTTP API, not its browser screens.

## Open issues

Each issue has options and a recommendation. Issues 1 to 3 need a decision from you. The rest are small and can wait.

### 1. Where the contract files live (decision needed)

The contract files (`*.tsp`) describe how the pieces talk to each other. The project rule file (`CLAUDE.md` line 88) says they belong in the folder `tsp_contract/`. The plan (lines 19 and 733) also says they move there once the work is done. They are still in `Documentation/Backlog/interface-parity/`. The agent checked how earlier features did it and found they kept their contracts beside their plan. So the rule and the practice disagree.

- **Option A: Move all 12 contract files to `tsp_contract/`** (7 for Search and 5 for the earlier service-layer work).
  - Pros: follows the written rule, and the plan becomes true as written.
  - Cons: more work (links in the plan, the task file, docs, four test comments and the `api-contracts/` tooling must change), and it breaks the habit of every earlier feature.
- **Option B: Leave them where they are and fix the rule.**
  - Pros: no risk to links, and it matches how all earlier features were done.
  - Cons: the rule file and the plan lines 19 and 733 must be edited so they stop saying the opposite.
- **Option C: Decide later.**
  - Pros: nothing breaks.
  - Cons: the contradiction stays, and the next feature will copy whichever habit it meets first.

**Recommendation: Option B.** The practice is consistent over many features, and a move buys nothing for the people who use the code. You give up the tidy "all contracts in one folder" rule, but you can keep that for new features only.

### 2. The plan's documentation checklist is still unchecked

The plan has a "Documentation update" list. Every box except the brief is unchecked, even though the T-7 agent did the work (about 45 files). The plan's text for contract C2 also still says "a `ServiceError` carries no exception", which is no longer true in detail (the cause is chained as `__cause__`).

- **Option A: Tick the boxes after a quick spot check by one person, and fix the C2 sentence.**
  - Pros: the plan shows the real state.
  - Cons: someone must actually check, or the ticks are empty.
- **Option B: Leave the plan as a historical record.**
  - Pros: no work.
  - Cons: the next reader will think the documentation work was not done.

**Recommendation: Option A**, combined with the human sign-offs in issue 8.

### 3. RAG Fusion never tells the user when it fails (RESOLVED in commit `f3f5939d`)

**Resolved.** A RAG Fusion failure now sets a fixed warning in `expansion_warning` ("RAG Fusion timed out" or "RAG Fusion expansion failed"), on REST, MCP and the terminal, and `/explain` reports the reason in `rag_fusion_failure_reason`. The generator now raises a typed `RAGFusionFailure` (`archon_search/rag_fusion_failure.py`) that never carries the query. `BREAKING.md` has the new entry (NR-86). The text below is the original problem statement.

When the AI model is missing or broken, HyDE says "HyDE expansion failed". RAG Fusion says nothing. The response only shows "attempted: true, applied: false". The live test proved it: a wrong model name gave no warning for RAG Fusion. The code hides every failure (`generate_variants` in `archon_search/rag_fusion.py` returns an empty list on each failure and only logs once). The user manual (`60_searching.md`) promises a warning, so users are misled. This was true before the refactor. The refactor did not change it.

- **Option A: Make RAG Fusion report why it failed, and show it as the warning.**
  - Pros: HyDE and RAG Fusion behave the same, and the manual becomes true.
  - Cons: a code change in the retrieval part (the generator must return a reason, and the service and tests change). It could change a documented response.
- **Option B: Keep the code and keep the honest text in the manual.**
  - Pros: no risk. The docs already say the warning is rare.
  - Cons: users still cannot see why RAG Fusion did nothing.
- **Option C: Log a stronger server message only.**
  - Pros: small change, and operators can see failures.
  - Cons: API users still see nothing.

**Recommendation: Option A**, as its own small task. It is the one real product gap found. It is safe to wait, because nothing breaks today.

### 4. One test fails rarely and nobody knows why

The test `test_request_after_shutdown_began_is_refused` (in `tests/test_be25_description_wiring.py`) failed in 1 of 3 full runs with an "unclosed network connection" warning. The agent then ran that file 13 times and 801 related tests 9 times, many under heavy load, and could not repeat it. Nothing in the product is affected, because it only concerns tests.

- **Option A: Wait for a failure that shows the full warning, then fix it.**
  - Pros: no guessing. The agent gave the exact command to capture it (`PYTHONTRACEMALLOC=20`, one worker).
  - Cons: the test may fail again for someone in the meantime.
- **Option B: Fix the likely cause now.** The best guess is that four tests start the app against a dead address and never wait for two start-up checks, so shutdown cancels them half way.
  - Pros: removes a plausible cause.
  - Cons: it is a guess, so you cannot prove it worked.
- **Option C: Mark the test as known-flaky.**
  - Pros: the build stays green.
  - Cons: it hides the real problem, and the repo rules discourage it.

**Recommendation: Option A.** A fix without proof would add code that nobody can verify. Do Option B only if the failure returns.

### 5. One watcher test failure is still unexplained

The test `test_watcher_ingested_new_file_is_tagged_watcher` waits for the macOS file-watching service to report a new file. The service is sometimes 4 seconds late, and in rare cases it did not report at all. The agent changed the test to write the file again every 3 seconds until it is found. It then passed 15 of 15 times alone and 3 of 3 full runs. One earlier 30-minute run still failed once, while other agents were using the machine heavily.

- **Option A: Keep the fix and watch.** The failure message now says how many writes and how many events happened.
  - Pros: no more work now.
  - Cons: a rare failure can still happen.
- **Option B: Make the test independent of the macOS file service** (call the sync code directly).
  - Pros: removes the dependence on the operating system.
  - Cons: the test then no longer proves that the real watcher works.

**Recommendation: Option A.** The new message will show which case it is next time.

### 6. Two functions do nearly the same job (small shared steps merged; the two response classes stay)

**Decision taken:** the two small shared steps are merged. `AclGateSchema.from_gate` builds the ACL details and `blank_unrequested_metadata` empties `metadata` (both in `server/schemas.py`). REST and MCP now call them. The two response classes stay separate, so the OpenAPI contract, the MCP error path and the field-parity test are unchanged. The text below is the original problem statement.

`_render_search_outcome` (`server/mcp.py`) and `_render_outcome` (`server/routes_search.py`) both turn the service result into a response. They use different response classes, so merging them is not trivial.

- **Option A: Merge them into one shared helper.**
  - Pros: one place to change.
  - Cons: needs a shared base for two response classes, and the benefit is small.
- **Option B: Leave both and guard them.** The field parity test (`tests/parity/test_field_parity.py`) already fails if the two answers drift apart.
  - Pros: no work, and drift is already caught.
  - Cons: two copies remain.

**Recommendation: Option B.** The guard that already exists makes the cost of the duplicate low.

### 7. Small gaps in the terminal command (RESOLVED in commits `d69bcf54`, `99924057`, `87bea2ff`)

**Resolved in three commits.** The 422 text now names the field (`top_k: Input should be greater than or equal to 1`), one helper `_response_message` replaces the three repeated calls, and a deeply nested JSON answer prints a clean "unexpected response" error. The nested-JSON fix also covers `collection`, `sync` and the `/ready` probe. Not changed: `key_cmd.py`, `maintenance_cmd.py`, `status.py` and `backup_cmd.py` still catch `ValueError` only. The text below is the original problem statement.

- The terminal error text drops the field name in a validation message (`_server_message` ignores the `loc` part of the 422 list). An error reads "must be at least 1" instead of "top_k: must be at least 1".
- The code that reads the server's error message is repeated in three places (`cli/search.py` and twice in `cli/collection.py`).
- A server answer made of extremely deeply nested JSON raises a Python error instead of a clean message (`resp.json()` in `cli/search.py` catches `ValueError` only, not `RecursionError`). A real server never sends this.

- **Option A: Fix all three in one small task.**
  - Pros: clearer messages, and one shared helper.
  - Cons: touches `collection.py` too, which was not part of this work.
- **Option B: Fix only the field name.**
  - Pros: this is the one a user will see.
  - Cons: the other two remain.
- **Option C: Leave all three.**
  - Pros: no work.
  - Cons: the field-name gap stays.

**Recommendation: Option B** now, and the other two with the next change to the command.

### 8. Steps only a person can do

- **T-1:** read the terminal output of `archon-search search` and check it is easy to read. An agent ran it and the JSON parsed in `jq`, but no person has judged it.
- **T-5:** sign off the review of `BREAKING.md` against the list of behaviour changes. An agent found no gaps.
- **T-2 and T-4** are now done with a local model and Open WebUI. Two parts of them were not covered (Anthropic and OpenAI providers, and the Open WebUI browser screens).

**Recommendation:** you or one team member does T-1 and T-5. Then change the plan's front-matter `status` from `in-progress` to `done`. Skip the provider and screen gaps unless you plan to ship those combinations to customers.

### 9. Details found during the live tests

- **The `/status` page does not show whether the AI provider works.** It said `key_available=true` for a wrong model name and for a closed port. An operator cannot see a broken setup there.
- **No warning line was seen in the server console** for the failed AI calls, although the manual promises one per process. This is not confirmed, because only the console was checked.
- **The manual test checklist is outdated.** It names the model `llama3.2`, which this machine does not have. It also does not say that files under `/tmp` cannot be ingested (`denied_location`).
- **MCP returns a normal reply for a bad `top_k`.** A value of 0 or 101 comes back as a payload with `code=validation_error` and not as an error flag (`is_error=False`). This may be intended and is consistent with the other MCP answers. It is worth one look.
- **Embedder-not-ready can never happen on the search paths** now (`SearchService` waits only 0.5 s, then answers from keywords). The code path for it is only reachable in tests. It is harmless, and the docs say so.

**Recommendation:** fix the checklist now, and put the other four on the backlog. The `/status` gap matters most if operators use that page.

## Leftovers on this machine

- The Docker image `ghcr.io/open-webui/open-webui:main` (5.83 GB) is still downloaded. Remove it with `docker rmi ghcr.io/open-webui/open-webui:main` if you do not need it.
- Test scripts and data from the live runs remain in `/tmp` (`/tmp/live_t4`, `/tmp/agent.py`, `/tmp/shim.py`, `/tmp/owui.py`, `/tmp/mcpdirect.py`), plus the two written reports `/tmp/live_t2_report.md` and `/tmp/live_t4_report.md`. Copy the reports somewhere permanent if you want to keep them.
- The file `concepts_brief.md` in the repository root is not part of this work. It is untracked, and no agent staged it.
- The smoke test `test_maintenance_run_without_server` was edited (it now uses the shared `unreachable_port()` helper) but not run, because the smoke lane is excluded from the default test run.

## Suggested order

1. Decide issue 1 (contract folder).
2. Do the human sign-offs from issue 8 and the plan housekeeping from issue 2.
3. Put issues 4 to 6 and 9 on the backlog. Act on 4 and 5 only if they fail again.
