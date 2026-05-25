# Feature Brief: Tiered Install Profiles

## Problem
Users installing archon-search have no guidance on which embedding and reranker models to use — the default is silently the smallest/fastest option, and the only way to change it is editing raw config after the fact. First-time users don't know what they're getting, and there's no path to better quality without reading documentation.

## Goal
`archon-search install` presents a clear, one-decision profile selection (Minimal / Balanced / Max) with an optional `--multilingual` modifier. The user sees exactly what will be downloaded, what quality/speed tradeoff they're making, and what hardware it suits — before confirming. After install, the server runs with the chosen models pre-warmed, no surprise download delay on first search.

## Users & Context
Developers and technical users installing archon-search for the first time, or reinstalling on new hardware. They are in setup mode: willing to read a few lines, not willing to read a manual. They know their machine (RAM, GPU) but not necessarily what ONNX or BGE means.

## Core Flow

1. User runs `archon-search install` (no flags) or `archon-search install --profile balanced --multilingual`
2. If no `--profile` flag given, interactive mode shows a compact table:

```
  Profile      Download    Quality       Speed (CPU / Apple Silicon)
  ─────────    ────────    ───────       ───────────────────────────
  1) Minimal    ~147 MB    ★★☆☆☆        ~40 ms/query  / ~15 ms
  2) Balanced   ~330 MB    ★★★★☆        ~150 ms/query / ~50 ms
  3) Max        ~2.3 GB    ★★★★★        ~400 ms/query / ~130 ms

  Models (all sizes from fastembed registry, verified):
  1) BAAI/bge-small-en-v1.5 (67 MB) + Xenova/ms-marco-MiniLM-L-6-v2 (80 MB)
  2) BAAI/bge-base-en-v1.5 (210 MB) + Xenova/ms-marco-MiniLM-L-12-v2 (120 MB)
  3) BAAI/bge-large-en-v1.5 (1.2 GB) + BAAI/bge-reranker-base (1.04 GB)

  Best for:
  1) Personal use, <10k docs, fast responses, low RAM
  2) Team use, 10k–200k docs, good recall, ~1 GB RAM
  3) Large corpora, 200k+ docs, highest precision, ~2.5 GB RAM

  Add --multilingual to use multilingual models instead.
  Choice [1-3, default 1]: _
```

3. If `--multilingual` flag is given (or selected), show the multilingual model substitution before confirming:

```
  Multilingual substitutions (all sizes from fastembed registry, verified):
  1) sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (220 MB) — no reranker
  2) sentence-transformers/paraphrase-multilingual-mpnet-base-v2 (1.0 GB) + jinaai/jina-reranker-v2-base-multilingual (1.11 GB)
  3) intfloat/multilingual-e5-large (2.24 GB) + jinaai/jina-reranker-v2-base-multilingual (1.11 GB)

  WARNING: jina-reranker-v2-base-multilingual is licensed CC-BY-NC-4.0 (non-commercial
  use only). Commercial use of profiles 2 and 3 (multilingual) is not permitted without
  an alternative reranker. You will be required to confirm license acceptance before
  this model is downloaded.
```

4. Summary confirmation screen before proceeding:

```
  Installing: Balanced · English
  Embedder:   BAAI/bge-base-en-v1.5             (~210 MB, downloaded during install)
  Reranker:   Xenova/ms-marco-MiniLM-L-12-v2   (~120 MB, downloaded during install)
  Chunk size: 512 tokens
  Providers:  CoreML (Apple Silicon detected)

  Note: Model files are downloaded now. ONNX session initialization happens in the
  server process on first query — expect ~5–15s latency on first search.

  Proceed? [Y/n]: _
```

5. Pre-warm step: CLI instantiates `TextEmbedding(model, lazy_load=True)` and `TextCrossEncoder(model, lazy_load=True)` — this downloads model files to the fastembed cache without creating an ONNX session, so there is no wasted session in the CLI process. The ONNX session is initialized server-side on first query (~5–15s depending on model size).

```
  [4/5] Downloading models...
        Embedder  ████████████████████  210 MB ✓
        Reranker  ████████████████████  120 MB ✓
        Download complete.
```

6. Service starts after model download completes. Install writes profile values to `~/.archon-search/archon-search.toml`.
7. On completion: "archon-search installed and running. Profile: Balanced · English."

## In Scope

- **Refactor `install_cmd.py` to delegate all install logic to `SearchInstaller` (`install.py`).** This is a substantial merge — see Key Decisions for the full scope of behavior that must be reconciled.
- `--profile [minimal|balanced|max]` CLI flag
- `--multilingual` CLI flag (modifies the selected profile's model set)
- Interactive profile selection table when `--profile` is not given
- Profile-to-model mapping: writes `embedding_model`, `reranker_model`, `chunk_size`, `profile`, and `multilingual` to `[database]` config section via direct tomlkit write (not via `save_config()`); config file persisted with `_durable_io.atomic_write_bytes(path, tomlkit.dumps(doc).encode())` for fsync atomicity
- **Add `profile: str` and `multilingual: bool` to `SearchConfig` and extend `load_config()` to read them from `[database]`.**
- Model pre-warm (download only) before service starts (blocking, shows progress); only skipped when `--skip-preload` is passed
- Reinstall guard: detect existing config with different `embedding_model` or `chunk_size`; abort with clear message; require `--force --delete-db` to override
- `--skip-preload` flag to opt out of model download pre-warm
- Speed estimates displayed as approximate (labeled "~Xms, varies by hardware")
- `uninstall` command stays in `install_cmd.py` (see Key Decisions)

## Out of Scope

- Runtime profile switching without reinstall — models are baked into the index at embed time; switching requires full re-index
- Per-collection model overrides — adds significant complexity; deferred
- Profile "upgrade" path that migrates existing index — requires background re-embedding job; deferred to a dedicated migration feature
- GPU provider selection by user at install time — auto-detection stays; profiles don't override GPU
- Chunk size as a separate user-facing option during install — advanced users can edit config post-install
- ColBERT / late-interaction or SPLADE hybrid retrieval tiers — architectural change; separate feature
- Windows service management — already a stub; out of scope here

## Key Decisions

- **Single installer**: All install logic lives in `SearchInstaller` (`install.py`). The Click command in `install_cmd.py` delegates to `SearchInstaller` and calls `run()` — no logic of its own. Profile selection, pre-warm, reinstall guard, and messaging all live in `SearchInstaller`. Note: `install_cmd.py` and `install.py` currently overlap but diverge in behavior. `install_cmd.py` handles: default config creation (via `_default_toml()`), legacy service cleanup (`_remove_legacy_service()`), log directory creation. `install.py` handles: GPU detection, ONNX provider validation, service file writing. The merge must explicitly assign each behavior to `SearchInstaller` and test the combined paths. This is a substantial merge, not a trivial shim conversion.
- **`uninstall` stays in `install_cmd.py`**: Since `install_cmd.py` is retained as the shim file, `uninstall` stays in the same file to avoid breaking `main.py` imports. Relocating would add churn with no benefit.
- **`--profile` + `--multilingual` flags (not a combined 6-option list)**: cleaner CLI API; composable; interactive mode collapses the matrix into readable sections rather than 6 confusing names
- **Interactive fallback when no flag**: `--profile` + `--multilingual` enable scripted/CI installs; bare `archon-search install` stays guided for first-time users
- **Chunk size is per-profile (512 / 512 / 1024)**: Max profile pairs better with 1024-token chunks; switching profiles already requires `--force --delete-db`, so the destructive risk is already gated
- **Pre-warm is default-on; only `--skip-preload` skips it**: CI deployments need predictable startup just as much as interactive installs — `--non-interactive` does NOT skip pre-warm. Only `--skip-preload` opts out. Models must download before first query anyway; doing it at install with progress feedback is strictly better UX than a silent delay on first query. Implementation: `TextEmbedding(model, lazy_load=True)` and `TextCrossEncoder(model, lazy_load=True)` — downloads to fastembed cache without creating an ONNX session in the CLI process. ONNX session initialization still occurs server-side on first query.
- **`--force` is only valid when paired with `--delete-db`**: Using `--force` alone must be rejected with: "Profile switch requires both --force and --delete-db. Using --force without --delete-db is not allowed because it would leave stranded vectors in the existing index." Even with `--force --delete-db`, a confirmation gate prints "WARNING: This will permanently delete all indexed data. Type 'yes' to confirm: " (bypassed only with `--non-interactive`, not with `--force` alone). Operation order for `--force --delete-db`: (1) back up config, (2) confirm destructive action, (3) stop service, (4) delete DB directory, (5) write new profile config, (6) download models (pre-warm), (7) start service. If any step fails, restore config backup and do NOT start service.

  > **Note:** If failure occurs at step (4) or later (DB already deleted), the config backup is restored but the old index data is unrecoverable — the user must re-run install. Clearly communicate this: "Install failed after database deletion. Your previous index has been removed. Re-run install to create a fresh index."

- **Reinstall with different `embedding_model` or `chunk_size` requires `--force --delete-db`**: changing either strands existing vectors; the consequence must be explicit, not implicit. The reinstall guard checks both `embedding_model` AND `chunk_size`. Reranker model changes do NOT require `--force --delete-db` because rerankers do not produce stored embeddings — they operate on retrieved results. A reranker change takes effect immediately after config edit without breaking the vector schema.
- **Persist `profile` and `multilingual` in config**: writing `profile = "balanced"` and `multilingual = false` (or `true`) to `[database]` enables accurate reinstall guard error messages ("Existing index uses Balanced (English) profile") and future migration tooling.
- **Minimal multilingual has no reranker**: fastembed's only multilingual cross-encoders are `jinaai/jina-reranker-v2-base-multilingual` (1.11 GB) and two English-only Jina models — none are small enough for the Minimal tier. Ranking falls back to RRF score; this is surfaced clearly in the profile table.
- **LanceDB Arrow schema bakes in embedding dimension**: The LanceDB Arrow schema bakes embedding dimension as a fixed-width `pa.list_(pa.float32(), embedding_dim)` column. This is the technical root cause why profile switching is destructive — it is not just a config incompatibility. The `--delete-db` requirement exists because the schema must be recreated with the correct dimension for the new model. Embedding dimensions by profile: Minimal=384d, Balanced=768d, Max=1024d.
- **Jina reranker license**: `jina-reranker-v2-base-multilingual` is licensed CC-BY-NC-4.0 (non-commercial use only). Commercial users of archon-search must not use profiles that include this model. Either (a) find an alternative multilingual reranker with a permissive license, or (b) add a license acknowledgment prompt during multilingual install that requires explicit user confirmation before downloading the Jina model.

## Edge Cases & Constraints

- **Reinstall same profile**: idempotent — detect matching `embedding_model` AND `chunk_size` in existing config and skip model config step; do not re-write config unnecessarily. Idempotent check requires the config file to exist and contain matching values. If the config file is missing (manually deleted), always write it — do not treat a missing-config + existing-DB state as idempotent.
- **Reinstall different profile without `--force --delete-db`**: print "Existing index uses [model]. Switching to [new model] requires re-indexing all documents. Run with --force --delete-db to proceed." and exit 1
- **`--force` without `--delete-db`**: reject immediately with "Profile switch requires both --force and --delete-db. Using --force without --delete-db is not allowed because it would leave stranded vectors in the existing index." and exit 1
- **`--delete-db` scope**: Must clear the LanceDB data directory. The `.indexing_state.json` state file currently lives inside `db_path`, so deleting the DB directory removes it automatically. If the state file location changes in a future refactor, the `--delete-db` cleanup step must be updated explicitly.
- **Failed install (pre-warm fails or service fails to start)**: The installer must back up the existing config file before writing any profile values. If pre-warm or service start fails, restore the backup. Never leave a config that references a model that failed to download. Atomic write-then-verify: only commit the new config after pre-warm download succeeds.
- **Partial pre-warm failure**: If the embedder downloads successfully but the reranker download fails, abort the install. Restore the config backup, do NOT start the service, and print which model failed along with a command to retry. This is consistent with the 7-step rollback in Key Decisions — partial success is not acceptable.
- **Pre-warm on METAL**: GPU validation already downloads ~150 MB in step 2; pre-warm must not double-download; detect if model is already cached
- **Pre-warm timeout**: The timeout must scale with expected download size: `timeout = min(1800, max(300, estimated_bytes / 100_000))` (assuming 100 KB/s floor, capped at 30 minutes). If the timeout fires, warn and continue — the service starts without a cached model and the first query pays the download cost.
- **Disk space**: Before starting model downloads, check available disk space using `shutil.disk_usage()`. If available space is less than 2× the total download size (to allow for partial download and decompression), abort with: "Insufficient disk space. [profile] requires ~[size] GB free; only [available] GB available."
- **`--non-interactive` + no `--profile` + no `--multilingual`**: default to `minimal` (English); log both choices. Multilingual defaults to false in all non-interactive paths.
- **Terminal width**: table display must degrade gracefully on narrow terminals (< 80 cols) — fall back to list format
- **Speed estimates**: labeled as approximate in UI; benchmark suite (`-m benchmark`) is the authoritative source; do not hardcode as guarantees
- **Fresh install config creation**: Currently `install_cmd.py` creates `archon-search.toml` via `_default_toml()` if absent. After consolidation, `SearchInstaller` must explicitly create the config file before writing profile values. It cannot rely on `load_config()` returning defaults — those defaults live in memory only and are not written to disk without explicit persistence.
- **Testing in CI**: The install flow starts OS daemons (launchd/systemd) which cannot run in CI. Tests must mock `service.start()`, `service.write_service_file()`, and `_wait_for_service()`. The profile selection, config write, and pre-warm download steps must be independently testable without starting a real service. `--non-interactive --profile minimal --skip-preload` is the canonical CI test invocation.
- **Concurrent installs**: Multiple simultaneous `archon-search install` invocations must be serialized via an advisory file lock (`~/.archon-search/.install.lock`). If a lock is held, the second invocation should exit with "Install already in progress." Use a PID-based lock file (write the PID, check if the PID is still alive on lock contention) to handle stale locks from crashed installs. If the PID is dead, remove the stale lock and proceed.

## Open Questions

- **Pre-warm progress display**: fastembed exposes download progress via tqdm (GCS-hosted models) and huggingface_hub (HF-hosted models), both writing to stderr. The challenge is capturing and redirecting it for custom rendering. Options: (a) let default tqdm/HF bars print to stderr — simplest; (b) use `huggingface_hub.utils.enable_progress_bars()` / `disable_progress_bars()` to control visibility; (c) redirect stderr and parse. Option (a) is recommended for v1 unless `rich` is already a dependency.
- **`_default_toml()` interaction**: Should `archon-search install --profile` replace or extend `config_cmd.py`'s `_default_toml()`? Profile-specific defaults may need a `_profile_toml(profile, multilingual)` factory.

### Resolved (formerly BLOCKING)

- **Model names and sizes**: Verified against fastembed registry. All profile table entries are now authoritative. The existing `config.py:35` default `cross-encoder/ms-marco-MiniLM-L-6-v2` is a P0 bug — the correct name is `Xenova/ms-marco-MiniLM-L-6-v2`. Fix this in the same PR as the profile work.
- **Pre-warm mechanism**: `TextEmbedding(model, lazy_load=True)` and `TextCrossEncoder(model, lazy_load=True)` download to the fastembed cache without creating an ONNX session in the CLI process. No throwaway session; no wasted time.
- **Config write path**: Direct tomlkit write to `[database]` section following the `configure_providers()` pattern. Persist with `_durable_io.atomic_write_bytes(path, tomlkit.dumps(doc).encode())` — do NOT use bare `write_text()` (the existing `configure_providers()` call is a gap that must be fixed in the same work). `SearchConfig` must gain `profile: str = ""` and `multilingual: bool = False` fields; `load_config()` must read them from `[database]`. `save_config()` non-durability is a separate tech-debt item.

## Future Iterations

- **Profile migration command** — `archon-search migrate --profile max` that re-embeds all collections in the background with a progress job tracked via the jobs API
- **Per-collection model overrides** — once the migration path exists, allow individual collections to pin a model
- **Auto-profile recommendation** — detect available RAM and GPU at install time and recommend a profile ("Your machine supports Max — recommended")
- **ColBERT / SPLADE ultra tier** — requires multi-vector index support in `store.py`; separate feature

## Recommendation

This is the right feature to build now — the current install experience actively hides quality options from users who would benefit from them. The pre-warm approach is clean: `lazy_load=True` downloads model files without wasting time on a throwaway ONNX session. The progress display challenge (tqdm/HF to stderr) is straightforward for v1. Do not ship without the pre-warm download phase — a silent first-query delay on the Max profile is worse than the current experience. The reinstall guard is non-negotiable; stranded vectors are a silent correctness bug. One prerequisite before any implementation: fix the P0 bug in `config.py:35` (`cross-encoder/ms-marco-MiniLM-L-6-v2` → `Xenova/ms-marco-MiniLM-L-6-v2`) — the reranker is currently silently broken on every install.
