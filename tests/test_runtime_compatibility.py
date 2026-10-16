"""#148: keep the bridge security floors, and document the Hermes 3.14 override.

v0.9.0 ships against Python 3.14 without lowering pyjwt[crypto]>=2.15.0.
Until NousResearch/hermes-agent#134394 relaxes the upstream pins, the
combined install is `uv pip install --override overrides.txt`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
NOTE = ROOT / "references" / "hermes-runtime-compatibility.md"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
OVERRIDES = ROOT / "overrides.txt"
TEST_WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"

# Packages whose published minimums are security floors. The value is a
# version that must remain excluded, and a version that must remain allowed.
FLOORS = {
    "pyjwt": (Version("2.14.0"), Version("2.15.0")),
    "cryptography": (Version("49.0.0"), Version("50.0.0")),
    "anyio": (Version("4.14.1"), Version("4.14.2")),
    "fastapi": (Version("0.133.1"), Version("0.141.1")),
    "starlette": (Version("1.0.0"), Version("1.3.1")),
}

OVERRIDE_LINES = (
    "pyjwt[crypto]>=2.15.0\n"
    "fastapi>=0.143\n"
)


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text())["project"]


def _extra_requirements(extra: str) -> list[str]:
    return list(_project()["optional-dependencies"][extra])


def _specifier_for(requirements: list[str], name: str) -> SpecifierSet:
    for req in requirements:
        head = req.split(";", 1)[0].strip()
        package, sep, spec = head.partition(">=")
        package = package.split("[", 1)[0].strip().lower()
        if sep and package == name:
            return SpecifierSet(">=" + spec.strip())
    raise AssertionError(f"{name} floor missing from {requirements}")


def test_requires_python_includes_hermes_3_14_runtime() -> None:
    spec = SpecifierSet(_project()["requires-python"])
    assert Version("3.11") in spec
    assert Version("3.14") in spec
    assert Version("3.10") not in spec
    classifiers = _project()["classifiers"]
    assert "Programming Language :: Python :: 3.11" in classifiers
    assert "Programming Language :: Python :: 3.14" in classifiers


def test_ci_matrix_runs_3_11_and_3_14() -> None:
    text = TEST_WORKFLOW.read_text()
    assert 'python-version: ["3.11", "3.14"]' in text


def test_override_file_keeps_the_security_floors() -> None:
    text = OVERRIDES.read_text()
    assert text == OVERRIDE_LINES
    pyjwt = SpecifierSet(">=2.15.0")
    fastapi_override = SpecifierSet(">=0.143")
    assert Version("2.13.0") not in pyjwt
    assert Version("2.15.0") in pyjwt
    # Stricter than the published fastapi>=0.141.1 floor, so the Hermes web
    # pin is replaced without admitting 0.133.1 or dropping below 0.141.1.
    assert Version("0.133.1") not in fastapi_override
    assert Version("0.141.1") not in fastapi_override
    assert Version("0.143.0") in fastapi_override


def test_bridge_and_dev_keep_security_floors() -> None:
    for extra in ("bridge", "dev"):
        requirements = _extra_requirements(extra)
        for name, (rejected, allowed) in FLOORS.items():
            spec = _specifier_for(requirements, name)
            assert allowed in spec, f"{extra} {name} {spec} excludes {allowed}"
            assert rejected not in spec, f"{extra} {name} {spec} still allows {rejected}"
        joined = " ".join(requirements).lower()
        assert "pyjwt[crypto]>=" in joined
        # The Hermes override may require a newer FastAPI. Our published floor
        # stays the security minimum and is not raised or lowered here.
        assert _specifier_for(requirements, "fastapi") == SpecifierSet(">=0.141.1")


def test_docs_show_the_override_install_and_upstream_issue() -> None:
    for path in (NOTE, README, CHANGELOG):
        text = path.read_text()
        assert "pyjwt[crypto]>=2.15.0" in text
        assert "fastapi>=0.143" in text
        assert "uv pip install --override overrides.txt" in text
        assert "PyJWT[crypto]==2.13.0" in text
        assert "fastapi==0.133.1" in text
        assert "134394" in text
        assert "#148" in text or "issues/148" in text
    note = NOTE.read_text()
    assert "do not lower" in note.lower()
    assert "--no-deps" in note
    assert "e6899c8090da3bd48c829893e4103ce989d3482d" in note
    assert "f60f6d534375ccd6ec78d47ff1b5a97371cd8d60" in note


def test_documented_hermes_pins_do_not_satisfy_our_floors() -> None:
    """The note's cited pins are actually incompatible. A doc edit that
    claims 2.13.0 or FastAPI 0.133.1 is fine should fail here too."""
    bridge = _extra_requirements("bridge")
    pyjwt = _specifier_for(bridge, "pyjwt")
    fastapi = _specifier_for(bridge, "fastapi")
    assert Version("2.13.0") not in pyjwt
    assert Version("2.15.0") in pyjwt
    assert Version("0.133.1") not in fastapi
    assert Version("0.141.1") in fastapi
