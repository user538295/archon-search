## Bug: GET /graph/{col}/impact/{symbol}?file_path=... is ignored entirely — same-named symbols are never disambiguated and the resolved symbol is arbitrary

**ID**: S68-file_path_query_param_is_ignored
**Scenario**: S68
**Severity**: high
**Version**: archon-search, version 26.8.1751

### What happened
The `file_path` query parameter has NO effect. On a collection containing two files that each define `helper()`, six variants of the request return byte-identical results:

  omitted                                   -> callers.direct = [caller_a, helpers_a]
  ?file_path=helpers_a.py                   -> callers.direct = [caller_a, helpers_a]
  ?file_path=helpers_b.py                   -> callers.direct = [caller_a, helpers_a]
  ?file_path=<abs>/helpers_a.py             -> callers.direct = [caller_a, helpers_a]
  ?file_path=<abs>/helpers_b.py             -> callers.direct = [caller_a, helpers_a]
  ?file_path=nope_does_not_exist.py         -> callers.direct = [caller_a, helpers_a]

All six are HTTP 200 with depth_used=1. A path that does not exist in the collection is accepted just as happily as a real one — so the value is not merely mismatched, it is not consulted.

The data needed to disambiguate IS present: GET /graph/{col} shows TWO distinct `helper` code_symbol nodes with different entity_ids (15b5b32b… and 7a78141c…), consistent with the documented file-qualified node IDs. The endpoint simply picks one and ignores the caller's stated file.

Worse, WHICH one it picks is arbitrary between runs. The same assertion on the same corpus resolved to `{caller_b, helpers_b}` in one process and `{caller_a, helpers_a}` in another. So a caller cannot even compensate by learning the fixed preference.

Also note `file_path` is `null` on every returned entry, so the response gives the caller no way to detect which file the answer is actually about.

### What should happen
docs/UserManual/70_code_graph_and_impact.md:84 documents the parameter in the impact endpoint's query-parameter table:

  | `file_path` | — | Disambiguates same-named symbols to the one defined in this file. |

and :28 states that "`code_symbol` node IDs are **file-qualified** (`name::path`), so two same-named symbols in different files stay distinct nodes". So `?file_path=helpers_a.py` should resolve `helper` to the definition in helpers_a.py and report `caller_a` — not `caller_b` — and the reverse for helpers_b.py.

At minimum the parameter must be honoured. A path matching no definition in the collection should not silently return another file's blast radius; an error or an empty result would both be defensible, silently answering about a different symbol is not.

### Steps to reproduce
1. Install the extras and enable the graph:
   uv tool install 'archon-search[graph,code]'
   # [graph] enabled = true in the config, then restart the server
2. Create two files that each define the same symbol:
   mkdir -p /tmp/archon-code-graph-dup
   printf 'def helper():
    return "a"

def caller_a():
    helper()
' > /tmp/archon-code-graph-dup/helpers_a.py
   printf 'def helper():
    return "b"

def caller_b():
    helper()
' > /tmp/archon-code-graph-dup/helpers_b.py
3. archon-search ingest --path /tmp/archon-code-graph-dup --collection code-dup --wait
   (run twice — cross-file inferred edges need a prior ingest, per 70:57)
4. curl -s -H "Authorization: Bearer $KEY" "$BASE/graph/code-dup/impact/helper?file_path=helpers_a.py"
5. curl -s -H "Authorization: Bearer $KEY" "$BASE/graph/code-dup/impact/helper?file_path=helpers_b.py"
6. curl -s -H "Authorization: Bearer $KEY" "$BASE/graph/code-dup/impact/helper?file_path=nope_does_not_exist.py"
Compare the three `callers.direct` arrays — they are identical.

### Evidence
```
Probed 2026-08-01 on a fresh graph-enabled isolated instance, archon-search 26.8.1751 with the [graph,code] extras installed (tree-sitter 0.25.2, all nine grammars importing).

GET /graph/s068-graph-dup -> 200, 6 nodes, all entity_type=code_symbol:
  {"entity_id": "15b5b32b80896eeb1c3bc0cbbe0fa1cb05b11fcb0ec8ddc3ff8bba8ad00e69a2", "entity_name": "helper", ...}
  {"entity_id": "7a78141c9288653313c7d5b52d6243b01766bb0c4ae5dcd9d8b0100f4321fff1", "entity_name": "helper", ...}
  {"entity_name": "helpers_b"}, {"entity_name": "helpers_a"}, {"entity_name": "caller_b"}, {"entity_name": "caller_a"}

GET /graph/s068-graph-dup/impact/helper[?file_path=...] — (entity_name, file_path, extraction_method):
  none        HTTP 200 depth_used=1 symbol='helper' callers.direct=[('caller_a', None, 'extracted'), ('helpers_a', None, 'extracted')]
  basename_a  HTTP 200 depth_used=1 symbol='helper' callers.direct=[('caller_a', None, 'extracted'), ('helpers_a', None, 'extracted')]
  basename_b  HTTP 200 depth_used=1 symbol='helper' callers.direct=[('caller_a', None, 'extracted'), ('helpers_a', None, 'extracted')]
  abs_a       HTTP 200 depth_used=1 symbol='helper' callers.direct=[('caller_a', None, 'extracted'), ('helpers_a', None, 'extracted')]
  abs_b       HTTP 200 depth_used=1 symbol='helper' callers.direct=[('caller_a', None, 'extracted'), ('helpers_a', None, 'extracted')]
  bogus       HTTP 200 depth_used=1 symbol='helper' callers.direct=[('caller_a', None, 'extracted'), ('helpers_a', None, 'extracted')]

Non-determinism, from the pytest run of the same scenario minutes earlier on a separate instance:
  AssertionError: caller_a missing: {'helpers_b', 'caller_b'}

Severity high rather than medium: impact analysis is the feature's stated payoff (70:8, "a caller/callee blast-radius answer to 'what is affected if I change this symbol?'"), this returns a confidently-wrong answer about a DIFFERENT symbol with no error and no field the caller could use to notice, and the docs themselves (70:26) warn that common names like `run`/`get`/`init` collide often — so the duplicate-name case this breaks on is the normal case in real code, not a corner.

The paired test test_helpers_b_resolves_caller_b currently PASSES, but only by coincidence of which node the arbitrary resolution happens to pick; it is not evidence the parameter works.

CONFIRMED 2026-08-01, later the same day — the prediction above held. Across three runs of the unchanged suite on the unchanged corpus, the failure moved between the two paired tests with no code change in between:

    run 1  FAILED test_helpers_a_resolves_caller_a   - caller_a missing: {'caller_b', 'helpers_b'}
    run 2  FAILED test_helpers_a_resolves_caller_a   - caller_a missing: {'caller_b', 'helpers_b'}
    run 3  FAILED test_helpers_b_resolves_caller_b   - caller_b missing: {'caller_a', 'helpers_a'}

Exactly one of the pair fails every time, and which one is not stable. This rules out the remaining benign reading — that the server has a fixed, undocumented preference (say, first-ingested or lexicographically-first node) that a caller could learn and work around. There is no preference to learn. It also means either test flipping red in a future run is THIS bug resurfacing, not a new regression.

CAVEAT ON RUN 3, stated so the evidence is not read as stronger than it is: run 3 overlapped the start of a full destructive suite run (12:33:30) that reinstalls the app mid-flight, so that process did not have the machine to itself and the flip it showed is not a clean observation. Runs 1 and 2 are clean and both failed the SAME test. The non-determinism claim therefore does NOT rest on run 3 — it rests on the two independent same-day observations recorded further up this report, where the identical assertion resolved to `{caller_b, helpers_b}` in one process and `{caller_a, helpers_a}` in another under no concurrent load. Run 3 is consistent with that and nothing more. The core finding — six `file_path` variants including a nonexistent path returning byte-identical bodies — is single-process and unaffected by any of this.
```

RUN 4 — CLEAN CONFIRMATION, 2026-08-01 audit pass. This is the observation run 3 was
supposed to be and was not: the machine was idle (no report.py, no concurrent agent run,
no reinstall in flight), the suite and corpus were unchanged, and the pair flipped again:

    run 4  PASSED test_helpers_a_resolves_caller_a
           FAILED test_helpers_b_resolves_caller_b  - caller_b missing: {'caller_a', 'helpers_a'}

Same shape as run 3, but obtained under conditions that cannot be dismissed. The
non-determinism claim no longer depends on the two earlier cross-process observations
alone, and the run-3 caveat above is superseded rather than deleted — it is left in place
because it records what the evidence looked like before this run existed.

Standing consequence for whoever maintains tests/test_s068_file_path_disambiguates_symbols.py:
EITHER member of the pair may be red on any given run. A red `helpers_a` and a red
`helpers_b` are the same defect, not two. Do not "fix" one by relaxing its assertion —
that would merely move the failure to its sibling.
