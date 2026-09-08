#!/bin/sh
# Installs optional extras into the /pip-packages named volume on first start
# (or when ARCHON_EXTRAS changes), then execs CMD.
set -e

log() { echo "[entrypoint] $(date '+%Y-%m-%d %H:%M:%S') $*"; }
trap 'rc=$?; if [ $rc -ne 0 ]; then log "ERROR: entrypoint aborted (exit code $rc) — see output above"; fi' EXIT

STAMP="${ARCHON_STAMP:-/pip-packages/.extras-installed}"
EXTRAS="${ARCHON_EXTRAS-graph,code,multilingual}"
# The first-start extras install pulls torch (and friends) over the network;
# a stalled read against download.pytorch.org's CDN mid-download previously
# killed the whole install outright (pip does not resume a partial download).
# Raise pip's tight 15s default read timeout and retry the install a few
# times with backoff before giving up.
PIP_INSTALL_TIMEOUT_S="${ARCHON_PIP_TIMEOUT_S:-100}"
PIP_INSTALL_RETRY_DELAY_S="${ARCHON_PIP_RETRY_DELAY_S:-5}"

log "=== archon-search startup ==="
log "ARCHON_EXTRAS=${EXTRAS}"

if [ -z "$EXTRAS" ]; then
    log "ARCHON_EXTRAS is empty — skipping extras install (core-only mode)."
elif [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$EXTRAS" ]; then
    log "Extras not yet installed (or list changed) — starting pip install …"
    log "Target: /pip-packages  Extras: [${EXTRAS}]"
    log "(First start: network-bound — this can take several minutes)"
    _BAKED_VERSION=$(python3 -c "import importlib.metadata; print(importlib.metadata.version('archon-search'))" 2>/dev/null || true)
    [ -n "$_BAKED_VERSION" ] && export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_ARCHON_SEARCH="$_BAKED_VERSION"
    _pip_ok=false
    for attempt in 1 2 3; do
        if PIP_DEFAULT_TIMEOUT="$PIP_INSTALL_TIMEOUT_S" python3 -m pip install --no-cache-dir --target /pip-packages ".[${EXTRAS}]" 2>&1; then
            _pip_ok=true
            break
        fi
        log "pip install attempt ${attempt}/3 failed"
        [ "$attempt" -lt 3 ] && sleep $((attempt * PIP_INSTALL_RETRY_DELAY_S))
    done
    [ "$_pip_ok" = true ] || { log "ERROR: pip install failed after 3 attempts"; exit 1; }
    printf '%s' "$EXTRAS" > "$STAMP"
    log "Extras install complete — stamp written to ${STAMP}."
else
    log "Extras already installed [${EXTRAS}] — skipping pip install."
fi

export PYTHONPATH="/pip-packages${PYTHONPATH:+:$PYTHONPATH}"

log "=== setup complete — handing off to: $* ==="
exec "$@"
