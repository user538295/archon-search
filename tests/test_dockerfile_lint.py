"""Static linting of the Dockerfile + .dockerignore deliverables (Task 4.1).

These tests do not build or run any container — they only inspect the
on-disk files so they are safe to run on machines without a Docker daemon.
They guard the structural invariants required by the C9 container-support
plan: non-root user UID 1000, tini entrypoint, healthcheck without curl,
container env vars, local-source install, GPU swap gating, and the
.dockerignore excluding the .git tree.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
import urllib.request
from pathlib import Path

import pytest
from packaging.markers import InvalidMarker, Marker, UndefinedEnvironmentName
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
UV_LOCK = REPO_ROOT / "uv.lock"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCKER_DOC = REPO_ROOT / "Documentation" / "UserManual" / "140_running_with_docker.md"

# S232: the first-start extras install was measured at 398s (lightest set) and
# 423s (default set) on an idle machine — both above the original 360s
# HEALTHCHECK start-period, so a container was marked unhealthy while still
# legitimately installing. The start-period must cover the measured worst-case
# (and leave margin for the network-bound variance the bug report documents).
MIN_START_PERIOD_SECONDS = 420


def _join_continuations(text: str) -> list[str]:
    """Join backslash-continued lines into single logical lines.

    Dockerfile syntax lets RUN / HEALTHCHECK / ENV / etc. span multiple
    physical lines via ``\\`` continuation. Linting the first physical
    line only would miss CMD payloads on subsequent lines; this helper
    returns logical lines so each assertion sees the full instruction.
    """
    logical: list[str] = []
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf.append(line[:-1].rstrip())
            continue
        buf.append(line)
        logical.append(" ".join(s for s in buf if s).strip())
        buf = []
    if buf:
        logical.append(" ".join(s for s in buf if s).strip())
    return logical


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def dockerfile_logical(dockerfile_text: str) -> list[str]:
    return _join_continuations(dockerfile_text)


@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    return DOCKERIGNORE.read_text()


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def test_dockerfile_exists() -> None:
    assert DOCKERFILE.exists(), f"{DOCKERFILE} not found"


def test_dockerignore_exists() -> None:
    assert DOCKERIGNORE.exists(), f"{DOCKERIGNORE} not found"


# ---------------------------------------------------------------------------
# Non-root user (spec invariant #2, #3)
# ---------------------------------------------------------------------------


def test_dockerfile_has_non_root_user(dockerfile_text: str) -> None:
    assert "appuser" in dockerfile_text, (
        "Dockerfile must create and switch to the non-root 'appuser' user"
    )


def test_dockerfile_uid_1000_is_explicit(dockerfile_text: str) -> None:
    # Spec invariant #2: `useradd --uid 1000 ...` so the UID is stable
    # for host-volume permission setups.
    assert re.search(r"--uid\s+1000\b", dockerfile_text), (
        "Dockerfile must create appuser with explicit `--uid 1000`"
    )


def test_dockerfile_chown_before_user_switch(dockerfile_text: str) -> None:
    # Spec invariant #3: /data must be chowned to appuser BEFORE the
    # `USER appuser` switch, otherwise anonymous-volume runs cannot
    # write the auto-generated key file.
    chown_idx = dockerfile_text.find("chown appuser:appuser /data")
    user_idx = dockerfile_text.find("USER appuser")
    assert chown_idx != -1, "Dockerfile must chown /data to appuser"
    assert user_idx != -1, "Dockerfile must switch to USER appuser"
    assert chown_idx < user_idx, (
        "chown of /data must happen BEFORE `USER appuser`; otherwise the "
        "anonymous-volume run path cannot write to /data"
    )


# ---------------------------------------------------------------------------
# Entrypoint and init (spec invariant #9)
# ---------------------------------------------------------------------------


def test_dockerfile_has_tini_entrypoint(dockerfile_text: str) -> None:
    assert 'ENTRYPOINT ["tini", "--", "/entrypoint.sh"]' in dockerfile_text, (
        'Dockerfile must use ENTRYPOINT ["tini", "--", "/entrypoint.sh"] to forward signals'
    )


def test_dockerfile_copies_and_chmod_entrypoint(dockerfile_text: str) -> None:
    assert "COPY scripts/docker-entrypoint.sh /entrypoint.sh" in dockerfile_text, (
        "Dockerfile must COPY the entrypoint script before switching to USER appuser"
    )
    assert "chmod +x /entrypoint.sh" in dockerfile_text, (
        "Dockerfile must chmod +x the entrypoint script so it is executable"
    )


def test_dockerfile_installs_tini(dockerfile_text: str) -> None:
    # Spec invariant #9: tini must actually be installed before being
    # invoked as the entrypoint.
    assert re.search(r"apt-get install[^\n]*\btini\b", dockerfile_text), (
        "Dockerfile must `apt-get install tini` before using it as ENTRYPOINT"
    )


def test_dockerfile_cmd_is_archon_search_serve(dockerfile_text: str) -> None:
    assert 'CMD ["archon-search", "serve"]' in dockerfile_text, (
        "Dockerfile CMD must invoke `archon-search serve` so the foreground "
        "serve subcommand is the container's main process"
    )


# ---------------------------------------------------------------------------
# Healthcheck (spec invariants #4, #5)
# ---------------------------------------------------------------------------


def test_dockerfile_has_healthcheck(dockerfile_logical: list[str]) -> None:
    healthcheck_line = next(
        (line for line in dockerfile_logical if line.startswith("HEALTHCHECK")),
        None,
    )
    assert healthcheck_line is not None, "Dockerfile must declare a HEALTHCHECK"
    # Spec invariant #4: must not invoke curl (python:3.12-slim does not
    # ship curl and installing it would add ~5MB plus apt overhead). The
    # logical line is the full continuation-joined instruction so this
    # check also catches `curl` on the CMD payload.
    assert "curl" not in healthcheck_line.lower(), (
        "HEALTHCHECK must not invoke curl (use python urllib instead)"
    )


def test_dockerfile_healthcheck_uses_urllib(dockerfile_logical: list[str]) -> None:
    healthcheck_line = next(
        line for line in dockerfile_logical if line.startswith("HEALTHCHECK")
    )
    assert "urllib.request" in healthcheck_line, (
        "HEALTHCHECK must call urllib.request.urlopen so the slim base "
        "needs no extra HTTP client"
    )


def test_dockerfile_healthcheck_targets_ready_endpoint(
    dockerfile_logical: list[str],
) -> None:
    # Spec invariant #5: the readiness probe targets /ready (not /health).
    healthcheck_line = next(
        line for line in dockerfile_logical if line.startswith("HEALTHCHECK")
    )
    assert "/ready" in healthcheck_line, (
        "HEALTHCHECK must target the /ready endpoint exposed by the FastAPI app"
    )


def _healthcheck_probe_url(dockerfile_logical: list[str], monkeypatch: pytest.MonkeyPatch, port: str | None) -> str:
    """Run the HEALTHCHECK's python payload with ``urlopen`` faked and return the URL it probes."""
    line = next(line for line in dockerfile_logical if line.startswith("HEALTHCHECK"))
    payload = re.search(r'python3 -c "(.*)" \|\| exit 1', line)
    assert payload is not None, f"cannot find the python3 -c payload in {line!r}"
    probed: list[str] = []
    monkeypatch.setattr(urllib.request, "urlopen", probed.append)
    if port is None:
        monkeypatch.delenv("ARCHON_SEARCH_PORT", raising=False)
    else:
        monkeypatch.setenv("ARCHON_SEARCH_PORT", port)
    exec(payload.group(1), {})  # noqa: S102 — the test's purpose is to run the HEALTHCHECK payload against a faked urlopen
    [url] = probed
    return url


@pytest.mark.parametrize(
    ("port", "expected"),
    [
        (None, "http://localhost:8765/ready"),
        ("", "http://localhost:8765/ready"),
        ("9001", "http://localhost:9001/ready"),
    ],
)
def test_dockerfile_healthcheck_follows_archon_search_port(
    dockerfile_logical: list[str], monkeypatch: pytest.MonkeyPatch, port: str | None, expected: str
) -> None:
    assert _healthcheck_probe_url(dockerfile_logical, monkeypatch, port) == expected


def test_dockerfile_prefetches_parse_models_into_a_cache_outside_the_data_volume(
    dockerfile_text: str, dockerfile_logical: list[str]
) -> None:
    """The first PDF/image parse must not download docling's models inside ``parse_timeout_seconds``."""
    cache = re.search(r"^ENV HF_HUB_CACHE=(\S+)", dockerfile_text, re.MULTILINE)
    assert cache is not None, "the Dockerfile must pin HF_HUB_CACHE to the prefetched model cache"
    assert not cache.group(1).startswith("/data"), "a cache on the data volume is not seeded by the image"
    prefetch = next(
        (
            i
            for i, line in enumerate(dockerfile_logical)
            if line.startswith("RUN ") and "prefetch-parse-models.py" in line
        ),
        None,
    )
    assert prefetch is not None, "the Dockerfile must run scripts/prefetch-parse-models.py at build time"
    assert "chown -R appuser:appuser" in dockerfile_logical[prefetch]
    pip_install = next(i for i, line in enumerate(dockerfile_logical) if "-m pip install" in line)
    user_switch = next(i for i, line in enumerate(dockerfile_logical) if line.startswith("USER "))
    assert pip_install < prefetch < user_switch, "prefetch needs the installed package and root's write access"


def test_prefetch_script_writes_a_scanned_pdf_and_a_png(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "prefetch_parse_models", REPO_ROOT / "scripts" / "prefetch-parse-models.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pdf, png = module._write_samples(tmp_path)

    assert pdf.read_bytes().startswith(b"%PDF")
    assert png.read_bytes().startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# Env vars (spec invariants on ENV block)
# ---------------------------------------------------------------------------


def test_dockerfile_has_data_dir_env(dockerfile_text: str) -> None:
    assert "ARCHON_SEARCH_DATA_DIR=/data" in dockerfile_text, (
        "Dockerfile must set ARCHON_SEARCH_DATA_DIR=/data so all runtime state "
        "lands on the mounted volume"
    )


def test_dockerfile_has_container_env(dockerfile_text: str) -> None:
    assert "ARCHON_SEARCH_CONTAINER=1" in dockerfile_text, (
        "Dockerfile must set ARCHON_SEARCH_CONTAINER=1 so logs flow to stderr"
    )


def test_dockerfile_sets_no_fastembed_cache_env(dockerfile_text: str) -> None:
    # PyTorch runtime (ADR 18): weights land in `$ARCHON_SEARCH_DATA_DIR/models`
    # (get_models_dir()); the old env var is gone and would silently do nothing.
    assert "FASTEMBED_CACHE_PATH" not in dockerfile_text


def test_dockerfile_precreates_models_dir_for_appuser(
    dockerfile_logical: list[str],
) -> None:
    # Docker seeds a fresh named volume from the image directory it mounts over,
    # ownership included; a mountpoint the image lacks is created root-owned. So the
    # documented `archon-model-cache:/data/models` opt-in (docker-compose.yml)
    # would be unwritable for UID 1000 and the server could not download weights.
    env_lines = " ".join(line for line in dockerfile_logical if line.startswith("ENV "))
    data_dir = re.search(r"\bARCHON_SEARCH_DATA_DIR=(\S+)", env_lines)
    assert data_dir, "Dockerfile ENV must set ARCHON_SEARCH_DATA_DIR"
    models_dir = f"{data_dir.group(1)}/models"
    user_idx = dockerfile_logical.index("USER appuser")
    before_user = [line for line in dockerfile_logical[:user_idx] if line.startswith("RUN ")]
    commands = [cmd.strip() for line in before_user for cmd in re.split(r";|&&", line[4:])]
    path = re.escape(models_dir)
    assert any(re.match(rf"mkdir -p .*(?<!\S){path}(?!\S)", c) for c in commands), (
        f"Dockerfile must `mkdir -p {models_dir}` before `USER appuser`"
    )
    assert any(re.match(rf"chown appuser:appuser .*(?<!\S){path}(?!\S)", c) for c in commands), (
        f"Dockerfile must `chown appuser:appuser {models_dir}` before `USER appuser`"
    )


def test_dockerfile_hardens_pip_against_network_stalls(dockerfile_logical: list[str]) -> None:
    # The bake-time `pip install .` pulls multi-hundred-MB wheels; pip's default 15 s read
    # timeout aborted a release gate run (ReadTimeoutError from files.pythonhosted.org).
    env_lines = " ".join(line for line in dockerfile_logical if line.startswith("ENV "))
    timeout = re.search(r"\bPIP_DEFAULT_TIMEOUT=(\d+)", env_lines)
    retries = re.search(r"\bPIP_RETRIES=(\d+)", env_lines)
    assert timeout and int(timeout.group(1)) >= 100, "Dockerfile ENV must set PIP_DEFAULT_TIMEOUT>=100"
    assert retries and int(retries.group(1)) >= 10, "Dockerfile ENV must set PIP_RETRIES>=10"


def test_dockerfile_sets_home_to_data(dockerfile_text: str) -> None:
    assert "HOME=/data" in dockerfile_text, (
        "Dockerfile must set HOME=/data so pip and other tools write caches "
        "to the persistent volume (appuser is created --no-create-home)"
    )


# ---------------------------------------------------------------------------
# Volume + port + project source layout
# ---------------------------------------------------------------------------


def test_dockerfile_declares_data_volume(dockerfile_text: str) -> None:
    assert re.search(r'VOLUME\s+\["/data"\]|VOLUME\s+/data\b', dockerfile_text), (
        "Dockerfile must declare VOLUME /data so anonymous-volume runs persist"
    )


def test_dockerfile_exposes_8765(dockerfile_text: str) -> None:
    assert re.search(r"^EXPOSE\s+8765\b", dockerfile_text, re.MULTILINE), (
        "Dockerfile must EXPOSE 8765 (the archon-search default port)"
    )


def test_dockerfile_copies_local_source_not_pypi(dockerfile_text: str) -> None:
    # Spec invariant #6: the image is built from local source so the docker
    # job in CI does not race with the PyPI publish job. `pip install
    # archon-search` from PyPI would serve the previous version.
    assert re.search(r"^COPY\s+\.\s+/app\b", dockerfile_text, re.MULTILINE), (
        "Dockerfile must `COPY . /app` to install from local source"
    )
    assert not re.search(
        r"pip\s+install[^\n]*\barchon-search\b(?![-_])", dockerfile_text
    ), (
        "Dockerfile must not `pip install archon-search` from PyPI — the "
        "image must be built from the local source copy"
    )


# ---------------------------------------------------------------------------
# Build-arg wiring (spec invariants #7, #8, #11)
# ---------------------------------------------------------------------------


def test_dockerfile_args_redeclared_after_from(dockerfile_text: str) -> None:
    # Spec invariant #7: ARGs declared before FROM are NOT available in
    # subsequent RUN instructions unless re-declared after FROM. Both
    # BASE_IMAGE (used by the GPU swap) and GIT_COMMIT (used by LABEL)
    # must therefore appear after the FROM line.
    from_idx = dockerfile_text.find("FROM ${BASE_IMAGE}")
    assert from_idx != -1, "Dockerfile must contain `FROM ${BASE_IMAGE}`"
    after_from = dockerfile_text[from_idx:]
    assert re.search(r"^ARG\s+BASE_IMAGE\b", after_from, re.MULTILINE), (
        "ARG BASE_IMAGE must be re-declared after FROM so RUN instructions "
        "can read it"
    )
    assert re.search(r"^ARG\s+GIT_COMMIT\b", after_from, re.MULTILINE), (
        "ARG GIT_COMMIT must be re-declared after FROM so LABEL and RUN "
        "instructions can read it"
    )


def test_dockerfile_labels_revision_with_git_commit(dockerfile_text: str) -> None:
    # Spec invariant #8: GIT_COMMIT build-arg must surface in image labels
    # so operators can `docker inspect` and see which commit produced the
    # image — critical for detecting stale `:gpu` floating tags.
    assert re.search(
        r"LABEL\s+org\.opencontainers\.image\.revision=\$GIT_COMMIT",
        dockerfile_text,
    ), (
        "Dockerfile must `LABEL org.opencontainers.image.revision=$GIT_COMMIT` "
        "so the commit SHA is baked into the image"
    )


def test_dockerfile_installs_no_onnxruntime_gpu(dockerfile_text: str) -> None:
    # The GPU arm runs on PyPI's CUDA torch; archon no longer configures
    # onnxruntime, so no `onnxruntime-gpu` swap (and no uninstall of the CPU build).
    assert "onnxruntime" not in dockerfile_text


def test_dockerfile_gpu_cuda_check_only_on_nvidia_base(dockerfile_text: str) -> None:
    # The CUDA-major consistency check must be inside a
    # `case ${BASE_IMAGE} in *nvidia/cuda*)` guard: it requires a CUDA torch and
    # CUDA_VERSION, which the CPU image has neither of. The Dockerfile has more
    # than one such case block (the CPU torch-index guard is another — S269), so
    # locate the block by its content rather than assuming there is exactly one.
    case_blocks = re.findall(
        r"case\s+\"?\$\{?BASE_IMAGE\}?\"?\s+in[\s\S]*?esac",
        dockerfile_text,
    )
    check_block = next((b for b in case_blocks if "check-gpu-cuda-majors.sh" in b), None)
    assert check_block is not None, (
        "the CUDA-major check must be guarded by a `case ${BASE_IMAGE} in ... esac` block"
    )
    assert "*nvidia/cuda*" in check_block, (
        "the check's guard must match `*nvidia/cuda*` (the documented GPU base)"
    )


# ---------------------------------------------------------------------------
# .dockerignore (spec invariant #10)
# ---------------------------------------------------------------------------


def test_dockerignore_excludes_git(dockerignore_text: str) -> None:
    lines = {line.strip() for line in dockerignore_text.splitlines() if line.strip()}
    assert any(entry in lines for entry in (".git", ".git/")), (
        ".dockerignore must exclude the .git directory"
    )


# ---------------------------------------------------------------------------
# CPU image must not ship the CUDA/torch stack (S269)
# ---------------------------------------------------------------------------


def test_uvlock_has_no_nvidia_cuda_package_on_any_platform() -> None:
    # S269 ground truth, now for both linux archs: PyPI's linux torch wheel declares ~18
    # nvidia-*/cuda-* dependencies (a ~6 GB CUDA runtime a CPU host never runs), so a lock that
    # routes linux/x86_64 AND linux/aarch64 torch through the PyTorch CPU index holds no such
    # package at all. A regression of the `[tool.uv.sources]` marker on either arch (e.g.
    # back to `platform_machine == 'x86_64'`) re-introduces them for the other arch.
    locked = {canonicalize_name(p["name"]) for p in tomllib.loads(UV_LOCK.read_text())["package"]}

    banned = sorted(n for n in locked if re.match(r"^(nvidia-|cuda-)", n))
    assert not banned, (
        f"uv.lock pins {len(banned)} nvidia/cuda packages: {banned}. torch/torchvision must "
        "come from the PyTorch CPU index on every linux arch ([tool.uv.sources] marker)."
    )


# Environment markers for the CPU image target: it is built and run as
# linux/amd64 (Documentation/UserManual/140_running_with_docker.md documents
# the `:latest` image as CPU inference only). nvidia-*/cuda-* deps in uv.lock
# are gated on `sys_platform == 'linux'`, so honoring these markers keeps the
# closure faithful to what actually installs inside the CPU container.
_CPU_TARGET_ENV = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "python_version": "3.12",
    "python_full_version": "3.12.0",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "extra": "",
}

# The CPU image installs the package with its default runtime extras. The
# entrypoint installs `archon-search[graph,code,multilingual]` on first start
# (default feature set); torch/torchvision enter the closure transitively via
# the core `docling` dependency, not via these extras.
_CPU_IMAGE_EXTRAS = ("graph", "code", "multilingual")

# The PyTorch CPU wheel index the fix pins torch/torchvision to; the `+cpu`
# local builds are sourced from here for the linux/amd64 CPU image.
_CPU_TORCH_INDEX = "download.pytorch.org/whl/cpu"


def _marker_holds_on_cpu_target(marker: str | None) -> bool:
    """Whether a PEP 508 dependency marker is true for the linux/amd64 CPU image.

    A dependency with no marker always installs. On any parse/eval error we
    conservatively include the dependency — a false inclusion can only make the
    nvidia/cuda ban stricter, never mask a genuine CUDA package.
    """
    if not marker:
        return True
    try:
        return Marker(marker).evaluate(_CPU_TARGET_ENV)
    except (InvalidMarker, UndefinedEnvironmentName):  # pragma: no cover - defensive, markers here are simple
        return True


def _requirement_name(spec: str) -> str:
    """Canonical distribution name from a PEP 508 requirement string."""
    return canonicalize_name(re.split(r"[<>=!~;\[ ]", spec.strip(), maxsplit=1)[0])


def _uvlock_cpu_target_closure() -> set[str]:
    """Resolve the full dependency closure the CPU image installs, offline.

    Seeds from the core `[project.dependencies]` plus the CPU image's default
    `[graph,code,multilingual]` extras, then BFS-walks each locked package's
    `dependencies` (and the `optional-dependencies` pulled via a dependency's
    ``extra = [...]``), honoring environment markers for linux/amd64. Returns
    the set of canonical package names in the closure.
    """
    lock = tomllib.loads(UV_LOCK.read_text())
    pyproject = tomllib.loads(PYPROJECT.read_text())
    packages = {canonicalize_name(p["name"]): p for p in lock["package"]}

    seed = list(pyproject["project"]["dependencies"])
    for group in _CPU_IMAGE_EXTRAS:
        seed += pyproject["project"]["optional-dependencies"][group]

    # Work items are (package_name, extra_or_None); an extra pulls that
    # package's optional-dependencies[extra] in addition to its base deps.
    work: list[tuple[str, str | None]] = [(_requirement_name(s), None) for s in seed]
    visited: set[tuple[str, str | None]] = set()
    closure: set[str] = set()
    while work:
        name, extra = work.pop()
        closure.add(name)
        if name not in packages or (name, extra) in visited:
            continue
        visited.add((name, extra))
        pkg = packages[name]
        deps = list(pkg.get("dependencies", []))
        if extra is not None:
            deps += pkg.get("optional-dependencies", {}).get(extra, [])
        for dep in deps:
            if not _marker_holds_on_cpu_target(dep.get("marker")):
                continue
            dep_name = canonicalize_name(dep["name"])
            for requested_extra in dep.get("extra") or [None]:
                work.append((dep_name, requested_extra))
    return closure


def test_uvlock_cpu_closure_has_no_nvidia_cuda_and_torch_is_cpu() -> None:
    # S269 REPRODUCER: the CPU `:latest` image is documented "CPU inference
    # only" (linux/amd64, no NVIDIA GPU), yet the locked dependency closure for
    # its default extras `[graph,code,multilingual]` drags in the full NVIDIA
    # CUDA runtime through docling -> docling-slim[standard] -> torch. A CPU
    # host cannot execute those CUDA libraries. The fix pins torch/torchvision
    # to the PyTorch CPU wheel index so the closure resolves `torch==<ver>+cpu`
    # with ZERO nvidia/cuda deps. This test bans nvidia-*/cuda* by name and
    # requires any resolved torch/torchvision to be a `+cpu` local build.
    closure = _uvlock_cpu_target_closure()

    banned = sorted(n for n in closure if re.match(r"^(nvidia-|cuda)", n, re.IGNORECASE))
    assert not banned, (
        "CPU image dependency closure must contain NO nvidia-*/cuda* packages, "
        f"but {len(banned)} are present: {banned}. They enter via "
        "docling -> docling-slim[standard] -> torch on linux/amd64."
    )

    # After the fix, uv.lock carries TWO blocks each for torch/torchvision: a
    # plain pypi build (for non-x86_64-linux platforms) and a `+cpu` whl/cpu
    # build (the one that resolves for the linux/amd64 CPU image). A dict keyed
    # by canonical name would collapse them and pick a block by file order, so
    # select the CPU block explicitly by its whl/cpu source (or `+cpu` version).
    lock = tomllib.loads(UV_LOCK.read_text())
    blocks_by_name: dict[str, list[dict]] = {}
    for p in lock["package"]:
        blocks_by_name.setdefault(canonicalize_name(p["name"]), []).append(p)

    for pkg in ("torch", "torchvision"):
        if pkg not in closure:
            continue
        blocks = blocks_by_name[pkg]
        cpu_blocks = [
            b
            for b in blocks
            if _CPU_TORCH_INDEX in b.get("source", {}).get("registry", "")
            or "+cpu" in b.get("version", "")
        ]
        assert cpu_blocks, (
            f"{pkg} is in the CPU image closure but uv.lock has no '+cpu' build "
            f"for it (registry {_CPU_TORCH_INDEX} or version containing '+cpu'). "
            f"Locked builds: {[b.get('version') for b in blocks]}. The CPU image "
            "must resolve the '+cpu' build from the PyTorch CPU wheel index."
        )
        for b in cpu_blocks:
            version = b.get("version", "")
            assert "+cu" not in version, (
                f"{pkg}=={version} is a CUDA (+cu) build; the CPU image must "
                "use the '+cpu' build."
            )


def test_dockerfile_cpu_base_resolves_cpu_only_torch(dockerfile_text: str) -> None:
    # S269: the CPU image is documented "CPU inference only" yet resolved
    # torch + torchvision + ~18 nvidia/cuda packages (~6.2 GB). The fix must
    # point pip at the PyTorch CPU wheel index so torch resolves to a
    # `+cpu` build with no nvidia/cuda dependencies. Both the bake-time
    # `pip install .` and the entrypoint's runtime install of the packages the
    # extras add to the image must pick it up (e.g. via a baked /etc/pip.conf).
    assert "download.pytorch.org/whl/cpu" in dockerfile_text, (
        "Dockerfile must route the CPU base at the PyTorch CPU wheel index "
        "(download.pytorch.org/whl/cpu) so torch resolves CPU-only"
    )


def test_dockerfile_cpu_torch_index_not_applied_to_gpu_base(
    dockerfile_text: str,
) -> None:
    # The CPU wheel index must be gated on the CPU base — the nvidia/cuda GPU
    # base needs the default CUDA torch build, so the CPU index must sit in a
    # `case ${BASE_IMAGE}` guard that explicitly excludes `*nvidia/cuda*`.
    case_blocks = re.findall(
        r"case\s+\"?\$\{?BASE_IMAGE\}?\"?\s+in[\s\S]*?esac", dockerfile_text
    )
    guard = next(
        (b for b in case_blocks if "download.pytorch.org/whl/cpu" in b), None
    )
    assert guard is not None, (
        "The PyTorch CPU index must live inside a `case ${BASE_IMAGE} in "
        "... esac` guard so it is applied per base image, not unconditionally"
    )
    assert "*nvidia/cuda*" in guard, (
        "The CPU-index case guard must have an explicit `*nvidia/cuda*` arm "
        "so the GPU base keeps the default CUDA torch build"
    )
    nvidia_arm = re.search(
        r"\*nvidia/cuda\*\)([\s\S]*?)(?:;;|esac)", guard
    )
    assert nvidia_arm is not None, "case guard must have a *nvidia/cuda*) arm"
    assert "download.pytorch.org/whl/cpu" not in nvidia_arm.group(1), (
        "The GPU (*nvidia/cuda*) arm must NOT apply the CPU torch index"
    )


# ---------------------------------------------------------------------------
# First-start install must fit inside the HEALTHCHECK start-period (S232)
# ---------------------------------------------------------------------------


def _healthcheck_start_period_seconds(dockerfile_text: str) -> int:
    m = re.search(r"--start-period=(\d+)s", dockerfile_text)
    assert m is not None, "HEALTHCHECK must declare a --start-period=<N>s"
    return int(m.group(1))


def test_dockerfile_start_period_covers_measured_first_start(
    dockerfile_text: str,
) -> None:
    # S232: the first-start extras install reached 398-423s on an idle
    # machine — above the original 360s start-period, so the container's own
    # HEALTHCHECK marked it unhealthy while it was still legitimately
    # installing, causing spurious restart loops on fresh deployments.
    sp = _healthcheck_start_period_seconds(dockerfile_text)
    assert sp >= MIN_START_PERIOD_SECONDS, (
        f"HEALTHCHECK --start-period={sp}s is below the measured idle "
        f"first-start install (~398-423s); must be >= "
        f"{MIN_START_PERIOD_SECONDS}s so a still-installing container is not "
        "marked unhealthy (S232)"
    )


def test_docker_doc_cites_actual_start_period(dockerfile_text: str) -> None:
    # S232: the Docker user manual quotes the HEALTHCHECK start-period as a
    # readiness-budget guide for operators. It must match the Dockerfile's
    # actual value — a stale figure under-provisions orchestrator readiness
    # budgets and reintroduces the restart-loop the fix removes.
    sp = _healthcheck_start_period_seconds(dockerfile_text)
    doc = DOCKER_DOC.read_text()
    assert f"{sp}s start-period" in doc, (
        f"{DOCKER_DOC.name} must cite the Dockerfile's actual HEALTHCHECK "
        f"start-period ({sp}s), not a stale value"
    )


@pytest.mark.parametrize(
    "entry",
    [
        "__pycache__/",
        "*.pyc",
        "*.pyo",
        ".venv/",
        "dist/",
        "*.egg-info/",
        "tests/",
        "Documentation/",
        ".github/",
        ".pytest_cache/",
        ".coverage",
        "*.jsonl",
    ],
)
def test_dockerignore_excludes_spec_entry(dockerignore_text: str, entry: str) -> None:
    # Spec invariant #10: every entry the plan lists must be present.
    lines = {line.strip() for line in dockerignore_text.splitlines() if line.strip()}
    assert entry in lines, (
        f".dockerignore must contain `{entry}` per the Task 4.1 spec"
    )


@pytest.mark.parametrize(
    "entry",
    [
        # `__pycache__/` above only matches at the context root; the nested ones are host-compiled
        # bytecode the image does not need (pip compiles its own at install).
        "**/__pycache__/",
        # Repo-root tool caches: a local `.mypy_cache` alone was 102 MB of the 125 MB `COPY . /app` layer.
        ".mypy_cache/",
        ".ruff_cache/",
        ".obsidian/",
        ":memory:",
        ".brooks-lint-history.json",
        ".double-check.json",
    ],
)
def test_dockerignore_excludes_dev_tool_state(dockerignore_text: str, entry: str) -> None:
    lines = {line.strip() for line in dockerignore_text.splitlines() if line.strip()}
    assert entry in lines, f".dockerignore must keep `{entry}` out of the `COPY . /app` layer"

# Credential files that live next to the code on a developer machine. `COPY . /app` would bake any
# of them into an image layer (anyone who can pull the image can read it); git must never see them.
_SECRET_FILE_PATTERNS = (".env", ".env.*", ".search.env", ".secrets.env", "keys.json", "*.pem")
_ENV_EXAMPLE_KEEP = "!.env.example"


def _ignore_patterns(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")}


@pytest.mark.parametrize("pattern", _SECRET_FILE_PATTERNS)
def test_dockerignore_excludes_credential_files(dockerignore_text: str, pattern: str) -> None:
    assert pattern in _ignore_patterns(dockerignore_text), (
        f".dockerignore must keep `{pattern}` out of the `COPY . /app` layer"
    )


@pytest.mark.parametrize("pattern", _SECRET_FILE_PATTERNS)
def test_gitignore_excludes_credential_files(pattern: str) -> None:
    patterns = _ignore_patterns((REPO_ROOT / ".gitignore").read_text())
    assert pattern in patterns, f".gitignore must list `{pattern}` so credentials are never committed"


@pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
def test_ignore_files_keep_env_example_trackable(ignore_file: str) -> None:
    """`.env.*` also matches the shipped `.env.example`; it must be re-included after it."""
    lines = [line.strip() for line in (REPO_ROOT / ignore_file).read_text().splitlines()]
    assert _ENV_EXAMPLE_KEEP in lines, f"{ignore_file} must re-include `{_ENV_EXAMPLE_KEEP}` after `.env.*`"
    assert lines.index(_ENV_EXAMPLE_KEEP) > lines.index(".env.*"), (
        f"in {ignore_file} the `{_ENV_EXAMPLE_KEEP}` negation must come after `.env.*` or it has no effect"
    )



def test_dockerfile_prunes_installed_test_suites_in_the_install_layer(dockerfile_text: str) -> None:
    """Installed packages ship their own test suites (pandas, scipy, numpy, sympy, torch/test: ~240 MB
    of a ~2.4 GB site-packages) that nothing imports at runtime. They must be removed in the same
    ``RUN`` as the install: a later ``RUN rm`` would leave them in the layer below."""
    install_run = re.search(r"^RUN set -eux; \\\n\s+SETUPTOOLS_SCM_PRETEND_VERSION.*?^\s*esac\n", dockerfile_text, re.DOTALL | re.MULTILINE)
    assert install_run is not None, "the `pip install .` RUN block was not found"
    block = install_run.group(0)
    assert re.search(r"-name tests -o -name test", block), "the install RUN must prune `tests`/`test` directories"
    assert "purelib" in block and "platlib" in block, "prune the interpreter's own install dirs, not a hardcoded path"


def test_dockerfile_installs_the_shared_libraries_opencv_links(dockerfile_text: str) -> None:
    """``opencv-python`` (a dependency of rapidocr and docling) links libGL, libX11, libxcb and
    libglib at load time, and ``python:3.12-slim`` ships none of them: in the container
    ``import cv2`` raised ``libxcb.so.1: cannot open shared object file``, so RapidOCR (image and
    scanned-PDF OCR) and docling's table-structure model could not even be imported."""
    base_os_run = re.search(r"^RUN set -eux; \\\n\s+apt-get update; .*?^\s*rm -rf /var/lib/apt/lists/\*\n", dockerfile_text, re.DOTALL | re.MULTILINE)
    assert base_os_run is not None, "the base-OS `apt-get` RUN block was not found"
    block = base_os_run.group(0)
    for package in ("libxcb1", "libx11-6", "libxext6", "libglib2.0-0", "libgl1", "libglvnd0", "libglx0"):
        assert package in block, f"the base-OS RUN must install {package} (needed by cv2)"


def test_dockerfile_does_not_install_mesa_for_libgl(dockerfile_text: str) -> None:
    """``apt-get install libgl1`` pulls Mesa's software renderer through ``libglx0`` (LLVM, ~190 MB of
    a ~3 GB image); nothing in the server renders, so libGL's three glvnd packages are unpacked alone."""
    assert not re.search(r"apt-get install[^\n]*\blibgl1\b", dockerfile_text), (
        "install libgl1 with `apt-get download` + `dpkg --force-depends`, not `apt-get install`"
    )


def test_dockerfile_asserts_opencv_imports_at_build_time(dockerfile_text: str) -> None:
    """A missing system library must fail the build, not the first OCR request."""
    assert re.search(r"python3 -c ['\"]import cv2", dockerfile_text), (
        "the install RUN must `import cv2` so a missing shared library fails `docker build`"
    )
