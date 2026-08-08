## Bug: Docs: ARCHON_SEARCH_CONFIG is not required for collection add in the container, and the default write stays inside /data

**ID**: S569-config_env_not_required_for_collection_add
**Scenario**: S569
**Severity**: medium
**Version**: ghcr.io/user538295/archon-search:latest — 26.8.1845+docker

### What happened
In a container started with no ARCHON_SEARCH_CONFIG, `archon-search collection add /data/src --wait --api-url http://127.0.0.1:8765 --api-key <key>` exited 0 and printed "Collection 'src' ingested successfully.", and the server wrote the collection list to /data/.archon-search/archon-search.toml (contents: [collections] collections = ["/data/src"]) — INSIDE the volume mounted at /data. UserManual/140_running_with_docker.md:151 states the variable is "Required if you want archon-search collection add to work inside the container" and that "without ARCHON_SEARCH_CONFIG=/data/archon-search.toml the server will try to write outside the mounted volume"; :287 repeats it ("Operators who need dynamic collection management inside the container must mount a config file under /data and point ARCHON_SEARCH_CONFIG at it"). Neither holds. The server does log the documented warning (30_configuration.md:282) claiming 'collection add/remove commands will fail' — they do not fail.

### What should happen
Either the documented behavior holds (collection add fails, or its TOML write lands outside the mounted volume, without ARCHON_SEARCH_CONFIG), or UserManual/140_running_with_docker.md:151 and :287 are corrected. The docs' own lines predict the observed result: the config path defaults to ~/.archon-search/archon-search.toml (30_configuration.md:22) and the image bakes HOME=/data (140:153), so the default resolves to /data/.archon-search/archon-search.toml — on the volume. The startup warning's wording ('will fail') needs the same correction.

### Steps to reproduce
1. docker volume create archon-s569-noconfig-data
2. docker run -d --name archon-s569-noconfig -e ARCHON_SEARCH_API_KEY=<hex> -e ARCHON_EXTRAS="" -e FASTEMBED_CACHE_PATH=/model-cache -v archon-search-model-cache:/model-cache -v archon-s569-noconfig-data:/data -p 18982:8765 ghcr.io/user538295/archon-search:latest   (no ARCHON_SEARCH_CONFIG)
3. Poll GET http://127.0.0.1:18982/health until 200.
4. docker exec archon-s569-noconfig sh -c 'mkdir -p /data/src && printf "# a
The release runs from the main branch.
" > /data/src/a.md'
5. docker exec archon-s569-noconfig archon-search collection add /data/src --wait --api-url http://127.0.0.1:8765 --api-key <hex>
6. docker exec archon-s569-noconfig sh -c 'find /data -name archon-search.toml'

### Evidence
```
step 5 stdout: Collection 'src' ingested successfully.  (exit 0)
step 6 stdout: /data/.archon-search/archon-search.toml
cat /data/.archon-search/archon-search.toml:
[collections]
collections = ["/data/src"]
pinned_collections = []
docker logs (startup): ARCHON_SEARCH_DATA_DIR is set but ARCHON_SEARCH_CONFIG is not — 'collection add/remove' commands will fail; set ARCHON_SEARCH_CONFIG=/data/archon-search.toml to enable collection management inside the container.
docker exec ... echo $HOME -> /data
```
