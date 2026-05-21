# Review: MigrationGuide/05_data_migration.md

## Summary

The doc is largely accurate on the high-level layout, the survives-upgrade table, the reindex semantics, and the telemetry privacy invariant. There are a small number of concrete inaccuracies — mostly around the reindex CLI's transport model and a broken shell snippet — plus a couple of unverifiable forward-looking claims. The state-directory layout listed in the doc is missing one runtime artefact (`.indexing_state.json`) that lives under `search/`, but the doc explicitly tells the reader to treat that subtree as opaque, so this is a minor omission rather than a contradiction.

## Inaccuracies (numbered)

1. **Line 33 — "implementation details of `archon_search/key_manager.py`".** The path of the auth key file is not an implementation detail in the sense the doc implies. `key_manager.py:18` hardcodes `~/.archon-search/.search.env`; the only override is the `ARCHON_SEARCH_KEY_FILE` env var (`key_manager.py:14`). So the filename `.search.env` shown in the layout block IS the canonical location, not "an implementation detail". Minor — phrasing only.

2. **Line 44 — "Indexing-state JSON | Yes".** The state file is real (`archon_search/progress.py:86` → `<db_path>/.indexing_state.json`), but it lives **inside** `search/`, not as a sibling. The layout block on lines 22–31 does not show it at all, and the table row "Indexing-state JSON" implies a top-level entry. Either show it under `search/` in the tree or drop the row.

3. **Line 56 — "By default `auto_reindex_on_chunk_size_change = true`, so affected collections are reindexed on the next start."** The default is correct (`config.py:37`), but "on the next start" is too narrow. The check runs whenever `sync.py`'s `_sync_collection` is invoked — i.e. on every sync pass for that collection (start, watcher-triggered re-sync, `archon-search sync`), not only at process start. See `sync.py:400-409`.

4. **Line 57 — "You changed `[database].embedding_model` or `[database].reranker_model`. ... Force a full reindex of every collection that should use the new model."** Half right. `embedding_model` changes ARE auto-detected and force a full reindex unconditionally — `sync.py:390-398` triggers `force_full_reindex = True` whenever the indexed embedding model differs from the configured one, with no opt-out flag. There is no equivalent guard for `reranker_model`: grep across `sync.py` / `store.py` / `pipeline.py` shows the reranker model is loaded fresh at runtime and is not persisted per-collection, so it does not require a reindex at all (the reranker operates on the second-stage candidates post-retrieval). The doc gets the user-facing instruction half wrong on both counts: embedding-model changes don't need manual action, and reranker-model changes don't need a reindex.

5. **Lines 63, 70 — "see `archon_search/cli/collection.py` `reindex`" and "Force a full reindex of one collection."** Accurate in spirit, but the command signature in the source is `archon-search collection reindex <collection_name>` where `<collection_name>` is the **path-derived collection name** (see `sync.path_to_collection_name`), not the raw filesystem path. The doc's snippet `archon-search collection reindex <collection-name>` is correct; the surrounding prose is fine. No fix needed — flagging only because point 7 below interacts with this.

6. **Line 73 — "It is safe to interrupt — restarting picks up where it stopped, because the on-disk state is rebuilt incrementally."** Misleading for the `reindex` command specifically. `cli/collection.py:223-235` (a) clears the `IndexingStateStore` entry for the collection AND (b) drops the LanceDB collection BEFORE re-walking. If you interrupt mid-reindex, the next `reindex` run starts from scratch again — state was deliberately wiped. Incremental resume is true for normal `sync`, not for `reindex`. The doc conflates the two.

7. **Lines 75–78 — the loop snippet.**
   ```bash
   archon-search collection list | awk 'NR>1 {print $1}' | \
     while read -r c; do archon-search collection reindex "$c"; done
   ```
   `archon-search collection list` does NOT print a header row. The implementation (`cli/collection.py:34-38`) prints either `"No collections found."` or one line per collection of the form `{name}  docs={N}  chunks={M}`. `awk 'NR>1'` therefore **skips the first collection**. Correct form would be `awk '{print $1}'` (and an explicit check for the "No collections found." line).

8. **Line 80 — "The command requires the server **process** to be either running locally (so the CLI can call its own pipeline) or available via the HTTP control plane".** Wrong on both clauses. `cli/collection.py:reindex` (a) does not talk to an HTTP control plane at all — it calls `create_pipeline(cfg)` and operates directly on LanceDB via `pipeline.store`; and (b) it MUST NOT be run while the server is running, because both processes would open the same LanceDB tree and the state file. If anything, the operator should `archon-search stop` first. Same applies to `collection list`, `add`, `remove`, `info`. None of the `collection` subcommands use the HTTP API.

9. **Line 87 — `tar -czf ~/archon-search-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C ~ .archon-search`.** Mechanically correct, but combined with the "stop the server first" instruction, it leaves a hole: `.search.env` has mode 600 and is included in the tarball. The doc does not mention that the restored permissions on `.search.env` must remain 600 (`key_manager.py:88-92` enforces 600 on write; if a restore lands the file with wider perms, subsequent key rotation will fix it, but until then the auth secret is on disk with whatever umask `tar -x` produced). Worth a one-line note.

10. **Lines 96, 113 — references to backlog item "D2" wording.** The doc calls D2 "Export / import / backup / restore". `Documentation/Backlog/03_world_class_roadmap.md:86` calls it "Export / import / backup / restore (item 20)" — title matches. `Documentation/Backlog/03_world_class_roadmap.md:87` calls D3 "Schema migration tooling (item 21)" — title matches. These refs are accurate; flagging only to confirm I checked.

11. **Line 103 — "`entry.py` factory methods do not accept a `query` argument".** Verified: the three factories `from_search_tool_result` (`telemetry/entry.py:85`), `from_route_response` (`:110`), and `from_error` (`:128`) — and the dataclass field list on `:41` — have no `query` parameter. Claim is correct. (Listed under "Verified" too; included here so a reader doing diff-style review sees it explicitly.)

## Verified claims

- `~/.archon-search/` is the single durable state root: `config.py:91` (`archon-search.toml`), `config.py:33` (`db_path = ~/.archon-search/search`), `config.py:51` (`log_file = ~/.archon-search/logs/archon-search.log`), `config.py:24` (`telemetry.log_dir = ~/.archon-search/search-logs`), `key_manager.py:18` (`.search.env`). Layout block matches source.
- `.search.env` mode 600 + `ARCHON_SEARCH_API_KEY` env override + `ARCHON_SEARCH_KEY_FILE` redirect: all in `key_manager.py:14-18` (consistent with the project CLAUDE.md too).
- `auto_reindex_on_chunk_size_change` default `True`: `config.py:37`. Used in `sync.py:402`.
- BREAKING.md currently has no reindex-forcing entries (only an MCP `search` response-shape change and a REST `top_k` change); the claim "No release in BREAKING.md has required a reindex" is currently true.
- D3 / D2 backlog refs exist and match the descriptions used in the doc (`Backlog/03_world_class_roadmap.md:86-87`).
- Telemetry "no raw query" structural invariant: confirmed against `archon_search/telemetry/entry.py` factory signatures (see inaccuracy #11 note).
- `[telemetry].retention_days` pruning is implemented (`config.py:204-208` for the knob; `pruner.py` referenced by project CLAUDE.md).
- `export_enabled` not implemented in v1, coerced to false with warning: `config.py:209-217`. Doc does not claim otherwise.
- Reindex command exists per-collection, no "reindex all": confirmed by reading the full `cli/collection.py`. Subcommands are `list`, `add`, `remove`, `info`, `reindex` — no global reindex.

## Unverifiable / ambiguous

- **Line 45 — "Appended to; not rotated by the server itself."** I did not find a log rotation handler in the codebase, which is consistent with the claim, but absence-of-evidence is weaker than direct verification. The default logging setup is not in the files I read; if the doc wants to be strict, it should cite the specific module (likely `server/app.py` logger setup) and confirm no `RotatingFileHandler` is wired in.
- **Line 96 — "LanceDB writes are not guaranteed atomic across the whole tree".** This is a claim about LanceDB's internal semantics. It's plausible and matches the standard caveat for column-store / Lance-format directories, but it's not verifiable from this repo alone — it depends on the LanceDB library version pinned in `pyproject.toml`. Not wrong, but the doc is asserting an upstream property without citation.
- **Line 100 — "one JSON line per `/search` and `/route` call".** Plausible given `entry.py` has `from_search_tool_result` and `from_route_response` factories, but I did not trace every call site that invokes the writer to confirm it's exactly those two endpoints (and not, say, `search_with_context` or MCP variants). Worth a follow-up verification against `telemetry/writer.py` call sites if precision matters.
- **Line 80 — "consult `archon-search collection --help` for the version you have installed".** Hedge clause; not falsifiable.
