"""Tests for hermes_a365.register — the v0.2 setup-orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hermes_a365._common import write_owner_only_text_atomic
from hermes_a365.a365_config import CONFIG_FILENAME
from hermes_a365.entra_state import RegistrationDecision, record_ownership_anchor
from hermes_a365.mutator import (
    AADSTS_CONSENT_REQUIRED,
    AADSTS_LICENSE_NOT_PROPAGATED,
    A365CliMutator,
    AADSTSError,
    CliInvocationError,
    RunResult,
)
from hermes_a365 import cli as standalone_cli
from hermes_a365.plugin import cli as plugin_cli
from hermes_a365.register import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_RETRIES,
    REGISTER_AITEAMMATE_REMOVED,
    ApplyResult,
    RegisterError,
    RegisterInputs,
    RegisterPlan,
    RegisterStep,
    SecretRecoveryOutcome,
    auto_recover_secret,
    build_parser,
    build_register_plan,
    default_recovery_display_name,
    detect_missing_secret,
    report_missing_secret_warning,
    run,
    update_config_for_agent,
)
from hermes_a365.register import (
    apply_register_plan as _apply_register_plan,
)
from hermes_a365.secrets_provider import KeychainSecretsProvider, set_default_provider

_TEST_TENANT = "aaaa1111-0000-0000-0000-000000000001"
# Keep apply() off the real ~/.hermes so a developer anchor cannot change
# the create-path tests. The directory is empty: no per-agent A365_APP_ID.
_REGISTER_TEST_HOME = Path(tempfile.mkdtemp(prefix="a365-register-home-"))

EXPECTED_REGISTER_AITEAMMATE_REMOVED = (
    "`register --aiteammate` was removed in 1.0. Register only runs "
    "`a365 setup blueprint` plus MCP/Bot permission setup; the AI Teammate "
    "agentic user is created/activated after `publish --aiteammate` via "
    "M365 Admin Centre upload and per-user activation."
)


def _build_plugin_parent_parser() -> argparse.ArgumentParser:
    import argparse

    parent = argparse.ArgumentParser(prog="hermes")
    subs = parent.add_subparsers(dest="cmd")
    a365_p = subs.add_parser("a365")
    plugin_cli.register_cli(a365_p)
    return parent

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeMutator:
    """Records every argv list; returns scripted RunResult / Exception."""

    available: bool = True
    calls: list[list[str]] = field(default_factory=list)
    scripted: list[RunResult | Exception] = field(default_factory=list)
    sensitive_flags: list[bool] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 60.0,
        stdin_input: str | None = None,
        sensitive: bool = False,
    ) -> RunResult:
        self.calls.append(list(argv))
        self.sensitive_flags.append(sensitive)
        if argv[:3] == ["az", "account", "show"]:
            if self.scripted:
                candidate = self.scripted[0]
                candidate_argv = getattr(candidate, "argv", [])
                if candidate_argv[:3] == ["az", "account", "show"]:
                    self.scripted.pop(0)
                    if isinstance(candidate, Exception):
                        raise candidate
                    return candidate
            return RunResult(
                argv=list(argv),
                returncode=0,
                stdout=json.dumps({"tenantId": _TEST_TENANT}),
                stderr="",
            )
        if argv[:4] == ["az", "rest", "--method", "GET"] or (
            len(argv) >= 4 and argv[:2] == ["az", "rest"] and "GET" in argv
        ):
            # Registration state reads must not consume scripted a365 results.
            # An empty collection is "no existing application" (the create path).
            if self.scripted:
                candidate = self.scripted[0]
                candidate_argv = getattr(candidate, "argv", [])
                if candidate_argv[:2] == ["az", "rest"]:
                    self.scripted.pop(0)
                    if isinstance(candidate, Exception):
                        raise candidate
                    return candidate
            return RunResult(
                argv=list(argv),
                returncode=0,
                stdout=json.dumps({"value": []}),
                stderr="",
            )
        if argv[:4] == ["az", "ad", "app", "show"]:
            if self.scripted:
                candidate = self.scripted[0]
                candidate_argv = getattr(candidate, "argv", [])
                if candidate_argv[:4] == ["az", "ad", "app", "show"]:
                    self.scripted.pop(0)
                    if isinstance(candidate, Exception):
                        raise candidate
                    return candidate
            app_id = argv[argv.index("--id") + 1]
            return RunResult(
                argv=list(argv),
                returncode=0,
                stdout=json.dumps(
                    {
                        "appId": app_id,
                        "id": "00000000-0000-0000-0000-000000000001",
                        "displayName": "Inbox Helper Blueprint",
                    }
                ),
                stderr="",
            )
        if self.scripted:
            nxt = self.scripted.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return RunResult(argv=list(argv), returncode=0, stdout="", stderr="")


def apply_register_plan(
    plan: RegisterPlan,
    *,
    mutator: FakeMutator | A365CliMutator,
    **kwargs: object,
) -> ApplyResult:
    """Give mutation tests an explicit tenant unless they exercise no-pin refusal."""
    if plan.inputs.tenant_id is None:
        plan.inputs.tenant_id = _TEST_TENANT
    kwargs.setdefault("hermes_home", _REGISTER_TEST_HOME)
    return _apply_register_plan(plan, mutator=mutator, **kwargs)  # type: ignore[arg-type]


class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# RegisterInputs validation
# ---------------------------------------------------------------------------


class TestRegisterInputs:
    def test_minimal_valid(self) -> None:
        inp = RegisterInputs(agent_name="inbox-helper")
        assert inp.agent_name == "inbox-helper"
        assert inp.tenant_id is None
        assert inp.m365 is False
        assert inp.authmode == "obo"

    def test_empty_agent_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="agent_name"):
            RegisterInputs(agent_name="")

    def test_invalid_authmode_rejected(self) -> None:
        with pytest.raises(ValueError, match="authmode"):
            RegisterInputs(agent_name="x", authmode="bogus")

    @pytest.mark.parametrize("mode", ["obo", "s2s", "both"])
    def test_valid_authmodes(self, mode: str) -> None:
        RegisterInputs(agent_name="x", authmode=mode)


# ---------------------------------------------------------------------------
# build_register_plan — argv shapes
# ---------------------------------------------------------------------------


class TestBuildRegisterPlan:
    def test_three_steps_in_canonical_order(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="inbox-helper"))
        assert isinstance(plan, RegisterPlan)
        names = [s.name for s in plan.steps]
        assert names == ["blueprint", "permissions-mcp", "permissions-bot"]

    def test_blueprint_argv_minimal(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="inbox-helper"))
        bp = plan.steps[0]
        assert bp.argv == ["a365", "setup", "blueprint", "--agent-name", "inbox-helper"]

    def test_blueprint_argv_with_tenant(self) -> None:
        plan = build_register_plan(
            RegisterInputs(agent_name="x", tenant_id="contoso.onmicrosoft.com")
        )
        assert plan.steps[0].argv == [
            "a365",
            "setup",
            "blueprint",
            "--agent-name",
            "x",
            "--tenant-id",
            "contoso.onmicrosoft.com",
        ]

    def test_blueprint_argv_with_all_optional_flags(self) -> None:
        plan = build_register_plan(
            RegisterInputs(
                agent_name="x",
                m365=True,
                no_endpoint=True,
                skip_requirements=True,
            )
        )
        bp = plan.steps[0]
        assert "--m365" in bp.argv
        assert "--no-endpoint" in bp.argv
        assert "--skip-requirements" in bp.argv

    def test_permissions_mcp_argv(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mcp = plan.steps[1]
        assert mcp.argv == ["a365", "setup", "permissions", "mcp", "--agent-name", "x"]

    def test_permissions_bot_argv(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        bot = plan.steps[2]
        assert bot.argv == ["a365", "setup", "permissions", "bot", "--agent-name", "x"]


def test_register_help_omits_removed_aiteammate() -> None:
    help_text = build_parser().format_help()
    assert "--aiteammate" not in help_text
    assert "--aiteammate" not in build_parser()._option_string_actions

    with pytest.raises(SystemExit) as exc:
        standalone_cli.main(["register", "--help"])
    assert exc.value.code == 0
    plugin_parent = _build_plugin_parent_parser()
    with pytest.raises(SystemExit) as exc_plugin:
        plugin_parent.parse_args(["a365", "register", "--help"])
    assert exc_plugin.value.code == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--aiteammate"],
        ["--agent-name", "x", "--aiteammate", "--apply"],
        ["--aiteammate=true"],
    ],
)
def test_removed_aiteammate_exits_2_on_both_entry_points(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_err = "ERROR: " + EXPECTED_REGISTER_AITEAMMATE_REMOVED + "\n"

    def _boom() -> object:
        raise AssertionError("get_mutator must not run for removed flag")

    monkeypatch.setattr("hermes_a365.register.get_mutator", _boom)

    register_py = Path(__file__).resolve().parents[1] / "src" / "hermes_a365" / "register.py"
    text = register_py.read_text(encoding="utf-8")
    assert "AITEAMMATE_REGISTER_UNSUPPORTED" not in text
    assert REGISTER_AITEAMMATE_REMOVED == EXPECTED_REGISTER_AITEAMMATE_REMOVED

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == expected_err
    assert "a365 setup blueprint" not in captured.out

    capsys.readouterr()
    plugin_argv = ["a365", "register", *argv]
    plugin_parent = _build_plugin_parent_parser()
    with pytest.raises(SystemExit) as exc_plugin:
        plugin_parent.parse_args(plugin_argv)
    assert exc_plugin.value.code == 2
    captured_plugin = capsys.readouterr()
    assert captured_plugin.err == expected_err

    capsys.readouterr()
    cli_argv = ["register", *argv]
    with pytest.raises(SystemExit) as exc_cli:
        standalone_cli.main(cli_argv)
    assert exc_cli.value.code == 2
    captured_cli = capsys.readouterr()
    assert captured_cli.err == expected_err


@pytest.mark.parametrize(
    "argv",
    [
        ["--nope"],
        ["--aiteammate-foo"],
        ["--agent-name", "x", "--nope"],
        ["--agent-name", "x", "--aiteammate-foo"],
    ],
)
def test_other_unknown_register_flag_stays_argparse(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    removed_snippet = "was removed in 1.0"

    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
    err = capsys.readouterr().err
    assert removed_snippet not in err

    capsys.readouterr()
    with pytest.raises(SystemExit):
        standalone_cli.main(["register", *argv])
    err_cli = capsys.readouterr().err
    assert removed_snippet not in err_cli

    capsys.readouterr()
    plugin_parent = _build_plugin_parent_parser()
    with pytest.raises(SystemExit):
        plugin_parent.parse_args(["a365", "register", *argv])
    err_plugin = capsys.readouterr().err
    assert removed_snippet not in err_plugin

    if "--agent-name" not in argv:
        assert "the following arguments are required: --agent-name" in err
        assert "the following arguments are required: --agent-name" in err_cli
        assert "the following arguments are required: --agent-name" in err_plugin
    else:
        unknown = "--nope" if "--nope" in argv else "--aiteammate-foo"
        assert f"unrecognized arguments: {unknown}" in err
        assert f"unrecognized arguments: {unknown}" in err_cli
        assert f"unrecognized arguments: {unknown}" in err_plugin


def test_register_inputs_has_no_aiteammate_field() -> None:
    from dataclasses import fields

    field_names = tuple(f.name for f in fields(RegisterInputs))
    assert field_names == (
        "agent_name",
        "tenant_id",
        "m365",
        "authmode",
        "no_endpoint",
        "skip_requirements",
    )
    with pytest.raises(TypeError):
        RegisterInputs(agent_name="x", aiteammate=True)  # type: ignore[call-arg]


class TestSkippedBlueprintSecret:
    @pytest.fixture
    def matching_blueprint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        mutator = FakeMutator()
        monkeypatch.setattr("hermes_a365.register.get_mutator", lambda: mutator)
        monkeypatch.setattr(
            "hermes_a365.register.resolve_registration",
            lambda *args, **kwargs: RegistrationDecision(
                action="repair",
                step_names=("permissions-bot",),
                reason=None,
                messages=("entra: blueprint matches; setup blueprint will not run",),
            ),
        )
        _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        return mutator

    @staticmethod
    def _args(*flags: str):
        return build_parser().parse_args(
            ["--agent-name", "Inbox Helper", "--tenant-id", _TEST_TENANT, "--m365", *flags]
        )

    @pytest.mark.parametrize("flags", [(), ("--update-permissions",), ("--auto-recover-secret",)])
    def test_dry_run_warns_without_mutations(self, matching_blueprint, capsys, flags):
        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        assert "no usable agentBlueprintClientSecret" in output.out
        assert "a365 setup blueprint" in output.out
        assert "--auto-recover-secret" in output.out
        assert "done. Next:" not in output.out
        assert all(call[:3] == ["az", "account", "show"] for call in matching_blueprint.calls)

    @pytest.mark.parametrize("flags", [(), ("--update-permissions",)])
    def test_apply_refuses_before_any_step(self, matching_blueprint, capsys, flags, tmp_path):
        assert run(self._args("--apply", *flags)) == 2
        output = capsys.readouterr()
        assert "no usable agentBlueprintClientSecret" in output.out
        assert "ERROR: blueprint client secret is missing" in output.err
        assert "--auto-recover-secret" in output.out
        assert "done. Next:" not in output.out
        assert all(call[:3] == ["az", "account", "show"] for call in matching_blueprint.calls)
        assert not (tmp_path / CONFIG_FILENAME).exists()

    @pytest.mark.parametrize("platform", ["win32", "linux"])
    @pytest.mark.parametrize("flags", [(), ("--apply",), ("--apply", "--auto-recover-secret")])
    def test_unsupported_default_backend_keeps_secret_gate(
        self, matching_blueprint, capsys, monkeypatch, tmp_path, platform, flags
    ):
        monkeypatch.setattr("hermes_a365.keychain.sys.platform", platform)
        monkeypatch.setattr("hermes_a365.keychain.shutil.which", lambda binary: None)
        set_default_provider(KeychainSecretsProvider())
        recovering = "--auto-recover-secret" in flags
        if recovering:
            matching_blueprint.scripted.append(
                RunResult(
                    argv=[], returncode=0, stdout='{"password": "RECOVERED-SECRET"}', stderr=""
                )
            )

        assert run(self._args(*flags)) == (2 if flags == ("--apply",) else 0)
        output = capsys.readouterr()
        assert "no usable agentBlueprintClientSecret" in output.out
        assert "Restore provider access" not in output.out
        assert "RECOVERED-SECRET" not in output.out + output.err
        assert any("reset" in call for call in matching_blueprint.calls) is recovering
        secret = json.loads((tmp_path / "a365.generated.config.json").read_text())[
            "agentBlueprintClientSecret"
        ]
        assert secret == ("RECOVERED-SECRET" if recovering else None)

    @pytest.mark.parametrize("uppercase", [False, True])
    def test_matching_anchor_missing_secret_still_refuses_apply(
        self, matching_blueprint, capsys, tmp_path, uppercase
    ):
        app_id = "bbbb2222-0000-0000-0000-000000000002"
        record_ownership_anchor(Path(os.environ["HERMES_HOME"]), "Inbox Helper", app_id)
        path = _write_generated(
            tmp_path,
            {
                "agentBlueprintId": app_id.upper() if uppercase else app_id,
                "agentBlueprintClientSecret": None,
            },
        )
        original = path.read_bytes()

        assert run(self._args("--apply")) == 2
        output = capsys.readouterr()
        assert "no usable agentBlueprintClientSecret" in output.out
        assert "ERROR: blueprint client secret is missing" in output.err
        assert "different blueprint" not in output.out
        assert path.read_bytes() == original
        assert all(call[:3] == ["az", "account", "show"] for call in matching_blueprint.calls)
        assert not (tmp_path / CONFIG_FILENAME).exists()

    def test_no_anchor_missing_secret_still_refuses_apply(
        self, matching_blueprint, capsys, tmp_path
    ):
        assert not (Path(os.environ["HERMES_HOME"]) / "agents").exists()
        path = tmp_path / "a365.generated.config.json"
        original = path.read_bytes()

        assert run(self._args("--apply")) == 2
        output = capsys.readouterr()
        assert "no usable agentBlueprintClientSecret" in output.out
        assert "ERROR: blueprint client secret is missing" in output.err
        assert "different blueprint" not in output.out
        assert path.read_bytes() == original
        assert all(call[:3] == ["az", "account", "show"] for call in matching_blueprint.calls)
        assert not (tmp_path / CONFIG_FILENAME).exists()

    @pytest.mark.parametrize("flags", [(), ("--apply",), ("--apply", "--auto-recover-secret")])
    @pytest.mark.parametrize("step_names", [(), ("permissions-bot",)])
    def test_mismatching_anchor_skips_secret_gate(
        self, matching_blueprint, capsys, tmp_path, monkeypatch, flags, step_names
    ):
        anchor_path = record_ownership_anchor(
            Path(os.environ["HERMES_HOME"]),
            "Inbox Helper",
            "bbbb2222-0000-0000-0000-000000000002",
        )
        # An unrelated credential in the anchor file must never reach output.
        with anchor_path.open("a") as env_file:
            env_file.write("BOT_PASSWORD=SENTINEL-ANCHOR-SECRET\n")
        anchor_original = anchor_path.read_bytes()
        path = tmp_path / "a365.generated.config.json"
        _write_generated(
            tmp_path,
            {
                "agentBlueprintId": "cccc3333-0000-0000-0000-000000000003",
                "agentBlueprintClientSecret": None,
                "otherSecret": "SENTINEL-CONFIG-SECRET",
            },
        )
        original = path.read_bytes()
        monkeypatch.setattr(
            "hermes_a365.register.resolve_registration",
            lambda *args, **kwargs: RegistrationDecision(
                action="repair" if step_names else "noop",
                step_names=step_names,
                reason=None,
                messages=("entra: blueprint matches; setup blueprint will not run",),
            ),
        )

        def unexpected_secret_gate(*args, **kwargs):
            pytest.fail("a different blueprint must skip the secret gate entirely")

        def unexpected_recovery(*args, **kwargs):
            pytest.fail("a different blueprint must not trigger secret recovery")

        monkeypatch.setattr(
            "hermes_a365.register._skipped_blueprint_secret_missing", unexpected_secret_gate
        )
        monkeypatch.setattr("hermes_a365.register.auto_recover_secret", unexpected_recovery)

        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        warning = (
            "[warn] The generated config refers to a different blueprint than "
            "this agent's, so the blueprint secret check was skipped."
        )
        assert output.out.count(warning) == 1
        assert "no usable agentBlueprintClientSecret" not in output.out + output.err
        assert output.err == ""
        assert "SENTINEL-ANCHOR-SECRET" not in output.out + output.err
        assert "SENTINEL-CONFIG-SECRET" not in output.out + output.err
        assert path.read_bytes() == original
        assert anchor_path.read_bytes() == anchor_original
        calls = matching_blueprint.calls
        assert not any("reset" in call for call in calls)
        assert all(call[:3] == ["az", "account", "show"] or call[0] == "a365" for call in calls)
        assert [call for call in calls if call[0] == "a365"] == (
            [
                [
                    "a365",
                    "setup",
                    "permissions",
                    "bot",
                    "--agent-name",
                    "Inbox Helper",
                    "--tenant-id",
                    _TEST_TENANT,
                ]
            ]
            if "--apply" in flags and step_names
            else []
        )

    @pytest.mark.parametrize("flags", [(), ("--apply",)])
    def test_present_secret_preserves_output_and_is_never_printed(
        self, matching_blueprint, capsys, tmp_path, flags
    ):
        # Compare to the existing no-signal path to catch any new output.
        path = tmp_path / "a365.generated.config.json"
        path.unlink()
        assert run(self._args(*flags)) == 0
        baseline = capsys.readouterr()
        matching_blueprint.calls.clear()
        _write_generated(
            tmp_path,
            {
                "agentBlueprintId": "bp-app-id",
                "agentBlueprintClientSecret": "SENTINEL-SECRET-DO-NOT-PRINT",
            },
        )
        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        assert output == baseline
        assert "SENTINEL-SECRET-DO-NOT-PRINT" not in output.out + output.err
        assert "no usable agentBlueprintClientSecret" not in output.out + output.err
        if "--apply" in flags:
            assert any(
                call[:4] == ["a365", "setup", "permissions", "bot"]
                for call in matching_blueprint.calls
            )

    def _assert_external_secret_preserves_output(
        self, matching_blueprint, capsys, tmp_path, monkeypatch, flags, dummy
    ):
        def unexpected_recovery(*args, **kwargs):
            pytest.fail("external secret must prevent recovery")

        monkeypatch.setattr("hermes_a365.register.auto_recover_secret", unexpected_recovery)
        path = tmp_path / "a365.generated.config.json"
        original = path.read_bytes()
        path.unlink()
        assert run(self._args(*flags)) == 0
        baseline = capsys.readouterr()
        matching_blueprint.calls.clear()
        path.write_bytes(original)

        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        assert dummy not in output.out + output.err
        assert "no usable agentBlueprintClientSecret" not in output.out + output.err
        assert "blueprint client secret is missing" not in output.out + output.err
        assert output == baseline
        assert path.read_bytes() == original
        assert not any("reset" in call for call in matching_blueprint.calls)

    @pytest.mark.parametrize("flags", [(), ("--apply",), ("--apply", "--auto-recover-secret")])
    def test_provider_hit_skips_secret_gate(
        self, matching_blueprint, capsys, tmp_path, monkeypatch, flags
    ):
        dummy = "dummy-provider-credential"
        lookups = []

        class Provider:
            name = "test-provider"

            def resolve(self, tenant, app_id):
                lookups.append((tenant, app_id))
                return dummy if (tenant, app_id) == (_TEST_TENANT, "bp-app-id") else None

        set_default_provider(Provider())
        self._assert_external_secret_preserves_output(
            matching_blueprint, capsys, tmp_path, monkeypatch, flags, dummy
        )
        assert lookups == [(_TEST_TENANT, "bp-app-id")]

    @pytest.mark.parametrize("flags", [(), ("--apply",), ("--apply", "--auto-recover-secret")])
    @pytest.mark.parametrize("source", ["profile", "process"])
    def test_env_hit_skips_secret_gate(
        self, matching_blueprint, capsys, tmp_path, monkeypatch, flags, source
    ):
        dummy = "dummy-env-credential"
        if source == "profile":
            home = Path(os.environ["HERMES_HOME"])
            home.mkdir()
            env_path = home / ".env"
            env_path.write_text(f"A365_BLUEPRINT_CLIENT_SECRET={dummy}\n")
            env_path.chmod(0o600)
        else:
            monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", dummy)
        self._assert_external_secret_preserves_output(
            matching_blueprint, capsys, tmp_path, monkeypatch, flags, dummy
        )

    @pytest.mark.parametrize("flags", [("--apply",), ("--apply", "--auto-recover-secret")])
    def test_unreadable_profile_env_warns_without_recovery(
        self, matching_blueprint, capsys, tmp_path, monkeypatch, flags
    ):
        dummy = "SENTINEL-UNREADABLE-PROFILE-SECRET"
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir()
        env_path = home / ".env"
        env_path.write_text(f"A365_BLUEPRINT_CLIENT_SECRET={dummy}\n")
        env_path.chmod(0o644)
        lookups = []

        class Provider:
            name = "empty-test-provider"

            def resolve(self, tenant, app_id):
                lookups.append((tenant, app_id))
                return None

        def unexpected_recovery(*args, **kwargs):
            pytest.fail("unreadable profile env must prevent recovery")

        set_default_provider(Provider())
        monkeypatch.setattr("hermes_a365.register.auto_recover_secret", unexpected_recovery)
        path = tmp_path / "a365.generated.config.json"
        original = path.read_bytes()
        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        assert (
            "[warn] Blueprint secret could not be confirmed because the profile "
            "env file could not be read (BridgeConfigError). "
            "Check profile env file access and re-run.\n"
        ) in output.out
        assert dummy not in output.out + output.err
        assert "no usable agentBlueprintClientSecret" not in output.out + output.err
        assert "blueprint client secret is missing" not in output.out + output.err
        assert lookups == [(_TEST_TENANT, "bp-app-id")]
        assert path.read_bytes() == original
        assert not any("reset" in call for call in matching_blueprint.calls)
        assert any(
            call[:4] == ["a365", "setup", "permissions", "bot"] for call in matching_blueprint.calls
        )

    @pytest.mark.parametrize("flags", [(), ("--apply",), ("--apply", "--auto-recover-secret")])
    def test_unreadable_profile_env_provider_hit_clears_warning(
        self, matching_blueprint, capsys, tmp_path, monkeypatch, flags
    ):
        dummy = "SENTINEL-UNREADABLE-PROFILE-SECRET"
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir()
        env_path = home / ".env"
        env_path.write_text(f"A365_BLUEPRINT_CLIENT_SECRET={dummy}\n")
        env_path.chmod(0o644)
        lookups = []

        class Provider:
            name = "test-provider"
            value = None

            def resolve(self, tenant, app_id):
                lookups.append((tenant, app_id))
                return self.value

        provider = Provider()
        set_default_provider(provider)
        # The same unreadable file warns only while no source confirms a secret.
        assert run(self._args()) == 0
        output = capsys.readouterr()
        assert "could not be confirmed" in output.out
        assert "profile env file could not be read (BridgeConfigError)" in output.out
        assert dummy not in output.out + output.err

        provider.value = dummy
        self._assert_external_secret_preserves_output(
            matching_blueprint, capsys, tmp_path, monkeypatch, flags, provider.value
        )
        assert lookups == [(_TEST_TENANT, "bp-app-id")] * 2

    @pytest.mark.parametrize("flags", [("--apply",), ("--apply", "--auto-recover-secret")])
    def test_provider_unreachable_warns_without_recovery(
        self, matching_blueprint, capsys, tmp_path, monkeypatch, flags
    ):
        class Provider:
            name = "unreachable-test-provider"

            def resolve(self, tenant, app_id):
                raise RuntimeError("provider unavailable")

        def unexpected_recovery(*args, **kwargs):
            pytest.fail("unreachable provider must prevent recovery")

        set_default_provider(Provider())
        monkeypatch.setattr("hermes_a365.register.auto_recover_secret", unexpected_recovery)
        path = tmp_path / "a365.generated.config.json"
        original = path.read_bytes()
        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        assert "[warn]" in output.out
        assert "secrets provider was unreachable" in output.out
        assert "Restore provider access and re-run" in output.out
        assert "no usable agentBlueprintClientSecret" not in output.out + output.err
        assert "blueprint client secret is missing" not in output.out + output.err
        assert path.read_bytes() == original
        assert not any("reset" in call for call in matching_blueprint.calls)
        assert any(
            call[:4] == ["a365", "setup", "permissions", "bot"] for call in matching_blueprint.calls
        )

    @pytest.mark.parametrize("recovered", [True, False])
    def test_auto_recovery_precedes_steps_and_failure_stops_apply(
        self, matching_blueprint, capsys, tmp_path, recovered
    ):
        matching_blueprint.scripted.append(
            RunResult(
                argv=[],
                returncode=0,
                stdout=json.dumps({"password": "RECOVERED-SECRET"} if recovered else {}),
                stderr="",
            )
        )
        assert run(self._args("--apply", "--auto-recover-secret")) == (0 if recovered else 2)
        output = capsys.readouterr()
        assert "RECOVERED-SECRET" not in output.out + output.err
        calls = matching_blueprint.calls
        reset_index = next(i for i, call in enumerate(calls) if "reset" in call)
        assert matching_blueprint.sensitive_flags[reset_index] is True
        assert "RECOVERED-SECRET" not in str(calls)
        steps = [i for i, call in enumerate(calls) if call[0] == "a365"]
        if recovered:
            assert steps and all(i > reset_index for i in steps)
            assert (
                json.loads((tmp_path / "a365.generated.config.json").read_text())[
                    "agentBlueprintClientSecret"
                ]
                == "RECOVERED-SECRET"
            )
        else:
            assert not steps
            assert not (tmp_path / CONFIG_FILENAME).exists()


class TestPostBlueprintSecretSources:
    """#289 — after the blueprint step, consult configured secret sources."""

    @pytest.fixture
    def blueprint_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        mutator = FakeMutator()
        monkeypatch.setattr("hermes_a365.register.get_mutator", lambda: mutator)
        monkeypatch.setattr(
            "hermes_a365.register.resolve_registration",
            lambda *args, **kwargs: RegistrationDecision(
                action="create",
                step_names=("blueprint",),
                reason=None,
                messages=(),
            ),
        )

        def fake_apply(plan, **kwargs):
            path = _write_generated(
                tmp_path,
                {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
            )
            path.chmod(0o600)
            return ApplyResult(plan=plan, completed=["blueprint"])

        monkeypatch.setattr("hermes_a365.register.apply_register_plan", fake_apply)
        return mutator

    @staticmethod
    def _args(*flags: str):
        return build_parser().parse_args(
            [
                "--agent-name",
                "Inbox Helper",
                "--tenant-id",
                _TEST_TENANT,
                "--m365",
                "--apply",
                *flags,
            ]
        )

    @staticmethod
    def _forbid_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
        def unexpected_recovery(*args, **kwargs):
            pytest.fail("a configured secret source must prevent recovery")

        monkeypatch.setattr("hermes_a365.register.auto_recover_secret", unexpected_recovery)

    @pytest.mark.parametrize("flags", [(), ("--auto-recover-secret",)])
    def test_provider_hit_skips_notice_and_recovery(
        self, blueprint_run, capsys, monkeypatch, flags
    ):
        dummy = "dummy-provider-credential"
        lookups = []

        class Provider:
            name = "test-provider"

            def resolve(self, tenant, app_id):
                lookups.append((tenant, app_id))
                return dummy

        set_default_provider(Provider())
        self._forbid_recovery(monkeypatch)
        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        assert lookups == [(_TEST_TENANT, "bp-app-id")]
        assert dummy not in output.out + output.err
        assert "agentBlueprintClientSecret" not in output.out + output.err
        assert "done. Next:" in output.out

    @pytest.mark.parametrize("flags", [(), ("--auto-recover-secret",)])
    @pytest.mark.parametrize("source", ["profile", "process"])
    def test_env_hit_skips_notice_and_recovery(
        self, blueprint_run, capsys, monkeypatch, tmp_path, flags, source
    ):
        dummy = "dummy-env-credential"
        if source == "profile":
            home = Path(os.environ["HERMES_HOME"])
            home.mkdir()
            env_path = home / ".env"
            env_path.write_text(f"A365_BLUEPRINT_CLIENT_SECRET={dummy}\n")
            env_path.chmod(0o600)
        else:
            monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", dummy)
        self._forbid_recovery(monkeypatch)
        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        assert dummy not in output.out + output.err
        assert "agentBlueprintClientSecret" not in output.out + output.err
        assert "done. Next:" in output.out

    @pytest.mark.parametrize("flags", [(), ("--auto-recover-secret",)])
    def test_provider_unreachable_warns_without_recovery(
        self, blueprint_run, capsys, monkeypatch, flags
    ):
        class Provider:
            name = "unreachable-test-provider"

            def resolve(self, tenant, app_id):
                raise RuntimeError("provider unavailable")

        set_default_provider(Provider())
        self._forbid_recovery(monkeypatch)
        assert run(self._args(*flags)) == 0
        output = capsys.readouterr()
        assert "secrets provider was unreachable" in output.out
        assert "Restore provider access and re-run" in output.out
        assert "agentBlueprintClientSecret" not in output.out + output.err
        assert not any("reset" in call for call in blueprint_run.calls)

    def test_no_source_keeps_missing_secret_notice(self, blueprint_run, capsys, tmp_path):
        assert run(self._args()) == 0
        output = capsys.readouterr()
        expected = report_missing_secret_warning("bp-app-id", Path("a365.generated.config.json"))
        assert expected in output.out

    @pytest.mark.parametrize("platform", ["win32", "linux"])
    @pytest.mark.parametrize("recovering", [False, True])
    def test_unsupported_default_backend_keeps_notice_and_recovery(
        self, blueprint_run, capsys, monkeypatch, tmp_path, platform, recovering
    ):
        monkeypatch.setattr("hermes_a365.keychain.sys.platform", platform)
        monkeypatch.setattr("hermes_a365.keychain.shutil.which", lambda binary: None)
        set_default_provider(KeychainSecretsProvider())
        if recovering:
            blueprint_run.scripted.append(
                RunResult(
                    argv=[], returncode=0, stdout='{"password": "RECOVERED-SECRET"}', stderr=""
                )
            )

        assert run(self._args(*(["--auto-recover-secret"] if recovering else []))) == 0
        output = capsys.readouterr()
        assert "Restore provider access" not in output.out
        assert "RECOVERED-SECRET" not in output.out + output.err
        if not recovering:
            expected = report_missing_secret_warning(
                "bp-app-id", Path("a365.generated.config.json")
            )
            assert expected in output.out
        resets = [i for i, call in enumerate(blueprint_run.calls) if "reset" in call]
        assert len(resets) == int(recovering)
        if recovering:
            assert blueprint_run.sensitive_flags[resets[0]] is True
        path = tmp_path / "a365.generated.config.json"
        assert json.loads(path.read_text())["agentBlueprintClientSecret"] == (
            "RECOVERED-SECRET" if recovering else None
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestPlanRender:
    def test_human_lists_steps_and_argv(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="inbox-helper"))
        text = plan.render_human()
        assert "[plan] hermes a365 register inbox-helper" in text
        assert "blueprint" in text
        assert "permissions-mcp" in text
        assert "permissions-bot" in text
        assert "$ a365 setup blueprint --agent-name inbox-helper" in text

    def test_human_says_apply_refuses_without_a_tenant_pin(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        text = plan.render_human()
        assert "no canonical pin" in text
        assert "refuses" in text
        assert "auto-detect" not in text

    def test_human_notes_that_m365_does_not_register_the_endpoint(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x", m365=True, tenant_id="tid"))
        text = plan.render_human()
        assert "--m365" in text
        assert "does not register the messaging endpoint" in text
        assert "update-endpoint" in text

    def test_human_shows_explicit_tenant(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x", tenant_id="foo"))
        assert "tenant: foo" in plan.render_human()

    def test_human_shell_quotes_multi_word_agent_name(self) -> None:
        """Slice 18p (bug #7): operators copy-pasting the printed `$` line
        need a working shell command. ``Hermes Inbox Helper`` must come
        out as one quoted argument."""
        plan = build_register_plan(RegisterInputs(agent_name="Hermes Inbox Helper"))
        text = plan.render_human()
        # `shlex.join` typically quotes with single quotes on POSIX.
        assert "--agent-name 'Hermes Inbox Helper'" in text
        # Negative: the broken form is gone.
        assert "--agent-name Hermes Inbox Helper " not in text


# ---------------------------------------------------------------------------
# apply_register_plan — happy path
# ---------------------------------------------------------------------------


class TestApplyHappyPath:
    def test_runs_three_steps_in_order(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="inbox-helper"))
        mutator = FakeMutator()
        result = apply_register_plan(plan, mutator=mutator)

        assert isinstance(result, ApplyResult)
        assert result.completed == ["blueprint", "permissions-mcp", "permissions-bot"]
        assert result.consent_deferred is False
        assert result.not_run == []
        # Mutator received argv lists matching the plan steps in order.
        step_calls = [argv for argv in mutator.calls if argv[0] == "a365"]
        assert [argv[2] for argv in step_calls] == [
            "blueprint",
            "permissions",
            "permissions",
        ]
        assert len(step_calls) == 3

    def test_messages_capture_each_step(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        result = apply_register_plan(plan, mutator=FakeMutator())
        assert any("blueprint" in m for m in result.messages)
        assert any("permissions-mcp" in m for m in result.messages)
        assert any("permissions-bot" in m for m in result.messages)

    def test_raw_outputs_keyed_by_step_name(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = FakeMutator(
            scripted=[
                RunResult(argv=["a"], returncode=0, stdout="bp ok", stderr=""),
                RunResult(argv=["a"], returncode=0, stdout="mcp ok", stderr=""),
                RunResult(argv=["a"], returncode=0, stdout="bot ok", stderr=""),
            ]
        )
        result = apply_register_plan(plan, mutator=mutator)
        assert result.raw_outputs["blueprint"].stdout == "bp ok"
        assert result.raw_outputs["permissions-mcp"].stdout == "mcp ok"
        assert result.raw_outputs["permissions-bot"].stdout == "bot ok"


class TestIssue256BlueprintSensitiveOutput:
    """#256 — ``a365 setup blueprint`` output is treated as sensitive."""

    _SENTINEL = "SENTINEL-BP-SECRET-256"
    _DEVICE_LINE = (
        "To sign in, use a web browser to open https://login.microsoft.com/device "
        "and enter the code ABCD1234 to authenticate."
    )

    @staticmethod
    def _fake_blueprint_only(*_args: object, **_kwargs: object) -> RegistrationDecision:
        return RegistrationDecision(
            action="create",
            step_names=("blueprint",),
            reason=None,
            messages=(),
        )

    @staticmethod
    def _fake_blueprint_repair(*_args: object, **_kwargs: object) -> RegistrationDecision:
        return RegistrationDecision(
            action="repair",
            step_names=("blueprint",),
            reason=None,
            messages=("entra: repair blueprint",),
        )

    def test_blueprint_step_uses_sensitive_flag(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = FakeMutator()
        apply_register_plan(plan, mutator=mutator)
        for call, sensitive in zip(mutator.calls, mutator.sensitive_flags, strict=True):
            if call[:3] == ["a365", "setup", "blueprint"]:
                assert sensitive is True
            elif call[0] == "a365":
                assert sensitive is False

    def test_apply_first_run_suppresses_secret_streams_device_code(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        body = (
            f"echo '{self._DEVICE_LINE}'\n"
            f"echo 'Client secret created: {self._SENTINEL}'\n"
            f"echo '{self._SENTINEL}' 1>&2"
        )
        _install_register_cli_shims(tmp_path, monkeypatch, a365_blueprint_body=body)
        monkeypatch.setattr(
            "hermes_a365.register.resolve_registration",
            self._fake_blueprint_only,
        )
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = A365CliMutator()
        mutator.available = True
        with caplog.at_level("INFO"):
            result = apply_register_plan(plan, mutator=mutator)
        out, err = capfd.readouterr()
        assert result.completed == ["blueprint"]
        assert self._SENTINEL not in out
        assert self._SENTINEL not in err
        assert self._SENTINEL not in caplog.text
        assert "ABCD1234" in out
        assert "To sign in, open https://login.microsoft.com/device" in out
        assert self._SENTINEL in result.raw_outputs["blueprint"].stdout

    def test_apply_repair_suppresses_secret_streams_device_code(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        body = (
            f"echo '{self._DEVICE_LINE}'\n"
            f"printf '%s\\n' '{self._SENTINEL}'\n"
            f"echo 'stderr-{self._SENTINEL}' 1>&2"
        )
        _install_register_cli_shims(tmp_path, monkeypatch, a365_blueprint_body=body)
        monkeypatch.setattr(
            "hermes_a365.register.resolve_registration",
            self._fake_blueprint_repair,
        )
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = A365CliMutator()
        mutator.available = True
        with caplog.at_level("INFO"):
            result = apply_register_plan(plan, mutator=mutator)
        out, err = capfd.readouterr()
        assert result.completed == ["blueprint"]
        assert self._SENTINEL not in out + err + caplog.text
        assert "login.microsoft.com/device" in out


# ---------------------------------------------------------------------------
# AADSTS handling
# ---------------------------------------------------------------------------


class TestApplyAADSTSConsentDeferred:
    def test_consent_required_at_permissions_step_is_deferred_not_raised(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = FakeMutator(
            scripted=[
                RunResult(argv=["a"], returncode=0, stdout="bp ok", stderr=""),
                AADSTSError(AADSTS_CONSENT_REQUIRED, "admin consent required"),
            ]
        )
        result = apply_register_plan(plan, mutator=mutator)
        assert result.consent_deferred is True
        assert result.completed == ["blueprint"]
        assert result.not_run == ["permissions-mcp", "permissions-bot"]
        assert any("AADSTS90094" in m for m in result.messages)

    def test_other_aadsts_codes_propagate(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = FakeMutator(scripted=[AADSTSError("AADSTS65001", "scope not consented")])
        with pytest.raises(AADSTSError) as excinfo:
            apply_register_plan(plan, mutator=mutator)
        assert excinfo.value.code == "AADSTS65001"


class TestApplyAADSTS500011Retry:
    def test_retries_until_success(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = FakeMutator(
            scripted=[
                AADSTSError(AADSTS_LICENSE_NOT_PROPAGATED, "license not propagated"),
                AADSTSError(AADSTS_LICENSE_NOT_PROPAGATED, "license not propagated"),
                RunResult(argv=["a"], returncode=0, stdout="ok", stderr=""),  # blueprint
                RunResult(argv=["a"], returncode=0, stdout="ok", stderr=""),  # mcp
                RunResult(argv=["a"], returncode=0, stdout="ok", stderr=""),  # bot
            ]
        )
        sleeper = _SleepRecorder()
        result = apply_register_plan(
            plan,
            mutator=mutator,
            retries=3,
            backoff=30.0,
            sleep_fn=sleeper,
        )
        assert result.completed == ["blueprint", "permissions-mcp", "permissions-bot"]
        # Two sleeps between three attempts on the blueprint step.
        assert sleeper.calls == [30.0, 30.0]

    def test_raises_after_retries_exhausted(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = FakeMutator(
            scripted=[
                AADSTSError(AADSTS_LICENSE_NOT_PROPAGATED, "license"),
                AADSTSError(AADSTS_LICENSE_NOT_PROPAGATED, "license"),
                AADSTSError(AADSTS_LICENSE_NOT_PROPAGATED, "license"),
                AADSTSError(AADSTS_LICENSE_NOT_PROPAGATED, "license"),
            ]
        )
        sleeper = _SleepRecorder()
        with pytest.raises(AADSTSError) as excinfo:
            apply_register_plan(
                plan,
                mutator=mutator,
                retries=3,
                backoff=30.0,
                sleep_fn=sleeper,
            )
        assert excinfo.value.code == AADSTS_LICENSE_NOT_PROPAGATED
        assert sleeper.calls == [30.0, 30.0, 30.0]


# ---------------------------------------------------------------------------
# Other CLI failures propagate
# ---------------------------------------------------------------------------


class TestNonAADSTSFailure:
    def test_cli_invocation_error_propagates(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        mutator = FakeMutator(scripted=[CliInvocationError(["a365"], 7, "weird crash")])
        with pytest.raises(CliInvocationError):
            apply_register_plan(plan, mutator=mutator)


# ---------------------------------------------------------------------------
# Default constants pinned
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_retries_and_backoff(self) -> None:
        # Documented in the docstring + CLI help — pin the values.
        assert DEFAULT_RETRIES == 3
        assert DEFAULT_BACKOFF_SECONDS == 30.0


# ---------------------------------------------------------------------------
# update_config_for_agent
# ---------------------------------------------------------------------------


class TestUpdateConfig:
    def test_writes_derived_display_names(self, tmp_path: Path) -> None:
        path = tmp_path / CONFIG_FILENAME
        inputs = RegisterInputs(agent_name="inbox-helper")
        update_config_for_agent(path, inputs)
        on_disk = json.loads(path.read_text())
        assert on_disk["agentIdentityDisplayName"] == "inbox-helper Identity"
        assert on_disk["agentBlueprintDisplayName"] == "inbox-helper Blueprint"

    def test_preserves_existing_unrelated_fields(self, tmp_path: Path) -> None:
        path = tmp_path / CONFIG_FILENAME
        path.write_text(
            json.dumps(
                {
                    "tenantId": "existing-tenant",
                    "subscriptionId": "existing-sub",
                    "agentDescription": "do not lose me",
                }
            )
        )
        update_config_for_agent(path, RegisterInputs(agent_name="x"))
        on_disk = json.loads(path.read_text())
        assert on_disk["tenantId"] == "existing-tenant"
        assert on_disk["subscriptionId"] == "existing-sub"
        assert on_disk["agentDescription"] == "do not lose me"
        assert on_disk["agentBlueprintDisplayName"] == "x Blueprint"

    def test_tenant_id_written_when_provided(self, tmp_path: Path) -> None:
        path = tmp_path / CONFIG_FILENAME
        update_config_for_agent(
            path, RegisterInputs(agent_name="x", tenant_id="contoso.onmicrosoft.com")
        )
        on_disk = json.loads(path.read_text())
        assert on_disk["tenantId"] == "contoso.onmicrosoft.com"


# ---------------------------------------------------------------------------
# Sanity on RegisterStep dataclass
# ---------------------------------------------------------------------------


def test_register_step_is_a_dataclass() -> None:
    step = RegisterStep(name="x", argv=["a"], description="d")
    assert step.name == "x"
    assert step.description == "d"


# ---------------------------------------------------------------------------
# Slice 19s (#14) — missing-secret detection + auto-recover
# ---------------------------------------------------------------------------


def _write_generated(tmp_path: Path, payload: dict[str, object]) -> Path:
    p = tmp_path / "a365.generated.config.json"
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return p


def _install_az_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Put a fake ``az`` on PATH whose body is ``script`` (sh).

    Lets CS-003 tests drive the *real* ``A365CliMutator`` subprocess
    paths with fake credential material — no live Azure involved.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "az"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "ad app show" ]; then\n'
        '  printf \'%s\\n\' \'{"appId":"bp-app-id","id":"obj-id"}\'\n'
        "  exit 0\n"
        "fi\n" + script + "\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _install_register_cli_shims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    a365_blueprint_body: str,
    tenant_id: str = _TEST_TENANT,
) -> None:
    """Fake ``az`` + ``a365`` on PATH for real-mutator register apply tests."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    az = bin_dir / "az"
    az.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "account show" ]; then\n'
        f"  printf '%s\\n' '{{\"tenantId\":\"{tenant_id}\"}}'\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected az: $*" >&2\n'
        "exit 1\n"
    )
    az.chmod(0o755)
    a365 = bin_dir / "a365"
    a365.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "setup blueprint" ]; then\n'
        f"{a365_blueprint_body}\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2" = "setup permissions" ]; then\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    a365.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


class TestDetectMissingSecret:
    """The bug shape: `agentBlueprintId` populated, secret null/empty.

    These cases pin the detection contract — the whole layer 1 fix
    hangs on getting this matrix right.
    """

    def test_detected_when_id_set_and_secret_null(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        is_missing, bp_id = detect_missing_secret(path)
        assert is_missing is True
        assert bp_id == "bp-app-id"

    def test_detected_when_id_set_and_secret_empty_string(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": ""},
        )
        is_missing, bp_id = detect_missing_secret(path)
        assert is_missing is True
        assert bp_id == "bp-app-id"

    def test_detected_when_id_set_and_secret_key_missing(self, tmp_path: Path) -> None:
        path = _write_generated(tmp_path, {"agentBlueprintId": "bp-app-id"})
        is_missing, bp_id = detect_missing_secret(path)
        assert is_missing is True
        assert bp_id == "bp-app-id"

    def test_not_detected_when_secret_populated(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": "real"},
        )
        is_missing, _bp_id = detect_missing_secret(path)
        assert is_missing is False

    def test_not_detected_when_blueprint_id_missing(self, tmp_path: Path) -> None:
        # No ``agentBlueprintId`` means ``setup blueprint`` never ran (or
        # the file is from a previous run state); we can't recover what
        # we can't address by id, so treat as no-signal.
        path = _write_generated(tmp_path, {"agentBlueprintClientSecret": None})
        is_missing, bp_id = detect_missing_secret(path)
        assert is_missing is False
        assert bp_id is None

    def test_not_detected_when_file_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "nope.json"
        is_missing, bp_id = detect_missing_secret(path)
        assert is_missing is False
        assert bp_id is None

    def test_not_detected_when_file_unreadable_json(self, tmp_path: Path) -> None:
        path = tmp_path / "a365.generated.config.json"
        path.write_text("{not valid json")
        is_missing, _bp_id = detect_missing_secret(path)
        assert is_missing is False

    def test_not_detected_when_top_level_is_array(self, tmp_path: Path) -> None:
        path = tmp_path / "a365.generated.config.json"
        path.write_text("[]")
        is_missing, _bp_id = detect_missing_secret(path)
        assert is_missing is False


class TestAutoRecoverSecret:
    """Auto-recover runs ``az ad app credential reset --append`` then
    patches the generated config. Cases here pin the argv shape, the
    JSON parsing of az output, the file mode, and the failure paths."""

    _AZ_OK_PAYLOAD = json.dumps(
        {
            "appId": "bp-app-id",
            "password": "FRESH-SECRET-VALUE",
            "tenant": "contoso.onmicrosoft.com",
            "keyId": "key-uuid",
        }
    )

    def test_argv_shape_includes_append_and_id(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout=self._AZ_OK_PAYLOAD, stderr="")]
        )
        auto_recover_secret(path, "bp-app-id", mutator=mutator, display_name="recovery-test")
        assert mutator.calls, "az should have been invoked"
        argv = mutator.calls[1]
        assert argv[:5] == ["az", "ad", "app", "credential", "reset"]
        assert "--append" in argv
        assert "--id" in argv and argv[argv.index("--id") + 1] == "bp-app-id"
        assert "--display-name" in argv
        assert argv[argv.index("--display-name") + 1] == "recovery-test"
        assert "-o" in argv and argv[argv.index("-o") + 1] == "json"

    def test_patches_secret_into_generated_config(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout=self._AZ_OK_PAYLOAD, stderr="")]
        )
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert outcome.recovered is True
        on_disk = json.loads(path.read_text())
        assert on_disk["agentBlueprintClientSecret"] == "FRESH-SECRET-VALUE"

    def test_chmods_file_to_0600(self, tmp_path: Path) -> None:
        # The file likely starts at the test runner's umask default,
        # which on most CI runners is ``0o644``. Auto-recover must
        # tighten it because the secret lives plaintext on macOS/Linux
        # (no DPAPI).
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        path.chmod(0o644)
        assert (path.stat().st_mode & 0o777) == 0o644
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout=self._AZ_OK_PAYLOAD, stderr="")]
        )
        auto_recover_secret(path, "bp-app-id", mutator=mutator, display_name="recovery-test")
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_credential_reset_runs_sensitive(self, tmp_path: Path) -> None:
        # CS-003 (#111): the reset's output IS the secret, so the call
        # must take the captured (non-streaming) mutator path.
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout=self._AZ_OK_PAYLOAD, stderr="")]
        )
        auto_recover_secret(path, "bp-app-id", mutator=mutator, display_name="recovery-test")
        assert mutator.sensitive_flags == [False, True]

    def test_secret_never_reaches_stdout_with_real_mutator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        # CS-003 (#111) end-to-end: fake `az` emits the minted secret on
        # its stdout; the real A365CliMutator must not echo one byte of
        # it to the parent's stdout/stderr (terminals, CI logs,
        # transcripts) while the config still gets patched.
        marker = "FAKE-MINTED-SECRET-xK9"
        _install_az_shim(
            tmp_path,
            monkeypatch,
            f'echo \'{{"appId": "bp-app-id", "password": "{marker}"}}\'',
        )
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")  # skip __init__ probing
        mutator = A365CliMutator()
        mutator.available = True  # bypass the a365-on-PATH check; az is the shim
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        out, err = capfd.readouterr()
        assert marker not in out
        assert marker not in err
        assert outcome.recovered is True
        assert json.loads(path.read_text())["agentBlueprintClientSecret"] == marker
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_failed_reset_never_leaks_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        # CS-003 (#111) failure path: az can emit credential material and
        # STILL exit non-zero; the error surfaced to the operator (and
        # printed via outcome.messages) must carry none of it.
        marker = "FAKE-PARTIAL-SECRET-zQ2"
        _install_az_shim(tmp_path, monkeypatch, f"echo 'oops {marker}'; exit 3")
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")
        mutator = A365CliMutator()
        mutator.available = True
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        out, err = capfd.readouterr()
        assert marker not in out
        assert marker not in err
        assert outcome.recovered is False
        assert all(marker not in m for m in outcome.messages)
        assert any("az ad app credential reset" in m for m in outcome.messages)

    def test_credential_reset_sensitive_failure_has_no_raw_capture(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = "FAKE-PARTIAL-SECRET-zQ2"
        _install_az_shim(tmp_path, monkeypatch, f"echo 'oops {marker}'; exit 3")
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")
        mutator = A365CliMutator()
        mutator.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            mutator.run(
                ["az", "ad", "app", "credential", "reset", "--id", "x"],
                sensitive=True,
            )
        assert marker not in repr(excinfo.value)
        assert marker not in str(excinfo.value)
        assert excinfo.value.captured_output is None

    def test_temp_file_owner_only_under_umask_022(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CS-004 (#112): the temp file must already be 0600 at the moment
        # of the atomic replace — i.e. before the secret ever sat on disk
        # at a permissive mode. Captured via an os.replace spy.
        old = os.umask(0o022)
        try:
            seen: dict[str, int] = {}
            real_replace = os.replace

            def spy_replace(src: object, dst: object) -> None:
                seen["tmp_mode"] = stat.S_IMODE(os.stat(src).st_mode)  # type: ignore[arg-type]
                real_replace(src, dst)  # type: ignore[arg-type]

            monkeypatch.setattr(os, "replace", spy_replace)
            path = _write_generated(
                tmp_path,
                {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
            )
            mutator = FakeMutator(
                scripted=[RunResult(argv=[], returncode=0, stdout=self._AZ_OK_PAYLOAD, stderr="")]
            )
            auto_recover_secret(path, "bp-app-id", mutator=mutator, display_name="recovery-test")
            assert seen["tmp_mode"] == 0o600
            assert (path.stat().st_mode & 0o777) == 0o600
        finally:
            os.umask(old)

    def test_preserves_other_fields(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {
                "agentBlueprintId": "bp-app-id",
                "agentBlueprintClientSecret": None,
                "agentBlueprintObjectId": "obj-id",
                "botMsaAppId": "bot-app-id",
                "messagingEndpoint": "https://example.test/api/messages",
            },
        )
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout=self._AZ_OK_PAYLOAD, stderr="")]
        )
        auto_recover_secret(path, "bp-app-id", mutator=mutator, display_name="recovery-test")
        on_disk = json.loads(path.read_text())
        assert on_disk["agentBlueprintObjectId"] == "obj-id"
        assert on_disk["botMsaAppId"] == "bot-app-id"
        assert on_disk["messagingEndpoint"] == "https://example.test/api/messages"
        assert on_disk["agentBlueprintClientSecret"] == "FRESH-SECRET-VALUE"

    def test_failure_when_az_returns_no_password(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        mutator = FakeMutator(
            scripted=[
                RunResult(
                    argv=[],
                    returncode=0,
                    stdout=json.dumps({"appId": "bp-app-id", "tenant": "t"}),
                    stderr="",
                )
            ]
        )
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert outcome.recovered is False
        # Original null preserved — we don't blank the file on partial failure.
        on_disk = json.loads(path.read_text())
        assert on_disk["agentBlueprintClientSecret"] is None
        assert any("no `.password`" in m for m in outcome.messages)

    def test_recovers_when_az_stdout_has_warning_preamble(self, tmp_path: Path) -> None:
        """Live regression caught 2026-05-07 round-6 §9d-style walkthrough.

        ``az`` writes a credential-protection ``WARNING:`` line to stderr
        whenever ``-o json`` returns a password. The mutator's
        ``_run_streaming`` (slice 18j) merges stderr into stdout, so
        ``run.stdout`` is::

            WARNING: The output includes credentials that you must protect...
            {"appId":"...","password":"...","tenant":"..."}

        The first cut of ``auto_recover_secret`` did
        ``json.loads(run.stdout)`` on the raw stream, raised
        ``JSONDecodeError`` on the WARNING line, and silently fell
        through to "no .password". Pin the working extractor here.
        """
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        live_az_output = (
            "WARNING: The output includes credentials that you must protect. "
            "Be sure that you do not include these credentials in your code "
            "or check the credentials into your source control. For more "
            "information, see https://aka.ms/azadsp-cli\n"
            "{\n"
            '  "appId": "bp-app-id",\n'
            '  "password": "RV88Q~MOCK-SECRET",\n'
            '  "tenant": "tenant-id"\n'
            "}\n"
        )
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout=live_az_output, stderr="")]
        )
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert outcome.recovered is True, outcome.messages
        on_disk = json.loads(path.read_text())
        assert on_disk["agentBlueprintClientSecret"] == "RV88Q~MOCK-SECRET"

    def test_recovers_when_az_stdout_has_trailing_diagnostics(self, tmp_path: Path) -> None:
        """Belt-and-braces: the JSON object is consumed even when other
        diagnostic lines follow it. ``json.JSONDecoder.raw_decode`` is
        tolerant of trailing content; ``json.loads`` is not."""
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        out = (
            '{"appId":"bp-app-id","password":"FRESH-SECRET","tenant":"t"}\n'
            "Note: credential will be appended to existing list.\n"
        )
        mutator = FakeMutator(scripted=[RunResult(argv=[], returncode=0, stdout=out, stderr="")])
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert outcome.recovered is True
        assert json.loads(path.read_text())["agentBlueprintClientSecret"] == "FRESH-SECRET"

    def test_failure_when_az_returns_unparseable_json(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout="not json at all", stderr="")]
        )
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert outcome.recovered is False
        on_disk = json.loads(path.read_text())
        assert on_disk["agentBlueprintClientSecret"] is None

    def test_failure_when_az_invocation_errors(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        mutator = FakeMutator(scripted=[CliInvocationError(["az"], 1, "permission denied")])
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert outcome.recovered is False
        assert outcome.detected is True
        assert any("recover by hand" in m for m in outcome.messages)

    def test_outcome_carries_app_id(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout=self._AZ_OK_PAYLOAD, stderr="")]
        )
        outcome = auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert isinstance(outcome, SecretRecoveryOutcome)
        assert outcome.blueprint_app_id == "bp-app-id"


class TestReportMissingSecretWarning:
    def test_includes_az_credential_reset_append(self, tmp_path: Path) -> None:
        path = tmp_path / "a365.generated.config.json"
        msg = report_missing_secret_warning("bp-app-id", path)
        assert "az ad app credential reset" in msg
        assert "--append" in msg
        assert "--id bp-app-id" in msg

    def test_mentions_issue_14(self, tmp_path: Path) -> None:
        msg = report_missing_secret_warning("bp-app-id", tmp_path / "a365.generated.config.json")
        assert "#14" in msg

    def test_mentions_auto_recover_flag(self, tmp_path: Path) -> None:
        msg = report_missing_secret_warning("bp-app-id", tmp_path / "a365.generated.config.json")
        assert "--auto-recover-secret" in msg

    def test_includes_config_path_in_patch_hint(self, tmp_path: Path) -> None:
        path = tmp_path / "a365.generated.config.json"
        msg = report_missing_secret_warning("bp-app-id", path)
        assert str(path) in msg

    def test_no_argv_secret_sink(self, tmp_path: Path) -> None:
        # CS-005 (#113): guidance must never tell the operator to put the
        # secret on a command line — argv lands in shell history and the
        # process list.
        msg = report_missing_secret_warning("bp-app-id", tmp_path / "a365.generated.config.json")
        assert "<paste-password>" not in msg
        assert "sys.argv[2]" not in msg

    def test_patch_hint_uses_hidden_prompt_and_exclusive_create(self, tmp_path: Path) -> None:
        msg = report_missing_secret_warning("bp-app-id", tmp_path / "a365.generated.config.json")
        assert "getpass" in msg
        assert "O_EXCL" in msg

    @staticmethod
    def _patch_argv(msg: str) -> list[str]:
        """Recover the manual patch command's argv by tokenising the
        rendered shell line exactly as a POSIX shell would (shlex.split).

        Review P1: the whole command is rendered with shlex.join, so this
        round-trips to ``['python3', '-c', <code>, <config-path>]`` — and
        the path survives as one token even with spaces/metacharacters.
        """
        line = next(ln for ln in msg.splitlines() if "python3 -c" in ln).strip()
        return shlex.split(line)

    def test_patch_hint_command_is_valid_python(self, tmp_path: Path) -> None:
        msg = report_missing_secret_warning("bp-app-id", tmp_path / "a365.generated.config.json")
        argv = self._patch_argv(msg)
        assert argv[0] == "python3" and argv[1] == "-c"
        compile(argv[2], "<hint>", "exec")

    def test_patch_hint_path_is_one_token_with_spaces(self, tmp_path: Path) -> None:
        # Review P1: a config path with a space must not shell-split into
        # multiple argv entries (which would make sys.argv[1] wrong).
        path = tmp_path / "weird dir" / "a365.generated.config.json"
        msg = report_missing_secret_warning("bp-app-id", path)
        argv = self._patch_argv(msg)
        assert argv[-1] == str(path)
        assert len(argv) == 4

    def test_patch_hint_patches_via_stdin_prompt(self, tmp_path: Path) -> None:
        # Run the emitted one-liner for real: the secret arrives on stdin
        # (getpass falls back to stdin without a tty), never argv, and
        # the file ends up owner-only.
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        msg = report_missing_secret_warning("bp-app-id", path)
        argv = self._patch_argv(msg)
        proc = subprocess.run(
            [sys.executable, *argv[1:]],
            input="PASTED-FAKE-SECRET\n",
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        on_disk = json.loads(path.read_text())
        assert on_disk["agentBlueprintClientSecret"] == "PASTED-FAKE-SECRET"
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_patch_hint_tolerates_stale_temp(self, tmp_path: Path) -> None:
        # A leftover .tmp from a prior failed manual run must not lock out
        # the retry via O_EXCL — the one-liner pre-unlinks it.
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        stale = path.with_suffix(path.suffix + ".tmp")
        stale.write_text("stale junk from a crashed prior attempt")
        msg = report_missing_secret_warning("bp-app-id", path)
        argv = self._patch_argv(msg)
        proc = subprocess.run(
            [sys.executable, *argv[1:]],
            input="RETRY-SECRET\n",
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(path.read_text())["agentBlueprintClientSecret"] == ("RETRY-SECRET")
        assert not stale.exists()

    def test_rendered_command_survives_a_real_shell(self, tmp_path: Path) -> None:
        # Review P1, the load-bearing test: execute the ACTUAL rendered
        # shell command (not the extracted code) with a config path that
        # contains a space AND a shell metacharacter, through a real shell.
        # shlex.join must have quoted it so pasting neither splits the path
        # nor executes the injected `touch INJECTED`.
        cfg_dir = tmp_path / "weird dir; touch INJECTED"
        cfg_dir.mkdir(parents=True)
        path = cfg_dir / "a365.generated.config.json"
        path.write_text(
            json.dumps({"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None}) + "\n"
        )
        msg = report_missing_secret_warning("bp-app-id", path)
        line = next(ln for ln in msg.splitlines() if "python3 -c" in ln).strip()
        # Use the test interpreter but STILL exercise shell tokenisation of
        # the rendered line (swap only the leading program token).
        run_line = line.replace("python3", shlex.quote(sys.executable), 1)
        proc = subprocess.run(
            run_line,
            shell=True,  # deliberate: prove the rendered line is shell-safe
            input="SHELL-SECRET\n",
            text=True,
            capture_output=True,
            timeout=30,
            cwd=tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(path.read_text())["agentBlueprintClientSecret"] == ("SHELL-SECRET")
        # The `; touch INJECTED` inside the path must NOT have executed.
        assert not (tmp_path / "INJECTED").exists()
        assert not (cfg_dir / "INJECTED").exists()


class TestWriteOwnerOnlyTextAtomic:
    """#112 / CS-004 — the exclusive-create atomic writer used for
    secret-bearing JSON: 0600 from birth, no permissive window."""

    def test_creates_owner_only_under_umask_022(self, tmp_path: Path) -> None:
        old = os.umask(0o022)
        try:
            target = tmp_path / "cfg.json"
            write_owner_only_text_atomic(target, '{"s": 1}\n')
            assert target.read_text() == '{"s": 1}\n'
            assert (target.stat().st_mode & 0o777) == 0o600
        finally:
            os.umask(old)

    def test_creates_missing_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "cfg.json"
        write_owner_only_text_atomic(target, "x\n")
        assert target.read_text() == "x\n"

    def test_mode_override_exact_under_umask_027(self, tmp_path: Path) -> None:
        # The mode must be forced exactly (fchmod), not just requested via
        # O_CREAT which the umask can further clear (0640 & ~027 == 0600).
        old = os.umask(0o027)
        try:
            target = tmp_path / "cfg.json"
            write_owner_only_text_atomic(target, "x\n", mode=0o640)
            assert (target.stat().st_mode & 0o777) == 0o640
        finally:
            os.umask(old)

    def test_tightens_a_permissive_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "cfg.json"
        target.write_text("{}")
        target.chmod(0o644)
        write_owner_only_text_atomic(target, '{"s": 2}\n')
        assert (target.stat().st_mode & 0o777) == 0o600
        assert target.read_text() == '{"s": 2}\n'

    def test_replaces_stale_temp_file(self, tmp_path: Path) -> None:
        target = tmp_path / "cfg.json"
        stale = tmp_path / "cfg.json.tmp"
        stale.write_text("stale")
        write_owner_only_text_atomic(target, "fresh\n")
        assert target.read_text() == "fresh\n"
        assert not stale.exists()

    def test_stale_temp_symlink_is_not_followed(self, tmp_path: Path) -> None:
        # A pre-planted symlink at the temp path must not route the
        # secret bytes into the symlink's target (O_EXCL + pre-unlink).
        victim = tmp_path / "victim.txt"
        victim.write_text("untouched")
        target = tmp_path / "cfg.json"
        (tmp_path / "cfg.json.tmp").symlink_to(victim)
        write_owner_only_text_atomic(target, "secret\n")
        assert victim.read_text() == "untouched"
        assert target.read_text() == "secret\n"

    def test_no_temp_left_when_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(src: object, dst: object) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", boom)
        target = tmp_path / "cfg.json"
        with pytest.raises(OSError, match="simulated"):
            write_owner_only_text_atomic(target, "x")
        assert not (tmp_path / "cfg.json.tmp").exists()
        assert not target.exists()


class TestRecoveryDisplayName:
    def test_default_uses_now(self) -> None:
        # Two calls in quick succession produce names with the same
        # second-resolution timestamp; we just want non-empty + the
        # ``hermes-bridge-recovery-`` prefix.
        name = default_recovery_display_name()
        assert name.startswith("hermes-bridge-recovery-")
        assert len(name) > len("hermes-bridge-recovery-")

    def test_explicit_now_is_used(self) -> None:
        import datetime as _dt

        when = _dt.datetime(2026, 5, 7, 14, 30, 0)
        assert default_recovery_display_name(when) == ("hermes-bridge-recovery-20260507T143000")


# ---------------------------------------------------------------------------
# #102 WP4 — tenant-pinned provisioning (M7)
# ---------------------------------------------------------------------------


def _account_show_result(tenant: str) -> RunResult:
    import json as _json

    return RunResult(
        argv=["az", "account", "show", "-o", "json", "--only-show-errors"],
        returncode=0,
        stdout=_json.dumps({"tenantId": tenant, "id": "sub-1"}),
        stderr="",
    )


class TestTenantPinnedProvisioning:
    _PINNED = "aaaa1111-0000-0000-0000-000000000001"
    _OTHER = "bbbb2222-0000-0000-0000-000000000002"

    def test_apply_refuses_on_tenant_mismatch_before_any_step(self, tmp_path: Path) -> None:
        # M7 (#102): the a365 CLI auto-detects its tenant from the ambient az
        # session, so provisioning under a mismatched login would create the
        # blueprint/permissions in the WRONG tenant. Refuse before step one.
        plan = build_register_plan(
            RegisterInputs(agent_name="Inbox Helper", tenant_id=self._PINNED)
        )
        mutator = FakeMutator(scripted=[_account_show_result(self._OTHER)])

        config_path = tmp_path / "a365.config.json"
        config_path.write_text('{"tenantId":"original-tenant"}\n')

        with pytest.raises(RegisterError, match="does not match the pinned tenant"):
            apply_register_plan(plan, mutator=mutator, config_path=config_path)

        assert mutator.calls == [["az", "account", "show", "-o", "json", "--only-show-errors"]]
        assert json.loads(config_path.read_text())["tenantId"] == "original-tenant"

    def test_domain_alias_gets_canonical_guid_migration_error(self) -> None:
        plan = build_register_plan(
            RegisterInputs(agent_name="Inbox Helper", tenant_id="contoso.onmicrosoft.com")
        )
        mutator = FakeMutator(scripted=[_account_show_result(self._PINNED)])

        with pytest.raises(RegisterError, match="not a canonical tenant GUID"):
            apply_register_plan(plan, mutator=mutator)

    def test_apply_refuses_when_session_unverifiable(self) -> None:
        plan = build_register_plan(
            RegisterInputs(agent_name="Inbox Helper", tenant_id=self._PINNED)
        )
        mutator = FakeMutator(scripted=[CliInvocationError(["az", "account", "show"], 1, "boom")])
        with pytest.raises(RegisterError, match="unverifiable"):
            apply_register_plan(plan, mutator=mutator)

    def test_apply_proceeds_when_tenant_matches(self) -> None:
        plan = build_register_plan(
            RegisterInputs(agent_name="Inbox Helper", tenant_id=self._PINNED)
        )
        mutator = FakeMutator(scripted=[_account_show_result(self._PINNED)])

        result = apply_register_plan(plan, mutator=mutator)

        assert mutator.calls[0] == ["az", "account", "show", "-o", "json", "--only-show-errors"]
        rest = [call for call in mutator.calls if call[:2] == ["az", "rest"]]
        a365 = [call for call in mutator.calls if call[0] == "a365"]
        assert rest, "apply must read Entra state before provisioning"
        assert a365
        assert mutator.calls.index(rest[0]) < mutator.calls.index(a365[0])
        assert result.completed == ["blueprint", "permissions-mcp", "permissions-bot"]
        assert any("tenant pin OK" in m for m in result.messages)

    def test_matching_preflight_updates_config_before_steps(self, tmp_path: Path) -> None:
        plan = build_register_plan(
            RegisterInputs(agent_name="Inbox Helper", tenant_id=self._PINNED)
        )
        mutator = FakeMutator(scripted=[_account_show_result(self._PINNED)])
        config_path = tmp_path / "a365.config.json"
        config_path.write_text('{"tenantId":"old"}\n')

        result = apply_register_plan(plan, mutator=mutator, config_path=config_path)

        config = json.loads(config_path.read_text())
        assert config["tenantId"] == self._PINNED
        assert config["agentBlueprintDisplayName"] == "Inbox Helper Blueprint"
        assert result.completed

    def test_apply_refuses_without_pin(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="Inbox Helper"))
        mutator = FakeMutator()

        with pytest.raises(RegisterError, match="no canonical tenant pin"):
            _apply_register_plan(plan, mutator=mutator)
        assert mutator.calls == []

    def test_tenant_verify_tolerates_az_warning_prefix(self) -> None:
        # Same stream-shape lesson as register._extract_first_json_object:
        # merged-stderr WARNING prefix must not fail-close a matching session.
        import json as _json

        pinned = "aaaa1111-0000-0000-0000-000000000001"
        plan = build_register_plan(RegisterInputs(agent_name="Inbox Helper", tenant_id=pinned))
        noisy = RunResult(
            argv=["az", "account", "show", "-o", "json", "--only-show-errors"],
            returncode=0,
            stdout="WARNING: upgrade available\n" + _json.dumps({"tenantId": pinned}),
            stderr="",
        )
        mutator = FakeMutator(scripted=[noisy])
        result = apply_register_plan(plan, mutator=mutator)
        assert any("tenant pin OK" in m for m in result.messages)


class TestSecurityRecoveryBinding:
    def test_recovery_refuses_app_id_substitution_before_reset(self, tmp_path: Path) -> None:
        path = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        app_show = RunResult(
            argv=["az", "ad", "app", "show", "--id", "bp-app-id"],
            returncode=0,
            stdout=json.dumps(
                {
                    "appId": "attacker-app-id",
                    "id": "00000000-0000-0000-0000-000000000099",
                }
            ),
            stderr="",
        )
        mutator = FakeMutator(scripted=[app_show])

        outcome = auto_recover_secret(
            path,
            "bp-app-id",
            mutator=mutator,
            display_name="recovery-test",
        )

        assert outcome.recovered is False
        assert "identity did not match" in outcome.messages[0]
        assert len(mutator.calls) == 1
        assert json.loads(path.read_text())["agentBlueprintClientSecret"] is None

    def test_recovery_refuses_symlinked_generated_config(self, tmp_path: Path) -> None:
        victim = _write_generated(
            tmp_path,
            {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None},
        )
        link = tmp_path / "linked-generated.json"
        link.symlink_to(victim)
        mutator = FakeMutator()

        outcome = auto_recover_secret(
            link,
            "bp-app-id",
            mutator=mutator,
            display_name="recovery-test",
        )

        assert outcome.recovered is False
        assert "non-symlink" in outcome.messages[0]
        assert mutator.calls == []
