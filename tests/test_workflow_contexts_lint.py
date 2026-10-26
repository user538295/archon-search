"""Workflow files must only use expression contexts that exist where they are written.

GitHub validates a workflow file before it creates a single job. A context used where it is not available ("Unrecognized
named-value") makes the *whole file* invalid: every push then records a zero-job failed run named after the file path,
whatever triggers the workflow declares, and the workflow never runs on its own schedule either.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))

# `jobs.<job_id>.env` sees github, needs, strategy, matrix, vars, secrets and inputs; `runner` and `env` exist only
# from the steps down (docs.github.com, "Contexts" availability table).
_UNAVAILABLE_IN_JOB_ENV = re.compile(r"\$\{\{[^}]*\b(runner|steps|env|job)\.")


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_no_job_level_env_uses_a_step_only_context(workflow: Path) -> None:
    jobs = yaml.safe_load(workflow.read_text())["jobs"]

    offenders = {
        f"{job_id}.env.{name}": value
        for job_id, job in jobs.items()
        for name, value in (job.get("env") or {}).items()
        if _UNAVAILABLE_IN_JOB_ENV.search(str(value))
    }

    assert not offenders, f"{workflow.name}: job-level env uses a context GitHub does not provide there: {offenders}"
