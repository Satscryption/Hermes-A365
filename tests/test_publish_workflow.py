"""Security contract tests for the PyPI publication workflow."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
_TEST_WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "test.yml"


def _workflow() -> dict:
    # BaseLoader preserves GitHub's `on` key and keeps expression scalars intact.
    return yaml.load(_WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def _needs(job: dict) -> set[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return {needs}
    return set(needs)


def _steps(job: dict) -> list[dict]:
    return list(job.get("steps", []))


def test_publish_job_uses_protected_pypi_environment() -> None:
    workflow = _workflow()
    publish = workflow["jobs"]["publish"]

    assert publish["environment"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    assert "startsWith(github.ref, 'refs/tags/v')" in publish["if"]
    assert _needs(publish) == {"build", "test"}


def test_approval_test_reaches_gate_but_cannot_publish() -> None:
    workflow = _workflow()
    approval_input = workflow["on"]["workflow_dispatch"]["inputs"]["approval_test"]
    publish = workflow["jobs"]["publish"]
    steps = {step["name"]: step for step in _steps(publish)}
    pypi_steps = [
        step
        for job in workflow["jobs"].values()
        for step in _steps(job)
        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
    ]

    assert approval_input["type"] == "boolean"
    assert approval_input["default"] == "false"
    assert "inputs.approval_test == true" in publish["if"]

    pypi_step = steps["Publish to PyPI"]
    assert pypi_steps == [pypi_step]
    assert pypi_step["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert "inputs.approval_test != true" in pypi_step["if"]
    assert pypi_step["with"]["skip-existing"] == "true"

    confirmation = steps["Confirm approval test completed without publishing"]
    assert "inputs.approval_test == true" in confirmation["if"]


def test_build_and_test_jobs_never_receive_oidc_permission() -> None:
    workflow = _workflow()
    text = _WORKFLOW_PATH.read_text()

    assert workflow["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["integrity"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["test"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["test"]["uses"] == "./.github/workflows/test.yml"
    for name, job in workflow["jobs"].items():
        permissions = job.get("permissions", {})
        if name == "publish":
            assert permissions.get("id-token") == "write"
        else:
            assert "id-token" not in permissions
    assert "--require-hashes" in text
    assert "--no-isolation" in text


def test_integrity_runs_before_the_protected_environment() -> None:
    workflow = _workflow()
    integrity = workflow["jobs"]["integrity"]
    provenance = next(
        step for step in _steps(integrity) if step.get("name") == "Verify release provenance"
    )
    command = provenance["run"]

    assert provenance["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert "git fetch --no-tags origin main" in command
    assert "scripts/release_integrity.py check" in command
    assert '--sha "$RELEASE_SHA"' in command
    assert _needs(workflow["jobs"]["build"]) == {"integrity"}
    assert _needs(workflow["jobs"]["test"]) == {"integrity"}
    assert "environment" not in integrity


def test_release_job_finalizes_after_pypi_without_pushing_tags() -> None:
    workflow = _workflow()
    release = workflow["jobs"]["release"]
    text = _WORKFLOW_PATH.read_text()
    checkout = next(
        step
        for step in _steps(release)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    finalize = next(
        step for step in _steps(release) if step.get("name") == "Finalize the GitHub Release"
    )

    assert _needs(release) == {"publish"}
    assert release["if"] == (
        "startsWith(github.ref, 'refs/tags/v') && "
        "(github.event_name != 'workflow_dispatch' || inputs.approval_test != true)"
    )
    assert "always()" not in release["if"]
    assert release["permissions"] == {"contents": "write"}
    assert "environment" not in release
    assert "id-token" not in release["permissions"]
    assert checkout["with"]["persist-credentials"] == "false"
    assert "publish-release" in finalize["run"]
    assert '--sha "$RELEASE_SHA"' in finalize["run"]
    assert finalize["env"]["RELEASE_SHA"] == "${{ github.sha }}"
    assert "git push" not in text
    assert "git tag" not in text
    environments = [
        job["environment"] for job in workflow["jobs"].values() if "environment" in job
    ]
    assert environments == ["pypi"]


def test_digest_verification_survives_smoke_and_gates_the_upload() -> None:
    workflow = _workflow()
    publish = workflow["jobs"]["publish"]
    verify = next(
        step for step in _steps(publish) if step.get("name") == "Verify artefact digests"
    )
    script = verify["run"]
    publish_names = [step.get("name") for step in _steps(publish)]

    assert "SHA256SUMS must be a regular file" in script
    assert "unsafe SHA256SUMS path" in script
    assert '"/" in name' in script
    assert "duplicate SHA256SUMS path" in script
    assert "exactly one wheel and one sdist" in script
    assert "cd dist" in script
    assert "sha256sum --check --strict SHA256SUMS" in script
    assert "rm dist/SHA256SUMS" in script
    assert publish_names.index("Verify artefact digests") < publish_names.index("Publish to PyPI")

    build = workflow["jobs"]["build"]
    build_steps = {step.get("name"): step for step in _steps(build)}
    build_names = [step.get("name") for step in _steps(build)]
    record = build_steps["Record artefact digests"]["run"]
    rebuild = build_steps["Build sdist + wheel"]["run"]
    recheck = build_steps["Re-verify artefact digests after smoke"]["run"]

    assert 'export SOURCE_DATE_EPOCH="$epoch"' in rebuild
    assert "git log -1 --format=%ct HEAD" in rebuild
    assert "cd dist" in record
    assert "-printf '%P\\0'" in record
    assert "sha256sum --" in record
    assert "SHA256SUMS.before-smoke" in record
    assert "cd dist" in recheck
    assert 'sha256sum --check --strict "$RUNNER_TEMP/SHA256SUMS.before-smoke"' in recheck
    assert "cmp dist/SHA256SUMS" in recheck
    assert build_names.index("Record artefact digests") < build_names.index(
        "Smoke-install the wheel"
    )
    assert build_names.index("Smoke-install the wheel") < build_names.index(
        "Re-verify artefact digests after smoke"
    )
    assert build_names.index("Re-verify artefact digests after smoke") < build_names.index(
        "Upload artefacts for the publish job"
    )


def test_smoke_installs_hash_pinned_packaging_for_version_compare() -> None:
    workflow = _workflow()
    smoke = next(
        step
        for step in _steps(workflow["jobs"]["build"])
        if step.get("name") == "Smoke-install the wheel"
    )
    requirements = (_WORKFLOW_PATH.parent / "publish-requirements.txt").read_text()
    lines = requirements.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("packaging=="))
    hashes: list[str] = []
    for line in lines[start:]:
        if line[:1].isalpha() and not line.startswith("packaging=="):
            break
        hashes.extend(re.findall(r"sha256:[0-9a-f]{64}", line))

    command = smoke["run"]
    assert "--require-hashes" in command
    assert "--no-deps" in command
    assert "packaging==26.2" in command
    assert len(hashes) >= 2
    assert all(digest in command for digest in hashes)
    wheel_install, packaging_install = command.split("bin/pip\" install", maxsplit=2)[1:]
    assert "dist/*.whl" in wheel_install
    assert "--require-hashes" not in wheel_install
    assert "packaging==26.2" in packaging_install


def test_called_test_workflow_keeps_python_3_14() -> None:
    text = _TEST_WORKFLOW_PATH.read_text()

    assert "workflow_call:" in text
    assert 'python-version: ["3.11", "3.14"]' in text
    assert "permissions:\n  contents: read\n" in text
