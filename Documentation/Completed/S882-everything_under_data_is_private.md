## Bug: Docker: everything under `/data` is created `0700`/`0600`

**ID**: S882-everything_under_data_is_private
**Scenario**: S882
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S882-everything_under_data_is_private.md — 52 entries under /data are not 0700 (dirs) / 0600 (files), but OperatorGuide/10_deployment_topologies.md:171 says everything under /data is created 0700/0600; first entries: [('755', '/data/models'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5/snapshots'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5/snapshots/5c38ec7c405ec4b44b94cc5a9bb96e735b38267a'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5/blobs'), ('644', '/data/models/models--BAAI--bge-small-en-v1.5/blobs/688882a79f44442ddc1f60d70334a7ff5df0fb47'), ('644', '/data/models/models--BAAI--bge-small-en-v1.5/blobs/3992bf890728a92476c700e6e657fd696eb94160'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5/trees'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/snapshots'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/snapshots/233902d25c440f23af6f7d6e94d2946bac0bee0a'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/blobs'), ('644', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/blobs/688882a79f44442ddc1f60d70334a7ff5df0fb47'), ('644', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/blobs/88bc4f74b33a2073abc9a66cb532b889448ac3ed'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/trees')]

### What should happen
- Every directory under (and including) `/data` has mode `700` and every regular file mode `600` — no group or other permission bits anywhere in the tree (`10:171`). In particular `/data/.search.env` and `/data/keys.json` are `600` (`140:206-207`).

### Steps to reproduce
1. `docker run -d --name archon-s882 -e ARCHON_SEARCH_API_KEY=<key> -e ARCHON_EXTRAS= -v archon-s882-data:/data -p 18883:8765 ghcr.io/user538295/archon-search:latest`; poll `GET /ready` until `200`.
2. `docker exec archon-s882 archon-search key create --namespace s882 --api-url http://127.0.0.1:8765 --api-key <key>` (`keys.json` is "created on first key create/rotate", `140:207`)
3. `docker exec archon-s882 find /data -printf '%m %y %p\n'`

### Evidence
```
E   Failed: bug: bugs/S882-everything_under_data_is_private.md — 52 entries under /data are not 0700 (dirs) / 0600 (files), but OperatorGuide/10_deployment_topologies.md:171 says everything under /data is created 0700/0600; first entries: [('755', '/data/models'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5/snapshots'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5/snapshots/5c38ec7c405ec4b44b94cc5a9bb96e735b38267a'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5/blobs'), ('644', '/data/models/models--BAAI--bge-small-en-v1.5/blobs/688882a79f44442ddc1f60d70334a7ff5df0fb47'), ('644', '/data/models/models--BAAI--bge-small-en-v1.5/blobs/3992bf890728a92476c700e6e657fd696eb94160'), ('755', '/data/models/models--BAAI--bge-small-en-v1.5/trees'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/snapshots'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/snapshots/233902d25c440f23af6f7d6e94d2946bac0bee0a'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/blobs'), ('644', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/blobs/688882a79f44442ddc1f60d70334a7ff5df0fb47'), ('644', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/blobs/88bc4f74b33a2073abc9a66cb532b889448ac3ed'), ('755', '/data/models/models--cross-encoder--ms-marco-MiniLM-L-6-v2/trees')]
```
