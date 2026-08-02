"""Static linting of the Dockerfile + .dockerignore deliverables (Task 4.1).

These tests do not build or run any container — they only inspect the
on-disk files so they are safe to run on machines without a Docker daemon.
They guard the structural invariants required by the C9 container-support
plan: non-root user UID 1000, tini entrypoint, healthcheck without curl,
container env vars, local-source install, GPU swap gating, and the
.dockerignore excluding the .git tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
UV_LOCK = REPO_ROOT / "uv.lock"
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


def test_dockerfile_has_fastembed_cache_env(dockerfile_text: str) -> None:
    # Without FASTEMBED_CACHE_PATH the model weights download into the
    # ephemeral container layer and are lost on every container recreate.
    assert "FASTEMBED_CACHE_PATH=/data/fastembed-cache" in dockerfile_text, (
        "Dockerfile must set FASTEMBED_CACHE_PATH so fastembed weights "
        "persist on the mounted volume"
    )


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


def test_dockerfile_gpu_swap_only_on_nvidia_base(dockerfile_text: str) -> None:
    # Spec invariant #11: the onnxruntime → onnxruntime-gpu swap must be
    # gated on the NVIDIA CUDA base — running it on the CPU base would
    # break CPU image inference.
    assert "onnxruntime-gpu" in dockerfile_text, (
        "Dockerfile must install onnxruntime-gpu for the GPU variant"
    )
    # The swap must be inside a `case ${BASE_IMAGE} in *nvidia/cuda*)` guard.
    # The Dockerfile has more than one such case block (the CPU torch-index
    # guard is another — S269), so locate the block by its content rather
    # than assuming there is exactly one.
    case_blocks = re.findall(
        r"case\s+\"?\$\{?BASE_IMAGE\}?\"?\s+in[\s\S]*?esac",
        dockerfile_text,
    )
    swap_block = next((b for b in case_blocks if "onnxruntime-gpu" in b), None)
    assert swap_block is not None, (
        "GPU swap must be guarded by a `case ${BASE_IMAGE} in ... esac` block"
    )
    assert "*nvidia/cuda*" in swap_block, (
        "GPU swap guard must match `*nvidia/cuda*` (the documented GPU base)"
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


def _uvlock_package_block(name: str) -> str:
    """Return the `[[package]]` TOML block for *name* from uv.lock.

    Anchors on the ``[[package]]\\nname = "<name>"`` header so it never
    matches a ``{ name = "<name>" }`` *dependency* entry inside some other
    package's block.
    """
    text = UV_LOCK.read_text()
    header = re.search(
        rf'^\[\[package\]\]\nname = "{re.escape(name)}"$', text, re.MULTILINE
    )
    assert header is not None, f"uv.lock has no package block for {name!r}"
    start = header.start()
    nxt = text.find("[[package]]", header.end())
    return text[start : nxt if nxt != -1 else len(text)]


def test_uvlock_torch_pulls_cuda_stack_on_linux() -> None:
    # Ground truth for S269: `torch` (a transitive core dependency via
    # docling) declares nvidia-*/cuda-* dependencies gated on linux. This is
    # WHY the CPU image must route torch through the PyTorch CPU wheel index —
    # the default PyPI linux wheel drags the ~6 GB CUDA runtime a CPU host
    # cannot execute. If torch ever stops depending on CUDA on linux this
    # guard fails and the CPU-index fix can be revisited.
    block = _uvlock_package_block("torch")
    cuda_dep = re.search(
        r'name = "(?:nvidia-[a-z0-9-]+|cuda-[a-z0-9-]+)"[^\n]*'
        r"sys_platform == 'linux'",
        block,
    )
    assert cuda_dep is not None, (
        "Expected torch to declare an nvidia-*/cuda-* dependency gated on "
        "`sys_platform == 'linux'` in uv.lock (root cause of S269)"
    )


def test_dockerfile_cpu_base_resolves_cpu_only_torch(dockerfile_text: str) -> None:
    # S269: the CPU image is documented "CPU inference only" yet resolved
    # torch + torchvision + ~18 nvidia/cuda packages (~6.2 GB). The fix must
    # point pip at the PyTorch CPU wheel index so torch resolves to a
    # `+cpu` build with no nvidia/cuda dependencies. Both the bake-time
    # `pip install .` and the entrypoint's runtime `pip install .[extras]`
    # must pick it up (e.g. via a baked /etc/pip.conf).
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
