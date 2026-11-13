"""Tests for hermes_a365.doctor — v0.2 with verified-real prereqs."""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from hermes_a365.bot_service import BotServiceConfig, CommandResult
from hermes_a365.bot_service_diagnostics import DiagnosticResult
from hermes_a365.doctor import (
    A365_CLI_SECRET_LATEST_AFFECTED_VERSION_TEXT,
    CUSTOM_CLIENT_APP_DOCS,
    DEFAULT_CLIENT_APP_NAME,
    FRONTIER_PROGRAM_URL,
    DoctorReport,
    ProbeResult,
    _gateway_has_plaintext_secret_coverage,
    overall_to_exit_code,
    parse_a365_cli_version,
    parse_az_cli_release,
    parse_az_cli_version,
    parse_dotnet_list_runtimes,
    probe_a365_cli,
    probe_agent_instance_allowlist,
    probe_az_cli,
    probe_custom_client_app,
    probe_dotnet_runtime,
    probe_file_transfer,
    probe_hermes_harness,
    probe_keychain,
    probe_local_config,
    probe_network,
    probe_powershell,
    render_human,
    render_json,
    run_all_probes,
)
from hermes_a365.secrets_provider import KeychainSecretsProvider

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
    def test_extracts_semver(self, text: str, expected: tuple[int, int, int] | None) -> None:
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
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.67.0"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("ok", None),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "az login" in r.detail
        assert "2.67.0" in r.detail

    def test_signed_out_includes_old_version(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.54.0"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("ok", None),
            ),
        ):
            r = probe_az_cli()
        assert "2.54.0" in r.detail
        assert "≥ 2.55.0" in r.detail

    def test_present_and_signed_in_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.67.0"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("ok", "alice@contoso.com"),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "ok"
        assert "alice@contoso.com" in r.detail
        assert "2.67.0" in r.detail

    def test_signed_in_below_floor_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.54.0"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("ok", "alice@contoso.com"),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "2.54.0" in r.detail
        assert "≥ 2.55.0" in r.detail

    def test_signed_in_prerelease_at_floor_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.55.0b1"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("ok", "alice@contoso.com"),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "2.55.0b1" in r.detail
        assert "≥ 2.55.0" in r.detail

    def test_signed_in_prerelease_above_floor_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.60.0b1"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("ok", "alice@contoso.com"),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "ok"
        assert "2.60.0b1" in r.detail

    def test_account_show_timeout_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.67.0"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("timeout", None),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "timed out" in r.detail
        assert "2.67.0" in r.detail
        assert "≥ 2.55.0" not in r.detail

    def test_account_show_oserror_includes_version_and_floor_when_below(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.54.0"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("failed", None),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "could not run `az account show`" in r.detail
        assert "2.54.0" in r.detail
        assert "≥ 2.55.0" in r.detail

    def test_account_show_timeout_includes_floor_when_version_below(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value='{"azure-cli": "2.54.0"}',
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("timeout", None),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "timed out" in r.detail
        assert "2.54.0" in r.detail
        assert "≥ 2.55.0" in r.detail


class TestParseAzCliVersion:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('{"azure-cli": "2.67.0", "azure-cli-core": "2.67.0"}', (2, 67, 0)),
            ('{"azure-cli": "2.55.0"}', (2, 55, 0)),
            ('{"azure-cli": "2.55.0-rc1"}', (2, 55, 0)),
            ('{"azure-cli": "2.55.0b1"}', (2, 55, 0)),
            ('{"azure-cli": "2.55.0-b1"}', (2, 55, 0)),
            ('{"azure-cli": "2.55.0.dev1"}', (2, 55, 0)),
            ('{"azure-cli": "2.55.0-dev"}', (2, 55, 0)),
            ('{"azure-cli": "2.55.0~b1"}', (2, 55, 0)),
            ('{"azure-cli": "2.55.0~rc1"}', (2, 55, 0)),
            ('{"azure-cli": "2.55.0rc1"}', (2, 55, 0)),
            ('{"azure-cli": "2.60.0rc1"}', (2, 60, 0)),
            ("not json", None),
            ("azure-cli    2.55.0", None),
        ],
    )
    def test_extracts_semver(self, text: str, expected: tuple[int, int, int] | None) -> None:
        assert parse_az_cli_version(text) == expected

    @pytest.mark.parametrize(
        ("raw", "expected_triple", "expected_prerelease"),
        [
            ("2.55.0", (2, 55, 0), False),
            ("2.55.0-b1", (2, 55, 0), True),
            ("2.55.0.dev1", (2, 55, 0), True),
            ("2.55.0-dev", (2, 55, 0), True),
            ("2.55.0~b1", (2, 55, 0), True),
            ("2.55.0~rc1", (2, 55, 0), True),
            ("2.55.0b1", (2, 55, 0), True),
            ("2.55.0rc1", (2, 55, 0), True),
            ("2.60.0rc1", (2, 60, 0), True),
            ("2.54.0rc1", (2, 54, 0), True),
            ("2.55.0a1", (2, 55, 0), True),
            ("2.55.0-b.1", (2, 55, 0), True),
            ("2.55.0~b.1", (2, 55, 0), True),
            ("2.55.0-developer", (2, 55, 0), False),
        ],
    )
    def test_parse_az_cli_release_prerelease_flags(
        self,
        raw: str,
        expected_triple: tuple[int, int, int],
        expected_prerelease: bool,
    ) -> None:
        triple, is_prerelease = parse_az_cli_release(raw)
        assert triple == expected_triple
        assert is_prerelease is expected_prerelease

    @pytest.mark.parametrize(
        ("raw", "expect_floor_in_detail"),
        [
            ("2.60.0rc1", False),
            ("2.55.0rc1", True),
            ("2.54.0rc1", True),
            ("2.55.0a1", True),
            ("2.55.0-b.1", True),
            ("2.55.0~b.1", True),
            ("2.55.0-developer", False),
        ],
    )
    def test_signed_in_prerelease_rc_floor_probe(
        self, raw: str, expect_floor_in_detail: bool
    ) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch(
                "hermes_a365.doctor.safe_run",
                return_value=json.dumps({"azure-cli": raw}),
            ),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("ok", "alice@contoso.com"),
            ),
        ):
            r = probe_az_cli()
        if expect_floor_in_detail:
            assert r.state == "warn"
            assert raw in r.detail
            assert "≥ 2.55.0" in r.detail
        else:
            assert r.state == "ok"
            assert raw in r.detail

    def test_extension_first_document_uses_azure_cli_only(self) -> None:
        text = json.dumps(
            {
                "extensions": {"devcenter": "8.0.0"},
                "azure-cli": "2.54.0",
            }
        )
        assert parse_az_cli_version(text) == (2, 54, 0)

    def test_trailing_stderr_warning_line(self) -> None:
        text = '{"azure-cli": "2.67.0"}\nWARNING: credential protection is on\n'
        assert parse_az_cli_version(text) == (2, 67, 0)

    def test_pretty_json_with_warnings_before_and_after(self) -> None:
        text = (
            "WARNING: AAZ credential protection is on\n"
            "{\n"
            '  "extensions": {"devcenter": "9.0.0"},\n'
            '  "azure-cli": "2.67.0"\n'
            "}\n"
            "WARNING: trailing diagnostic\n"
        )
        assert parse_az_cli_version(text) == (2, 67, 0)

    def test_pretty_json_probe_reads_version_not_extensions(self) -> None:
        stream = (
            "WARNING: leading\n"
            "{\n"
            '  "extensions": {"devcenter": "8.0.0"},\n'
            '  "azure-cli": "2.54.0"\n'
            "}\n"
        )
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/az"),
            patch("hermes_a365.doctor.safe_run", return_value=stream),
            patch(
                "hermes_a365.doctor._az_account_show_user",
                return_value=("ok", "alice@contoso.com"),
            ),
        ):
            r = probe_az_cli()
        assert r.state == "warn"
        assert "2.54.0" in r.detail
        assert "8.0.0" not in r.detail


# ---------------------------------------------------------------------------
# probe_dotnet_runtime
# ---------------------------------------------------------------------------


class TestProbeDotnetRuntime:
    def test_missing_dotnet_warns(self) -> None:
        with patch("hermes_a365.doctor.shutil.which", return_value=None):
            r = probe_dotnet_runtime()
        assert r.state == "warn"
        assert "dotnet not found" in r.detail

    def test_runtime_8_ok(self) -> None:
        runtimes = "Microsoft.NETCore.App 8.0.10 [/usr/share/dotnet/shared]\n"
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/dotnet"),
            patch("hermes_a365.doctor.safe_run", return_value=runtimes),
        ):
            r = probe_dotnet_runtime()
        assert r.state == "ok"
        assert "8.0.10" in r.detail

    def test_runtime_below_8_warns(self) -> None:
        runtimes = "Microsoft.NETCore.App 6.0.29 [/usr/share/dotnet/shared]\n"
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/dotnet"),
            patch("hermes_a365.doctor.safe_run", return_value=runtimes),
        ):
            r = probe_dotnet_runtime()
        assert r.state == "warn"
        assert "6.0.29" in r.detail
        assert "≥ 8.0" in r.detail

    def test_list_runtimes_failure_warns(self) -> None:
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/dotnet"),
            patch("hermes_a365.doctor.safe_run", return_value=None),
        ):
            r = probe_dotnet_runtime()
        assert r.state == "warn"
        assert "failed or timed out" in r.detail

    def test_preview_only_runtime_warns(self) -> None:
        runtimes = "Microsoft.NETCore.App 8.0.0-preview.7 [/usr/share/dotnet/shared]\n"
        with (
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/dotnet"),
            patch("hermes_a365.doctor.safe_run", return_value=runtimes),
        ):
            r = probe_dotnet_runtime()
        assert r.state == "warn"
        assert "prerelease" in r.detail


class TestParseDotnetListRuntimes:
    def test_picks_highest_stable_netcore_app(self) -> None:
        text = (
            "Microsoft.AspNetCore.App 8.0.10 [/x]\n"
            "Microsoft.NETCore.App 8.0.10 [/x]\n"
            "Microsoft.NETCore.App 10.0.0 [/x]\n"
            "Microsoft.NETCore.App 8.0.0-rc.1 [/x]\n"
        )
        scan = parse_dotnet_list_runtimes(text)
        assert scan.best_stable == (10, 0, 0)
        assert scan.best_prerelease == (8, 0, 0)


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

    def test_env_var_overrides_default_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_explicit_name_arg_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert "writes require" in r.detail

    def test_linux_secret_tool_present_ok(self) -> None:
        with (
            patch("hermes_a365.doctor.sys.platform", "linux"),
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/secret-tool"),
        ):
            r = probe_keychain()
        assert r.state == "ok"
        assert "libsecret" in r.detail

    def test_linux_secret_tool_present_runs_despite_plaintext_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _write_mode(home / "config.yaml", _agent365_config())
        _write_mode(home / ".env", _path_a_profile())
        monkeypatch.setenv("HERMES_HOME", str(home))
        with (
            patch("hermes_a365.doctor.sys.platform", "linux"),
            patch("hermes_a365.doctor.shutil.which", return_value="/usr/bin/secret-tool"),
        ):
            r = probe_keychain()
        assert r.state == "ok"
        assert r.state != "skipped"

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
        assert r.data["tenant_pinned"] is True

    def test_env_without_tenant_pin_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("A365_APP_ID=a\n")

        r = probe_local_config()

        assert r.state == "error"
        assert "no canonical tenant pin was resolved" in r.detail
        assert r.data["tenant_pinned"] is False


# ---------------------------------------------------------------------------
# probe_file_transfer (#76 / R4)
# ---------------------------------------------------------------------------


class TestProbeFileTransfer:
    def test_env_fallback_enabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # config present but the profile key is absent → env is the fallback.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  platforms:\n    agent365:\n      enabled: true\n"
        )
        monkeypatch.setenv("A365_FILE_HOST_ALLOWLIST", "contoso.sharepoint.com")
        r = probe_file_transfer()
        assert r.state == "ok"
        assert r.data["source"] == "env"
        assert r.data["host_count"] == 1

    def test_profile_beats_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mirrors the adapter: a present profile key wins; env is ignored.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("A365_FILE_HOST_ALLOWLIST", "stale-env.sharepoint.com")
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  platforms:\n    agent365:\n      extra:\n"
            "        file_host_allowlist:\n          - profile.sharepoint.com\n"
        )
        r = probe_file_transfer()
        assert r.data["source"] == "profile"
        assert r.data["host_count"] == 1

    def test_explicit_empty_profile_beats_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The P2 bug: a profile that explicitly disables files (empty list) is NOT
        # re-enabled by a stale env var — reported disabled, not enabled.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("A365_FILE_HOST_ALLOWLIST", "stale-env.sharepoint.com")
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  platforms:\n    agent365:\n      extra:\n        file_host_allowlist: []\n"
        )
        r = probe_file_transfer()
        assert r.data["source"] == "profile-empty"
        assert r.data["host_count"] == 0

    def test_profile_config_enabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The documented profile source must NOT be misreported as disabled.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("A365_FILE_HOST_ALLOWLIST", raising=False)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n"
            "  platforms:\n"
            "    agent365:\n"
            "      extra:\n"
            "        file_host_allowlist:\n"
            "          - contoso.sharepoint.com\n"
            "          - contoso-my.sharepoint.com\n"
        )
        r = probe_file_transfer()
        assert r.state == "ok"
        assert r.data["source"] == "profile"
        assert r.data["host_count"] == 2

    def test_indeterminate_when_no_config_and_no_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no config.yaml
        monkeypatch.delenv("A365_FILE_HOST_ALLOWLIST", raising=False)
        r = probe_file_transfer()
        assert r.state == "ok"
        assert r.data["source"] == "indeterminate"

    def test_disabled_when_config_present_but_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("A365_FILE_HOST_ALLOWLIST", raising=False)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  platforms:\n    agent365:\n      enabled: true\n"
        )
        r = probe_file_transfer()
        assert r.state == "ok"
        assert r.data["source"] == "none"

    def test_null_profile_key_falls_back_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `file_host_allowlist:` (explicit null) is key_absent to the adapter → env.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("A365_FILE_HOST_ALLOWLIST", "env.sharepoint.com")
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  platforms:\n    agent365:\n      extra:\n        file_host_allowlist:\n"
        )
        r = probe_file_transfer()
        assert r.data["source"] == "env"

    def test_scalar_misconfig_reads_as_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Red-team catch: a scalar value must not crash the probe (list(int)).
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("A365_FILE_HOST_ALLOWLIST", raising=False)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  platforms:\n    agent365:\n      extra:\n        file_host_allowlist: 5\n"
        )
        r = probe_file_transfer()
        assert r.state == "ok"
        assert r.data["source"] == "profile-empty"

    def test_multiplex_reports_indeterminate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Red-team catch: under multiplex, only the default profile is visible, so
        # a determinate verdict would mislead — report indeterminate.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("A365_FILE_HOST_ALLOWLIST", "env.sharepoint.com")
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  multiplex_profiles: true\n  platforms:\n    agent365:\n"
            "      extra:\n        file_host_allowlist:\n          - d.sharepoint.com\n"
        )
        r = probe_file_transfer()
        assert r.data["source"] == "indeterminate-multiplex"


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
            patch("hermes_a365.doctor.safe_run", return_value="Hermes Agent v0.22.2\n"),
            patch(
                "hermes_a365.doctor._agent365_file_hosts_from_config",
                return_value=("unreadable", [], False),
            ),
        ):
            r = probe_hermes_harness()
        assert r.state == "ok"
        assert "Hermes Agent" in r.detail


# ---------------------------------------------------------------------------
# probe_agent_instance_allowlist
# ---------------------------------------------------------------------------


class TestProbeAgentInstanceAllowlist:
    def test_warns_when_instance_is_not_allowlisted(self) -> None:
        result = probe_agent_instance_allowlist(
            blueprint_app_id="blueprint-id",
            allowlist=("listed-id",),
            identities=[
                {
                    "appId": "listed-id",
                    "agentIdentityBlueprintId": "blueprint-id",
                },
                {
                    "appId": "missing-id",
                    "agentIdentityBlueprintId": "BLUEPRINT-ID",
                },
                {
                    "appId": "foreign-id",
                    "agentIdentityBlueprintId": "other-blueprint",
                },
                {
                    "appId": "blueprint-id",
                    "agentIdentityBlueprintId": "blueprint-id",
                },
                {"agentIdentityBlueprintId": "blueprint-id"},
            ],
        )
        assert result.state == "warn"
        assert result.name == "agent_instance_allowlist"
        assert "missing-id" in result.detail
        assert "foreign-id" not in result.detail
        assert result.data["unlisted_app_ids"] == ["missing-id"]

    def test_ok_when_instance_is_allowlisted(self) -> None:
        result = probe_agent_instance_allowlist(
            blueprint_app_id="blueprint-id",
            allowlist=("Instance-ID",),
            identities=[
                {
                    "appId": "instance-id",
                    "agentIdentityBlueprintId": "blueprint-id",
                }
            ],
        )
        assert result.state == "ok"
        assert "allowlisted or absent" in result.detail

    def test_skip_without_blueprint(self) -> None:
        result = probe_agent_instance_allowlist(
            blueprint_app_id="  ",
            allowlist=(),
            identities=[],
        )
        assert result.state == "ok"
        assert "no blueprint" in result.detail

    def test_skip_when_lookup_cannot_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hermes_a365.doctor._collect_agent_identity_targets",
            lambda: [("blueprint-id", frozenset())],
        )
        monkeypatch.setattr(
            "hermes_a365.doctor._list_blueprint_agent_identities",
            lambda _blueprint: None,
        )
        result = probe_agent_instance_allowlist()
        assert result.state == "ok"
        assert "could not list" in result.detail

    def test_agent_env_allowlist_suppresses_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("A365_APP_ID", raising=False)
        monkeypatch.delenv("A365_AGENT_IDENTITY_IDS", raising=False)
        monkeypatch.delenv("A365_GENERATED_CONFIG_PATH", raising=False)
        agent = tmp_path / "agents" / "inbox"
        agent.mkdir(parents=True)
        (agent / ".env").write_text(
            "A365_APP_ID=blueprint-app\nA365_AGENT_IDENTITY_IDS=instance-app\n"
        )
        result = probe_agent_instance_allowlist(
            identities=[
                {
                    "appId": "instance-app",
                    "agentIdentityBlueprintId": "blueprint-app",
                }
            ]
        )
        assert result.state == "ok"


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
    @pytest.fixture(autouse=True)
    def _stub_secret_expiry_graph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(_url: str) -> dict[str, object]:
            return {"passwordCredentials": []}

        monkeypatch.setattr(
            "hermes_a365.client_secret_expiry.build_graph_get",
            lambda *_a, **_k: fake_get,
        )

    def test_includes_all_probes_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        report = run_all_probes()
        names = [p.name for p in report.probes]
        for required in (
            "python",
            "host_platform",
            "a365_cli",
            "dotnet_runtime",
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

    def test_path_a_only_install_does_not_add_bot_service_probes(self, tmp_path: Path) -> None:
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
                "hermes_a365.doctor.probe_dotnet_runtime",
                return_value=ProbeResult("dotnet_runtime", "ok", ""),
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
            bot_service_operator_env={"A365_BF_APP_ID": "11111111-1111-1111-1111-111111111111"},
        )
        bot_results = [p for p in report.probes if p.name.startswith("bot_service")]

        assert [p.name for p in bot_results] == [
            "bot_service_config",
            "bot_service_msa_app_id",
        ]
        assert {p.state for p in bot_results} == {"ok"}

    def test_path_b_runtime_auth_failure_maps_to_doctor_error(self, tmp_path: Path) -> None:
        sidecar = _write_bot_service_sidecar(tmp_path)

        report = run_all_probes(
            bot_service_sidecar_path=sidecar,
            generated_config_path=tmp_path / "a365.generated.config.json",
            bot_service_runner=_GreenBotServiceRunner(),
            bot_service_operator_env={"A365_BF_APP_ID": "11111111-1111-1111-1111-111111111111"},
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
# #320 — Linux keychain skipped when gateway has plaintext coverage
# ---------------------------------------------------------------------------

_T1_TENANT = "11111111-1111-1111-1111-111111111111"
_T1_APP = "22222222-2222-2222-2222-222222222222"
_T1_BP_SECRET = "canary-blueprint-secret-ALPHA"
_T1_BF_APP = "33333333-3333-3333-3333-333333333333"
_T1_BF_SECRET = "canary-bf-secret-BETA"
_T1_GEN_SECRET = "canary-generated-secret-GAMMA"

_KEYCHAIN_SKIP_DETAIL = (
    "Linux `secret-tool` not found (install libsecret-tools / libsecret-1-0); "
    "configured gateway credentials have plaintext coverage; provider lookup is not required"
)

_DOCTOR_CRED_ENV_KEYS = (
    "A365_TENANT_ID",
    "A365_APP_ID",
    "A365_BLUEPRINT_CLIENT_SECRET",
    "A365_BF_APP_ID",
    "A365_BF_CLIENT_SECRET",
    "A365_GENERATED_CONFIG_PATH",
    "AGENT_IDENTITY",
)


def _write_mode(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def _agent365_config(
    *,
    enabled: bool = True,
    multiplex: bool = False,
    extra: dict[str, object] | None = None,
    plugins_only: bool = False,
    plugins_with_agent365: bool = False,
) -> str:
    if plugins_only:
        return yaml.safe_dump({"plugins": {"enabled": ["agent365"]}})
    data: dict[str, object] = {}
    if plugins_with_agent365:
        data["plugins"] = {"enabled": ["agent365"]}
    gw: dict[str, object] = {}
    if multiplex:
        gw["multiplex_profiles"] = True
    agent365: dict[str, object] = {"enabled": enabled}
    if extra:
        agent365["extra"] = extra
    gw["platforms"] = {"agent365": agent365}
    data["gateway"] = gw
    return yaml.safe_dump(data)


def _home_tree_snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    snap: dict[str, tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_file():
            snap[rel] = (path.stat().st_mode & 0o777, path.read_bytes())
        elif path.is_dir():
            snap[rel + "/"] = (path.stat().st_mode & 0o777, b"")
    return snap


def _hermes_and_cwd_tree_snapshot(
    home: Path, cwd: Path
) -> dict[str, dict[str, tuple[int, bytes]]]:
    return {"home": _home_tree_snapshot(home), "cwd": _home_tree_snapshot(cwd)}


def _path_a_profile(*, bf: bool = False, identity: str = "") -> str:
    lines = [
        f"A365_TENANT_ID={_T1_TENANT}",
        f"A365_APP_ID={_T1_APP}",
        f"A365_BLUEPRINT_CLIENT_SECRET={_T1_BP_SECRET}",
    ]
    if identity:
        lines.append(f"AGENT_IDENTITY={identity}")
    if bf:
        lines.extend([f"A365_BF_APP_ID={_T1_BF_APP}", f"A365_BF_CLIENT_SECRET={_T1_BF_SECRET}"])
    return "\n".join(lines) + "\n"


def _generated_json(secret: str = _T1_GEN_SECRET, **extra_fields: object) -> str:
    payload: dict[str, object] = {"agentBlueprintClientSecret": secret, **extra_fields}
    return json.dumps(payload) + "\n"


def _stub_ok_probes(*, hermes_state: str = "ok") -> list[patch]:
    ok = lambda name: ProbeResult(name, "ok", "")  # noqa: E731
    return [
        patch("hermes_a365.doctor.probe_python_runtime", return_value=ok("python")),
        patch("hermes_a365.doctor.probe_host_platform", return_value=ok("host_platform")),
        patch("hermes_a365.doctor.probe_a365_cli", return_value=ok("a365_cli")),
        patch("hermes_a365.doctor.probe_dotnet_runtime", return_value=ok("dotnet_runtime")),
        patch("hermes_a365.doctor.probe_az_cli", return_value=ok("az_cli")),
        patch("hermes_a365.doctor.probe_powershell", return_value=ok("powershell")),
        patch("hermes_a365.doctor.probe_custom_client_app", return_value=ok("custom_client_app")),
        patch(
            "hermes_a365.doctor.probe_agent_instance_allowlist",
            return_value=ok("agent_instance_allowlist"),
        ),
        patch(
            "hermes_a365.doctor.probe_local_config",
            return_value=ProbeResult("local_config", "ok", ""),
        ),
        patch("hermes_a365.doctor.probe_file_transfer", return_value=ok("file_transfer")),
        patch(
            "hermes_a365.doctor.probe_hermes_harness",
            return_value=ProbeResult("hermes_harness", hermes_state, ""),
        ),
        patch(
            "hermes_a365.doctor.gather_client_secret_expiry_checks",
            return_value=[],
        ),
        patch(
            "hermes_a365.doctor.bot_service_diagnostics.collect_bot_service_diagnostics",
            return_value=[],
        ),
    ]


def _run_linux_keychain_report(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    *,
    cwd: Path | None = None,
    doctor_env: dict[str, str | None] | None = None,
    hermes_warn: bool = False,
) -> DoctorReport:
    monkeypatch.setenv("HERMES_HOME", str(home))
    if cwd is not None:
        monkeypatch.chdir(cwd)
    for key in _DOCTOR_CRED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    if doctor_env:
        for key, value in doctor_env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
    monkeypatch.delenv("PATH", raising=False)
    hermes_state = "warn" if hermes_warn else "ok"
    stack = ExitStack()
    stack.enter_context(patch("hermes_a365.doctor.sys.platform", "linux"))
    stack.enter_context(patch("hermes_a365.doctor.shutil.which", return_value=None))
    for p in _stub_ok_probes(hermes_state=hermes_state):
        stack.enter_context(p)
    with stack:
        return run_all_probes(no_network=True)


def _keychain_probe(report: DoctorReport) -> ProbeResult:
    return next(p for p in report.probes if p.name == "keychain")


def _per_agent_env(home: Path, slug: str, body: str, *, mode: int = 0o600) -> Path:
    path = home / "agents" / slug / ".env"
    _write_mode(path, body, mode)
    return path


class TestLinuxPlaintextCoverageControlsReadiness:
    @pytest.fixture(autouse=True)
    def _linux_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "profile-home"
        home.mkdir()
        work = tmp_path / "gw-cwd"
        work.mkdir()
        monkeypatch.chdir(work)
        _write_mode(home / "config.yaml", _agent365_config())
        return home

    def test_profile_path_a_skipped_exit_0(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        kc = _keychain_probe(report)
        assert kc.state == "skipped"
        assert kc.detail == _KEYCHAIN_SKIP_DETAIL
        assert report.overall == "ok"
        assert overall_to_exit_code(report.overall) == 0

    def test_profile_path_a_with_bf_pair(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile(bf=True))
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "skipped"
        assert report.overall == "ok"

    def test_extra_only_credentials(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(
            _linux_home / "config.yaml",
            _agent365_config(
                extra={
                    "tenant_id": _T1_TENANT,
                    "app_id": _T1_APP,
                    "blueprint_client_secret": _T1_BP_SECRET,
                }
            ),
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "skipped"
        assert report.overall == "ok"

    def test_profile_empty_strings_fall_through_to_extra(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(
            _linux_home / ".env",
            "A365_TENANT_ID=\nA365_APP_ID=\nA365_BLUEPRINT_CLIENT_SECRET=\n",
        )
        _write_mode(
            _linux_home / "config.yaml",
            _agent365_config(
                extra={
                    "tenant_id": _T1_TENANT,
                    "app_id": _T1_APP,
                    "blueprint_client_secret": _T1_BP_SECRET,
                }
            ),
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "skipped"

    def test_profile_nonempty_beats_invalid_extra_tenant_type(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        cfg = (
            "gateway:\n  platforms:\n    agent365:\n      enabled: true\n      extra:\n"
            "        tenant_id: 1\n"
            f"        app_id: {_T1_APP}\n"
            f"        blueprint_client_secret: {_T1_BP_SECRET}\n"
        )
        _write_mode(_linux_home / "config.yaml", cfg)
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "skipped"

    def test_profile_nonempty_beats_invalid_extra_app_id_type(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        cfg = (
            "gateway:\n  platforms:\n    agent365:\n      enabled: true\n      extra:\n"
            f"        tenant_id: {_T1_TENANT}\n"
            "        app_id: 1\n"
            f"        blueprint_client_secret: {_T1_BP_SECRET}\n"
        )
        _write_mode(_linux_home / "config.yaml", cfg)
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "skipped"

    @pytest.mark.parametrize(
        "profile_path_line",
        [
            'A365_GENERATED_CONFIG_PATH=" real.json "\n',
            'A365_GENERATED_CONFIG_PATH="   "\n',
        ],
        ids=["padded_profile_path", "spaces_only_profile_path"],
    )
    def test_profile_generated_path_not_stripped_before_path(
        self,
        _linux_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        profile_path_line: str,
    ) -> None:
        work = tmp_path / "gw-cwd"
        work.mkdir(exist_ok=True)
        _write_mode(work / "real.json", _generated_json())
        _write_mode(work / "a365.generated.config.json", _generated_json())
        profile = f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n{profile_path_line}"
        _write_mode(_linux_home / ".env", profile)
        report = _run_linux_keychain_report(monkeypatch, _linux_home, cwd=work)
        assert _keychain_probe(report).state == "error"

    @pytest.mark.parametrize(
        "path_setup",
        [
            ("extra", {"generated_config_path": "via-extra.json"}),
            ("profile", "A365_GENERATED_CONFIG_PATH=via-profile.json\n"),
            ("cwd_default", None),
        ],
        ids=["extra_path", "profile_path", "cwd_default"],
    )
    def test_generated_file_blueprint_fallback(
        self,
        _linux_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        path_setup: tuple[str, object],
    ) -> None:
        _kind, spec = path_setup
        gen_name = "via-extra.json" if _kind == "extra" else "via-profile.json"
        if _kind == "cwd_default":
            gen_name = "a365.generated.config.json"
        gen_path = tmp_path / "gw-cwd" / gen_name
        _write_mode(gen_path, _generated_json())
        profile = f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n"
        if _kind == "profile":
            profile += spec
        _write_mode(_linux_home / ".env", profile)
        extra: dict[str, object] = {}
        if _kind == "extra":
            extra = dict(spec)  # type: ignore[arg-type]
        _write_mode(_linux_home / "config.yaml", _agent365_config(extra=extra or None))
        report = _run_linux_keychain_report(monkeypatch, _linux_home, cwd=tmp_path / "gw-cwd")
        kc = _keychain_probe(report)
        assert kc.state == "skipped"
        assert kc.detail == _KEYCHAIN_SKIP_DETAIL
        assert _T1_GEN_SECRET not in kc.detail
        assert _T1_BP_SECRET not in kc.detail

    @pytest.mark.parametrize(
        "whitespace_case",
        [
            "profile_bf_app_id_spaces",
            "extra_bf_app_id_spaces",
            "extra_generated_path_padded",
            "extra_generated_path_spaces_only",
        ],
    )
    def test_whitespace_does_not_count_as_adapter_present(
        self,
        _linux_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        whitespace_case: str,
    ) -> None:
        work = tmp_path / "gw-cwd"
        work.mkdir(exist_ok=True)
        base = f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n"
        extra: dict[str, object] = {}
        if whitespace_case == "profile_bf_app_id_spaces":
            _write_mode(
                _linux_home / ".env",
                base
                + f"A365_BLUEPRINT_CLIENT_SECRET={_T1_BP_SECRET}\n"
                + 'A365_BF_APP_ID="   "\n',
            )
        elif whitespace_case == "extra_bf_app_id_spaces":
            _write_mode(
                _linux_home / ".env",
                base + f"A365_BLUEPRINT_CLIENT_SECRET={_T1_BP_SECRET}\n",
            )
            extra = {"bf_app_id": "   "}
        elif whitespace_case == "extra_generated_path_padded":
            _write_mode(work / "real.json", _generated_json())
            _write_mode(_linux_home / ".env", base)
            extra = {"generated_config_path": " real.json "}
        elif whitespace_case == "extra_generated_path_spaces_only":
            _write_mode(work / "profile-fallback.json", _generated_json())
            _write_mode(
                _linux_home / ".env",
                base + "A365_GENERATED_CONFIG_PATH=profile-fallback.json\n",
            )
            extra = {"generated_config_path": "   "}
        _write_mode(_linux_home / "config.yaml", _agent365_config(extra=extra or None))
        report = _run_linux_keychain_report(monkeypatch, _linux_home, cwd=work)
        assert _keychain_probe(report).state == "error"

    def test_generated_whitespace_only_blueprint_secret_counts_as_present(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        work = tmp_path / "gw-cwd"
        work.mkdir(exist_ok=True)
        _write_mode(
            work / "a365.generated.config.json",
            '{"agentBlueprintClientSecret": "   "}\n',
        )
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home, cwd=work)
        assert _keychain_probe(report).state == "skipped"

    def test_per_agent_file_has_no_effect_on_complete_coverage(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile(identity="my-agent"))
        _per_agent_env(
            _linux_home,
            "my-agent",
            "\n".join(
                [
                    "A365_BLUEPRINT_CLIENT_SECRET=wrong",
                    "A365_TENANT_ID=99999999-9999-9999-9999-999999999999",
                    "A365_APP_ID=88888888-8888-8888-8888-888888888888",
                    "A365_GENERATED_CONFIG_PATH=/other/generated.json",
                    "AGENT_IDENTITY=other-agent",
                    "A365_BF_CLIENT_SECRET=only-in-agent",
                ]
            )
            + "\n",
            mode=0o600,
        )
        agent_env = _linux_home / "agents" / "my-agent" / ".env"
        real_read_text = Path.read_text
        real_stat = Path.stat

        def _guard_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self == agent_env:
                raise AssertionError("per-agent .env must not be stat'd")
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        def _guard_read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self == agent_env:
                raise AssertionError("per-agent .env must not be read")
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(Path, "stat", _guard_stat),
            patch.object(Path, "read_text", _guard_read_text),
        ):
            report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "skipped"

    def test_unreadable_per_agent_does_not_veto_coverage(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile(identity="locked-agent"))
        agent_dir = _linux_home / "agents" / "locked-agent"
        agent_dir.mkdir(parents=True)
        bad = agent_dir / ".env"
        bad.write_text(f"A365_BLUEPRINT_CLIENT_SECRET={_T1_BP_SECRET}-steal-me\n")
        bad.chmod(0o000)
        agent_env = bad
        real_stat = Path.stat

        def _guard_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self == agent_env:
                raise AssertionError("per-agent .env must not be stat'd even when unreadable")
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        try:
            with patch.object(Path, "stat", _guard_stat):
                report = _run_linux_keychain_report(monkeypatch, _linux_home)
            assert _keychain_probe(report).state == "skipped"
        finally:
            bad.chmod(0o600)

    def test_group_readable_per_agent_does_not_veto_coverage(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile(identity="leaky-agent"))
        agent_env = _per_agent_env(
            _linux_home,
            "leaky-agent",
            f"A365_BLUEPRINT_CLIENT_SECRET={_T1_BP_SECRET}-only-here\n",
            mode=0o644,
        )
        real_stat = Path.stat

        def _guard_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self == agent_env:
                raise AssertionError("per-agent .env must not be stat'd")
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(Path, "stat", _guard_stat):
            report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "skipped"

    @pytest.mark.parametrize(
        "slug_source,expected_slug",
        [
            ("extra", "from-extra"),
            ("profile", "from-profile"),
            ("default", "default"),
        ],
    )
    def test_valid_slug_inference(
        self,
        _linux_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        slug_source: str,
        expected_slug: str,
    ) -> None:
        extra: dict[str, object] = {}
        profile = _path_a_profile()
        if slug_source == "extra":
            extra["slug"] = expected_slug
        elif slug_source == "profile":
            profile = _path_a_profile(identity=expected_slug)
        _write_mode(_linux_home / ".env", profile)
        _write_mode(_linux_home / "config.yaml", _agent365_config(extra=extra or None))
        monkeypatch.setenv("AGENT_IDENTITY", "doctor-process-override")
        report = _run_linux_keychain_report(
            monkeypatch,
            _linux_home,
            doctor_env={"AGENT_IDENTITY": "doctor-process-override"},
        )
        assert _keychain_probe(report).state == "skipped"

    def test_invalid_slug_from_profile_agents_identity_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile(identity="../escape"))
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_invalid_extra_slug_beats_profile_agents_identity(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile(identity="good-agent"))
        _write_mode(
            _linux_home / "config.yaml",
            _agent365_config(extra={"slug": "../escape"}),
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_extra_slug_beats_invalid_profile_agents_identity(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile(identity="../escape"))
        _write_mode(
            _linux_home / "config.yaml",
            _agent365_config(extra={"slug": "good-agent"}),
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "skipped"

    def test_doctor_process_agent_identity_does_not_override_profile(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile(identity="good-agent"))
        report = _run_linux_keychain_report(
            monkeypatch,
            _linux_home,
            doctor_env={"AGENT_IDENTITY": "../escape"},
        )
        assert _keychain_probe(report).state == "skipped"

    def test_unrelated_warning_still_exit_1(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        report = _run_linux_keychain_report(monkeypatch, _linux_home, hermes_warn=True)
        assert _keychain_probe(report).state == "skipped"
        assert report.overall == "warn"
        assert overall_to_exit_code(report.overall) == 1

    def test_partial_bf_app_id_without_secret_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(
            _linux_home / ".env",
            _path_a_profile()
            + f"A365_BF_APP_ID={_T1_BF_APP}\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"
        assert report.overall == "error"
        assert overall_to_exit_code(report.overall) == 2

    def test_partial_bf_secret_without_app_id_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(
            _linux_home / ".env",
            _path_a_profile() + f"A365_BF_CLIENT_SECRET={_T1_BF_SECRET}\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"
        assert overall_to_exit_code(report.overall) == 2

    def test_generated_bf_fields_do_not_complete_pair(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gen = tmp_path / "gw-cwd" / "a365.generated.config.json"
        _write_mode(
            gen,
            _generated_json(bfAppId=_T1_BF_APP, agentBfClientSecret=_T1_BF_SECRET),
        )
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n"
            f"A365_BF_APP_ID={_T1_BF_APP}\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home, cwd=tmp_path / "gw-cwd")
        assert _keychain_probe(report).state == "error"

    def test_blueprint_only_in_per_agent_stays_error(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        _per_agent_env(
            _linux_home,
            "default",
            f"A365_BLUEPRINT_CLIENT_SECRET={_T1_BP_SECRET}\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_bf_secret_only_in_per_agent_stays_error(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile() + f"A365_BF_APP_ID={_T1_BF_APP}\n")
        _per_agent_env(
            _linux_home,
            "default",
            f"A365_BF_CLIENT_SECRET={_T1_BF_SECRET}\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_doctor_process_secret_overlay_stays_error(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gen = tmp_path / "only-doctor-sees.json"
        _write_mode(gen, _generated_json())
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        report = _run_linux_keychain_report(
            monkeypatch,
            _linux_home,
            cwd=tmp_path / "gw-cwd",
            doctor_env={
                "A365_BLUEPRINT_CLIENT_SECRET": _T1_BP_SECRET,
                "A365_GENERATED_CONFIG_PATH": str(gen),
            },
        )
        assert _keychain_probe(report).state == "error"

    def test_doctor_process_path_only_overlay_stays_error(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gen = tmp_path / "only-doctor-sees.json"
        _write_mode(gen, _generated_json())
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        report = _run_linux_keychain_report(
            monkeypatch,
            _linux_home,
            cwd=tmp_path / "gw-cwd",
            doctor_env={"A365_GENERATED_CONFIG_PATH": str(gen)},
        )
        assert _keychain_probe(report).state == "error"

    def test_doctor_process_identities_only_stays_error(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(
            _linux_home / ".env",
            f"A365_BLUEPRINT_CLIENT_SECRET={_T1_BP_SECRET}\n",
        )
        report = _run_linux_keychain_report(
            monkeypatch,
            _linux_home,
            doctor_env={
                "A365_TENANT_ID": _T1_TENANT,
                "A365_APP_ID": _T1_APP,
            },
        )
        assert _keychain_probe(report).state == "error"

    @pytest.mark.parametrize("platform_enabled", [False, "absent"])
    def test_plugins_enabled_without_platform_stanza_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, platform_enabled: object
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        if platform_enabled == "absent":
            _write_mode(
                _linux_home / "config.yaml",
                yaml.safe_dump({"plugins": {"enabled": ["agent365"]}}),
            )
        else:
            _write_mode(
                _linux_home / "config.yaml",
                _agent365_config(enabled=False, plugins_with_agent365=True),
            )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    @pytest.mark.parametrize(
        "config_body",
        [
            _agent365_config(multiplex=True),
            _agent365_config(enabled=False),
            "",
            "gateway: not-a-mapping\n",
        ],
        ids=["multiplex", "disabled", "absent", "malformed"],
    )
    def test_bad_gateway_config_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, config_body: str
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        path = _linux_home / "config.yaml"
        if config_body == "":
            path.unlink(missing_ok=True)
        else:
            _write_mode(path, config_body)
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_non_mapping_extra_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        yaml_body = (
            "gateway:\n  platforms:\n    agent365:\n      enabled: true\n      extra: 1\n"
        )
        _write_mode(_linux_home / "config.yaml", yaml_body)
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_unreadable_config_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        cfg = _linux_home / "config.yaml"
        cfg.write_text(_agent365_config())
        cfg.chmod(0o000)
        try:
            report = _run_linux_keychain_report(monkeypatch, _linux_home)
            assert _keychain_probe(report).state == "error"
        finally:
            cfg.chmod(0o600)

    def test_missing_blueprint_after_fallbacks_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_invalid_extra_tenant_type_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = (
            "gateway:\n  platforms:\n    agent365:\n      enabled: true\n      extra:\n"
            "        tenant_id: 1\n"
            f"        app_id: {_T1_APP}\n"
            f"        blueprint_client_secret: {_T1_BP_SECRET}\n"
        )
        _write_mode(_linux_home / "config.yaml", cfg)
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_insecure_profile_env_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _linux_home / ".env"
        _write_mode(path, _path_a_profile(), mode=0o644)
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_unreadable_profile_env_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _linux_home / ".env"
        path.mkdir()
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    @pytest.mark.parametrize(
        "gen_fixture",
        [
            "missing",
            "unreadable",
            "malformed",
            "insecure",
            "non_mapping",
            "empty_secret",
            "numeric_secret",
        ],
    )
    def test_bad_generated_fallback(
        self,
        _linux_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gen_fixture: str,
    ) -> None:
        work = tmp_path / "gw-cwd"
        work.mkdir(exist_ok=True)
        gen = work / "a365.generated.config.json"
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        if gen_fixture == "missing":
            pass
        elif gen_fixture == "unreadable":
            gen.mkdir()
        elif gen_fixture == "malformed":
            _write_mode(gen, "{not-json")
        elif gen_fixture == "insecure":
            _write_mode(gen, _generated_json(), mode=0o644)
        elif gen_fixture == "non_mapping":
            _write_mode(gen, "[]\n")
        elif gen_fixture == "empty_secret":
            _write_mode(gen, _generated_json(secret=""))
        elif gen_fixture == "numeric_secret":
            _write_mode(gen, '{"agentBlueprintClientSecret": 99999}\n')
        report = _run_linux_keychain_report(monkeypatch, _linux_home, cwd=work)
        assert _keychain_probe(report).state == "error"

    def test_higher_priority_generated_path_wins_no_search(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        work = tmp_path / "gw-cwd"
        work.mkdir(exist_ok=True)
        bad = work / "picked.json"
        bad.mkdir()
        good = work / "ignored.json"
        _write_mode(good, _generated_json())
        _write_mode(
            _linux_home / "config.yaml",
            _agent365_config(extra={"generated_config_path": "picked.json"}),
        )
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n"
            f"A365_GENERATED_CONFIG_PATH=ignored.json\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home, cwd=work)
        assert _keychain_probe(report).state == "error"

    def test_profile_generated_path_beats_cwd_default(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        work = tmp_path / "gw-cwd"
        work.mkdir(exist_ok=True)
        bad = work / "profile-pick.json"
        bad.mkdir()
        _write_mode(work / "a365.generated.config.json", _generated_json())
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n"
            f"A365_GENERATED_CONFIG_PATH=profile-pick.json\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home, cwd=work)
        assert _keychain_probe(report).state == "error"

    def test_sibling_home_secrets_do_not_cover_active_home(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sibling = tmp_path / "sibling-home"
        sibling.mkdir()
        _write_mode(sibling / ".env", _path_a_profile())
        _write_mode(sibling / "config.yaml", _agent365_config())
        _write_mode(
            _linux_home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"

    def test_invalid_inferred_slug_errors(
        self, _linux_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_mode(_linux_home / ".env", _path_a_profile())
        _write_mode(
            _linux_home / "config.yaml",
            _agent365_config(extra={"slug": "../escape"}),
        )
        report = _run_linux_keychain_report(monkeypatch, _linux_home)
        assert _keychain_probe(report).state == "error"


class TestPlaintextKeychainDiagnosticIsReadOnlyAndRedacted:
    def test_skipped_detail_fixed_string_no_provider_or_writes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        home = tmp_path / "home"
        work = tmp_path / "cwd"
        home.mkdir()
        work.mkdir()
        profile = home / ".env"
        config = home / "config.yaml"
        _write_mode(config, _agent365_config())
        _write_mode(profile, _path_a_profile())
        _per_agent_env(
            home,
            "default",
            f"A365_BLUEPRINT_CLIENT_SECRET={_T1_BP_SECRET}-per-agent-only\n",
        )
        tree_before = _hermes_and_cwd_tree_snapshot(home, work)

        def _fail_provider(*_a: object, **_k: object) -> str:
            raise AssertionError("provider must not be consulted")

        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.chdir(work)
        for key in _DOCTOR_CRED_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        stack = ExitStack()
        stack.enter_context(patch("hermes_a365.doctor.sys.platform", "linux"))
        stack.enter_context(patch("hermes_a365.doctor.shutil.which", return_value=None))
        stack.enter_context(
            patch(
                "hermes_a365.secrets_provider.resolve_secret_state",
                side_effect=_fail_provider,
            )
        )
        stack.enter_context(
            patch("hermes_a365.secrets_provider.resolve_secret", side_effect=_fail_provider)
        )
        stack.enter_context(
            patch.object(
                KeychainSecretsProvider,
                "__init__",
                side_effect=AssertionError("KeychainSecretsProvider must not be constructed"),
            )
        )
        for p in _stub_ok_probes():
            stack.enter_context(p)
        with stack:
            report = run_all_probes(no_network=True)

        kc = _keychain_probe(report)
        assert kc.state == "skipped"
        assert kc.detail == _KEYCHAIN_SKIP_DETAIL
        assert kc.data == {}
        assert _T1_BP_SECRET not in kc.detail
        human = render_human(report)
        payload = render_json(report)
        assert _T1_BP_SECRET not in human
        assert _T1_BP_SECRET not in payload
        for record in caplog.records:
            assert _T1_BP_SECRET not in (record.getMessage() + str(record.exc_text or ""))

        assert _hermes_and_cwd_tree_snapshot(home, work) == tree_before

    def test_cwd_and_home_unchanged_after_failed_coverage(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        work = tmp_path / "cwd"
        home.mkdir()
        work.mkdir()
        gen = work / "a365.generated.config.json"
        _write_mode(gen, "{not-json")
        _write_mode(home / "config.yaml", _agent365_config())
        _write_mode(
            home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        before = _hermes_and_cwd_tree_snapshot(home, work)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.chdir(work)
        stack = ExitStack()
        stack.enter_context(patch("hermes_a365.doctor.sys.platform", "linux"))
        stack.enter_context(patch("hermes_a365.doctor.shutil.which", return_value=None))
        for p in _stub_ok_probes():
            stack.enter_context(p)
        with stack:
            report = run_all_probes(no_network=True)
        assert _keychain_probe(report).state == "error"
        assert _hermes_and_cwd_tree_snapshot(home, work) == before

    def test_classification_never_constructs_keychain_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _write_mode(home / "config.yaml", _agent365_config())
        _write_mode(home / ".env", _path_a_profile())
        monkeypatch.setenv("HERMES_HOME", str(home))
        with (
            patch.object(
                KeychainSecretsProvider,
                "__init__",
                side_effect=AssertionError("KeychainSecretsProvider must not be constructed"),
            ),
            patch("hermes_a365.doctor.sys.platform", "linux"),
            patch("hermes_a365.doctor.shutil.which", return_value=None),
        ):
            assert _gateway_has_plaintext_secret_coverage() is True

    def test_malformed_generated_errors_without_leaks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        home = tmp_path / "home"
        work = tmp_path / "cwd"
        home.mkdir()
        work.mkdir()
        canary_path = work / f"canary-{_T1_GEN_SECRET}.json"
        canary_content = f'{{"agentBlueprintClientSecret": "", "note": "{_T1_BP_SECRET}"}}'
        _write_mode(canary_path, canary_content)
        _write_mode(home / "config.yaml", _agent365_config())
        _write_mode(
            home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n"
            f"A365_GENERATED_CONFIG_PATH={canary_path.name}\n",
        )
        before = canary_path.read_bytes()

        def _fail_provider(*_a: object, **_k: object) -> str:
            raise AssertionError("provider must not be consulted")

        caplog.set_level(logging.DEBUG)
        stack = ExitStack()
        stack.enter_context(patch("hermes_a365.doctor.sys.platform", "linux"))
        stack.enter_context(patch("hermes_a365.doctor.shutil.which", return_value=None))
        stack.enter_context(
            patch("hermes_a365.secrets_provider.resolve_secret", side_effect=_fail_provider)
        )
        for p in _stub_ok_probes():
            stack.enter_context(p)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.chdir(work)
        with stack:
            report = run_all_probes(no_network=True)

        kc = _keychain_probe(report)
        assert kc.state == "error"
        assert kc.detail == (
            "Linux `secret-tool` not found (install libsecret-tools / libsecret-1-0)"
        )
        assert "{not-json" not in kc.detail
        assert _T1_BP_SECRET not in kc.detail
        assert _T1_GEN_SECRET not in kc.detail
        assert canary_path.read_bytes() == before
        human = render_human(report)
        payload = render_json(report)
        assert "{not-json" not in human
        assert "{not-json" not in payload
        for record in caplog.records:
            joined = record.getMessage() + str(record.exc_text or "")
            assert _T1_BP_SECRET not in joined
            assert _T1_GEN_SECRET not in joined
            assert "{not-json" not in joined

    def test_malformed_generated_json_fixed_error_detail_no_leak(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        home = tmp_path / "profile-home"
        home.mkdir()
        work = tmp_path / "gw-cwd"
        work.mkdir()
        gen = work / "a365.generated.config.json"
        _write_mode(gen, "{not-json")
        _write_mode(home / "config.yaml", _agent365_config())
        _write_mode(
            home / ".env",
            f"A365_TENANT_ID={_T1_TENANT}\nA365_APP_ID={_T1_APP}\n",
        )
        caplog.set_level(logging.DEBUG)
        report = _run_linux_keychain_report(monkeypatch, home, cwd=work)
        kc = _keychain_probe(report)
        assert kc.state == "error"
        assert kc.detail == (
            "Linux `secret-tool` not found (install libsecret-tools / libsecret-1-0)"
        )
        assert "{not-json" not in kc.detail
        for record in caplog.records:
            assert "{not-json" not in (record.getMessage() + str(record.exc_text or ""))


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
