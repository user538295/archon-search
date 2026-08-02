#!/bin/sh
# Installs optional extras and the spaCy model into the /pip-packages named
# volume on first start (or when ARCHON_EXTRAS changes), then execs CMD.
# ponytail: PIP_TARGET propagates into spacy's internal pip call so the model
# lands in the same volume without a separate --target flag.
set -e

log() { echo "[entrypoint] $(date '+%Y-%m-%d %H:%M:%S') $*"; }
trap 'rc=$?; if [ $rc -ne 0 ]; then log "ERROR: entrypoint aborted (exit code $rc) — see output above"; fi' EXIT

STAMP="${ARCHON_STAMP:-/pip-packages/.extras-installed}"
EXTRAS="${ARCHON_EXTRAS-graph,code,multilingual}"

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
    { python3 -m pip install --no-cache-dir --target /pip-packages ".[${EXTRAS}]"; } 2>&1
    printf '%s' "$EXTRAS" > "$STAMP"
    log "Extras install complete — stamp written to ${STAMP}."
else
    log "Extras already installed [${EXTRAS}] — skipping pip install."
fi

export PYTHONPATH="/pip-packages${PYTHONPATH:+:$PYTHONPATH}"

case ",${EXTRAS}," in
    *,graph,*)
        if ! python3 -c "import en_core_web_sm" 2>/dev/null; then
            log "spaCy model en_core_web_sm not found — downloading …"
            { PIP_TARGET=/pip-packages python3 -m spacy download en_core_web_sm; } 2>&1
            log "spaCy model en_core_web_sm download complete."
        else
            log "spaCy model en_core_web_sm already present — skipping download."
        fi
        ;;
    *)
        log "graph extra not in ARCHON_EXTRAS — skipping spaCy model download."
        ;;
esac

log "=== setup complete — handing off to: $* ==="
exec "$@"
