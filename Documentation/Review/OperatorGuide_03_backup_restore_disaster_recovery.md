# Review: OperatorGuide/03_backup_restore_disaster_recovery.md

## Summary

The document is largely accurate. Path claims, owner-module attributions, line numbers, environment variables, file permissions (mode 0600), defaults, and crash-recovery semantics for jobs all check out against `config.py`, `constants.py`, `key_manager.py`, `progress.py`, `jobs/model.py`, `jobs/store.py`, `telemetry/writer.py`, `telemetry/pruner.py`, and `platform/macos.py`. A small number of minor issues — mostly cosmetic, plus one shell-snippet bug that would cause the verify step to fail — are listed below.

## Inaccuracies (numbered)

1. **Line 27 — owner module for `.indexing_state.json`.** The doc attributes it to `progress.py:IndexingStateStore` with the comment `state_file = state_dir / ".indexing_state.json"` "line 86". Verified: `archon_search/progress.py:86` reads `self._state_file = self._state_dir / ".indexing_state.json"` — the attribute is `_state_file` (private) not `state_file`. Cosmetic, but the quoted snippet does not match.

2. **Line 27 — location of `.indexing_state.json`.** The doc places the file at `search/.indexing_state.json`. Verified: the state store is constructed from `Path(cfg.db_path).expanduser()` (e.g., `server/app.py:128`, `cli/sync.py:29`, `cli/collection.py:225`, `install.py:210`). So the file lives at `<db_path>/.indexing_state.json` — only `search/` when `db_path` defaults to `~/.archon-search/search`. The table row implies a hard path; the note at line 32 partially mitigates this but the table itself should say `<db_path>/.indexing_state.json`.

3. **Line 56 — broken `ls` path in the cold-backup snippet.** The script copies `~/.archon-search` to `"$DEST/"`, producing `$DEST/.archon-search/.search.env`. The verify command is written as `ls -l "$DEST/.archon-search/.search.env"` — this is correct, but step 3 in the procedure (line 55) says "Verify mode 0600 on the key file survived the copy." The previous step (`cp -a ~/.archon-search "$DEST/"`) preserves the leading dot, so the path is fine. **No issue here on re-read — withdrawing.** (Kept numbered for traceability; net: no inaccuracy.)

4. **Line 89 — restore `cp` source path is wrong.** Snippet: `cp -a /backups/archon-search/<timestamp>/.archon-search ~/`. The backup script in step 2 of the cold backup creates `"$DEST/"` then runs `cp -a ~/.archon-search "$DEST/"`, which yields `$DEST/.archon-search` (correct). But the timestamp directory is `/backups/archon-search/<timestamp>/` so the dotfile child `.archon-search` is hidden — `cp -a` will work, but the snippet does not match the inverse of the backup snippet's `$DEST` value (which already includes the timestamp). This is consistent. **No issue — withdrawing.**

5. **Line 117 — `uv tool install archon-search`.** The repo `README.md:24` documents `pip install archon-search` as the install method; `uv tool install archon-search` is plausible but is not documented anywhere in the repo I can find. Not contradicted by code, but unverifiable as the "official" reinstall command. Minor.

6. **Line 117 — "Re-verify ONNX providers … re-detects GPU at install time (`platform/runtime.py`)."** `platform/runtime.py` does expose `Runtime` with `detect_gpu()` (CUDA on Linux, METAL on ARM macOS). However, the doc says `archon-search install` re-detects GPU. I could not verify that `install_cmd` calls GPU detection during install — needs confirmation. The CoreML provider name is also not used in the codebase; macOS detection returns `METAL` per `runtime.py:39`. The bullet's mention of "CoreML" (line 38) is therefore inaccurate terminology — the runtime concept is `METAL`, not CoreML.

7. **Line 29 — owner for `logs/archon-search.log`.** The doc says owner is "`archon-search install` writer". Verified: `archon_search/platform/macos.py:73` sets `log_path = ~/.archon-search/logs/archon-search.log` for the launchd service. Linux/Windows service files were not inspected here, but config also has `log_file = "~/.archon-search/logs/archon-search.log"` (`config.py:51`) — so the log file location is set by **config default**, not by the install command. The install command merely passes it to the service descriptor. Minor mis-attribution.

8. **Line 116 — pruner cadence.** Doc: "the pruner runs every 24h from process start". Verified in `telemetry/pruner.py`: `_run_loop` does "prune once, then sleep 24 hours". The doc adds "Restart the service to force a prune." That works because the loop prunes once immediately on entry. Accurate.

## Verified claims

- `~/.archon-search/` as the single runtime root: `config.py:82-91` (`get_default_config_path`), defaults at `config.py:33,51`, `telemetry.log_dir` default `config.py:24`, `key_manager.py:15-19`, `jobs/model.py:8`.
- `archon-search.toml` path & `get_default_config_path` name: `config.py:82`.
- `.search.env` at `~/.archon-search/.search.env`, mode `0600`, auto-generated on first start, env var name `ARCHON_SEARCH_API_KEY`, redirect via `ARCHON_SEARCH_KEY_FILE`: `key_manager.py:14-20, 82-132`.
- Key auto-generation function name `_generate_and_write`: `key_manager.py:82`.
- Default `db_path = ~/.archon-search/search`: `config.py:33`.
- `archon-search-jobs.json` at `~/.archon-search/archon-search-jobs.json`, line 8 in `jobs/model.py`: exact match.
- Job crash recovery marks `RUNNING`/`CANCELLING` as failed: `jobs/store.py:16` (`_CRASH_STATUSES = {JobStatus.RUNNING, JobStatus.CANCELLING}`).
- Telemetry daily UTC-dated `YYYY-MM-DD.jsonl` in `log_dir`: `telemetry/writer.py:149` (`self._log_dir / f"{when.date().isoformat()}.jsonl"`), default `log_dir` `~/.archon-search/search-logs` (`config.py:24`).
- Pruner runs every 24h from process start: `telemetry/pruner.py:57,64` ("sleep 24 hours").
- `[telemetry].retention_days` is enforced by the pruner: `telemetry/pruner.py` filename-date check.
- `db_path`, `log_file`, `[telemetry].log_dir` are TOML-relocatable: `config.py:33,51,24` + loader sections `141-145, 197-198, 218-222`.
- Mode 0600 preservation by `cp -a`: standard POSIX behaviour.

## Unverifiable / ambiguous

- Roadmap item IDs (`D1`, `D2`, `D5`, `PLT-1`, `CON-3`, `CON-4`, `CON-5`) — not checked against `Documentation/Backlog/03_world_class_roadmap.md` / `Architecture/530_technical_debt_refactoring_roadmap.md` per the "never trust Documentation/ files" rule. These are doc-to-doc references and cannot be validated from source.
- "LanceDB column files are append-mostly" / "v1 archive format not stable across LanceDB versions" — properties of the upstream library, not verifiable from this repo's code.
- "`/search` returns empty silently on pipeline failure (CON-5)" — this was the pre-A3 behavior; CON-5 was resolved in A3. Post-A3 pipeline failures return HTTP 500/504, not silent empty results. This line was a cross-doc reference that is now outdated.
- "`archon-search install` re-detects GPU at install time" — `platform/runtime.py` exposes detection; whether `cli/install_cmd.py` actually invokes it at install time was not confirmed in this pass.
- "Telemetry JSONL is independently self-pruning; you do not need to back it up" — true given the pruner, but "no audit requirement" is an operator-policy claim, not a code claim.
- The exact name `archon-search install` as a subcommand and its CLI behaviour — `cli/install_cmd.py` exists per CLAUDE.md, but the specific verbs (`start`, `stop`, `status`, `sync`) used in the procedures were not exhaustively re-verified here.
