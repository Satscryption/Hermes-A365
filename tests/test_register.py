"""Tests for scripts/register.py — the v0.2 setup-orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from a365_config import CONFIG_FILENAME
from mutator import (
    AADSTS_CONSENT_REQUIRED,
    AADSTS_LICENSE_NOT_PROPAGATED,
    AADSTSError,
    CliInvocationError,
    RunResult,
)
from register import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_RETRIES,
    ApplyResult,
    RegisterInputs,
    RegisterPlan,
    RegisterStep,
    apply_register_plan,
    build_register_plan,
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

    def run(self, argv: list[str], *, timeout: float = 60.0) -> RunResult:
        self.calls.append(list(argv))
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
