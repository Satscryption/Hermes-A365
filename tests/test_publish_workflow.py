"""Security contract tests for the PyPI publication workflow."""

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"


def _workflow() -> dict:
    # BaseLoader preserves GitHub's `on` key and keeps expression scalars intact.
    return yaml.load(_WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def test_publish_job_uses_protected_pypi_environment() -> None:
    workflow = _workflow()
    publish = workflow["jobs"]["publish"]

    assert publish["environment"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    assert "startsWith(github.ref, 'refs/tags/v')" in publish["if"]


def test_approval_test_reaches_gate_but_cannot_publish() -> None:
    workflow = _workflow()
    approval_input = workflow["on"]["workflow_dispatch"]["inputs"]["approval_test"]
    publish = workflow["jobs"]["publish"]
    steps = {step["name"]: step for step in publish["steps"]}
    pypi_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
    ]

    assert approval_input["type"] == "boolean"
    assert approval_input["default"] == "false"
    assert "inputs.approval_test == true" in publish["if"]

    pypi_step = steps["Publish to PyPI"]
    assert pypi_steps == [pypi_step]
    assert pypi_step["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert "inputs.approval_test != true" in pypi_step["if"]

    confirmation = steps["Confirm approval test completed without publishing"]
    assert "inputs.approval_test == true" in confirmation["if"]


def test_build_job_never_receives_oidc_permission() -> None:
    workflow = _workflow()
    build = workflow["jobs"]["build"]
    provenance = next(
        step
        for step in build["steps"]
        if step.get("name") == "Verify release provenance"
    )

    assert build["permissions"] == {"contents": "read"}
    assert provenance["if"] == "startsWith(github.ref, 'refs/tags/v')"
