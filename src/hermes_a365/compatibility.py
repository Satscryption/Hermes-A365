"""v1.0 support matrix constants and Hermes version parsing (#126).

The authoritative operator-facing table lives in
``references/v1-compatibility-matrix.md``. This module is what ``doctor``
and packaging metadata align against.
"""

from __future__ import annotations

import re
import sys
from typing import Final

# --- Python (standalone CLI / wheel) -----------------------------------------

PYTHON_VERSION_MIN: Final[tuple[int, int]] = (3, 11)
PYTHON_VERSION_MAX_EXCLUSIVE: Final[tuple[int, int]] = (3, 15)
PYTHON_VERSIONS_CI: Final[tuple[str, ...]] = ("3.11", "3.12", "3.13", "3.14")
REQUIRES_PYTHON: Final[str] = ">=3.11,<3.15"

# Hermes gateway venv (NousResearch/hermes-agent documents 3.14 as runtime).
HERMES_GATEWAY_PYTHON: Final[str] = "3.14"

# --- Host operating systems --------------------------------------------------

SUPPORTED_HOST_PLATFORMS: Final[frozenset[str]] = frozenset({"darwin", "linux"})
WINDOWS_KEYCHAIN_SUPPORTED: Final[bool] = False

# --- Hermes Agent (gateway plugin) -------------------------------------------

# Calendar-tag floors (``v2026.M.D``) documented in references.
HERMES_PLUGIN_MIN_CALENDAR: Final[tuple[int, int, int]] = (2026, 4, 30)
HERMES_MULTIPLEX_MIN_CALENDAR: Final[tuple[int, int, int]] = (2026, 6, 19)
HERMES_SESSION_PIN_RESOLUTION_MIN_CALENDAR: Final[tuple[int, int, int]] = (2026, 9, 7)

# Equivalent PyPI-style versions observed on those tags (for ``hermes --version``).
HERMES_PLUGIN_MIN_SEMVER: Final[tuple[int, int, int]] = (0, 12, 0)
HERMES_MULTIPLEX_MIN_SEMVER: Final[tuple[int, int, int]] = (0, 17, 0)
HERMES_SESSION_PIN_RESOLUTION_MIN_SEMVER: Final[tuple[int, int, int]] = (0, 21, 1)

# Integration smoke refs in CI (git tags on NousResearch/hermes-agent).
HERMES_INTEGRATION_SMOKE_REFS: Final[tuple[str, ...]] = ("v2026.6.19", "v0.22.2")

# --- Microsoft operator tooling (live-verified snapshots; doctor warns on drift) -

AZURE_CLI_MIN: Final[tuple[int, int, int]] = (2, 55, 0)
POWERSHELL_MIN_MAJOR: Final[int] = 7
DOTNET_RUNTIME_MIN: Final[tuple[int, int, int]] = (10, 0, 0)
A365_CLI_LIVE_VERIFIED: Final[tuple[int, int, int]] = (1, 1, 181)

# --- M365 manifest / program -------------------------------------------------

CEA_MANIFEST_VERSION: Final[str] = "1.27"
FRONTIER_PREVIEW_REQUIRED: Final[bool] = True

_CALENDAR_RE = re.compile(r"(?:\bv)?(2026)\.(\d+)\.(\d+)")
_SEMVER_RE = re.compile(r"(?:\bv)?(0)\.(\d+)\.(\d+)")


def python_version_tuple() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def python_in_supported_range(version: tuple[int, int, int] | None = None) -> bool:
    ver = version or python_version_tuple()
    if ver[:2] < PYTHON_VERSION_MIN:
        return False
    return ver[:2] < PYTHON_VERSION_MAX_EXCLUSIVE


def host_platform_supported(platform: str | None = None) -> bool:
    name = platform if platform is not None else sys.platform
    return name in SUPPORTED_HOST_PLATFORMS or name.startswith("linux")


def parse_hermes_version_text(text: str) -> tuple[str, tuple[int, ...]] | None:
    """Return ``('calendar', (2026, M, D))`` or ``('semver', (0, M, D))``."""

    calendar = _CALENDAR_RE.search(text)
    if calendar:
        return (
            "calendar",
            (int(calendar.group(1)), int(calendar.group(2)), int(calendar.group(3))),
        )
    semver = _SEMVER_RE.search(text)
    if semver:
        return (
            "semver",
            (int(semver.group(1)), int(semver.group(2)), int(semver.group(3))),
        )
    return None


def _calendar_ge(left: tuple[int, ...], right: tuple[int, int, int]) -> bool:
    return tuple(left) >= tuple(right)


def hermes_meets_plugin_floor(parsed: tuple[str, tuple[int, ...]] | None) -> bool | None:
    if parsed is None:
        return None
    kind, parts = parsed
    if kind == "calendar":
        return _calendar_ge(parts, HERMES_PLUGIN_MIN_CALENDAR)
    return parts >= HERMES_PLUGIN_MIN_SEMVER


def hermes_meets_multiplex_floor(parsed: tuple[str, tuple[int, ...]] | None) -> bool | None:
    if parsed is None:
        return None
    kind, parts = parsed
    if kind == "calendar":
        return _calendar_ge(parts, HERMES_MULTIPLEX_MIN_CALENDAR)
    return parts >= HERMES_MULTIPLEX_MIN_SEMVER


def hermes_meets_session_pin_resolution_floor(
    parsed: tuple[str, tuple[int, ...]] | None,
) -> bool | None:
    if parsed is None:
        return None
    kind, parts = parsed
    if kind == "calendar":
        return _calendar_ge(parts, HERMES_SESSION_PIN_RESOLUTION_MIN_CALENDAR)
    return parts >= HERMES_SESSION_PIN_RESOLUTION_MIN_SEMVER


def format_calendar_tag(parts: tuple[int, int, int]) -> str:
    return f"v{parts[0]}.{parts[1]}.{parts[2]}"
