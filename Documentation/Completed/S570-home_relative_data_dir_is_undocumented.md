## Bug: changing `HOME` in the container does **not** relocate the data directory (undocumented; `ARCHON_SEARCH_DATA_DIR` is the documented root)

**ID**: S570-home_relative_data_dir_is_undocumented
**Scenario**: S570
**Severity**: medium
**Version**: archon-search, version 26.8.1931

### What happened
AssertionError: the set of docs mentioning HOME changed: ['docs/SecurityGuide/02_authentication_and_keys.md', 'docs/UserManual/140_running_with_docker.md', 'docs/UserManual/30_configuration.md'] (was ['docs/UserManual/140_running_with_docker.md', 'docs/UserManual/30_configuration.md']). Re-check whether HOME is now documented as relocating the data directory and re-implement S570 against it.
assert {'docs/Securi...iguration.md'} == {'docs/UserMa...iguration.md'}

Extra items in the left set:
'docs/SecurityGuide/02_authentication_and_keys.md'
Use -v to get more diff

### What should happen
- **The row's premise is UNDOCUMENTED.** No doc states that changing `HOME` moves the data directory. The docs anchor the runtime tree on `ARCHON_SEARCH_DATA_DIR` (`140:151`, `30_configuration.md:278`), which the image bakes to `/data`; `HOME`'s documented effect (`140:153`) is scoped to "pip operations", and the only other `HOME` mention (`30_configuration.md:20`) says path resolution explicitly does **not** go through `$HOME`. Grep confirms `HOME` appears in exactly two doc files.
- **Documented, exercisable assertion:** with `HOME` overridden and `ARCHON_SEARCH_DATA_DIR` untouched, the container becomes healthy and the runtime tree stays under `/data` — step 3 lists `.search.env` and `search` (both named at `140:151` as `ARCHON_SEARCH_DATA_DIR`-rooted paths). This is what stands in for the row's claim: the data dir follows `ARCHON_SEARCH_DATA_DIR`, not `HOME`.
- **Documented, exercisable assertion (negative):** step 4 shows no `.search.env`, no `search/`, no `keys.json` and no `logs/` under the new `HOME` — the runtime tree did not follow it. (Unrelated third-party caches such as `.cache/huggingface` may appear there; only the four `140:151`-named runtime paths are asserted.)
- **Doc-gap reopening gate:** `HOME` appears today in exactly `UserManual/140_running_with_docker.md` and `UserManual/30_configuration.md`, and `140:153`'s row still scopes its effect to pip operations. If either changes, the paired test flips **red** so S570 is re-implemented against a then-documented `HOME`-relative data dir instead of this `ARCHON_SEARCH_DATA_DIR` proxy.
- No bug is filed: a missing documentation statement is not an application defect.

### Steps to reproduce
1. `docker run -d --name archon-s570-home -e ARCHON_SEARCH_API_KEY=<hex> -e ARCHON_EXTRAS="" -e HOME=/tmp/s570-home -e FASTEMBED_CACHE_PATH=/model-cache -v archon-search-model-cache:/model-cache -v archon-s570-data:/data -p 18984:8765 ghcr.io/user538295/archon-search:latest`
2. Poll `GET http://127.0.0.1:18984/health` until `200`.
3. `docker exec archon-s570-home sh -c 'ls -a /data'`
4. `docker exec archon-s570-home sh -c 'ls -a /tmp/s570-home'`
5. Grep `./docs/` for `HOME`.

### Evidence
```
E   AssertionError: the set of docs mentioning HOME changed: ['docs/SecurityGuide/02_authentication_and_keys.md', 'docs/UserManual/140_running_with_docker.md', 'docs/UserManual/30_configuration.md'] (was ['docs/UserManual/140_running_with_docker.md', 'docs/UserManual/30_configuration.md']). Re-check whether HOME is now documented as relocating the data directory and re-implement S570 against it.
E   assert {'docs/Securi...iguration.md'} == {'docs/UserMa...iguration.md'}
E     
E     Extra items in the left set:
E     'docs/SecurityGuide/02_authentication_and_keys.md'
E     Use -v to get more diff
```
