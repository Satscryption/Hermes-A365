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
PYTHON_VERSIONS_CI: Final[tuple[str, ...]] = ("3.11", "3.12", "3.13", "3.14")
REQUIRES_PYTHON: Final[str] = ">=3.11"

# Hermes gateway venv (NousResearch/hermes-agent documents 3.14 as runtime).
HERMES_GATEWAY_PYTHON: Final[str] = "3.14"

# --- Host operating systems --------------------------------------------------

SUPPORTED_HOST_PLATFORMS: Final[frozenset[str]] = frozenset({"darwin", "linux"})
WINDOWS_KEYCHAIN_SUPPORTED: Final[bool] = False

# --- Hermes Agent (gateway plugin) -------------------------------------------

# Calendar-tag floors (``vYYYY.M.D``) documented in references.
# Plugin baseline aligns with Hermes Agent 0.12.0+ / the Teams adapter ship tag
# ``v2026.4.30`` (not a separate agent365 import break).
HERMES_PLUGIN_MIN_CALENDAR: Final[tuple[int, int, int]] = (2026, 4, 30)
HERMES_MULTIPLEX_MIN_CALENDAR: Final[tuple[int, int, int]] = (2026, 6, 19)
HERMES_SESSION_PIN_RESOLUTION_MIN_CALENDAR: Final[tuple[int, int, int]] = (2026, 9, 7)

# Equivalent PyPI-style versions observed on those tags (for ``hermes --version``).
HERMES_PLUGIN_MIN_SEMVER: Final[tuple[int, int, int]] = (0, 12, 0)
HERMES_MULTIPLEX_MIN_SEMVER: Final[tuple[int, int, int]] = (0, 17, 0)
HERMES_SESSION_PIN_RESOLUTION_MIN_SEMVER: Final[tuple[int, int, int]] = (0, 21, 1)

# Integration smoke refs in CI (git tags on NousResearch/hermes-agent).
HERMES_INTEGRATION_SMOKE_REFS: Final[tuple[str, ...]] = ("v2026.6.19", "v0.22.5")

# Plugin-floor evidence (calendar tag only; below the multiplex floor).
HERMES_PLUGIN_FLOOR_SMOKE_REF: Final[str] = (
    f"v{HERMES_PLUGIN_MIN_CALENDAR[0]}."
    f"{HERMES_PLUGIN_MIN_CALENDAR[1]}."
    f"{HERMES_PLUGIN_MIN_CALENDAR[2]}"
)

# --- Microsoft operator tooling (live-verified snapshots; doctor warns on drift) -

AZURE_CLI_MIN: Final[tuple[int, int, int]] = (2, 55, 0)
# Agent 365 CLI prerequisites: .NET 8.0 or later (Microsoft Learn, agent-365-cli).
DOTNET_RUNTIME_MIN: Final[tuple[int, int, int]] = (8, 0, 0)
POWERSHELL_MIN_MAJOR: Final[int] = 7
A365_CLI_LIVE_VERIFIED: Final[tuple[int, int, int]] = (1, 1, 181)

# --- M365 manifest / program -------------------------------------------------

CEA_MANIFEST_VERSION: Final[str] = "1.27"
FRONTIER_PREVIEW_REQUIRED: Final[bool] = True

_SEMVER_RE = re.compile(r"(?:\bv)?(0)\.(\d+)\.(\d+)")
_PAREN_CALENDAR_RE = re.compile(r"\((\d{4})\.(\d+)\.(\d+)\)")
_YEAR_CALENDAR_RE = re.compile(r"(?:\bv)?(\d{4})\.(\d+)\.(\d+)")


def python_version_tuple() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def python_in_supported_range(version: tuple[int, int, int] | None = None) -> bool:
    ver = version or python_version_tuple()
    return ver[:2] >= PYTHON_VERSION_MIN


def python_in_ci_matrix(version: tuple[int, int, int] | None = None) -> bool:
    ver = version or python_version_tuple()
    label = f"{ver[0]}.{ver[1]}"
    return label in PYTHON_VERSIONS_CI


def host_platform_supported(platform: str | None = None) -> bool:
    name = platform if platform is not None else sys.platform
    return name in SUPPORTED_HOST_PLATFORMS or name.startswith("linux")


def parse_hermes_version_text(text: str) -> tuple[str, tuple[int, ...]] | None:
    """Return ``('calendar', (Y, M, D))`` or ``('semver', (0, M, D))``.

    Prefer the semantic ``v0.M.D`` token from ``hermes --version`` (for example
    ``Hermes Agent v0.22.2 (2026.9.24)``). Use a parenthetical or bare calendar
    tag only when no semver token is present.
    """

    semver = _SEMVER_RE.search(text)
    if semver:
        return (
            "semver",
            (int(semver.group(1)), int(semver.group(2)), int(semver.group(3))),
        )
    paren = _PAREN_CALENDAR_RE.search(text)
    if paren:
        return (
            "calendar",
            (int(paren.group(1)), int(paren.group(2)), int(paren.group(3))),
        )
    calendar = _YEAR_CALENDAR_RE.search(text)
    if calendar:
        year = int(calendar.group(1))
        if year >= 2020:
            return (
                "calendar",
                (
                    year,
                    int(calendar.group(2)),
                    int(calendar.group(3)),
                ),
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
