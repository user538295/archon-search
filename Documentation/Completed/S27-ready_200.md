## Bug: Ephemeral quickstart [standalone]

**ID**: S27-ready_200
**Scenario**: S27
**Severity**: medium
**Version**: archon-search, version 26.10.2181

### What happened
AssertionError: /ready returned 503
assert 503 == 200

### What should happen
- Step 3: `{"status":"running","version":"<version>","mcp":…}` (exact `mcp` value may vary).
- Step 4: HTTP 200 once storage is connected (may be 503 briefly during startup).
- Container exits cleanly on stop.

### Steps to reproduce
1. `docker run --rm -d --name archon-ephemeral -p 18765:8765 ghcr.io/user538295/archon-search:latest`
2. Poll health until 200 (up to 5 min):
   ```bash
   for i in $(seq 1 60); do
     STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18765/health)
     [ "$STATUS" = "200" ] && break
     sleep 5
   done
   ```
3. `curl -s http://127.0.0.1:18765/health`
4. `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18765/ready`
5. `docker stop archon-ephemeral`

### Evidence
```
E   AssertionError: /ready returned 503
E   assert 503 == 200
```
