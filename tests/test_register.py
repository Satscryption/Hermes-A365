"""Tests for hermes_a365.register — the v0.2 setup-orchestrator."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hermes_a365._common import write_owner_only_text_atomic
from hermes_a365.a365_config import CONFIG_FILENAME
from hermes_a365.mutator import (
    AADSTS_CONSENT_REQUIRED,
    AADSTS_LICENSE_NOT_PROPAGATED,
    A365CliMutator,
    AADSTSError,
    CliInvocationError,
    RunResult,
)
from hermes_a365.register import (
    AITEAMMATE_REGISTER_UNSUPPORTED,
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_RETRIES,
    ApplyResult,
    RegisterInputs,
    RegisterPlan,
    RegisterStep,
    SecretRecoveryOutcome,
    apply_register_plan,
    auto_recover_secret,
    build_parser,
    build_register_plan,
    default_recovery_display_name,
    detect_missing_secret,
    report_missing_secret_warning,
    run,
    update_config_for_agent,
)

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
        if self.scripted:
            nxt = self.scripted.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return RunResult(argv=list(argv), returncode=0, stdout="", stderr="")


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
        assert inp.aiteammate is False
        assert inp.authmode == "obo"

    def test_aiteammate_rejected_with_real_flow_hint(self) -> None:
        with pytest.raises(ValueError, match="publish --aiteammate"):
            RegisterInputs(agent_name="x", aiteammate=True)

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


class TestCliAiteammateUnsupported:
    def test_help_marks_aiteammate_as_unsupported(self) -> None:
        help_text = build_parser().format_help()
        assert "--aiteammate" in help_text
        assert "deprecated/unsupported on register" in help_text
        assert "publish --aiteammate" in help_text

    def test_run_rejects_aiteammate_before_plan_or_apply(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = build_parser().parse_args(
            ["--agent-name", "x", "--aiteammate", "--apply"]
        )

        rc = run(args)

        assert rc == 2
        captured = capsys.readouterr()
        assert AITEAMMATE_REGISTER_UNSUPPORTED in captured.err
        assert "a365 setup blueprint" not in captured.out

    def test_dry_run_rejects_aiteammate_before_plan_render(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = build_parser().parse_args(["--agent-name", "x", "--aiteammate"])

        rc = run(args)

        assert rc == 2
        captured = capsys.readouterr()
        assert AITEAMMATE_REGISTER_UNSUPPORTED in captured.err
        assert "a365 setup blueprint" not in captured.out


class TestPlanRender:
    def test_human_lists_steps_and_argv(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="inbox-helper"))
        text = plan.render_human()
        assert "[plan] hermes a365 register inbox-helper" in text
        assert "blueprint" in text
        assert "permissions-mcp" in text
        assert "permissions-bot" in text
        assert "$ a365 setup blueprint --agent-name inbox-helper" in text

    def test_human_shows_auto_detect_when_tenant_unset(self) -> None:
        plan = build_register_plan(RegisterInputs(agent_name="x"))
        assert "auto-detect" in plan.render_human()

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
        assert [argv[2] for argv in mutator.calls] == ["blueprint", "permissions", "permissions"]
        # Three calls total.
        assert len(mutator.calls) == 3

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


def _install_az_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    """Put a fake ``az`` on PATH whose body is ``script`` (sh).

    Lets CS-003 tests drive the *real* ``A365CliMutator`` subprocess
    paths with fake credential material — no live Azure involved.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "az"
    shim.write_text("#!/bin/sh\n" + script + "\n")
    shim.chmod(0o755)
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
        auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert mutator.calls, "az should have been invoked"
        argv = mutator.calls[0]
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
        auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
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
        auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
        assert mutator.sensitive_flags == [True]

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
            f"echo '{{\"appId\": \"bp-app-id\", \"password\": \"{marker}\"}}'",
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
        # the by-hand az hint still gets through
        assert any("az ad app credential reset" in m for m in outcome.messages)

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
                scripted=[
                    RunResult(argv=[], returncode=0, stdout=self._AZ_OK_PAYLOAD, stderr="")
                ]
            )
            auto_recover_secret(
                path, "bp-app-id", mutator=mutator, display_name="recovery-test"
            )
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
        auto_recover_secret(
            path, "bp-app-id", mutator=mutator, display_name="recovery-test"
        )
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
            '{\n'
            '  "appId": "bp-app-id",\n'
            '  "password": "RV88Q~MOCK-SECRET",\n'
            '  "tenant": "tenant-id"\n'
            '}\n'
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

    def test_recovers_when_az_stdout_has_trailing_diagnostics(
        self, tmp_path: Path
    ) -> None:
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
        mutator = FakeMutator(
            scripted=[RunResult(argv=[], returncode=0, stdout=out, stderr="")]
        )
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
            scripted=[
                RunResult(argv=[], returncode=0, stdout="not json at all", stderr="")
            ]
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
        mutator = FakeMutator(
            scripted=[CliInvocationError(["az"], 1, "permission denied")]
        )
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
        msg = report_missing_secret_warning(
            "bp-app-id", tmp_path / "a365.generated.config.json"
        )
        assert "#14" in msg

    def test_mentions_auto_recover_flag(self, tmp_path: Path) -> None:
        msg = report_missing_secret_warning(
            "bp-app-id", tmp_path / "a365.generated.config.json"
        )
        assert "--auto-recover-secret" in msg

    def test_includes_config_path_in_patch_hint(self, tmp_path: Path) -> None:
        path = tmp_path / "a365.generated.config.json"
        msg = report_missing_secret_warning("bp-app-id", path)
        assert str(path) in msg

    def test_no_argv_secret_sink(self, tmp_path: Path) -> None:
        # CS-005 (#113): guidance must never tell the operator to put the
        # secret on a command line — argv lands in shell history and the
        # process list.
        msg = report_missing_secret_warning(
            "bp-app-id", tmp_path / "a365.generated.config.json"
        )
        assert "<paste-password>" not in msg
        assert "sys.argv[2]" not in msg

    def test_patch_hint_uses_hidden_prompt_and_exclusive_create(
        self, tmp_path: Path
    ) -> None:
        msg = report_missing_secret_warning(
            "bp-app-id", tmp_path / "a365.generated.config.json"
        )
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
        msg = report_missing_secret_warning(
            "bp-app-id", tmp_path / "a365.generated.config.json"
        )
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
        assert json.loads(path.read_text())["agentBlueprintClientSecret"] == (
            "RETRY-SECRET"
        )
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
            json.dumps(
                {"agentBlueprintId": "bp-app-id", "agentBlueprintClientSecret": None}
            )
            + "\n"
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
        assert json.loads(path.read_text())["agentBlueprintClientSecret"] == (
            "SHELL-SECRET"
        )
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
        assert default_recovery_display_name(when) == (
            "hermes-bridge-recovery-20260507T143000"
        )
