## Bug: Docker smoke test [standalone]

**ID**: S40-ingest_exits_zero
**Scenario**: S40
**Severity**: medium
**Version**: archon-search, version 26.8.1800

### What happened
AssertionError: collection add inside container exited 1:

Error: server returned 500: Internal Server Error

assert 1 == 0

### What should happen
- Step 1: `docker pull` exits 0; digest is printed (confirms image is reachable and records what was tested).
- Step 3: Loop breaks before 60 iterations; startup time noted.
- Step 4: `/health` → `{"status":"running","version":"<semver>"}`. `/ready` → HTTP 200. `/status` (auth) → HTTP 200.
- Step 5: `collection add` exits 0 with a success message; no errors.
- Step 6: HTTP 200 response body contains at least one result with `doc.md` in the source path.
- Step 7: Both commands exit 0.

### Steps to reproduce
```bash
# 1. Pull image explicitly to confirm registry access and record the digest
docker pull ghcr.io/user538295/archon-search:latest
docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/user538295/archon-search:latest

# 2. Generate a static API key and start the container
export ARCHON_SEARCH_API_KEY=$(openssl rand -hex 32)
docker volume create archon-smoke-data
docker run -d \
  --name archon-smoke \
  -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
  -v archon-smoke-data:/data \
  -p 19765:8765 \
  ghcr.io/user538295/archon-search:latest

# 3. Wait for /health (up to 5 min — first-start extras install)
for i in $(seq 1 60); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:19765/health)
  [ "$STATUS" = "200" ] && echo "health OK after ${i}x5s" && break
  sleep 5
done

# 4. Verify health, ready, and authenticated status
curl -s http://127.0.0.1:19765/health
curl -s -o /dev/null -w "ready: %{http_code}\n" http://127.0.0.1:19765/ready
curl -s -o /dev/null -w "status (auth): %{http_code}\n" \
  http://127.0.0.1:19765/status \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"

# 5. Ingest one document
docker exec archon-smoke sh -c 'mkdir -p /tmp/smoke && printf "# Smoke\nHello world.\n" > /tmp/smoke/doc.md'
docker exec archon-smoke archon-search collection add /tmp/smoke \
  --wait \
  --api-url http://127.0.0.1:8765 \
  --api-key $ARCHON_SEARCH_API_KEY

# 6. Search from the host
curl -s -X POST http://127.0.0.1:19765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"smoke","query":"hello"}'

# 7. Teardown
docker rm -f archon-smoke
docker volume rm archon-smoke-data
```

### Evidence
```
E   AssertionError: collection add inside container exited 1:
E     
E     Error: server returned 500: Internal Server Error
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=['docker', 'exec', 'archon-smoke', 'archon-search', 'collection', 'add', '/tmp/smoke', '--wait',...3d09a04c17212b5f6401ad550841a'], returncode=1, stdout='', stderr='Error: server returned 500: Internal Server Error
').returncode
```

---

### Analysis — Product defect, resolved (feature-level)

**Verdict:** confirmed product defect — now fixed.

Adding a folder as a collection failed inside the container because the server could not write its configuration file on first use, which returned a server error before anything was ingested; every downstream search and "data not persisted" failure followed from that single error. The configuration write now succeeds on first use, so the collection is created and search returns the expected results across restarts. Covered by a new automated regression test.
