## Bug: Default multilingual extras install fails on a pip hash mismatch (entrypoint exits 1)

**ID**: S232-first_start_becomes_healthy
**Scenario**: S232
**Severity**: high
**Version**: ghcr.io/user538295/archon-search:latest @ sha256:43e4e63d

### What happened
On a fresh /pip-packages volume with the default ARCHON_EXTRAS=graph,code,multilingual, the entrypoint's 'pip install --no-cache-dir --target /pip-packages .[graph,code,multilingual]' aborts with 'THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE'. The container exits 1 (not OOM), never reaches /health 200, and no .extras-installed stamp is written. Reproduced on native linux/arm64 (image digest sha256:43e4e63d70a80dda9e0027259bf9fa8b36e6b546664cbae26ce705dd3e78fe34). The graph,code subset (multilingual removed) installs cleanly to 6.2 GB and becomes healthy in ~375 s, isolating the failing package to the multilingual set.

### What should happen
- Step 2: Loop exits within 180 iterations; first start takes noticeably longer due to pip install.
- Step 3: File exists; contents equal the extras string that was installed (e.g., `graph,code,multilingual`).
- Step 5: Loop exits within 12 iterations (instant start — stamp matched, no re-install).
- Step 6: The second run logs the *skip* message (`skipping pip install`), not the install message
  (`starting pip install`). Both lines contain the bare substring `pip install`, so the presence of
  that phrase alone proves nothing either way — only the `starting`/`skipping` prefix distinguishes
  a re-install from a stamp hit.
- Step 7: Both commands exit 0.

### Steps to reproduce
1. ```bash
   export ARCHON_SEARCH_API_KEY=$(openssl rand -hex 32)
   docker volume create archon-recreate-data
   docker volume create archon-recreate-pkgs
   docker run -d \
     --name archon-recreate \
     -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
     -v archon-recreate-data:/data \
     -v archon-recreate-pkgs:/pip-packages \
     -p 18771:8765 \
     ghcr.io/user538295/archon-search:latest
   ```
2. Poll `/health` until 200 (up to 15 min — first-start extras install, measured at 423 s):
   ```bash
   for i in $(seq 1 180); do
     STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18771/health)
     [ "$STATUS" = "200" ] && echo "first start ready after ${i}x5s" && break
     sleep 5
   done
   ```
3. `docker exec archon-recreate cat /pip-packages/.extras-installed`
4. Recreate the container (full remove + run with the same volumes):
   ```bash
   docker rm -f archon-recreate
   docker run -d \
     --name archon-recreate \
     -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
     -v archon-recreate-data:/data \
     -v archon-recreate-pkgs:/pip-packages \
     -p 18771:8765 \
     ghcr.io/user538295/archon-search:latest
   ```
5. Poll `/health` until 200 — this time within 60 s (stamp matches, no re-install):
   ```bash
   for i in $(seq 1 12); do
     STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18771/health)
     [ "$STATUS" = "200" ] && echo "second start ready after ${i}x5s" && break
     sleep 5
   done
   ```
6. `docker logs archon-recreate 2>&1 | grep -i "pip install"`
7. ```bash
   docker rm -f archon-recreate
   docker volume rm archon-recreate-data archon-recreate-pkgs
   ```

### Evidence
```
docker logs archon-recreate (tail):
Downloading dill-0.4.1-py3-none-any.whl (120 kB)
ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE. If you have updated the package versions, please update the hashes. Otherwise, examine the package contents carefully; someone may have tampered with them.
    unknown package:
        Expected sha256 572df8be8ffb4599c88cbd6a0726f1f854f4da65d2e3c09f0e2c2283333cd6d4
             Got        f4c3c44092e9a3fdc7aa760c8562a305a11973e4ccf4e46d414d38744404b74f
[entrypoint] 2026-08-02 05:21:38 ERROR: entrypoint aborted (exit code 1)
container state: exited oom=false exit=1
```
