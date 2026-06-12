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
    assert 'ENTRYPOINT ["tini", "--"]' in dockerfile_text, (
        'Dockerfile must use ENTRYPOINT ["tini", "--"] to forward signals'
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
    case_block = re.search(
        r"case\s+\"?\$\{?BASE_IMAGE\}?\"?\s+in[\s\S]*?esac",
        dockerfile_text,
    )
    assert case_block is not None, (
        "GPU swap must be guarded by a `case ${BASE_IMAGE} in ... esac` block"
    )
    assert "*nvidia/cuda*" in case_block.group(0), (
        "GPU swap guard must match `*nvidia/cuda*` (the documented GPU base)"
    )
    assert "onnxruntime-gpu" in case_block.group(0), (
        "GPU swap must install onnxruntime-gpu INSIDE the case guard, not "
        "unconditionally"
    )


# ---------------------------------------------------------------------------
# .dockerignore (spec invariant #10)
# ---------------------------------------------------------------------------


def test_dockerignore_excludes_git(dockerignore_text: str) -> None:
    lines = {line.strip() for line in dockerignore_text.splitlines() if line.strip()}
    assert any(entry in lines for entry in (".git", ".git/")), (
        ".dockerignore must exclude the .git directory"
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
