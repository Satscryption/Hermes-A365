"""#148: keep the bridge security floors; document Hermes resolve + overrides.

v0.9.0+ ships against Python 3.14 without lowering pyjwt[crypto]>=2.15.0.
Hermes >= v0.22.3 pins PyJWT 2.15.1; overrides.txt keeps pyjwt for older refs
and fastapi for hermes-agent[web].
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
NOTE = ROOT / "references" / "hermes-runtime-compatibility.md"
MATRIX = ROOT / "references" / "v1-compatibility-matrix.md"
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
    "idna": (Version("3.14"), Version("3.15")),
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


def test_requires_python_matches_v1_matrix() -> None:
    from hermes_a365.compatibility import REQUIRES_PYTHON

    assert _project()["requires-python"] == REQUIRES_PYTHON
    spec = SpecifierSet(REQUIRES_PYTHON)
    assert Version("3.11") in spec
    assert Version("3.14") in spec
    assert Version("3.10") not in spec
    assert Version("3.15") in spec
    classifiers = _project()["classifiers"]
    for minor in (11, 12, 13, 14):
        assert f"Programming Language :: Python :: 3.{minor}" in classifiers
    assert "Operating System :: MacOS" in classifiers
    assert "Operating System :: POSIX :: Linux" in classifiers


def test_ci_matrix_covers_supported_python_versions() -> None:
    text = TEST_WORKFLOW.read_text()
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert f'python-version: "{version}"' in text
    assert "macos-latest" in text
    assert "windows-negative" in text
    assert "keychain-linux" in text
    assert "wheel-smoke" in text
    assert "hermes-source-smoke" in text
    assert "  unit:" in text
    assert "  test:" in text
    assert "needs:" in text and "unit" in text
    assert "--timeout=60" in text
    assert "--timeout-method=thread" in text
    assert "--randomly-seed=" in text
    assert "--durations=15" in text
    assert "timeout-minutes: 15" in text
    assert "v2026.6.19" in text
    assert "v0.22.5" in text


def test_v1_compatibility_matrix_is_linked_from_readme() -> None:
    readme = README.read_text()
    assert "references/v1-compatibility-matrix.md" in readme
    matrix = MATRIX.read_text()
    assert "Snapshot date: 2026-10-24" in matrix
    assert "Windows" in matrix and "Not supported" in matrix


def test_override_file_keeps_the_security_floors() -> None:
    text = OVERRIDES.read_text()
    assert text == OVERRIDE_LINES
    pyjwt = SpecifierSet(">=2.15.0")
    fastapi_override = SpecifierSet(">=0.143")
    assert Version("2.13.0") not in pyjwt
    assert Version("2.15.0") in pyjwt
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
        assert _specifier_for(requirements, "fastapi") == SpecifierSet(">=0.141.1")
        assert _specifier_for(requirements, "idna") == SpecifierSet(">=3.15")
    note = NOTE.read_text()
    assert "| `idna` | `>=3.15` |" in note


def test_handoff_contract_pin_names_hermes_session_apis() -> None:
    """#82 — v2026.7.20 skips the pin; acquire resolves from v2026.9.7."""
    text = NOTE.read_text()
    assert "75efd73961a88a531276d0e2e1cd30d1ec18019e" in text
    assert "00ce5f04d9cfad2e30d5e08e7a6bf135f6e53030" in text
    assert "d39c62409b5f00da6f7bd31dd3fb437d8c2daad6" in text
    assert "db339f0051e76733923d3d8c4e4319e3e45444a3" in text
    assert "session_store.switch_session" in text
    assert 'metadata["gateway_session_id"]' in text
    assert "find_session_by_origin" in text
    assert "hermes_state_registry.acquire" in text
    assert "v2026.7.20" in text
    assert "v2026.9.7" in text
    assert "**Safety floor.**" in text
    assert "**Resolution floor.**" in text
    assert "does not call `reopen_session`" in text


def test_docs_show_override_install_and_hermes_v0225_pyjwt() -> None:
    for path in (NOTE, CHANGELOG):
        text = path.read_text()
        assert "pyjwt[crypto]>=2.15.0" in text
        assert "fastapi>=0.143" in text
        assert "uv pip install --override overrides.txt" in text
        assert "fastapi==0.133.1" in text
        assert "134394" in text
        assert "refs #148" in text or "Refs [#148]" in text
    note = NOTE.read_text()
    assert "do not lower" in note.lower()
    assert "--no-deps" in note
    assert "v0.22.5" in note
    assert "v0.22.3" in note
    assert "PyJWT[crypto]==2.15.1" in note
    assert "hermes-agent[web]" in note
    assert "no-op" in note.lower() or "no-op" in note
    assert "fastapi==0.162.0" in note
    assert "0.19.0" in note
    readme = README.read_text()
    assert "pyjwt[crypto]>=2.15.0" in readme
    assert "fastapi>=0.143" in readme
    assert "uv pip install --override overrides.txt" in readme
    assert "PyJWT[crypto]==2.15.1" in readme or "v0.22.5" in readme
    assert "no 0.22.x release" in readme.lower() or "has no 0.22" in readme.lower()
    assert "refs" in readme.lower() and "#148" in readme
    for chunk in note.split("."):
        if "134394" not in chunk or "fastapi" not in chunk.lower():
            continue
        lowered = chunk.lower()
        assert (
            "is not the fastapi exit" in lowered or "does not mention fastapi" in lowered
        ), chunk


def test_documented_hermes_pins_do_not_satisfy_our_floors() -> None:
    """The note's cited web-extra pin is still incompatible. A doc edit that
    claims FastAPI 0.133.1 is fine should fail here too."""
    bridge = _extra_requirements("bridge")
    pyjwt = _specifier_for(bridge, "pyjwt")
    fastapi = _specifier_for(bridge, "fastapi")
    assert Version("2.13.0") not in pyjwt
    assert Version("2.15.0") in pyjwt
    assert Version("0.133.1") not in fastapi
    assert Version("0.141.1") in fastapi
