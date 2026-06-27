# archon-search notes

## Feature ideas

### Handle more file type without file size limit.

**Resolved by E0d.** The 1 MB limitation no longer exists. Large files (PDF and all other supported formats) ingest at any size when no size guard is configured (`[ingest].max_file_mb = 0`, the default). Operators can set a size ceiling via `[ingest].max_file_mb` in `archon-search.toml`; exceeding it returns HTTP 413 / MCP `code="file_too_large"` with an actionable message. See `Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md` for full details.

### The user can search between the visited websites and if he asks questions in a topic, then these websites also could help him to recall and use those information.

A chrome extension could be written to use it to ingest the current website content and link to be able to use later and be indexed. In this case a new type of sources could be added to the archon-search. A challange can be if it is stored in one collections, because many various type of contents would be stored in one collection (from cooking recipes to technical deep dive in documentation and other researches). It could be hard to find the right collection for the app.

### Link frontier model chat history into archon-search

It would be nice if the user could search in the cloud AI chat history as well. Many times the users use more AI providers in the same topic and in this case we could connect the similar topic into one merged search. For example is the user develops an app like Financialwell.app and he has chats in two providers like Claude and ChatGPT, it would be powerfull if the user or another LLM could use this search and could have a hollistic view and access to these contens in this topic. The user don't need to remember wher does this topic have discussed before, just talk wiht his LLM and the LLM will remember (find) the right conversation and use it.
We should ingest the whole chat history from the beginning. Need to solve the sync and handle rate limits as well.
First we should support ChatGPT, Claude and Perplexity. Is there any services to be able to have access all of them via one MCP, API interface?

### Handle 30+ collections better

If we have more collections in the future, then we should think over the handling of the search between collections. Now we have a heuristic, to determine in which collections should be search (not all of them), but later we should handle it even better. Eg.: What if we put another layer to collection search? The collections should have a larger more detailed description about what are in that collection and run a very fast and effective search on these descriptions to determine which collections will fit the best. We should define what does the more detailed collection mean. I expect that the 

### Anthorpic key save and usage

Where to set ANTHROPIC_API_KEY today (depends on how you start the server):

  - archon-search serve (foreground / container) — export ANTHROPIC_API_KEY=... in the shell. The process inherits the env. This is the documented path in Documentation/UserManual/05_searching.md:210-215.
  - archon-search start (launchd on macOS, systemd on Linux) — the service templates only forward ARCHON_SEARCH_CONFIG (archon_search/platform/macos.py:33-37, archon_search/platform/linux.py:26). Shell env is NOT
  inherited. To make HyDE / RAG Fusion / description-generation work under the managed service you must:
    - macOS: edit ~/Library/LaunchAgents/com.archon.search.plist, add the key inside <key>EnvironmentVariables</key>, then launchctl unload && launchctl load.
    - Linux: edit ~/.config/systemd/user/archon-search.service, add Environment=ANTHROPIC_API_KEY=... (or EnvironmentFile=~/.archon-search/.secrets.env with mode 600), then systemctl --user daemon-reload &&
  systemctl --user restart archon-search.
  - Tests — don't set it. C18 (just committed) clears it in the autouse fixture so the 30 s SDK timeout floor doesn't fire.

  There's a real doc gap: the user manual says "set it in the server's environment before starting" but doesn't explain that launchd/systemd templates won't inherit your shell. That's worth fixing as a separate doc
   PR.

  Should the wizard ask for it? My recommendation: no direct prompt.

  - The key would end up written in plain text into ~/Library/LaunchAgents/com.archon.search.plist or ~/.config/systemd/user/archon-search.service — a security smell that contradicts the "operator-provisioned
  credentials" stance in Documentation/ADRs/C4-hyde-external-llm-dependency.md:66.
  - The wizard already does the right thing at discovery time: it asks to enable HyDE + RAG Fusion only when the key is detected in the env (archon_search/install.py:1083), and prints a post-install Tip when it's
  missing (archon_search/install.py:1855-1861).
  - The description-generator call site is silent on missing key (debug log only), so no UX nudge needed.

  If you do want a wizard-level improvement, a safer design: mirror the existing ~/.archon-search/.search.env pattern. Add EnvironmentFile=~/.archon-search/.secrets.env (mode 600, gitignored) to both service
  templates; the wizard creates the empty file with right perms; the operator drops the key into the file. No secret ever lands in a wizard-prompted text and rotation = edit one file + restart [Superseded by D7 — use `archon-search key rotate`]. That's a separate,
  meaningful design ticket — not in scope for C18.

  