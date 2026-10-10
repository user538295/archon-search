#!/bin/sh
# Installs the packages the optional extras add to the image into the
# /pip-packages named volume, then execs CMD. The volume is (re)filled on first
# start and whenever the image or ARCHON_EXTRAS changes; it never holds
# archon-search itself, so a newer image always runs its own code.
set -e

PKG_ROOT=/pip-packages
STAMP="${PKG_ROOT}/.extras-installed"
# Every install attempt writes here; the volume's content is replaced only
# after an attempt succeeds, so a failed install leaves it untouched.
STAGING_NAME=.staging
STAGING_DIR="${PKG_ROOT}/${STAGING_NAME}"
MAX_INSTALL_ATTEMPTS=3
WORK_DIR=""

log() { echo "[entrypoint] $(date '+%Y-%m-%d %H:%M:%S') $*"; }
on_exit() {
    rc=$?
    [ -n "$WORK_DIR" ] && rm -rf "$WORK_DIR"
    if [ $rc -ne 0 ]; then log "ERROR: entrypoint aborted (exit code $rc) — see output above"; fi
}
trap on_exit EXIT

EXTRAS="${ARCHON_EXTRAS-graph,code,multilingual}"
# The first-start extras install pulls torch (and friends) over the network;
# a stalled read against download.pytorch.org's CDN mid-download previously
# killed the whole install outright (pip does not resume a partial download).
# Raise pip's tight 15s default read timeout and retry the install a few
# times with backoff before giving up.
PIP_INSTALL_TIMEOUT_S="${ARCHON_PIP_TIMEOUT_S:-100}"
PIP_INSTALL_RETRY_DELAY_S="${ARCHON_PIP_RETRY_DELAY_S:-5}"

# Exits non-zero when an entry of the comma-separated extras list $1 is empty
# (a stray or doubled comma, or a blank value) or is not an extra of the image's
# archon-search, naming the valid ones. Names compare PEP 685-normalised
# (`rag_fusion` matches `rag-fusion`). pip itself would only warn and install
# nothing for an unknown extra.
check_extras_exist() {
    _problem=$(python3 -I -c '
import importlib.metadata, re, sys

def normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()

valid = sorted(importlib.metadata.metadata("archon-search").get_all("Provides-Extra") or [])
known = {normalise(name) for name in valid}
entries = [name.strip() for name in sys.argv[1].split(",")]
problems = []
if "" in entries:
    problems.append("empty entry in ARCHON_EXTRAS (stray or doubled comma, or a blank value)")
unknown = [name for name in entries if name and normalise(name) not in known]
if unknown:
    problems.append("unknown extra(s) in ARCHON_EXTRAS: " + ", ".join(unknown))
if problems:
    print("; ".join(problems) + "; valid extras: " + ", ".join(valid))
' "$1")
    if [ -n "$_problem" ]; then
        log "ERROR: ${_problem}"
        exit 1
    fi
}

# Prints the stamp for extras list $1: the extras, the Python version and the
# image's package list. Any image change (new version, or a rebuild with the
# same version but other packages) or extras change therefore invalidates the
# volume. `-I` keeps the volume itself out of the listing.
image_fingerprint() {
    _python_version=$(python3 -I -c 'import sys; print(sys.version)')
    _image_packages=$(python3 -I -m pip list --format=freeze)
    printf 'extras=%s\npython=%s\n%s' "$1" "$_python_version" "$_image_packages"
}

# Prints the version of the image's onnxruntime-gpu, or nothing on an image
# without it. The GPU image swapped `onnxruntime` for `onnxruntime-gpu`, which
# ships the same `onnxruntime` import package. fastembed and magika (in the base
# closure) still require `onnxruntime` by name, as does gliner 0.2.26-0.2.28
# (the `graph` extra), so the extras resolution adds the CPU build back, and
# a CPU copy in the volume, first on PYTHONPATH, would shadow the GPU build; on
# that image the CPU `onnxruntime` is therefore left out. Leaving it out
# leaves none of its dependencies missing: the CPU onnxruntime's
# Requires-Dist equals onnxruntime-gpu's outside their extras (flatbuffers,
# numpy, packaging, protobuf as of 1.31.0:
# https://pypi.org/pypi/onnxruntime/1.31.0/json,
# https://pypi.org/pypi/onnxruntime-gpu/1.31.0/json), and the image installed
# those with onnxruntime-gpu. The resolution's
# requirements on onnxruntime (e.g. fastembed's version range) are NOT
# re-checked against the image's onnxruntime-gpu: its version stands in for
# whichever onnxruntime version the resolution picked, and the log names both.
print_image_onnxruntime_gpu_version() {
    python3 -I -c '
import importlib.metadata
try:
    print(importlib.metadata.version("onnxruntime-gpu"))
except importlib.metadata.PackageNotFoundError:
    pass
'
}

# Python shared by the helpers that read pip's dry-run report. Argument
# contract of every helper built on it: sys.argv[1] is the report path,
# sys.argv[2] the distribution to leave out (empty: none), and the appended
# fragment reads its own arguments from sys.argv[3] on. Defines `packages`,
# the report's install list; `is_left_out(package)`, which compares the
# package's name PEP 503-normalised with sys.argv[2]; and
# `format_pins(packages)`, which returns them as a space-separated
# `name==version` list.
READ_REPORT_PY='
import json, pathlib, re, sys

def normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()

def is_left_out(package):
    return bool(sys.argv[2]) and normalise(package["metadata"]["name"]) == normalise(sys.argv[2])

def format_pins(packages):
    return " ".join(p["metadata"]["name"] + "==" + p["metadata"]["version"] for p in packages)

packages = json.loads(pathlib.Path(sys.argv[1]).read_text())["install"]
'

# Reads pip's dry-run report $1 and writes one `name @ url` line per package to
# install into requirements file $3 — the exact artifacts the resolution chose,
# so the install cannot pick another one by re-resolving a name — leaving out
# distribution $2 (empty: none). Prints the packages written as
# `name==version` for the log; prints nothing when there are none.
write_exact_requirements() {
    python3 -I -c "${READ_REPORT_PY}"'
installed = [p for p in packages if not is_left_out(p)]
pathlib.Path(sys.argv[3]).write_text(
    "".join(p["metadata"]["name"] + " @ " + p["download_info"]["url"] + "\n" for p in installed)
)
print(format_pins(installed))
' "$1" "$2" "$3"
}

# Prints, as `name==version`, the packages of pip's dry-run report $1 that are
# distribution $2 (empty: none); prints nothing when there are none.
print_left_out_pins() {
    python3 -I -c "${READ_REPORT_PY}"'
print(format_pins([p for p in packages if is_left_out(p)]))
' "$1" "$2"
}

# Installs into directory $4 the packages the extras $1 add to the image, whose
# archon-search is version $2, using work directory $3 for pip's report.
# Resolves archon-search[$1]==$2 against the image's environment only (`-I`
# ignores PYTHONPATH, so the volume is invisible) and installs exactly what
# that resolution adds, less the CPU onnxruntime on an image with onnxruntime-gpu
# (see print_image_onnxruntime_gpu_version). pip keeps every image package that already satisfies
# the extras (its default only-if-needed strategy), so a base package is added
# only when an extra forces a different version — e.g. the `code` extra pins
# tree-sitter<0.26 while the image's base closure can ship a newer tree-sitter
# (0.26 or later), so a 0.25.x tree-sitter is added, exactly as
# `pip install archon-search[code]` would do in a plain env. `--target` alone
# cannot do this: it implies --ignore-installed and copies the whole closure,
# archon-search included, which then shadows the image.
install_extras_into() {
    _extras=$1
    _image_version=$2
    _work_dir=$3
    _target=$4
    PIP_DEFAULT_TIMEOUT="$PIP_INSTALL_TIMEOUT_S" python3 -I -m pip install --dry-run --quiet \
        --report "${_work_dir}/report.json" "archon-search[${_extras}]==${_image_version}" 2>&1 || return 1
    _gpu_version=$(print_image_onnxruntime_gpu_version) || return 1
    _left_out_name=""
    if [ -n "$_gpu_version" ]; then
        _left_out_name=onnxruntime
    fi
    _left_out=$(print_left_out_pins "${_work_dir}/report.json" "$_left_out_name") || return 1
    if [ -n "$_left_out" ]; then
        log "Leaving out ${_left_out}: the image's onnxruntime-gpu ${_gpu_version} replaces it (same onnxruntime import package)."
    fi
    _packages=$(write_exact_requirements "${_work_dir}/report.json" "$_left_out_name" "${_work_dir}/requirements.txt") || return 1
    if [ -z "$_packages" ]; then
        log "The image already provides every package of [${_extras}] — nothing to install."
        return 0
    fi
    log "Installing: ${_packages}"
    PIP_DEFAULT_TIMEOUT="$PIP_INSTALL_TIMEOUT_S" python3 -I -m pip install --no-cache-dir --no-deps \
        --target "$_target" -r "${_work_dir}/requirements.txt" 2>&1
}

# Runs up to MAX_INSTALL_ATTEMPTS installs (arguments $1-$3 as for
# install_extras_into), each into a fresh STAGING_DIR, backing off between
# attempts. Returns non-zero, with STAGING_DIR removed, when every attempt fails.
install_with_retries() {
    _attempt=1
    while :; do
        rm -rf "$STAGING_DIR"
        if mkdir "$STAGING_DIR" && install_extras_into "$1" "$2" "$3" "$STAGING_DIR"; then
            return 0
        fi
        log "pip install attempt ${_attempt}/${MAX_INSTALL_ATTEMPTS} failed"
        if [ "$_attempt" -ge "$MAX_INSTALL_ATTEMPTS" ]; then
            rm -rf "$STAGING_DIR"
            return 1
        fi
        sleep $((_attempt * PIP_INSTALL_RETRY_DELAY_S))
        _attempt=$((_attempt + 1))
    done
}

# Replaces the volume's content with the staged packages. Removes every other
# entry of the volume root — packages of the previous image or extras, and the
# archon-search copy that older entrypoints left by running
# `pip install --target` over the whole closure — never the root itself. The
# old stamp goes first, so a crash from here on leaves no matching stamp and
# the next start redoes the install.
promote_staged_packages() {
    rm -f "$STAMP"
    find "$PKG_ROOT" -mindepth 1 -maxdepth 1 ! -name "$STAGING_NAME" -exec rm -rf {} +
    find "$STAGING_DIR" -mindepth 1 -maxdepth 1 -exec mv {} "$PKG_ROOT" \;
    rmdir "$STAGING_DIR"
}

# Refills the volume with the packages the extras $1 add to the image — install
# with retries, then promote — and stamps it with fingerprint $2, written last.
refresh_extras_volume() {
    _archon_version=$(python3 -I -c "import importlib.metadata as m; print(m.version('archon-search'))")
    log "Extras not yet installed (or image/extras changed) — starting pip install …"
    log "Target: ${PKG_ROOT}  Extras: [$1]  archon-search: ${_archon_version}"
    log "(First start: network-bound — this can take several minutes)"
    WORK_DIR=$(mktemp -d)
    if ! install_with_retries "$1" "$_archon_version" "$WORK_DIR"; then
        log "ERROR: pip install failed after ${MAX_INSTALL_ATTEMPTS} attempts — ${PKG_ROOT} left unchanged"
        exit 1
    fi
    rm -rf "$WORK_DIR"
    promote_staged_packages
    printf '%s\n' "$2" > "$STAMP"
    log "Extras install complete — stamp written to ${STAMP}."
}

log "=== archon-search startup ==="
log "ARCHON_EXTRAS=${EXTRAS}"

if [ -z "$EXTRAS" ]; then
    log "ARCHON_EXTRAS is empty — skipping extras install (core-only mode)."
else
    check_extras_exist "$EXTRAS"
    FINGERPRINT=$(image_fingerprint "$EXTRAS")
    if [ "$(cat "$STAMP" 2>/dev/null)" = "$FINGERPRINT" ]; then
        log "Extras already installed [${EXTRAS}] — skipping pip install."
    else
        refresh_extras_volume "$EXTRAS" "$FINGERPRINT"
    fi
    export PYTHONPATH="${PKG_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
fi

log "=== setup complete — handing off to: $* ==="
exec "$@"
