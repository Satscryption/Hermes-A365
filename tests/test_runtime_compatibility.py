"""#148: keep the bridge security floors, and keep the Hermes conflict documented.

The compatible path is an upstream Hermes pin bump. These tests fail if a
future change lowers our floors or stops documenting the pins that conflict
with them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
NOTE = ROOT / "references" / "hermes-runtime-compatibility.md"

# Packages whose published minimums are security floors. The value is a
# version that must remain excluded, and a version that must remain allowed.
FLOORS = {
    "pyjwt": (Version("2.14.0"), Version("2.15.0")),
    "cryptography": (Version("49.0.0"), Version("50.0.0")),
    "anyio": (Version("4.14.1"), Version("4.14.2")),
    "fastapi": (Version("0.133.1"), Version("0.141.1")),
    "starlette": (Version("1.0.0"), Version("1.3.1")),
}


def _extra_requirements(extra: str) -> list[str]:
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    return list(project["optional-dependencies"][extra])


def _specifier_for(requirements: list[str], name: str) -> SpecifierSet:
    for req in requirements:
        head = req.split(";", 1)[0].strip()
        package, sep, spec = head.partition(">=")
        package = package.split("[", 1)[0].strip().lower()
        if sep and package == name:
            return SpecifierSet(">=" + spec.strip())
    raise AssertionError(f"{name} floor missing from {requirements}")


def test_bridge_and_dev_keep_security_floors() -> None:
    for extra in ("bridge", "dev"):
        requirements = _extra_requirements(extra)
        for name, (rejected, allowed) in FLOORS.items():
            spec = _specifier_for(requirements, name)
            assert allowed in spec, f"{extra} {name} {spec} excludes {allowed}"
            assert rejected not in spec, f"{extra} {name} {spec} still allows {rejected}"
        joined = " ".join(requirements).lower()
        assert "pyjwt[crypto]>=" in joined


def test_compatibility_note_refuses_to_lower_the_pyjwt_floor() -> None:
    text = NOTE.read_text()
    assert "pyjwt[crypto]" in text
    assert ">=2.15.0" in text
    assert "PyJWT[crypto]==2.13.0" in text
    assert "fastapi==0.133.1" in text
    assert "do not" in text.lower()
    assert "override" in text
    assert "--no-deps" in text
    assert "e6899c8090da3bd48c829893e4103ce989d3482d" in text
    assert "f60f6d534375ccd6ec78d47ff1b5a97371cd8d60" in text


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
