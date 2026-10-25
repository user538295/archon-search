"""Static linting of the docker-compose.yml + .env.example deliverables (Task 4.2).

These tests do not invoke `docker compose` — they only inspect the on-disk
files so they are safe to run on machines without a Docker daemon. They
guard the structural invariants required by the C9 container-support plan:
three services (dev/test/prod) with separate named volumes, unique port
mappings, `stop_grace_period: 30s`, API key via variable substitution
(never hardcoded), and the LanceDB single-writer / no-volume caveats
documented in comments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

EXPECTED_SERVICES = ("archon-dev", "archon-test", "archon-prod")
EXPECTED_VOLUMES = ("archon-dev-data", "archon-test-data", "archon-prod-data")
EXPECTED_PORT_MAP = {
    "archon-dev": ("18765", "8765"),
    "archon-test": ("18766", "8765"),
    "archon-prod": ("8765", "8765"),
}


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_FILE.read_text()


@pytest.fixture(scope="module")
def compose_doc(compose_text: str) -> dict:
    return yaml.safe_load(compose_text)


@pytest.fixture(scope="module")
def env_example_text() -> str:
    return ENV_EXAMPLE.read_text()


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def test_compose_file_exists() -> None:
    assert COMPOSE_FILE.exists(), f"{COMPOSE_FILE} not found"


def test_env_example_exists() -> None:
    # The plan mandates a `.env.example` companion file so operators know
    # which variables they can override.
    assert ENV_EXAMPLE.exists(), f"{ENV_EXAMPLE} not found"


# ---------------------------------------------------------------------------
# Services (spec: three named services dev/test/prod)
# ---------------------------------------------------------------------------


def test_compose_has_three_services(compose_doc: dict) -> None:
    services = compose_doc.get("services", {})
    assert isinstance(services, dict), "`services` must be a mapping"
    for name in EXPECTED_SERVICES:
        assert name in services, f"service `{name}` must be declared"


# ---------------------------------------------------------------------------
# Volumes (spec: separate named volumes per service)
# ---------------------------------------------------------------------------


def test_compose_separate_named_volumes(compose_doc: dict) -> None:
    services = compose_doc["services"]
    mounted: dict[str, str] = {}
    for svc_name in EXPECTED_SERVICES:
        volumes = services[svc_name].get("volumes", [])
        assert volumes, f"service `{svc_name}` must mount at least one volume"
        # Find the entry that targets `/data` (the canonical data mount).
        # Iterating instead of indexing [0] guards against future edits
        # that reorder the volumes list.
        data_mount: str | None = None
        for entry in volumes:
            assert isinstance(entry, str), (
                f"service `{svc_name}` must use the short-syntax volume entry"
            )
            name, _, container_path = entry.partition(":")
            # Strip any options suffix (e.g., `:ro`) before comparing.
            container_path = container_path.split(":")[0]
            if container_path == "/data":
                data_mount = name
                break
        assert data_mount is not None, (
            f"service `{svc_name}` must mount a named volume at /data exactly "
            f"(got {volumes!r})"
        )
        mounted[svc_name] = data_mount
    # All three names must be distinct.
    assert len(set(mounted.values())) == 3, (
        f"each service must mount its own named volume; got {mounted}"
    )


def test_compose_volumes_declared(compose_doc: dict) -> None:
    top_level_volumes = compose_doc.get("volumes", {})
    assert isinstance(top_level_volumes, dict), (
        "top-level `volumes:` block must be a mapping"
    )
    for name in EXPECTED_VOLUMES:
        assert name in top_level_volumes, (
            f"named volume `{name}` must be declared at the top level"
        )


def test_compose_keeps_the_first_start_extras_install_on_a_volume(compose_doc: dict) -> None:
    """Without a ``/pip-packages`` volume the extras install (internet needed) repeats after every ``down``/``up``."""
    mounted: dict[str, str] = {}
    for svc_name in EXPECTED_SERVICES:
        targets = {}
        for entry in compose_doc["services"][svc_name]["volumes"]:
            source, _, target = entry.partition(":")
            targets[target.split(":")[0]] = source
        assert "/pip-packages" in targets, f"service `{svc_name}` must mount a volume at /pip-packages"
        mounted[svc_name] = targets["/pip-packages"]
        assert mounted[svc_name] in compose_doc["volumes"], f"`{mounted[svc_name]}` must be declared at the top level"
    assert len(set(mounted.values())) == len(EXPECTED_SERVICES), f"each service needs its own volume; got {mounted}"


def test_env_example_explains_the_api_key_format_the_server_accepts(env_example_text: str) -> None:
    from archon_search.constants import MIN_API_KEY_LENGTH
    from archon_search.key_manager import api_key_problem

    assert "lowercase hex" in env_example_text
    assert f"at least {MIN_API_KEY_LENGTH}" in env_example_text
    assert "token_hex(32)" in env_example_text
    assert api_key_problem("0123456789abcdef" * 4) is None  # what the documented generator yields is accepted


# ---------------------------------------------------------------------------
# Stop grace period (spec invariant: 30s on every service)
# ---------------------------------------------------------------------------


def test_compose_stop_grace_period(compose_doc: dict) -> None:
    services = compose_doc["services"]
    for svc_name in EXPECTED_SERVICES:
        svc = services[svc_name]
        grace = svc.get("stop_grace_period")
        assert grace == "30s", (
            f"service `{svc_name}` must declare `stop_grace_period: 30s` "
            f"(got {grace!r})"
        )


# ---------------------------------------------------------------------------
# Port mapping (dev 18765, test 18766, prod 8765)
# ---------------------------------------------------------------------------


LOOPBACK_HOST = "127.0.0.1"


def test_compose_port_mappings(compose_doc: dict) -> None:
    services = compose_doc["services"]
    for svc_name, (host_port, container_port) in EXPECTED_PORT_MAP.items():
        ports = services[svc_name].get("ports", [])
        assert ports, f"service `{svc_name}` must declare a port mapping"
        first = ports[0]
        assert isinstance(first, str), (
            f"service `{svc_name}` must use short-syntax port mapping"
        )
        expected = f"{LOOPBACK_HOST}:{host_port}:{container_port}"
        assert first == expected, (
            f"service `{svc_name}` must publish `{expected}` (loopback only), got `{first}`"
        )


def test_compose_publishes_only_on_loopback(compose_doc: dict) -> None:
    """A bare `8765:8765` binds 0.0.0.0, exposing the plaintext API (and its key) to the whole LAN."""
    for svc_name, service in compose_doc["services"].items():
        for port in service.get("ports", []):
            assert isinstance(port, str) and port.startswith(f"{LOOPBACK_HOST}:"), (
                f"service `{svc_name}` publishes `{port}` on every host interface; "
                f"prefix it with `{LOOPBACK_HOST}:`"
            )


def test_compose_documents_remote_exposure_needs_explicit_override(compose_text: str) -> None:
    header = compose_text.split("services:", 1)[0].lower()
    assert "loopback" in header and "reverse proxy" in header and "override" in header, (
        "the compose header must say ports bind to loopback and that remote access needs an "
        "explicit override or a reverse proxy"
    )


# ---------------------------------------------------------------------------
# API key via variable substitution (spec invariant: never hardcoded)
# ---------------------------------------------------------------------------


def test_compose_api_key_uses_variable_substitution(compose_text: str) -> None:
    # The YAML parser resolves `${VAR}` to its default at load time, so we
    # must inspect the raw file content to verify the substitution syntax
    # is actually present (a hardcoded key would still parse to a string).
    assert "${ARCHON_SEARCH_API_KEY" in compose_text, (
        "docker-compose.yml must reference ARCHON_SEARCH_API_KEY via "
        "`${ARCHON_SEARCH_API_KEY:-...}` variable substitution"
    )
    # Every occurrence of `${ARCHON_SEARCH_API_KEY...}` MUST use the
    # empty-default form `${VAR:-}`. The bare form `${VAR}` would make
    # `docker compose up` *fail* when no key is set — breaking the
    # documented "auto-generate on first boot" contract. A non-empty
    # default would either bake a production secret into the file or
    # pin the key to a known literal. Both fail; only `:-` (empty) passes.
    bracket = re.compile(r"\$\{ARCHON_SEARCH_API_KEY([^}]*)\}")
    occurrences = bracket.findall(compose_text)
    assert occurrences, (
        "docker-compose.yml must reference ${ARCHON_SEARCH_API_KEY...} "
        "in at least one place"
    )
    for suffix in occurrences:
        # `suffix` is whatever sits between `ARCHON_SEARCH_API_KEY` and
        # the closing `}`. The only legal value is `:-` (empty default).
        assert suffix == ":-", (
            "docker-compose.yml must use `${ARCHON_SEARCH_API_KEY:-}` exactly. "
            f"Bare `${{ARCHON_SEARCH_API_KEY}}` makes compose fail when the "
            f"key is unset; `${{ARCHON_SEARCH_API_KEY:-<value>}}` bakes a "
            f"default into the file. Got suffix {suffix!r}."
        )


# ---------------------------------------------------------------------------
# DATA_DIR (spec invariant: all services read state from /data)
# ---------------------------------------------------------------------------


def test_compose_data_dir_set_to_slash_data(compose_doc: dict) -> None:
    services = compose_doc["services"]
    for svc_name in EXPECTED_SERVICES:
        env = services[svc_name].get("environment", {})
        # `environment` may be a list (`KEY=VALUE`) or a mapping. Normalize.
        if isinstance(env, list):
            env_map = {}
            for entry in env:
                key, _, value = entry.partition("=")
                env_map[key] = value
        else:
            env_map = dict(env)
        assert env_map.get("ARCHON_SEARCH_DATA_DIR") == "/data", (
            f"service `{svc_name}` must set ARCHON_SEARCH_DATA_DIR=/data"
        )


# ---------------------------------------------------------------------------
# Image (spec invariant: variable-substituted image with GHCR default)
# ---------------------------------------------------------------------------


def test_compose_image_uses_variable_substitution(compose_text: str) -> None:
    assert "${ARCHON_SEARCH_IMAGE" in compose_text, (
        "docker-compose.yml must use `${ARCHON_SEARCH_IMAGE:-...}` so "
        "operators can override the registry path without editing the file"
    )


# ---------------------------------------------------------------------------
# Restart policy (spec: prod = unless-stopped; dev/test = default)
# ---------------------------------------------------------------------------


def test_compose_prod_restart_unless_stopped(compose_doc: dict) -> None:
    prod = compose_doc["services"]["archon-prod"]
    assert prod.get("restart") == "unless-stopped", (
        "service `archon-prod` must declare `restart: unless-stopped`"
    )


def test_compose_dev_test_no_restart_policy(compose_doc: dict) -> None:
    for svc_name in ("archon-dev", "archon-test"):
        svc = compose_doc["services"][svc_name]
        assert "restart" not in svc, (
            f"service `{svc_name}` must NOT declare a restart policy "
            "(plan: default `no` for dev/test)"
        )


# ---------------------------------------------------------------------------
# Operator-facing comments (spec: must surface known limitations)
# ---------------------------------------------------------------------------


def test_compose_documents_tls_caveat(compose_text: str) -> None:
    # YAML comments are stripped by the parser, so we must inspect the raw
    # file. The plan mandates an operator-facing reminder that TLS
    # termination is not handled by this stack.
    assert "TLS termination" in compose_text, (
        "docker-compose.yml must include a comment noting that TLS "
        "termination is the operator's responsibility"
    )


def test_compose_documents_single_writer_caveat(compose_text: str) -> None:
    assert "single-writer" in compose_text.lower() or "LanceDB" in compose_text, (
        "docker-compose.yml must warn operators about the LanceDB "
        "single-writer invariant for the shared volume"
    )


def test_compose_documents_key_regeneration_caveat(compose_text: str) -> None:
    assert "regenerate" in compose_text.lower() or "regenerates" in compose_text, (
        "docker-compose.yml must warn that the API key regenerates on every "
        "start without a persistent volume"
    )


def test_compose_documents_model_cache_volume(compose_text: str) -> None:
    # The plan asks for a commented-out `archon-model-cache` volume with
    # an explanation. We only verify the marker word; the comment body is
    # operator-facing prose.
    assert "model cache" in compose_text.lower(), (
        "docker-compose.yml must document the optional model cache volume (commented-out template)"
    )


def test_compose_services_do_not_set_the_removed_fastembed_cache_env(compose_text: str) -> None:
    # PyTorch runtime (ADR 18): weights live in `<data dir>/models`, there is no
    # separate cache env var any more; a leftover would silently do nothing.
    override_text = COMPOSE_OVERRIDE_FILE.read_text()
    for text in (compose_text, override_text):
        assert "FASTEMBED_CACHE_PATH" not in text


def test_compose_archon_model_cache_volume_commented_out(
    compose_doc: dict, compose_text: str
) -> None:
    # Parallel guard to the active-volumes contract:
    # the parsed YAML must NOT declare `archon-model-cache` as an active
    # top-level volume, but the raw text MUST mention it (as a commented
    # template). This locks in the "opt-in only" contract on both sides.
    top_level_volumes = compose_doc.get("volumes", {}) or {}
    assert "archon-model-cache" not in top_level_volumes, (
        "Top-level `volumes:` must NOT declare `archon-model-cache` as "
        "active — it ships as a commented-out template that operators "
        "uncomment alongside the `archon-model-cache` mounts"
    )
    assert "archon-model-cache" in compose_text, (
        "docker-compose.yml must mention `archon-model-cache` in a "
        "commented-out template block so operators know how to wire it"
    )


# ---------------------------------------------------------------------------
# model cache wiring (the app downloads weights into `get_models_dir()`, i.e.
# `<ARCHON_SEARCH_DATA_DIR>/models`, so every cache mount must target that path)
# ---------------------------------------------------------------------------

COMPOSE_OVERRIDE_FILE = REPO_ROOT / "docker-compose.override.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
MODELS_SUBDIR = "models"
DATA_DIR_ENV = "ARCHON_SEARCH_DATA_DIR"
# Directories the Dockerfile.test `runner` user can create its data dir under.
RUNNER_WRITABLE_ROOTS = ("/tmp/", "/home/runner/")


def _image_env(dockerfile: Path) -> dict[str, str]:
    """``KEY=VALUE`` pairs set by the Dockerfile's ``ENV`` instructions."""
    return dict(re.findall(r"^\s*(?:ENV\s+)?([A-Z_]+)=(\S+?)\s*\\?$", dockerfile.read_text(), re.MULTILINE))


def _override_services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE_OVERRIDE_FILE.read_text())["services"]


def _effective_env(service: dict) -> dict[str, str]:
    """The image's ENV overlaid by the service's compose ``environment:``."""
    build = service.get("build", {})
    dockerfile = REPO_ROOT / build.get("dockerfile", "Dockerfile")
    return {**_image_env(dockerfile), **{k: str(v) for k, v in service.get("environment", {}).items()}}


def test_compose_opt_in_cache_lines_target_image_models_dir(compose_text: str) -> None:
    # The commented opt-in must mount the directory the image's server
    # actually downloads model weights into, one line per service.
    image_models_dir = f"{_image_env(DOCKERFILE)[DATA_DIR_ENV]}/{MODELS_SUBDIR}"
    mounts = re.findall(r"^\s*#\s*-\s*archon-model-cache:(\S+)\s*$", compose_text, re.MULTILINE)
    assert len(mounts) == len(EXPECTED_SERVICES), f"one opt-in mount per service, got {mounts!r}"
    wrong = [p for p in mounts if p != image_models_dir]
    assert not wrong, f"opt-in cache lines point at {wrong!r}, not the image's {image_models_dir}"


@pytest.mark.parametrize("service_name", sorted(_override_services()))
def test_override_service_mounts_its_models_dir(service_name: str) -> None:
    # Unmounted, the weights would land in the container layer and be re-downloaded
    # on every container recreate.
    service = _override_services()[service_name]
    models_dir = f"{_effective_env(service)[DATA_DIR_ENV]}/{MODELS_SUBDIR}"
    targets = [v.split(":")[1] for v in service.get("volumes", []) if ":" in v]
    assert models_dir in targets, (
        f"`{service_name}` loads model weights from {models_dir} but mounts nothing there (targets: {targets!r})"
    )


@pytest.mark.parametrize(
    "service_name",
    sorted(n for n, s in _override_services().items() if s.get("build", {}).get("dockerfile") == "Dockerfile.test"),
)
def test_test_image_service_data_dir_is_writable_by_runner(service_name: str) -> None:
    # Dockerfile.test runs as the non-root `runner` user and creates no /data, so a
    # data dir outside /tmp or its HOME makes a hand-started `archon-search serve` fail.
    data_dir = _effective_env(_override_services()[service_name])[DATA_DIR_ENV]
    assert data_dir.startswith(RUNNER_WRITABLE_ROOTS), (
        f"`{service_name}` sets {DATA_DIR_ENV}={data_dir}, which `runner` cannot create"
    )


# ---------------------------------------------------------------------------
# .env.example contents
# ---------------------------------------------------------------------------


def test_env_example_has_api_key_placeholder(env_example_text: str) -> None:
    assert "ARCHON_SEARCH_API_KEY" in env_example_text, (
        ".env.example must list ARCHON_SEARCH_API_KEY so operators know "
        "how to inject their key"
    )


def test_env_example_has_commented_local_image_override(env_example_text: str) -> None:
    # The plan mandates a commented-out local-build override so operators
    # can pivot from the GHCR default without editing docker-compose.yml.
    assert "ARCHON_SEARCH_IMAGE" in env_example_text, (
        ".env.example must mention ARCHON_SEARCH_IMAGE so operators can "
        "override the image path (e.g. for a local build)"
    )


def test_env_example_registry_path_uncommented(env_example_text: str) -> None:
    # MIS BE-2: the real GHCR registry path must appear on an uncommented line
    # so operators can see and pin the correct image without editing
    # docker-compose.yml.  Lines starting with '#' are excluded.
    uncommented_lines = [
        line for line in env_example_text.splitlines() if not line.startswith("#")
    ]
    assert any(
        "ghcr.io/user538295/archon-search" in line for line in uncommented_lines
    ), (
        ".env.example must contain an uncommented ARCHON_SEARCH_IMAGE line "
        "pointing to ghcr.io/user538295/archon-search so operators can pin "
        "a specific release tag"
    )


def test_env_example_no_active_api_key(env_example_text: str) -> None:
    # MIS BE-2: copying .env.example to .env must NOT silently set a shared
    # API key that defeats per-instance key isolation across Docker services.
    # Any ARCHON_SEARCH_API_KEY= line must be commented out (after optional
    # leading whitespace) or entirely absent.
    import re

    active_key_line = re.compile(r"^\s*ARCHON_SEARCH_API_KEY=.+", re.MULTILINE)
    assert not active_key_line.search(env_example_text), (
        ".env.example must not contain an uncommented ARCHON_SEARCH_API_KEY= "
        "line with a non-empty value — setting this env var defeats per-instance "
        "key isolation when multiple Docker services share the same .env file"
    )


# ---------------------------------------------------------------------------
# MIS cross-links and doc-index (spec: 150_multi_instance_setup.md is indexed
# and cross-linked from the running-server and docker guides)
# ---------------------------------------------------------------------------


def test_doc_index_contains_multi_instance_setup() -> None:
    index_file = REPO_ROOT / "Documentation/Architecture/990_documentation_index_and_contribution_guide.md"
    content = index_file.read_text()
    assert "150_multi_instance_setup" in content, (
        "990_documentation_index_and_contribution_guide.md must reference "
        "150_multi_instance_setup so the new user manual file is discoverable "
        "via the documentation index"
    )


def test_running_server_doc_cross_links_multi_instance_setup() -> None:
    doc_file = REPO_ROOT / "Documentation/UserManual/40_running_the_server.md"
    content = doc_file.read_text()
    assert "150_multi_instance_setup" in content, (
        "40_running_the_server.md must cross-link to 150_multi_instance_setup "
        "so operators running the server can discover the multi-instance guide"
    )


def test_docker_guide_cross_links_multi_instance_setup() -> None:
    doc_file = REPO_ROOT / "Documentation/UserManual/140_running_with_docker.md"
    content = doc_file.read_text()
    assert "150_multi_instance_setup" in content, (
        "140_running_with_docker.md must cross-link to 150_multi_instance_setup "
        "so operators following the Docker guide can discover the multi-instance setup"
    )
