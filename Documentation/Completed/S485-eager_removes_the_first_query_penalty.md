## Bug: `eager_load_embedders = true` pre-warms embedders at startup, not on first query

**ID**: S485-eager_removes_the_first_query_penalty
**Scenario**: S485
**Severity**: medium
**Version**: archon-search, version 26.8.1952

### What happened
AssertionError: eager_load_embedders=true did not reduce first-query latency: median first search 0.073s vs 0.066s with the lazy default. OperatorGuide/80_capacity_and_performance.md:119 — 'no first-query latency spike'; UserManual/30_configuration.md:70 — 'Pre-warm all embedding models at startup to remove first-query latency'.
medians: boot lazy=1.676s eager=1.877s | first-query lazy=0.066s eager=0.073s
lazy  trial 0: boot=1.676s first=0.068s steady=0.009s all=[0.068, 0.01, 0.009, 0.009, 0.009, 0.009]
lazy  trial 1: boot=1.654s first=0.066s steady=0.009s all=[0.066, 0.01, 0.009, 0.009, 0.009, 0.009]
lazy  trial 2: boot=1.692s first=0.064s steady=0.009s all=[0.064, 0.01, 0.009, 0.009, 0.009, 0.008]
eager trial 0: boot=1.882s first=0.076s steady=0.009s all=[0.076, 0.011, 0.009, 0.009, 0.009, 0.009]
eager trial 1: boot=1.877s first=0.073s steady=0.009s all=[0.073, 0.01, 0.009, 0.01, 0.009, 0.009]
eager trial 2: boot=1.657s first=0.062s steady=0.009s all=[0.062, 0.01, 0.01, 0.009, 0.009, 0.009]

assert 0.0730274161323905 < 0.06555320834740996

### What should happen
- With `eager_load_embedders = true` the server starts healthy and `POST /search` returns `200` —
  the value is accepted (30:70 lists it as a valid `[database]` key).
- **Slower boot**: the median launch→`/health 200` time with `true` is **greater** than with
  `false`, because "ONNX weights are reconstructed at startup" (80:119).
- **First-query latency moves off the first query**: the median **first** `/search` duration with
  `true` is **lower** than with `false` — the doc's "no first-query latency spike" (80:119) /
  "remove first-query latency" (30:70), stated as the comparison the doc itself draws between the
  two settings.
- Steady-state latency is unaffected: with both settings the searches after the first are far
  faster than the first, i.e. the flag changes *when* the model load is paid, not the per-query
  cost afterwards (80:119 describes a boot-vs-first-query trade, nothing about steady state).

### Steps to reproduce
1. Write an isolated `archon-search.toml` with `reranker_model = ""` and the flag under test.
2. Start `archon-search serve`; ingest one small document; stop the process.
3. Restart `archon-search serve` on the same data dir, timing **process launch → first
   `GET /health` 200**.
4. `POST /search {"collection": "eager_docs", "query": "quick brown fox"}` six times, timing each.
5. Compare medians across trials for each setting.

### Evidence
```
E   AssertionError: eager_load_embedders=true did not reduce first-query latency: median first search 0.073s vs 0.066s with the lazy default. OperatorGuide/80_capacity_and_performance.md:119 — 'no first-query latency spike'; UserManual/30_configuration.md:70 — 'Pre-warm all embedding models at startup to remove first-query latency'.
E     medians: boot lazy=1.676s eager=1.877s | first-query lazy=0.066s eager=0.073s
E       lazy  trial 0: boot=1.676s first=0.068s steady=0.009s all=[0.068, 0.01, 0.009, 0.009, 0.009, 0.009]
E       lazy  trial 1: boot=1.654s first=0.066s steady=0.009s all=[0.066, 0.01, 0.009, 0.009, 0.009, 0.009]
E       lazy  trial 2: boot=1.692s first=0.064s steady=0.009s all=[0.064, 0.01, 0.009, 0.009, 0.009, 0.008]
E       eager trial 0: boot=1.882s first=0.076s steady=0.009s all=[0.076, 0.011, 0.009, 0.009, 0.009, 0.009]
E       eager trial 1: boot=1.877s first=0.073s steady=0.009s all=[0.073, 0.01, 0.009, 0.01, 0.009, 0.009]
E       eager trial 2: boot=1.657s first=0.062s steady=0.009s all=[0.062, 0.01, 0.01, 0.009, 0.009, 0.009]
E     
E   assert 0.0730274161323905 < 0.06555320834740996
```
