# Testing rules

Root `CLAUDE.md` carries the two safety rules only. Everything else is here.
Marker descriptions and the rationale for `norecursedirs` / `-n 8` / `--cov-fail-under=85`
live as comments in `pyproject.toml` `[tool.pytest.ini_options]` — read those, don't duplicate them.
Strategy and coverage policy: `Documentation/Architecture/200_testing_strategy.md`.

## Running

```bash
uv run pytest                                   # full default suite
bash scripts/test-fast.sh                       # macOS RAM disk, ~24 s quicker; extra args pass through
TMPDIR=/dev/shm uv run pytest --basetemp=/dev/shm/archon-pt   # Linux/CI equivalent
uv run pytest tests/test_x.py --no-cov          # scoped, while iterating
uv run pytest -n0 -x tests/test_router.py::test_name          # serial — developer debugging ONLY
```

`scripts/test-fast.sh` refuses to start if another pytest is running (OOM guard) and always
tears the RAM disk down, even on Ctrl-C. `--no-cov` is a CLI-only override — never bake it into `addopts`.

Lanes excluded from the default run (`norecursedirs` primary, `-m` filter secondary):

```bash
uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov   # needs fastembed model cache
uv run pytest tests/smoke/ --no-cov                                   # spawns a real `archon-search serve`
uv run pytest tests/smoke/docker/ --no-cov                            # covered by the same tests/smoke exclusion
uv run pytest tests/eval/live/ --no-cov                               # real fastembed weights; hangs if auto-collected
uv run pytest -m docling --no-cov                                     # `-m` filter only — not a dedicated directory
```

## PARALLEL TESTS ARE MANDATORY

`addopts` sets `-n 8 --dist=loadgroup`. Never `-n auto` (=14 here) and never `-n0` in a normal run:
`-n auto` on the real-model lanes (`live_benchmark`/`smoke`, ~2 GB/worker) OOM-crashed the 48 GB machine
on 2026-07-05. `-n0` disables parallelism and is reserved for developer debugging. For more failure
detail use `--tb=short` / `--tb=long`, never `-n0 -s`.

Release CI (`archon-search-release.yml`, `archon-search-pr.yml`) passes `-n0` explicitly: it uses
multi-step `--cov-append` across separate invocations, and xdist's per-invocation combine step would
corrupt the accumulated `.coverage` file.

## ONE TEST SUITE AT A TIME

Never launch `uv run pytest` fire-and-forget (`run_in_background` without `Monitor`). Subagents may use
`run_in_background: true` + `Monitor` to outlast the ~120 s Bash foreground ceiling — `Monitor` watches
the process so nothing is abandoned. Before any run, verify no workers are alive:

```bash
ps -Ao args= | grep -E '[/]bin/pytest|[u]v run pytest'
```

Match on `args`, never `comm` (macOS truncates a Homebrew interpreter to `/opt/homebrew/Ce`, so the older
`awk '$1 ~ /[Pp]ython/'` form reports "no workers" mid-suite). `pgrep -fl pytest` self-matches the shell.
Stacked runs multiply workers and OOM-crashed the 48 GB machine on 2026-07-05.

While iterating, run scoped paths; run the full suite once at task completion and always report its
wall-clock duration (last measured: ~163 s, `--no-cov -n 8`, 8433 tests, 2026-08-12).

## Suite layout

- `tests/` — unit tests, using the ML stubs from `tests/conftest.py` (`tests/_search_stubs.py`).
- `tests/integration/` — real `SearchStore` / `SearchPipeline` / LanceDB in `tmp_path`, `TestClient`
  against a real app. Marked `integration`, run by default. Shared helpers (`make_real_app`, `ingest_doc`,
  `ingest_file_via_path`, `search`, `make_real_pipeline`) live in `tests/integration/conftest.py` —
  do NOT modify `tests/conftest.py` when adding integration tests.
- `tests/eval/` — the sanctioned regression gate for retrieval / reranking / routing / latency changes.
  Deterministic, corpus-aware but label-blind backends (`archon_search/eval/backends.py`) keep metrics
  stable without real weights; latency p50/p95 is a regression guard, not a production SLA.
  Read `tests/eval/README.md` before touching `thresholds.toml` or fixtures.

`eval` tests run by default because `--thresholds-path tests/eval/thresholds.toml` is in `addopts`; they
skip only when invoked without it. `live` tests always skip on default runs because the autouse fixture in
`tests/conftest.py` clears `ANTHROPIC_API_KEY` (this removes a 30 s SDK-timeout floor). To run them for
real, temporarily comment out that `monkeypatch.delenv` line — every invocation loads the root conftest,
so there is no shell-level workaround.
