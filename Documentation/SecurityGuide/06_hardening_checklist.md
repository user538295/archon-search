**Purpose**: A concrete pre-production checklist for hardening an `archon-search` deployment.
**Audience**: IT admins and security engineers performing a deployment review.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-05-20

# Hardening Checklist

Run through this checklist before exposing `archon-search` beyond a single developer's loopback. Each item points to the doc and the code that backs it. Items are intentionally non-cute and verifiable.

For background, see [`01_threat_model.md`](./01_threat_model.md). For controls in detail, see the surrounding files in this guide.

## Principles

1. **Verify, do not assume.** Every item below has a verification command or an inspectable file.
2. **Trust nothing about defaults you haven't read.** Defaults are documented; check the version you actually run.
3. **Layered defenses survive single failures.** Loopback bind plus bearer auth plus reverse proxy is the minimum stack for any non-developer deployment.

## Pre-production checklist

### 1. Key and state files are private

```bash
ls -ld ~/.archon-search ~/.archon-search/search ~/.archon-search/search-logs ~/.archon-search/logs
# Expected: drwx------  (mode 0700)
ls -l ~/.archon-search/.search.env ~/.archon-search/keys.json ~/.archon-search/archon-search-jobs.json
# Expected: -rw-------  (mode 0600)
stat -c '%a' ~/.archon-search/.search.env   # Linux
stat -f '%Lp' ~/.archon-search/.search.env  # macOS
# Expected: 600
```

The server creates the data directory and its subdirectories `0700` (and tightens existing ones inside the data directory) and writes the key file, `keys.json`, the job store, telemetry, logs and export/backup archives `0600`. A directory you point a setting at outside the data directory (`[logging].log_file`, `[telemetry].log_dir`, `[backup].output_dir`) keeps its own mode: check it. `log_file` and `log_dir` can point outside it only while `ARCHON_SEARCH_DATA_DIR` is unset; when it is set, both are forced under it. The model cache (`<data dir>/models`) is filled by `huggingface_hub` under the process umask, so `model_store.py::_ensure_snapshot` makes it `0700` with its files `0600` (`_durable_io.restrict_tree_to_owner`) after every download and, on a cache hit, the first time a model loads while the directory is group- or world-accessible, so a volume or data directory created by an earlier version is fixed on the first start. If the key file's mode has drifted, the loader retightens it on the next read (`archon_search/key_manager.py::_parse_key_file`), but verify explicitly during review.

**Backed by**: [`02_authentication_and_keys.md`](./02_authentication_and_keys.md) "File permissions"; `archon_search/_durable_io.py`.

### 2. Keys meet the format rules and the admin key is held tightly

- The default key (`ARCHON_SEARCH_API_KEY` or the key file) must be lowercase hex of at least 32 characters; a weaker one stops the server at startup. TOML `[namespaces]` tokens must be at least 32 printable ASCII characters without whitespace.
- The default key is the **admin** key: it can rotate keys, trigger `/sync`, `/backup/trigger` and `/maintenance/trigger`, and read `/telemetry/*`. Hand out namespace tokens or non-admin managed keys (`archon-search key create --namespace <ns>`) to clients, and keep the default key to operators.

```bash
# A namespace token must be refused on an admin-only route (expect 403)
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $NAMESPACE_KEY" http://127.0.0.1:8765/sync
```

**Backed by**: [`02_authentication_and_keys.md`](./02_authentication_and_keys.md) "Admin scope"; `archon_search/server/authz.py`.

### 3. Server binds to loopback, or the port is firewalled

```bash
grep -E '^(host|port)' ~/.archon-search/archon-search.toml
# Expected: host = "127.0.0.1"  (or no [server] block at all → default)
ss -ltnp | grep :8765   # Linux
lsof -nP -iTCP:8765 -sTCP:LISTEN   # macOS
# Expected: bound to 127.0.0.1 only
```

Outside a container the default is loopback for `serve`, `start` and the service units. The container image binds `0.0.0.0` inside the container; check the published port instead (`docker compose ps`, or `-p 127.0.0.1:8765:8765`; the shipped compose file already publishes loopback only). If the deployment must bind to a non-loopback interface, a host firewall must restrict inbound access to the proxy host's address. There is no per-IP allow-list in `archon-search`.

**Backed by**: [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md); `archon_search/config.py` defaults.

### 4. A reverse proxy terminates TLS for any non-loopback access

`archon-search` does not serve TLS natively (`archon_search/server/app.py::run_server`). For any deployment reachable from another host:

- Confirm the proxy is in front (`curl -kv https://<endpoint>/health` returns the proxy's certificate chain).
- Confirm the proxy passes `Authorization` through unchanged.
- Confirm plaintext HTTP on the public listener is refused or redirected.

**Backed by**: [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md) "Recommended topology".

### 5. CORS origins are explicit, and docs are off

CORS is off by default: the server sends no `Access-Control-*` header. Check that `[server].cors_origins` lists only the browser origins you own (the config loader rejects `*`), and that `[server].docs_enabled` is unset or `false` (with `true`, `/docs`, `/redoc` and `/openapi.json` are served without a token).

```bash
grep -E 'cors_origins|docs_enabled' ~/.archon-search/archon-search.toml
curl -i -H "Origin: https://evil.example" -H "Access-Control-Request-Method: POST" \
     -X OPTIONS https://<your-endpoint>/search
# Expected: no Access-Control-Allow-Origin header (or only an origin you listed, never *).
curl -s -o /dev/null -w '%{http_code}\n' https://<your-endpoint>/openapi.json
# Expected: 401 (404 or 401 for /docs)
```

**Backed by**: [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md) "CORS".

### 6. Path-based ingest is scoped

A client that can send `POST /ingest` (or the MCP `ingest_file` / `ingest_directory`) with a `path` makes the server read that file. Two controls apply:

- **Deny list (always, every caller).** The data directory and the server's own state directories, the key, `keys.json`, `.secrets.env`, config, job store and log files, `/proc`, `/sys`, `/dev`, `/root`, `/etc/passwd`, `/etc/shadow`, credential directories (`.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`, cloud-CLI and browser profile directories, macOS keychains) and credential-named files (`id_*`, `.env`, `.env.*`, `*.pem`, `.npmrc`, `.netrc`, `.pgpass`, `.git-credentials`, `keys.json`, a `*.key` file that starts with a PEM header, ...) are refused with `400 path is unsafe: denied_location`. A symlink or a different letter case does not bypass it, and neither does a hard link to a server file or to a system file: those are matched by device and inode. **Limit:** a credential-named file (`.env`, `id_rsa`, ...) is recognised by its name, so a hard link to it under another name is refused only when a link with a credential name is in the **same directory** as the path (checked for a regular file with more than one link); a hard link to such a file placed in a different directory is not detected, because nothing records where the other name is. The list is a floor, not a guarantee: a secret in an ordinary-looking file is not recognised.
- **`[ingest].allowed_roots`.** When the list is non-empty, a client-supplied path must lie under one of the roots (`outside_allowed_roots` otherwise). When it is **empty** (the default), a **non-admin** key's path ingest is refused (`allowed_roots_required`, answer: send inline `documents` instead), and the server logs one WARNING at startup if non-admin keys exist. Only the admin key, and paths the operator wrote into the config, can ingest outside any root. **Set `allowed_roots` in every deployment that has namespace keys.**
- **Symlink as the `path`.** A client-supplied `path` whose last component is a symlink to a file is refused with `400 path is unsafe: symlink_path`; the deny list and `allowed_roots` are applied to its target first, so a link to a denied location still answers `denied_location`. A symlinked parent directory and a symlink to a directory are accepted. Paths the operator wrote into the config do not go through this check.

```bash
grep -A2 '^\[ingest\]' ~/.archon-search/archon-search.toml
# Expected: allowed_roots = ["/srv/docs", ...]
```

Directory ingest skips hidden directories (denied credential or server-state ones are opened only to count their files, never indexed), `node_modules`, `__pycache__`, `venv` and `site-packages`, and stops at `[ingest].max_discovered_files` (100000); the parser reads only regular files (no symlinks, FIFOs or devices).

**Backed by**: `archon_search/_path_safety.py` (`IngestPathPolicy`); `[ingest]` in `archon-search.toml.example`.

### 7. Resource limits fit the host

- `[server].max_request_body_mb` (default 32): a bigger body gets `413` before authentication.
- `[ingest].max_concurrent_tasks` (default 4): a further `POST /ingest` or `POST /collections/` gets `503` `ingest_busy` with `Retry-After`; jobs are not queued.
- `[ingest].max_file_mb` (default 100), `max_zip_entries` (10000) and `max_zip_uncompressed_mb` (512) bound a single document; `parse_timeout_seconds` (300) plus `parse_timeout_seconds_per_mb` (10) per MiB of the file bound the PDF/image parser, which is killed and replaced on timeout.
- `[graph].max_cross_collection_collections` (default 8) bounds a cross-collection graph request.

There is still no rate limiter: put one at the proxy.

**Backed by**: `archon-search.toml.example`; `archon_search/server/middleware_body_limit.py`, `archon_search/server/_ingest_admission.py`.

### 8. Telemetry is disabled unless needed

Default is off. Confirm explicitly:

```bash
grep -A2 '^\[telemetry\]' ~/.archon-search/archon-search.toml
# Expected: enabled = false  (or no [telemetry] block at all)
ls ~/.archon-search/search-logs/ 2>/dev/null
# Expected after sending a few /search requests: empty or non-existent
```

If telemetry is enabled, also confirm `[telemetry].retention_days` matches your retention policy and that path-derived `doc_id`s in the logs are acceptable for your host (or set `hash_doc_ids = true`). Today's file is never deleted regardless of `retention_days`. `/telemetry/*` needs an admin key.

**Backed by**: [`04_telemetry_privacy.md`](./04_telemetry_privacy.md); `archon_search/config.py`, `archon_search/telemetry/pruner.py`.

### 9. The service runs as a dedicated, restricted user

`archon-search` reads and writes `~/.archon-search/` for the user running it. The OS service installer (`archon_search/cli/install_cmd.py`) registers a launchd/systemd unit; verify the unit runs as the intended user, not root, and that the user has no shell or login on the host beyond what's required. The container image already runs as a non-root user (uid 1000) and works with `--read-only` plus writable `/data`, `/tmp` and (when extras are installed) `/pip-packages`.

```bash
# Linux systemd
systemctl --user status archon-search   # or system unit if installed system-wide
# macOS launchd
launchctl print gui/$(id -u)/com.archon.search
```

Backup access to `~/.archon-search/` should be limited to the same user; otherwise the bearer key and the LanceDB contents are reachable via the backup path.

**Backed by**: [`01_threat_model.md`](./01_threat_model.md) "Out of scope" (hostile same-user processes); `archon_search/cli/install_cmd.py`.

### 10. Backups of `~/.archon-search/` are encrypted

`~/.archon-search/` contains the API key file, LanceDB tables (with chunk text and `source_path` in clear), telemetry logs (if enabled), and the indexing state. Treat the whole directory as sensitive. Export and backup archives (`<data dir>/exports/<namespace>/`, `<[backup].output_dir>/<namespace>/`) hold the documents of a collection in plain text.

- Verify the backup target encrypts at rest.
- Verify backup transport is encrypted (no rsync over plain SSH-less channels).
- Verify backup retention does not outlast the key rotation cadence — see item 11.

**Backed by**: [`01_threat_model.md`](./01_threat_model.md) "Assets".

### 11. Keys are rotated on personnel changes

**D7** adds live key rotation and revocation — no server restart required. Bake rotation into your operational runbook:

- Anyone who once had a managed key token retains it until you revoke it: `archon-search key revoke <id>`.
- To rotate the default key without downtime: `archon-search key rotate` (or `POST /keys/rotate`, admin only). Use `--grace <duration>` to allow in-flight requests to drain before the old key expires.
- Anyone who had access to `.search.env` or the TOML `[namespaces]` block can still use those tokens until you rotate via the legacy procedure (edit file + restart). Legacy TOML tokens cannot be revoked via `key revoke` — remove them from `archon-search.toml` and restart.
- See [`02_authentication_and_keys.md`](./02_authentication_and_keys.md) for both the managed-key and legacy rotation procedures.

**Backed by**: [`02_authentication_and_keys.md`](./02_authentication_and_keys.md); `archon_search/key_manager.py`, `archon_search/server/middleware_auth.py`.

### 12. ACL sources are reviewed where confidentiality matters

Per-chunk ACL fails **closed**: an `_acl` or `<doc>.acl` that is declared but unusable (wrong type, symlink, oversized, not UTF-8, unreadable, no valid namespace name) hides the chunk instead of opening it. A partly valid list keeps its valid names. Collection ownership is enforced on every write path, so a namespace cannot ingest into or delete another namespace's collection. If your collection includes documents whose `_acl` or `.acl` files are critical:

- Watch the ingest result and the application log for `_acl in <path> has invalid …` and `ACL sidecar … chunk denied` warnings — these mean the chunk is hidden until the declaration is fixed and the document reindexed.
- Prefer front-matter `_acl` over the sidecar (front-matter rides through copies).
- Reindex after changing or removing a sidecar; ingest-time ACL is sticky on the LanceDB row.

**Backed by**: [`03_authorization_and_acl.md`](./03_authorization_and_acl.md); `archon_search/acl.py`.

### 13. MCP is behind the same auth

`/mcp` is mounted on the same server with its own `APIKeyMiddleware`, and its tools fail closed: a call with no authenticated request is refused (`unauthenticated`), not run as the default namespace. Nothing to configure; if you front the service with a proxy that serves `/mcp`, forward `Authorization` there too.

**Backed by**: [`01_threat_model.md`](./01_threat_model.md) "Trust boundaries"; `archon_search/server/mcp.py`.

### 14. Operators subscribe to the release feed

Breaking REST/MCP and security-relevant changes are recorded in [`../../BREAKING.md`](../../BREAKING.md). Subscribe via GitHub watch or feed; do not rely on out-of-band notification.

Releases happen by tag push and are visible in the repo's tag/release stream. CalVer (`YY.M.<rev-count>`) means version numbers carry no compatibility signal — `BREAKING.md` is the contract. Run `pip-audit` (or your scanner) against the dependency set you install when you upgrade; the last audit of the locked set (2026-10-25) found no known vulnerabilities, but it is not a CI gate.

**Backed by**: [`../Architecture/510_release_and_environment_strategy.md`](../Architecture/510_release_and_environment_strategy.md); `release.sh`.

## Operational hygiene (post-deployment)

These are not strictly pre-production gates, but a deployment that ignores them will degrade quietly:

- **Watch the server log** for ACL parse warnings and for the auth middleware's `Middleware: resolved namespace … is invalid` error (`archon_search/server/middleware_auth.py`). The latter means a `[namespaces]` entry has a name that fails validation — clients using that key will receive `500`.
- **Watch `acl_filtered`** in search responses on namespaces that should never see filtered hits. A persistent `true` may mean a document is mis-tagged.
- **Watch for `503 ingest_busy`** in client logs: ingest concurrency is at its cap.
- **Periodically reread this guide** when upgrading. The `Last reviewed` and `Next review` headers tell you when each doc was last validated against the code.

## Known gaps not solved by this checklist

These are explicitly out of scope for v1 hardening; recording them so operators are not surprised:

- **No request-correlation IDs across every layer** (`ARCH-3` in the debt register); a request ID header is honoured and threaded into logs and telemetry, but incident triage across modules is harder than it should be.
- **No native rate limiting.** The reverse proxy must provide it.
- **No read-only or per-collection key scope.** A key is admin or namespace-scoped (roadmap item E4).
- **Collection names are global.** One table exists per name, so a taken name is observable across namespaces; no owner or content leaks.
- **Hash-pinned extras install is not implemented.** The container's first-start extras install writes the exact artifacts its own resolve chose (`name @ url`) but verifies no hashes; real protection needs a hash catalog generated at build time for every extras combination.
- **A non-admin ingest of a foreign collection name still takes the collection lock briefly** and, over REST, answers `202` with a failed job rather than a `4xx`.
- ~~**No key expiry / revocation list.** `SEC-1`, roadmap item D7.~~ **Resolved by D7** — `archon-search key create/revoke/rotate`, `POST /keys`, `DELETE /keys/{id}`, `POST /keys/rotate`.
- ~~**Path-derived `doc_id` in telemetry.** `SEC-2`, roadmap item D8.~~ **Resolved by D8** — enable `[telemetry] hash_doc_ids = true` in `archon-search.toml` to HMAC-SHA256 hash `result_doc_ids` before logging. Recommended for any deployment where telemetry logs may be shared or forwarded off-host.
- ~~**No CORS config knob.**~~ **Resolved** — CORS is off by default and `[server].cors_origins` lists allowed origins.

## Related documents

- [`01_threat_model.md`](./01_threat_model.md) — scope.
- [`02_authentication_and_keys.md`](./02_authentication_and_keys.md) — auth and rotation procedure.
- [`03_authorization_and_acl.md`](./03_authorization_and_acl.md) — ACL semantics.
- [`04_telemetry_privacy.md`](./04_telemetry_privacy.md) — telemetry posture.
- [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md) — TLS and CORS.
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — broader architecture.
- [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — `SEC-1`, `SEC-2`, `SEC-3`, `TEL-1`, proposed `CORS-1`.
- [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) — items D7, D8, E4.
- [`../../BREAKING.md`](../../BREAKING.md) — compatibility contract.
