"""Tests for hermes_a365.doctor — v0.2 with verified-real prereqs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_a365.bot_service import BotServiceConfig, CommandResult
from hermes_a365.bot_service_diagnostics import DiagnosticResult
from hermes_a365.doctor import (
    A365_CLI_SECRET_LATEST_AFFECTED_VERSION_TEXT,
    CUSTOM_CLIENT_APP_DOCS,
    DEFAULT_CLIENT_APP_NAME,
    FRONTIER_PROGRAM_URL,
    DoctorReport,
    ProbeResult,
    overall_to_exit_code,
    parse_a365_cli_version,
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
        with patch("hermes_a365.doctor.shutil.which", return_value=None):
            r = probe_a365_cli()
        assert r.state == "error"
        assert "a365 not found" in r.detail
        assert "dotnet tool install" in r.detail

    def test_old_version_warns_with_recovery_hint(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/Users/x/.dotnet/tools/a365"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value="Agent 365 Developer Tools CLI v1.1.171",
            ),
        ):
            r = probe_a365_cli()
        assert r.state == "warn"
        assert "v1.1.171" in r.detail
        assert A365_CLI_SECRET_LATEST_AFFECTED_VERSION_TEXT in r.detail
        assert "--auto-recover-secret" in r.detail

    def test_latest_known_affected_version_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/Users/x/.dotnet/tools/a365"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value="1.1.181+abcdef",
            ),
        ):
            r = probe_a365_cli()
        assert r.state == "warn"
        assert "1.1.181" in r.detail
        assert "--auto-recover-secret" in r.detail

    def test_newer_unverified_version_still_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/Users/x/.dotnet/tools/a365"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value="1.1.182+abcdef",
            ),
        ):
            r = probe_a365_cli()
        assert r.state == "warn"
        assert "not yet verified fixed" in r.detail
        assert "--auto-recover-secret" in r.detail

    def test_unknown_version_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/Users/x/.dotnet/tools/a365"),
            patch("hermes_a365.doctor.safe_run", return_value="Agent 365 CLI"),
        ):
            r = probe_a365_cli()
        assert r.state == "warn"
        assert "could not confirm" in r.detail


class TestParseA365CliVersion:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1.1.178+abcdef", (1, 1, 178)),
            ("Agent 365 Developer Tools CLI v1.1.171", (1, 1, 171)),
            ("cliVersion: 1.1.174.9623", (1, 1, 174)),
            ("Microsoft.Agents.A365.DevTools.Cli 1.1.124-preview", (1, 1, 124)),
            ("version unknown", None),
        ],
    )
    def test_extracts_semver(
        self, text: str, expected: tuple[int, int, int] | None
    ) -> None:
        assert parse_a365_cli_version(text) == expected


# ---------------------------------------------------------------------------
# probe_az_cli
# ---------------------------------------------------------------------------


class TestProbeAzCli:
    def test_missing_returns_error(self) -> None:
        with patch("hermes_a365.doctor.shutil.which", return_value=None):
            r = probe_az_cli()
        assert r.state == "error"

    def test_present_but_signed_out_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch("hermes_a365.doctor.safe_run", return_value=None),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "az login" in r.detail

    def test_present_and_signed_in_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch("hermes_a365.doctor.safe_run", return_value="alice@contoso.com\n"),
        ):
            r = probe_az_cli()
        assert r.state == "ok"
        assert "alice@contoso.com" in r.detail


# ---------------------------------------------------------------------------
# probe_powershell
# ---------------------------------------------------------------------------


class TestProbePowerShell:
    def test_missing_pwsh_errors(self) -> None:
        with patch("hermes_a365.doctor.shutil.which", return_value=None):
            r = probe_powershell()
        assert r.state == "error"
        assert "pwsh" in r.detail

    def test_v7_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/local/bin/pwsh"),
            patch("hermes_a365.doctor.safe_run", return_value="7.4.1"),
        ):
            r = probe_powershell()
        assert r.state == "ok"
        assert "7.4.1" in r.detail

    def test_pre_v7_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/local/bin/pwsh"),
            patch("hermes_a365.doctor.safe_run", return_value="5.1.22621"),
        ):
            r = probe_powershell()
        assert r.state == "warn"
        assert "requires 7+" in r.detail


# ---------------------------------------------------------------------------
# probe_custom_client_app
# ---------------------------------------------------------------------------


class TestProbeCustomClientApp:
    def test_az_missing_warns(self) -> None:
        with patch("hermes_a365.doctor.shutil.which", return_value=None):
            r = probe_custom_client_app()
        assert r.state == "warn"

    def test_app_present_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value="11111111-2222-3333-4444-555555555555",
            ),
        ):
            r = probe_custom_client_app()
        assert r.state == "ok"
        assert "11111111" in r.detail

    def test_app_absent_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch("hermes_a365.doctor.safe_run", return_value=""),
        ):
            r = probe_custom_client_app()
        assert r.state == "warn"
        assert "no Entra app" in r.detail
        assert CUSTOM_CLIENT_APP_DOCS in r.detail

    def test_az_query_failure_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch("hermes_a365.doctor.safe_run", return_value=None),
        ):
            r = probe_custom_client_app()
        assert r.state == "warn"
        assert "az not signed in" in r.detail

    def test_constants_pinned(self) -> None:
        assert DEFAULT_CLIENT_APP_NAME == "Agent 365 CLI"

    def test_env_var_overrides_default_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slice 18r (bug #3): operators with a custom client-app name can
        set A365_CLIENT_APP_NAME to skip the rename."""
        monkeypatch.setenv("A365_CLIENT_APP_NAME", "Agent365-CLI-OpenClaw")
        captured: list[list[str]] = []

        def fake_safe_run(argv: list[str], **_: object) -> str:
            captured.append(argv)
            return "11111111-2222-3333-4444-555555555555"

        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch("hermes_a365.doctor.safe_run", side_effect=fake_safe_run),
        ):
            r = probe_custom_client_app()
        assert r.state == "ok"
        # The query should have used the env-var name, not the default.
        assert "Agent365-CLI-OpenClaw" in captured[0]
        assert "Agent 365 CLI" not in captured[0]

    def test_explicit_name_arg_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_CLIENT_APP_NAME", "From-Env")
        captured: list[list[str]] = []

        def fake_safe_run(argv: list[str], **_: object) -> str:
            captured.append(argv)
            return "11111111-2222-3333-4444-555555555555"

        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch("hermes_a365.doctor.safe_run", side_effect=fake_safe_run),
        ):
            probe_custom_client_app(name="From-Arg")
        assert "From-Arg" in captured[0]
        assert "From-Env" not in captured[0]


# ---------------------------------------------------------------------------
# probe_keychain
# ---------------------------------------------------------------------------


class TestProbeKeychain:
    def test_macos_security_present_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.sys.platform", "darwin"),
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/security"),
        ):
            r = probe_keychain()
        assert r.state == "ok"
        assert "Security framework" in r.detail

    def test_linux_secret_tool_present_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.sys.platform", "linux"),
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/secret-tool"),
        ):
            r = probe_keychain()
        assert r.state == "ok"
        assert "libsecret" in r.detail

    def test_unsupported_platform_errors(self) -> None:
        with patch("hermes_a365.doctor.sys.platform", "win32"):
            r = probe_keychain()
        assert r.state == "error"
        assert "unsupported" in r.detail


# ---------------------------------------------------------------------------
# probe_network
# ---------------------------------------------------------------------------


class TestProbeNetwork:
    def test_all_reachable_ok(self) -> None:
        with patch("hermes_a365.doctor.tcp_reachable", return_value=True):
            r = probe_network(("a", "b"))
        assert r.state == "ok"

    def test_some_unreachable_errors(self) -> None:
        with patch("hermes_a365.doctor.tcp_reachable", side_effect=[True, False]):
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
        with patch("hermes_a365.doctor.shutil.which", return_value=None):
            r = probe_hermes_harness()
        assert r.state == "warn"

    def test_present_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/local/bin/hermes"),
            patch("hermes_a365.doctor.safe_run", return_value="Hermes Agent v0.12.0\n"),
        ):
            r = probe_hermes_harness()
        assert r.state == "ok"
        assert "Hermes Agent" in r.detail


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _write_bot_service_sidecar(tmp_path: Path) -> Path:
    sidecar = tmp_path / "a365.bot-service.config.json"
    sidecar.write_text(
        BotServiceConfig(
            schemaVersion=1,
            subscriptionId="33333333-3333-3333-3333-333333333333",
            resourceGroup="hermes-a365-bots",
            botName="hermes-inbox-helper-bot",
            armResourceId=(
                "/subscriptions/33333333-3333-3333-3333-333333333333"
                "/resourceGroups/hermes-a365-bots/providers/Microsoft.BotService"
                "/botServices/hermes-inbox-helper-bot"
            ),
            msaAppId="11111111-1111-1111-1111-111111111111",
            tenantId="22222222-2222-2222-2222-222222222222",
            messagingEndpoint="https://example.test/api/messages",
            channelsEnabled=["msteams"],
            createdAt="2026-05-18T12:30:00Z",
        ).to_json()
    )
    return sidecar


class _GreenBotServiceRunner:
    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        if argv[:3] == ["az", "account", "show"]:
            return CommandResult(
                argv,
                0,
                stdout=json.dumps({"id": "33333333-3333-3333-3333-333333333333"}),
            )
        if argv[:3] == ["az", "bot", "show"]:
            return CommandResult(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "properties": {
                            "msaAppId": "11111111-1111-1111-1111-111111111111",
                            "endpoint": "https://example.test/api/messages",
                        }
                    }
                ),
            )
        if argv[:4] == ["az", "bot", "msteams", "show"]:
            return CommandResult(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "properties": {
                            "properties": {
                                "acceptedTerms": True,
                                "isEnabled": True,
                            }
                        }
                    }
                ),
            )
        raise AssertionError(f"unexpected command: {argv}")


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

    def test_path_a_only_install_does_not_add_bot_service_probes(
        self, tmp_path: Path
    ) -> None:
        with (
            patch(
                "hermes_a365.doctor.probe_a365_cli",
                return_value=ProbeResult("a365_cli", "ok", ""),
            ),
            patch(
                "hermes_a365.doctor.probe_az_cli",
                return_value=ProbeResult("az_cli", "ok", ""),
            ),
            patch(
                "hermes_a365.doctor.probe_powershell",
                return_value=ProbeResult("powershell", "ok", ""),
            ),
            patch(
                "hermes_a365.doctor.probe_custom_client_app",
                return_value=ProbeResult("custom_client_app", "ok", ""),
            ),
            patch(
                "hermes_a365.doctor.probe_keychain",
                return_value=ProbeResult("keychain", "ok", ""),
            ),
            patch(
                "hermes_a365.doctor.probe_local_config",
                return_value=ProbeResult("local_config", "ok", ""),
            ),
            patch(
                "hermes_a365.doctor.probe_hermes_harness",
                return_value=ProbeResult("hermes_harness", "ok", ""),
            ),
        ):
            report = run_all_probes(
                no_network=True,
                bot_service_sidecar_path=tmp_path / "missing.json",
            )

        assert report.overall == "ok"
        assert all(not p.name.startswith("bot_service") for p in report.probes)

    def test_path_b_sidecar_adds_green_local_probes(self, tmp_path: Path) -> None:
        sidecar = _write_bot_service_sidecar(tmp_path)
        report = run_all_probes(
            no_network=True,
            bot_service_sidecar_path=sidecar,
            generated_config_path=tmp_path / "a365.generated.config.json",
            bot_service_operator_env={
                "A365_BF_APP_ID": "11111111-1111-1111-1111-111111111111"
            },
        )
        bot_results = [p for p in report.probes if p.name.startswith("bot_service")]

        assert [p.name for p in bot_results] == [
            "bot_service_config",
            "bot_service_msa_app_id",
        ]
        assert {p.state for p in bot_results} == {"ok"}

    def test_path_b_runtime_auth_failure_maps_to_doctor_error(
        self, tmp_path: Path
    ) -> None:
        sidecar = _write_bot_service_sidecar(tmp_path)

        report = run_all_probes(
            bot_service_sidecar_path=sidecar,
            generated_config_path=tmp_path / "a365.generated.config.json",
            bot_service_runner=_GreenBotServiceRunner(),
            bot_service_operator_env={
                "A365_BF_APP_ID": "11111111-1111-1111-1111-111111111111"
            },
            bot_service_runtime_auth_probe=lambda _config: DiagnosticResult(
                "bot_service_runtime_auth",
                "error",
                "standalone activity-bridge serve must dispatch BF-token replies",
            ),
        )

        runtime = next(p for p in report.probes if p.name == "bot_service_runtime_auth")
        assert runtime.state == "error"
        assert "activity-bridge serve" in runtime.detail
        assert "BF-token" in runtime.detail

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
