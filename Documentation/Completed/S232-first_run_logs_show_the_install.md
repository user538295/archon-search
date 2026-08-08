## Bug: `/pip-packages` named volume persists extras across container recreate

**ID**: S232-first_run_logs_show_the_install
**Scenario**: S232
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: first-start logs lack the install marker 'starting pip install' — the marker is wrong, so test_no_pip_install_on_second_run proves nothing; logs:
Error response from daemon: {"message":"No such container: archon-recreate"}

assert 'starting pip install' in 'error response from daemon: {"message":"no such container: archon-recreate"}

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
E   AssertionError: first-start logs lack the install marker 'starting pip install' — the marker is wrong, so test_no_pip_install_on_second_run proves nothing; logs:
E     Error response from daemon: {"message":"No such container: archon-recreate"}
E     
E   assert 'starting pip install' in 'error response from daemon: {"message":"no such container: archon-recreate"}
'
E    +  where 'error response from daemon: {"message":"no such container: archon-recreate"}
' = <built-in method lower of str object at 0x10a42a9b0>()
E    +    where <built-in method lower of str object at 0x10a42a9b0> = 'Error response from daemon: {"message":"No such container: archon-recreate"}
'.lower
```
