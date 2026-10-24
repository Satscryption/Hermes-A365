"""v1 compatibility matrix constants and doctor integration (#126)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_a365.compatibility import (
    HERMES_INTEGRATION_SMOKE_REFS,
    HERMES_MULTIPLEX_MIN_CALENDAR,
    HERMES_PLUGIN_MIN_CALENDAR,
    REQUIRES_PYTHON,
    format_calendar_tag,
    hermes_meets_multiplex_floor,
    hermes_meets_plugin_floor,
    parse_hermes_version_text,
    python_in_supported_range,
)
from hermes_a365.doctor import (
    probe_hermes_harness,
    probe_host_platform,
    probe_python_runtime,
)


def test_requires_python_matches_compatibility_module() -> None:
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert data["project"]["requires-python"] == REQUIRES_PYTHON


def test_python_range() -> None:
    assert python_in_supported_range((3, 11, 0))
    assert python_in_supported_range((3, 14, 9))
    assert python_in_supported_range((3, 15, 0))
    assert not python_in_supported_range((3, 10, 0))


def test_parse_hermes_versions() -> None:
    assert parse_hermes_version_text("v2026.4.30") == ("calendar", (2026, 4, 30))
    assert parse_hermes_version_text("Hermes Agent v0.22.2 (v2026.9.24)") == (
        "semver",
        (0, 22, 2),
    )
    assert parse_hermes_version_text("Hermes Agent v0.22.2 (2026.9.24)") == (
        "semver",
        (0, 22, 2),
    )
    assert parse_hermes_version_text("Hermes Agent v0.12.0") == ("semver", (0, 12, 0))
    assert parse_hermes_version_text("(2026.9.24)") == ("calendar", (2026, 9, 24))
    assert parse_hermes_version_text("unknown") is None


def test_hermes_floors() -> None:
    assert hermes_meets_plugin_floor(parse_hermes_version_text("v2026.4.30"))
    assert not hermes_meets_plugin_floor(parse_hermes_version_text("v2026.4.8"))
    assert hermes_meets_multiplex_floor(parse_hermes_version_text("v2026.6.19"))
    assert not hermes_meets_multiplex_floor(parse_hermes_version_text("v2026.6.5"))


def test_integration_smoke_refs_meet_floors() -> None:
    for ref in HERMES_INTEGRATION_SMOKE_REFS:
        cal = ref.lstrip("v").split(".")
        if len(cal) == 3 and cal[0].isdigit() and int(cal[0]) >= 2020:
            parts = (int(cal[0]), int(cal[1]), int(cal[2]))
            assert parts >= HERMES_PLUGIN_MIN_CALENDAR
            assert parts >= HERMES_MULTIPLEX_MIN_CALENDAR
        else:
            assert ref.startswith("v0.")


def test_probe_python_runtime_ok() -> None:
    result = probe_python_runtime()
    assert result.name == "python"
    assert result.state in {"ok", "warn", "error"}


def test_probe_host_platform_windows_errors() -> None:
    with patch("hermes_a365.doctor.sys.platform", "win32"):
        result = probe_host_platform()
    assert result.state == "error"
    assert "Windows" in result.detail


def test_probe_hermes_below_plugin_floor_errors_when_plugin_in_use() -> None:
    with (
        patch("hermes_a365.doctor.shutil.which", return_value="/bin/hermes"),
        patch("hermes_a365.doctor.safe_run", return_value="Hermes Agent v0.11.0\n"),
        patch("hermes_a365.doctor._hermes_agent365_plugin_in_use", return_value=True),
        patch(
            "hermes_a365.doctor._agent365_file_hosts_from_config",
            return_value=("unreadable", [], False),
        ),
    ):
        result = probe_hermes_harness()
    assert result.state == "error"
    assert format_calendar_tag(HERMES_PLUGIN_MIN_CALENDAR) in result.detail


def test_probe_hermes_below_plugin_floor_warns_for_standalone_cli() -> None:
    with (
        patch("hermes_a365.doctor.shutil.which", return_value="/bin/hermes"),
        patch("hermes_a365.doctor.safe_run", return_value="Hermes Agent v0.11.0\n"),
        patch("hermes_a365.doctor._hermes_agent365_plugin_in_use", return_value=False),
        patch(
            "hermes_a365.doctor._agent365_file_hosts_from_config",
            return_value=("unreadable", [], False),
        ),
    ):
        result = probe_hermes_harness()
    assert result.state == "warn"
    assert "standalone CLI" in result.detail


def test_runtime_guard_warns_on_unverified_python(monkeypatch: pytest.MonkeyPatch) -> None:
    import warnings

    from hermes_a365._runtime_guard import emit_runtime_warnings

    monkeypatch.setattr(
        "hermes_a365._runtime_guard.python_version_tuple",
        lambda: (3, 15, 0),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        emit_runtime_warnings()
    assert any("outside the CI-tested" in str(w.message) for w in caught)


def test_runtime_guard_warns_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    import warnings

    from hermes_a365._runtime_guard import emit_runtime_warnings

    monkeypatch.setattr("hermes_a365._runtime_guard.sys.platform", "win32")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        emit_runtime_warnings()
    assert any("Windows is unsupported" in str(w.message) for w in caught)


def test_probe_hermes_multiplex_floor_when_config_on() -> None:
    with (
        patch("hermes_a365.doctor.shutil.which", return_value="/bin/hermes"),
        patch("hermes_a365.doctor.safe_run", return_value="Hermes Agent v0.16.0\n"),
        patch(
            "hermes_a365.doctor._agent365_file_hosts_from_config",
            return_value=("key_absent", [], True),
        ),
    ):
        result = probe_hermes_harness()
    assert result.state == "error"
    assert "multiplex" in result.detail.lower()
