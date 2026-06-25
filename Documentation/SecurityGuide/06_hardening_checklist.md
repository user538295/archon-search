**Purpose**: A concrete pre-production checklist for hardening an `archon-search` deployment.
**Audience**: IT admins and security engineers performing a deployment review.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Hardening Checklist

Run through this checklist before exposing `archon-search` beyond a single developer's loopback. Each item points to the doc and the code that backs it. Items are intentionally non-cute and verifiable.

For background, see [`01_threat_model.md`](./01_threat_model.md). For controls in detail, see the surrounding files in this guide.

## Principles

1. **Verify, do not assume.** Every item below has a verification command or an inspectable file.
2. **Trust nothing about defaults you haven't read.** Defaults are documented; check the version you actually run.
3. **Layered defenses survive single failures.** Loopback bind plus bearer auth plus reverse proxy is the minimum stack for any non-developer deployment.

## Pre-production checklist

### 1. Key file permissions are `0600`

```bash
ls -l ~/.archon-search/.search.env
# Expected: -rw-------  (mode 0600)
stat -c '%a' ~/.archon-search/.search.env   # Linux
stat -f '%Lp' ~/.archon-search/.search.env  # macOS
# Expected: 600
```

If the mode has drifted, the loader will retighten it on the next read (`archon_search/key_manager.py:54–59`), but verify explicitly during review. Also confirm the parent directory `~/.archon-search/` is not group/world-readable.

**Backed by**: [`02_authentication_and_keys.md`](./02_authentication_and_keys.md) "File permissions"; `archon_search/key_manager.py`.

### 2. Server binds to loopback, or the port is firewalled

```bash
grep -E '^(host|port)' ~/.archon-search/archon-search.toml
# Expected: host = "127.0.0.1"  (or no [server] block at all → default)
ss -ltnp | grep :8765   # Linux
lsof -nP -iTCP:8765 -sTCP:LISTEN   # macOS
# Expected: bound to 127.0.0.1 only
```

If the deployment must bind to a non-loopback interface, a host firewall must restrict inbound access to the proxy host's address. There is no per-IP allow-list in `archon-search`.

**Backed by**: [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md); `archon_search/config.py` defaults.

### 3. A reverse proxy terminates TLS for any non-loopback access

`archon-search` does not serve TLS natively (`archon_search/server/app.py::run_server`). For any deployment reachable from another host:

- Confirm the proxy is in front (`curl -kv https://<endpoint>/health` returns the proxy's certificate chain).
- Confirm the proxy passes `Authorization` through unchanged.
- Confirm plaintext HTTP on the public listener is refused or redirected.

**Backed by**: [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md) "Recommended topology".

### 4. CORS is hardened at the proxy

The application sets `Access-Control-Allow-Origin: *` (`archon_search/server/app.py:122`). On any non-loopback bind, the proxy **must** override this with a tight allow-list (or drop the headers entirely if you have no browser clients).

```bash
curl -i -H "Origin: https://evil.example" \
        -X OPTIONS https://<your-endpoint>/search
# Expected at the proxy: no wildcard origin in the response, or a 403.
```

**Backed by**: [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md) "CORS — `CORS-1` risk".

### 5. Telemetry is disabled unless needed

Default is off. Confirm explicitly:

```bash
grep -A2 '^\[telemetry\]' ~/.archon-search/archon-search.toml
# Expected: enabled = false  (or no [telemetry] block at all)
ls ~/.archon-search/search-logs/ 2>/dev/null
# Expected after sending a few /search requests: empty or non-existent
```

If telemetry is enabled, also confirm `[telemetry].retention_days` matches your retention policy and that path-derived `doc_id`s in the logs are acceptable for your host. Today's file is never deleted regardless of `retention_days`.

**Backed by**: [`04_telemetry_privacy.md`](./04_telemetry_privacy.md); `archon_search/config.py`, `archon_search/telemetry/pruner.py`.

### 6. The service runs as a dedicated, restricted user

`archon-search` reads and writes `~/.archon-search/` for the user running it. The OS service installer (`archon_search/cli/install_cmd.py`) registers a launchd/systemd unit; verify the unit runs as the intended user, not root, and that the user has no shell or login on the host beyond what's required.

```bash
# Linux systemd
systemctl --user status archon-search   # or system unit if installed system-wide
# macOS launchd
launchctl print gui/$(id -u)/com.archon.search
```

Backup access to `~/.archon-search/` should be limited to the same user; otherwise the bearer key and the LanceDB contents are reachable via the backup path.

**Backed by**: [`01_threat_model.md`](./01_threat_model.md) "Out of scope" (hostile same-user processes); `archon_search/cli/install_cmd.py`.

### 7. Backups of `~/.archon-search/` are encrypted

`~/.archon-search/` contains the API key file, LanceDB tables (with chunk text and `source_path` in clear), telemetry logs (if enabled), and the indexing state. Treat the whole directory as sensitive.

- Verify the backup target encrypts at rest.
- Verify backup transport is encrypted (no rsync over plain SSH-less channels).
- Verify backup retention does not outlast the key rotation cadence — see item 8.

**Backed by**: [`01_threat_model.md`](./01_threat_model.md) "Assets".

### 8. Keys are rotated on personnel changes

**D7** adds live key rotation and revocation — no server restart required. Bake rotation into your operational runbook:

- Anyone who once had a managed key token retains it until you revoke it: `archon-search key revoke <id>`.
- To rotate the default key without downtime: `archon-search key rotate` (or `POST /keys/rotate`). Use `--grace <duration>` to allow in-flight requests to drain before the old key expires.
- Anyone who had access to `.search.env` or the TOML `[namespaces]` block can still use those tokens until you rotate via the legacy procedure (edit file + restart). Legacy TOML tokens cannot be revoked via `key revoke` — remove them from `archon-search.toml` and restart.
- See [`02_authentication_and_keys.md`](./02_authentication_and_keys.md) for both the managed-key and legacy rotation procedures.

**Backed by**: [`02_authentication_and_keys.md`](./02_authentication_and_keys.md); `archon_search/key_manager.py`, `archon_search/server/middleware_auth.py`.

### 9. ACL sources are reviewed where confidentiality matters

Per-chunk ACL fails open on parse errors. If your collection includes documents whose `_acl` or `<doc>.acl` files are critical:

- Watch the application log for `_acl in <path> has invalid …` warnings — these mean the chunk was indexed as open.
- Prefer front-matter `_acl` over the sidecar (front-matter rides through copies).
- Reindex after changing or removing a sidecar; ingest-time ACL is sticky on the LanceDB row.

**Backed by**: [`03_authorization_and_acl.md`](./03_authorization_and_acl.md); `archon_search/acl.py`.

### 10. Operators subscribe to the release feed

Breaking REST/MCP and security-relevant changes are recorded in [`../../BREAKING.md`](../../BREAKING.md). Subscribe via GitHub watch or feed; do not rely on out-of-band notification.

Releases happen by tag push and are visible in the repo's tag/release stream. CalVer (`YY.M.<rev-count>`) means version numbers carry no compatibility signal — `BREAKING.md` is the contract.

**Backed by**: [`../Architecture/510_release_and_environment_strategy.md`](../Architecture/510_release_and_environment_strategy.md); `release.sh`.

## Operational hygiene (post-deployment)

These are not strictly pre-production gates, but a deployment that ignores them will degrade quietly:

- **Watch the server log** for ACL parse warnings and for the auth middleware's `Middleware: resolved namespace … is invalid` error (`archon_search/server/middleware_auth.py:58`). The latter means a `[namespaces]` entry has a name that fails validation — clients using that key will receive `500`.
- **Watch `acl_filtered`** in search responses on namespaces that should never see filtered hits. A persistent `true` may mean a document is mis-tagged.
- **Periodically reread this guide** when upgrading. The `Last reviewed` and `Next review` headers tell you when each doc was last validated against the code.

## Known gaps not solved by this checklist

These are explicitly out of scope for v1 hardening; recording them so operators are not surprised:

- **No request-correlation IDs** across middleware → pipeline → telemetry (`ARCH-3` in the debt register). Incident triage across modules is harder than it should be.
- **No native rate limiting.** The reverse proxy must provide it.
- **No CORS config knob.** Hardcoded wildcard in `app.py:122`; reverse-proxy override only.
- ~~**No key expiry / revocation list.** `SEC-1`, roadmap item D7.~~ **Resolved by D7** — `archon-search key create/revoke/rotate`, `POST /keys`, `DELETE /keys/{id}`, `POST /keys/rotate`.
- ~~**Path-derived `doc_id` in telemetry.** `SEC-2`, roadmap item D8.~~ **Resolved by D8** — enable `[telemetry] hash_doc_ids = true` in `archon-search.toml` to HMAC-SHA256 hash `result_doc_ids` before logging. Recommended for any deployment where telemetry logs may be shared or forwarded off-host.

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
