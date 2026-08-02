## Bug: First-start extras install exceeds the documented 3–5 min on an idle machine: 398s (lightest set), 423s (default set)

**ID**: S232-first_start_exceeds_documented_3_to_5_minutes
**Scenario**: S232
**Severity**: medium
**Version**: ghcr.io/user538295/archon-search:latest (archon-search, version 0.0.0+local; linux/arm64)

### What happened
Both measured first starts exceeded the documented upper bound, on an otherwise idle machine:

1. ARCHON_EXTRAS=code (a SINGLE extra, the lightest possible set): 398s (6m38s) — 33% over the documented 5-minute bound, and 38s beyond the image's own 360s HEALTHCHECK start-period.

2. Default graph,code,multilingual (no ARCHON_EXTRAS override — the exact set doc :212 describes as "~3-5 min"): 423s (7m03s) — 41% over the documented bound, 63s beyond the 360s start-period. Stamp written correctly as 'graph,code,multilingual'.

So the documented 3-5 min range is exceeded for BOTH the lightest and the default extras set, before any adverse conditions are introduced.

VARIANCE — first-start time is network-bound and varies widely. A separate run of the same default set failed to reach /health 200 within 600s while other processes on the machine were concurrently downloading Python packages over the same uplink. That >600s figure is NOT reproducible in isolation and should not be treated as the expected duration; it is reported only as evidence of how sensitive this operation is to competing network load. The two figures above are the reproducible idle-machine measurements.

The product is not malfunctioning: the install completes and the stamp is written correctly in every case. Only the documented duration is wrong.

Likely cause of the magnitude — the image bakes in PIP_NO_CACHE_DIR=1 (verified via docker image inspect .Config.Env). Persisting /pip-packages saves the *install* on subsequent starts via the stamp, but nothing on the *first* start of any new volume: pip retains no wheel cache, so every first start re-downloads the full dependency set — including large ML wheels such as lancedb, docling and the fastembed/multilingual stack — over the network. That makes first-start time network-bound, explains the magnitude, and explains the variance under load. Doc :28's qualifier "depending on network speed" acknowledges the dependence, but the 3-5 min range does not reflect it.

Consequence: the 360s start-period at :94 is undersized against the image's own default behaviour even on an idle machine, so a container can be marked unhealthy by its own HEALTHCHECK while it is still legitimately installing. An operator sizing an orchestrator readiness/liveness budget from this doc will under-provision it and see spurious restart loops on fresh deployments — and more so on a busy network.

### What should happen
docs/UserManual/140_running_with_docker.md states the first-start extras install completes in 3–5 minutes (180–300s) in three places:
  - :28  "First start triggers a pip install that can take 3–5 minutes depending on network speed."
  - :94  "The image declares a HEALTHCHECK that polls /ready every 15s after a 360s start-period. The generous start-period accommodates the first-start extras install (up to 3–5 minutes)"
  - :212 "First start only: the entrypoint installs graph + code + multilingual extras and downloads en_core_web_sm (~3–5 min)."
A first start should reach /health 200 within 300s, comfortably inside the 360s HEALTHCHECK start-period the image declares for exactly this purpose.

### Steps to reproduce
Default set (measurement 2 — the set the doc describes):
1. docker volume create archon-measure-default-pkgs
2. docker run -d --name archon-measure-default -e ARCHON_SEARCH_API_KEY=$(openssl rand -hex 32) -v archon-measure-default-pkgs:/pip-packages -p 18778:8765 ghcr.io/user538295/archon-search:latest
3. Poll http://127.0.0.1:18778/health every 5s from the moment of docker run, recording elapsed seconds until it first returns 200.
4. docker exec archon-measure-default cat /pip-packages/.extras-installed
5. docker rm -f archon-measure-default && docker volume rm archon-measure-default-pkgs

Lightest set (measurement 1): as above, adding -e ARCHON_EXTRAS=code.

Run both on an otherwise idle machine — concurrent downloads inflate the result substantially (see VARIANCE).

### Evidence
```
Both runs used a fresh (empty) /pip-packages named volume.

Image env, confirming no wheel cache is retained (docker image inspect ghcr.io/user538295/archon-search:latest --format '{{range .Config.Env}}{{println .}}{{end}}'):
    PIP_DISABLE_PIP_VERSION_CHECK=1
    PIP_NO_CACHE_DIR=1
    FASTEMBED_CACHE_PATH=/data/fastembed-cache

Measurement 1 — ARCHON_EXTRAS=code:
    HEALTHY after 398s
    code <- stamp

Measurement 2 — default graph,code,multilingual (idle machine):
    started 10:19:14 — default extras (no ARCHON_EXTRAS override)
      … still installing at 116s
      … still installing at 237s
      … still installing at 358s
    HEALTHY after 423s
    stamp: graph,code,multilingual

Variance datapoint (concurrent package downloads on the same uplink) — default set, 600s budget, pytest S232:
    AssertionError: /health never reached 200 on first start
The captured container log carries hundreds of pip 'Collecting'/'Downloading' lines, confirming the install was genuinely progressing rather than stuck, and that a recreate restarted it from scratch because no stamp had yet been written:
    [entrypoint] ARCHON_EXTRAS=graph,code,multilingual
    [entrypoint] Extras not yet installed (or list changed) — starting pip install …
    [entrypoint] (First start: this may take 3–5 minutes)

Host: macOS (Darwin 25.5.0), Docker Desktop 4.73.0, linux/arm64 native image (no QEMU emulation).
Documented bound: 300s. Image HEALTHCHECK start-period: 360s. Observed (idle): 398s lightest, 423s default.
```

### Third independent measurement, 2026-08-01 audit pass

Re-ran `tests/test_s232_pip_packages_volume_persists_extras.py` on an idle machine. Container
`archon-recreate`, default extras (`graph,code,multilingual`), image already pulled locally:

    entrypoint "starting pip install"   11:37:47
    container reports healthy           ~11:44:5x
    -> first start ~7 min, consistent with the 423 s figure recorded above

The whole 5-test module took 433.50 s, of which the first start is the dominant term. This is a
THIRD idle-machine observation over the documented 3-5 min bound, on a different day-part and a
different container name from the two above. The documented range at
`docs/UserManual/140_running_with_docker.md:212` ("~3-5 min") is not attainable here.

Note also what the same run proves is NOT broken: `test_second_start_healthy_within_60s` PASSED,
so the `/pip-packages` volume genuinely persists the extras and the second start is fast. The
duration defect is confined to the first start, exactly as this report already states.
