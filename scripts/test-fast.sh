#!/usr/bin/env bash
#
# scripts/test-fast.sh — run the default test suite on a RAM disk (macOS) for speed.
#
# The suite is I/O-bound on many small LanceDB / pytest temp writes (fsync
# latency, not data volume). Putting that temp I/O in RAM removes it.
# Measured 2026-07-20 (14-core / 48 GB): ~153 s vs ~177 s on regular disk, both
# at -n 8, --no-cov. Peak RAM-disk footprint ~0.4 GB (so 2 GB is generous).
# Coverage (the default) adds ~8 %.
#
# Safety guards (this is why the script exists — see CLAUDE.md testing policy):
#   * Refuses to start if ANY pytest is already running anywhere on the machine.
#     Concurrent runs stack xdist workers and have OOM-crashed this box.
#   * Single-instance lock so two invocations of this script can't race.
#   * ALWAYS tears down the RAM disk and lock on exit — success, failure, or
#     Ctrl-C — via a trap, so a crashed run never leaves a RAM disk mounted.
#
# Usage:
#   scripts/test-fast.sh                  # full canonical suite (coverage) on RAM disk
#   scripts/test-fast.sh --no-cov -q      # extra args pass through to pytest
#   scripts/test-fast.sh tests/test_x.py  # scoped run
#   ARCHON_TESTFAST_RAM_MB=4096 scripts/test-fast.sh   # bigger RAM disk
#
# Linux / CI: skip this script — tmpfs is already available:
#   TMPDIR=/dev/shm uv run pytest --basetemp=/dev/shm/archon-pt
#
set -euo pipefail

# Always operate from the repo root (this script lives in scripts/).
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LOCKFILE="/tmp/archon-testfast.lock"
RAM_VOL="archon-testfast-ram"
RAM_MOUNT="/Volumes/${RAM_VOL}"
RAM_SIZE_MB="${ARCHON_TESTFAST_RAM_MB:-2048}"   # peak footprint ~0.4 GB; 2 GB is generous

RAM_DEV=""
HELD_LOCK=0

# Tear everything down on ANY exit (normal, error, or signal). Idempotent.
cleanup() {
  if [[ -n "${RAM_DEV}" ]]; then
    hdiutil detach "${RAM_DEV}" >/dev/null 2>&1 || diskutil eject "${RAM_DEV}" >/dev/null 2>&1 || true
  fi
  if [[ -d "${RAM_MOUNT}" ]]; then
    diskutil eject "${RAM_MOUNT}" >/dev/null 2>&1 || true
  fi
  # rm -f on our OWN ephemeral lockfile only — never a user file.
  [[ "${HELD_LOCK}" == "1" ]] && rm -f "${LOCKFILE}"
  return 0
}
trap cleanup EXIT INT TERM HUP

# --- Guard 1: no other pytest running (a direct `uv run pytest`, another agent, ...) ---
# comm must be python AND args must contain /pytest → self-match-safe (awk's comm is "awk").
if ps -Ao comm=,args= | awk '$1 ~ /[Pp]ython/ && /\/pytest/ {f=1} END{exit !f}'; then
  echo "ERROR: a pytest process is already running — refusing to start a second suite." >&2
  echo "       Concurrent runs stack xdist workers and have OOM-crashed this machine." >&2
  ps -Ao pid=,args= | awk '/[p]ytest/ && !/awk/' >&2
  exit 1
fi

# --- Guard 2: single-instance lock (atomic create; reclaim only if the owner is dead) ---
if ( set -o noclobber; echo "$$" > "${LOCKFILE}" ) 2>/dev/null; then
  HELD_LOCK=1
else
  OWNER="$(cat "${LOCKFILE}" 2>/dev/null || true)"
  if [[ -n "${OWNER}" ]] && kill -0 "${OWNER}" 2>/dev/null; then
    echo "ERROR: another test-fast.sh (PID ${OWNER}) is already running." >&2
    exit 1
  fi
  echo "Reclaiming stale lock (owner PID '${OWNER}' is not running)." >&2
  rm -f "${LOCKFILE}"
  ( set -o noclobber; echo "$$" > "${LOCKFILE}" ) 2>/dev/null && HELD_LOCK=1 || {
    echo "ERROR: lost a lock race, giving up." >&2; exit 1; }
fi

# --- Non-macOS: RAM disk is macOS-specific; run normally with a tmpfs hint. ---
if [[ "$(uname)" != "Darwin" ]]; then
  echo "NOTE: the RAM-disk fast path is macOS-only. On Linux use tmpfs directly:" >&2
  echo "        TMPDIR=/dev/shm uv run pytest --basetemp=/dev/shm/archon-pt \"\$@\"" >&2
  echo "Running a normal suite instead..." >&2
  set +e; uv run pytest "$@"; exit $?
fi

# --- Reclaim a stale RAM disk left by a previously-crashed run, then create a fresh one. ---
if [[ -d "${RAM_MOUNT}" ]]; then
  echo "Ejecting a stale RAM disk at ${RAM_MOUNT} (leftover from a crashed run)..." >&2
  diskutil eject "${RAM_MOUNT}" >/dev/null 2>&1 || true
fi

BLOCKS=$(( RAM_SIZE_MB * 2048 ))   # 2048 512-byte blocks per MiB
RAM_DEV="$(hdiutil attach -nomount "ram://${BLOCKS}" | awk 'NR==1{print $1}')"
if [[ "${RAM_DEV}" != /dev/* ]]; then
  echo "ERROR: failed to create RAM disk (got '${RAM_DEV}')." >&2
  exit 1
fi
diskutil erasevolume APFS "${RAM_VOL}" "${RAM_DEV}" >/dev/null
mkdir -p "${RAM_MOUNT}/tmp" "${RAM_MOUNT}/pt"

echo "RAM disk ${RAM_DEV} (${RAM_SIZE_MB} MB) mounted at ${RAM_MOUNT}. Running the suite..." >&2
echo >&2

# addopts already carries -n 8 / --dist / -m / coverage. TMPDIR redirects
# tempfile.TemporaryDirectory() (the eval-corpus ingests); --basetemp redirects
# pytest's tmp_path / tmp_path_factory (the per-worker data dirs). Extra args pass through.
set +e
TMPDIR="${RAM_MOUNT}/tmp" uv run pytest --basetemp="${RAM_MOUNT}/pt" "$@"
RC=$?
set -e
exit ${RC}   # cleanup() runs via the EXIT trap and tears the RAM disk down.
