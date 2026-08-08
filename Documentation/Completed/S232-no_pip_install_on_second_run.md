## Bug: `/pip-packages` named volume persists extras across container recreate

**ID**: S232-no_pip_install_on_second_run
**Scenario**: S232
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
Collecting packaging>=24.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
Downloading packaging-26.3-py3-none-any.whl.metadata (3.5 kB)
Collecting pyperclip>=1.9.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
Downloading pyperclip-1.11.0-py3-none-any.whl.metadata (2.4 kB)
Collecting python-multipart>=0.0.26 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
Downloading python_multipart-0.0.32-py3-none-any.whl.metadata (2.1 kB)
Collecting pyyaml<7.0,>=6.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
Downloading pyyaml-6.0.3-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl.metadata (2.4 kB)
Collecting uncalled-for>=0.2.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
Downloading uncalled_for-0.3.2-py3-none-any.whl.metadata (2.9 kB)
Collecting watchfiles>=1.0.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
Downloading watchfiles-1.2.0-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl.metadata (4.9 kB)
Collecting pybind11>=2.2 (from fasttext-wheel>=0.9.2->archon-search==0.0.0+local)
Downloading pybind11-3.0.4-py3-none-any.whl.metadata (10 kB)
Collecting setuptools>=0.7.0 (from fasttext-wheel>=0.9.2->archon-search==0.0.0+local)
Downloading setuptools-83.0.0-py3-none-any.whl.metadata (6.6 kB)
Collecting httpcore==1.* (from httpx>=0.25->archon-search==0.0.0+local)
Downloading httpcore-1.0.9-py3-none-any.whl.metadata (21 kB)
Collecting idna (from httpx>=0.25->archon-search==0.0.0+local)
Downloading idna-3.18-py3-none-any.whl.metadata (6.1 kB)
Collecting h11>=0.16 (from httpcore==1.*->httpx>=0.25->archon-search==0.0.0+local)
Downloading h11-0.16.0-py3-none-any.whl.metadata (8.3 kB)
Collecting texttable>=1.6.2 (from igraph>=0.11->archon-search==0.0.0+local)
Downloading texttable-1.7.0-py2.py3-none-any.whl.metadata (9.8 kB)
Collecting deprecation>=2.1.0 (from lancedb>=0.30.0->archon-search==0.0.0+local)
Downloading deprecation-2.1.0-py2.py3-none-any.whl.metadata (4.6 kB)
Collecting pyarrow>=16 (from lancedb>=0.30.0->archon-search==0.0.0+local)
Downloading pyarrow-25.0.0-cp312-cp312-manylinux_2_28_aarch64.whl.metadata (3.0 kB)
Collecting lance-namespace>=0.3.2 (from lancedb>=0.30.0->archon-search==0.0.0+local)
Downloading lance_namespace-0.9.0-py3-none-any.whl.metadata (1.6 kB)
Collecting charset-normalizer (from markitdown<0.2,>=0.1.6->markitdown[docx,outlook,pptx,xls,xlsx]<0.2,>=0.1.6->archon-search==0.0.0+local)
Downloading charset_normalizer-3.4.9-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl.metadata (41 kB)
Collecting magika~=0.6.1 (from markitdown<0.2,>=0.1.6->markitdown[docx,outlook,pptx,xls,xlsx]<0.2,>=0.1.6->archon-search==0.0.0+local)
Downloading magika-0.6.3-py3-none-any.whl.metadata (10 kB)
Collecting markdownify (from markitdown<0.2,>=0.1.6->markitdown[docx,outlook,pptx,xls,xlsx]<0.2,>=0.1.6->archon-search==0.0.0+local)
Downloading markdownify-1.2.3-py3-none-any.whl.metadata (9.9 kB)

assert 'starting pip install' not in '[entrypoint...a (9.9 kb)

'starting pip install' is contained here:
hanged) — starting pip install …
[entrypoint] 2026-08-06 20:13:53 target: /pip-packages  extras: [graph,code,multilingual]
[entrypoint] 2026-08-06 20:13:53 (first start: network-bound — this can take several minutes)
looking in indexes: https://pypi.org/simple, https://download.pytorch.org/whl/cpu
processing /app...

...Full output truncated (222 lines hidden), use '-vv' to show

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
whl.metadata (10 kB)
E     Collecting packaging>=24.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
E       Downloading packaging-26.3-py3-none-any.whl.metadata (3.5 kB)
E     Collecting pyperclip>=1.9.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
E       Downloading pyperclip-1.11.0-py3-none-any.whl.metadata (2.4 kB)
E     Collecting python-multipart>=0.0.26 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
E       Downloading python_multipart-0.0.32-py3-none-any.whl.metadata (2.1 kB)
E     Collecting pyyaml<7.0,>=6.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
E       Downloading pyyaml-6.0.3-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl.metadata (2.4 kB)
E     Collecting uncalled-for>=0.2.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
E       Downloading uncalled_for-0.3.2-py3-none-any.whl.metadata (2.9 kB)
E     Collecting watchfiles>=1.0.0 (from fastmcp-slim[client,server]==3.4.6->fastmcp<4,>=3.4->archon-search==0.0.0+local)
E       Downloading watchfiles-1.2.0-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl.metadata (4.9 kB)
E     Collecting pybind11>=2.2 (from fasttext-wheel>=0.9.2->archon-search==0.0.0+local)
E       Downloading pybind11-3.0.4-py3-none-any.whl.metadata (10 kB)
E     Collecting setuptools>=0.7.0 (from fasttext-wheel>=0.9.2->archon-search==0.0.0+local)
E       Downloading setuptools-83.0.0-py3-none-any.whl.metadata (6.6 kB)
E     Collecting httpcore==1.* (from httpx>=0.25->archon-search==0.0.0+local)
E       Downloading httpcore-1.0.9-py3-none-any.whl.metadata (21 kB)
E     Collecting idna (from httpx>=0.25->archon-search==0.0.0+local)
E       Downloading idna-3.18-py3-none-any.whl.metadata (6.1 kB)
E     Collecting h11>=0.16 (from httpcore==1.*->httpx>=0.25->archon-search==0.0.0+local)
E       Downloading h11-0.16.0-py3-none-any.whl.metadata (8.3 kB)
E     Collecting texttable>=1.6.2 (from igraph>=0.11->archon-search==0.0.0+local)
E       Downloading texttable-1.7.0-py2.py3-none-any.whl.metadata (9.8 kB)
E     Collecting deprecation>=2.1.0 (from lancedb>=0.30.0->archon-search==0.0.0+local)
E       Downloading deprecation-2.1.0-py2.py3-none-any.whl.metadata (4.6 kB)
E     Collecting pyarrow>=16 (from lancedb>=0.30.0->archon-search==0.0.0+local)
E       Downloading pyarrow-25.0.0-cp312-cp312-manylinux_2_28_aarch64.whl.metadata (3.0 kB)
E     Collecting lance-namespace>=0.3.2 (from lancedb>=0.30.0->archon-search==0.0.0+local)
E       Downloading lance_namespace-0.9.0-py3-none-any.whl.metadata (1.6 kB)
E     Collecting charset-normalizer (from markitdown<0.2,>=0.1.6->markitdown[docx,outlook,pptx,xls,xlsx]<0.2,>=0.1.6->archon-search==0.0.0+local)
E       Downloading charset_normalizer-3.4.9-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl.metadata (41 kB)
E     Collecting magika~=0.6.1 (from markitdown<0.2,>=0.1.6->markitdown[docx,outlook,pptx,xls,xlsx]<0.2,>=0.1.6->archon-search==0.0.0+local)
E       Downloading magika-0.6.3-py3-none-any.whl.metadata (10 kB)
E     Collecting markdownify (from markitdown<0.2,>=0.1.6->markitdown[docx,outlook,pptx,xls,xlsx]<0.2,>=0.1.6->archon-search==0.0.0+local)
E       Downloading markdownify-1.2.3-py3-none-any.whl.metadata (9.9 kB)
E     
E   assert 'starting pip install' not in '[entrypoint...a (9.9 kb)
'
E     
E     'starting pip install' is contained here:
E       hanged) — starting pip install …
E       [entrypoint] 2026-08-06 20:13:53 target: /pip-packages  extras: [graph,code,multilingual]
E       [entrypoint] 2026-08-06 20:13:53 (first start: network-bound — this can take several minutes)
E       looking in indexes: https://pypi.org/simple, https://download.pytorch.org/whl/cpu
E       processing /app...
E     
E     ...Full output truncated (222 lines hidden), use '-vv' to show
```
