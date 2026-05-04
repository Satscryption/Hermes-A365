"""Tests for scripts/doctor.py — v0.2 with verified-real prereqs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from doctor import (
    CUSTOM_CLIENT_APP_DOCS,
    DEFAULT_CLIENT_APP_NAME,
    FRONTIER_PROGRAM_URL,
    DoctorReport,
    ProbeResult,
    overall_to_exit_code,
    probe_a365_cli,
    probe_az_cli,
    probe_custom_client_app,
    probe_hermes_harness,
    probe_keychain,
    probe_local_config,
    probe_network,
    probe_powershell,
    render_human,
    render_json,
    run_all_probes,
)

# ---------------------------------------------------------------------------
# probe_a365_cli
# ---------------------------------------------------------------------------


class TestProbeA365Cli:
    def test_missing_binary_returns_error(self) -> None:
        with patch("doctor.shutil.which", return_value=None):
            r = probe_a365_cli()
        assert r.state == "error"
        assert "a365 not found" in r.detail
        assert "dotnet tool install" in r.detail

    def test_present_returns_ok_with_version(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/Users/x/.dotnet/tools/a365"),
            patch(
                "doctor.safe_run",
                return_value="Agent 365 Developer Tools CLI v1.1.171",
            ),
        ):
            r = probe_a365_cli()
        assert r.state == "ok"
        assert "v1.1.171" in r.detail


# ---------------------------------------------------------------------------
# probe_az_cli
# ---------------------------------------------------------------------------


class TestProbeAzCli:
    def test_missing_returns_error(self) -> None:
        with patch("doctor.shutil.which", return_value=None):
            r = probe_az_cli()
        assert r.state == "error"

    def test_present_but_signed_out_warns(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/usr/bin/az"),
            patch("doctor.safe_run", return_value=None),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "az login" in r.detail

    def test_present_and_signed_in_ok(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/usr/bin/az"),
            patch("doctor.safe_run", return_value="alice@contoso.com\n"),
        ):
            r = probe_az_cli()
        assert r.state == "ok"
        assert "alice@contoso.com" in r.detail


# ---------------------------------------------------------------------------
# probe_powershell
# ---------------------------------------------------------------------------


class TestProbePowerShell:
    def test_missing_pwsh_errors(self) -> None:
        with patch("doctor.shutil.which", return_value=None):
            r = probe_powershell()
        assert r.state == "error"
        assert "pwsh" in r.detail

    def test_v7_ok(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/usr/local/bin/pwsh"),
            patch("doctor.safe_run", return_value="7.4.1"),
        ):
            r = probe_powershell()
        assert r.state == "ok"
        assert "7.4.1" in r.detail

    def test_pre_v7_warns(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/usr/local/bin/pwsh"),
            patch("doctor.safe_run", return_value="5.1.22621"),
        ):
            r = probe_powershell()
        assert r.state == "warn"
        assert "requires 7+" in r.detail


# ---------------------------------------------------------------------------
# probe_custom_client_app
# ---------------------------------------------------------------------------


class TestProbeCustomClientApp:
    def test_az_missing_warns(self) -> None:
        with patch("doctor.shutil.which", return_value=None):
            r = probe_custom_client_app()
        assert r.state == "warn"

    def test_app_present_ok(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/usr/bin/az"),
            patch("doctor.safe_run", return_value="11111111-2222-3333-4444-555555555555"),
        ):
            r = probe_custom_client_app()
        assert r.state == "ok"
        assert "11111111" in r.detail

    def test_app_absent_warns(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/usr/bin/az"),
            patch("doctor.safe_run", return_value=""),
        ):
            r = probe_custom_client_app()
        assert r.state == "warn"
        assert "no Entra app" in r.detail
        assert CUSTOM_CLIENT_APP_DOCS in r.detail

    def test_az_query_failure_warns(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/usr/bin/az"),
            patch("doctor.safe_run", return_value=None),
        ):
            r = probe_custom_client_app()
        assert r.state == "warn"
        assert "az not signed in" in r.detail

    def test_constants_pinned(self) -> None:
        assert DEFAULT_CLIENT_APP_NAME == "Agent 365 CLI"


# ---------------------------------------------------------------------------
# probe_keychain
# ---------------------------------------------------------------------------


class TestProbeKeychain:
    def test_macos_security_present_ok(self) -> None:
        with (
            patch("doctor.sys.platform", "darwin"),
            patch("doctor.shutil.which", return_value="/usr/bin/security"),
        ):
            r = probe_keychain()
        assert r.state == "ok"
        assert "Security framework" in r.detail

    def test_linux_secret_tool_present_ok(self) -> None:
        with (
            patch("doctor.sys.platform", "linux"),
            patch("doctor.shutil.which", return_value="/usr/bin/secret-tool"),
        ):
            r = probe_keychain()
        assert r.state == "ok"
        assert "libsecret" in r.detail

    def test_unsupported_platform_errors(self) -> None:
        with patch("doctor.sys.platform", "win32"):
            r = probe_keychain()
        assert r.state == "error"
        assert "unsupported" in r.detail


# ---------------------------------------------------------------------------
# probe_network
# ---------------------------------------------------------------------------


class TestProbeNetwork:
    def test_all_reachable_ok(self) -> None:
        with patch("doctor.tcp_reachable", return_value=True):
            r = probe_network(("a", "b"))
        assert r.state == "ok"

    def test_some_unreachable_errors(self) -> None:
        with patch("doctor.tcp_reachable", side_effect=[True, False]):
            r = probe_network(("a", "b"))
        assert r.state == "error"
        assert r.data["unreachable"] == ["b"]


# ---------------------------------------------------------------------------
# probe_local_config
# ---------------------------------------------------------------------------


class TestProbeLocalConfig:
    def test_env_absent_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        r = probe_local_config()
        assert r.state == "warn"
        assert ".env: absent" in r.detail

    def test_env_present_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("A365_TENANT_ID=t\nA365_APP_ID=a\n")
        r = probe_local_config()
        assert r.state == "ok"
        assert ".env: 2 keys" in r.detail


# ---------------------------------------------------------------------------
# probe_hermes_harness
# ---------------------------------------------------------------------------


class TestProbeHermesHarness:
    def test_missing_warns(self) -> None:
        with patch("doctor.shutil.which", return_value=None):
            r = probe_hermes_harness()
        assert r.state == "warn"

    def test_present_ok(self) -> None:
        with (
            patch("doctor.shutil.which", return_value="/usr/local/bin/hermes"),
            patch("doctor.safe_run", return_value="Hermes Agent v0.12.0\n"),
        ):
            r = probe_hermes_harness()
        assert r.state == "ok"
        assert "Hermes Agent" in r.detail


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class TestRunAllProbes:
    def test_includes_all_probes_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        report = run_all_probes()
        names = [p.name for p in report.probes]
        for required in (
            "a365_cli",
            "az_cli",
            "powershell",
            "custom_client_app",
            "network",
            "keychain",
            "local_config",
            "hermes_harness",
        ):
            assert required in names

    def test_no_network_skips_network_probe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        report = run_all_probes(no_network=True)
        names = [p.name for p in report.probes]
        assert "network" not in names

    @pytest.mark.parametrize(
        "states,expected",
        [
            (["ok", "ok"], "ok"),
            (["ok", "warn"], "warn"),
            (["warn", "error"], "error"),
        ],
    )
    def test_overall_aggregation(self, states: list[str], expected: str) -> None:
        report = DoctorReport(
            probes=[
                ProbeResult(name=f"x{i}", state=s, detail="")  # type: ignore[arg-type]
                for i, s in enumerate(states)
            ]
        )
        assert report.overall == expected

    def test_exit_code_mapping(self) -> None:
        assert overall_to_exit_code("ok") == 0
        assert overall_to_exit_code("warn") == 1
        assert overall_to_exit_code("error") == 2


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_human_includes_overall_and_frontier_url(self) -> None:
        report = DoctorReport(
            probes=[ProbeResult(name="x", state="ok", detail="d")]  # type: ignore[arg-type]
        )
        text = render_human(report)
        assert "overall: ok" in text
        assert FRONTIER_PROGRAM_URL in text
        assert "setup requirements" in text

    def test_json_round_trip(self) -> None:
        report = DoctorReport(
            probes=[ProbeResult(name="a", state="ok", detail="d")]  # type: ignore[arg-type]
        )
        payload = json.loads(render_json(report))
        assert payload["overall"] == "ok"
        assert payload["probes"][0]["name"] == "a"


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_constants_pinned() -> None:
    assert FRONTIER_PROGRAM_URL.startswith("https://adoption.microsoft.com/")
    assert "custom-client-app-registration" in CUSTOM_CLIENT_APP_DOCS
