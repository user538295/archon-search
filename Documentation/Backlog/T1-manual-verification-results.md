---
id: T-1
feature: cli-startup-latency (2026-07-15-190)
date: 2026-07-19
tester: Claude Code (Sonnet 4.6)
---

# T-1 Manual Verification Results

*Contains: one manual timing measurement (Test 1), one automated-guard re-confirmation (Test 2, BE-4), and one structural code verification (Test 3).*

## Manual Test 1 — Lightweight command wall-time (S11)

**Method:** Python `time.perf_counter()` wrapping `subprocess.run` of
`.venv/bin/archon-search config show`.  
**Isolation:** `ARCHON_SEARCH_DATA_DIR` and `ARCHON_SEARCH_CONFIG` pointed at a
fresh `tempfile.TemporaryDirectory()`; `ARCHON_SEARCH_API_KEY` set to 64 zeros;
`ANTHROPIC_API_KEY` removed from env.  
**Binary used:** `/Users/manczg/Documents/development/archon-search/.venv/bin/archon-search`
(direct venv entry point — NOT `uv run`, per plan Q2).

| Run | Elapsed (s) |
|-----|-------------|
| 1   | 0.343       |
| 2   | 0.223       |
| 3   | 0.206       |
| 4   | 0.230       |
| 5   | 0.239       |

**Sorted:** 0.206 · 0.223 · **0.230** · 0.239 · 0.343  
**Median (run 3 of sorted):** **0.230 s**  
**< 0.2 s:** No

### Interpretation

The median of 0.230 s does not meet the < 0.2 s headline target.  Run 1 (0.343 s)
is a spawn/OS file-cache outlier (common on first invocation due to OS disk I/O variance); runs 2–5 cluster around 0.21–0.24 s.

This is expected per the plan's "Known limitations" section:

> "The remaining gap to 0.2 s is the Python interpreter + Click startup floor
> and — for store-reading commands — the ~900 ms lancedb first-import floor,
> neither of which this feature touches."

`config show` is a lightweight command: it calls `load_config()` and prints
TOML values without touching LanceDB.  The ~0.23 s floor is the CPython
interpreter startup plus Click group registration across all subcommand modules
(which are eagerly imported by `main.py` at group-build time, but are now cheap
thanks to BE-1, BE-2, BE-3).

**Verdict for S11 (timing component):** Target not met (0.230 s vs < 0.2 s goal) — gap attributed to the documented interpreter + Click startup floor, explicitly out of scope for this feature (plan: Known limitations section).

- [x] Five timing measurements collected with real binary
- [x] Median computed: 0.230 s
- [ ] Median strictly < 0.2 s (not yet — 0.230 s; gap is interpreter floor, documented in-plan)

---

## Manual Test 2 — sys.modules absence (S1, S6, S11)

**Method:** Automated test `tests/test_cli_startup_latency.py` (BE-4, already
shipped).  Spawns a fresh Python interpreter subprocess running
`archon-search config show` and asserts `claude_agent_sdk` and `fastembed`
are absent from `sys.modules`.

**Command run:**

```
uv run pytest tests/test_cli_startup_latency.py --no-cov -n0 -v
```

**Result:**

```
tests/test_cli_startup_latency.py::test_lightweight_cmd_no_claude_agent_sdk PASSED
tests/test_cli_startup_latency.py::test_lightweight_cmd_no_fastembed PASSED

2 passed in 0.65s
```

- [x] `claude_agent_sdk` absent from `sys.modules` after `config show`
- [x] `fastembed` absent from `sys.modules` after `config show`
- [x] Positive control: `archon_search.cli.main` and `archon_search.cli.serve` present (subprocess ran correctly, not vacuously)

**Verdict for S1, S6, and the S11 absence-clause (automated proof):** PASS

---

## Manual Test 3 — Cold-cache heavy command / structural verification (S7)

S7 is marked *smoke/manual only; excluded from the default CI suite*.  A live
network test with an empty fastembed model cache is non-automatable.  Per the
task spec, structural code verification is the accepted substitute.

### Structural check: `serve.py` defers fastembed

`archon_search/cli/serve.py` line 55:

```python
from archon_search.server.app import run_server  # noqa: PLC0415
```

This line is **inside the `serve()` function body** (not at module top).  
Before BE-2, the import was at module level (line 25), which pulled in
`server/app.py` → `model_validation.py` → `from fastembed import TextEmbedding`
at startup.  After BE-2, the chain only fires when `serve()` is actually called.

**Verification:** `grep -n "from archon_search.server.app import"` in
`archon_search/cli/serve.py` returns only line 55 (inside the function).  No
module-level `server.app` import exists.

- [x] `run_server` import is deferred into `serve()` — fastembed does NOT load on
  startup for lightweight commands
- [x] Heavy command (`serve`) still imports `run_server` and the full ML stack
  when invoked — confirmed by existing passing test suite (test_cli_serve.py)

### Structural check: `description_generator.py` defers claude_agent_sdk

`archon_search/description_generator.py` line 100:

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage  # noqa: PLC0415
```

This line is **inside `_call_haiku()`** (not at module top).  Module-level
imports are: `asyncio`, `logging`, `os`, `random`,
`archon_search.constants.DEFAULT_FAST_MODEL`.  No `claude_agent_sdk` attribute
exists at module scope.

- [x] `claude_agent_sdk` import deferred into `_call_haiku()` — SDK does NOT load
  on any pipeline import path (CLI or server startup)
- [x] Verified no module-level `claude_agent_sdk` / `ClaudeSDKClient` /
  `ClaudeAgentOptions` / `ResultMessage` attributes in `description_generator.py`

**Verdict for S7:** PASS (structural verification; live first-download test not
run — non-automatable, accepted per plan)

---

## Overall Conclusion

| Test | Criterion | Result |
|------|-----------|--------|
| T1-1: Wall-time median | Approaches < 0.2 s | NOT MET — 0.230 s vs < 0.2 s; gap is documented interpreter + Click floor (out of scope, accepted per plan) |
| T1-2: sys.modules absence (automated) | `claude_agent_sdk` and `fastembed` absent | PASS |
| T1-3: Cold-cache structural (S7) | Heavy imports deferred into function bodies | PASS |

**Overall verdict for T-1:** The automated `sys.modules`-absence guard (authoritative per
plan Q2) passes with zero issues — this is the authoritative acceptance criterion per the plan.
The timing result (0.230 s median) does not meet the headline < 0.2 s target but is within
the expected range given the documented interpreter + Click startup floor that is explicitly
out of scope for this feature; the gap is accepted per the plan's Known limitations section.
