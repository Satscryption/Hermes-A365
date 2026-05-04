"""Tests for scripts/publish.py — wraps `a365 publish`."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from mutator import AADSTSError, CliInvocationError, RunResult
from publish import (
    ADMIN_CENTRE_URL,
    PublishInputs,
    PublishPlan,
    PublishResult,
    _extract_package_path,
    apply_publish_plan,
    build_publish_plan,
)

# ---------------------------------------------------------------------------
# FakeMutator
# ---------------------------------------------------------------------------


@dataclass
class FakeMutator:
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


# ---------------------------------------------------------------------------
# PublishInputs validation
# ---------------------------------------------------------------------------


class TestPublishInputs:
    def test_minimal_valid(self) -> None:
        inp = PublishInputs(agent_name="x")
        assert inp.aiteammate is False
        assert inp.use_blueprint is False

    def test_empty_agent_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="agent_name"):
            PublishInputs(agent_name="")

    def test_use_blueprint_with_aiteammate_rejected(self) -> None:
        # Per CLI help: "--use-blueprint only meaningful with --aiteammate false".
        with pytest.raises(ValueError, match="--use-blueprint"):
            PublishInputs(agent_name="x", aiteammate=True, use_blueprint=True)


# ---------------------------------------------------------------------------
# build_publish_plan — argv shapes
# ---------------------------------------------------------------------------


class TestBuildPublishPlan:
    def test_argv_minimal(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="inbox-helper"))
        assert plan.step.argv == ["a365", "publish", "--agent-name", "inbox-helper"]

    def test_argv_with_tenant(self) -> None:
        plan = build_publish_plan(
            PublishInputs(agent_name="x", tenant_id="contoso.onmicrosoft.com")
        )
        assert plan.step.argv == [
            "a365",
            "publish",
            "--agent-name",
            "x",
            "--tenant-id",
            "contoso.onmicrosoft.com",
        ]

    def test_argv_with_aiteammate(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", aiteammate=True))
        assert "--aiteammate" in plan.step.argv

    def test_argv_with_use_blueprint(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", use_blueprint=True))
        assert "--use-blueprint" in plan.step.argv

    def test_argv_with_verbose(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", verbose=True))
        assert "--verbose" in plan.step.argv


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------


class TestPlanRender:
    def test_human_blueprint_only_default(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="inbox-helper"))
        text = plan.render_human()
        assert "[plan] hermes a365 publish inbox-helper" in text
        assert "blueprint-only" in text
        assert "auto-detect" in text
        assert "$ a365 publish --agent-name inbox-helper" in text

    def test_human_aiteammate_flavour(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", aiteammate=True))
        assert "AI Teammate" in plan.render_human()

    def test_human_use_blueprint_flow(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", use_blueprint=True))
        assert "blueprint-based non-DW" in plan.render_human()


# ---------------------------------------------------------------------------
# _extract_package_path
# ---------------------------------------------------------------------------


class TestExtractPackagePath:
    @pytest.mark.parametrize(
        "line",
        [
            "Created package: /tmp/inbox-helper-manifest.zip",
            "Wrote zip: ./build/agent.zip",
            "package created: /var/folders/x/agent-pkg.zip",
            "Package: /tmp/foo.zip",
        ],
    )
    def test_recognises_common_phrasings(self, line: str) -> None:
        assert _extract_package_path(line) is not None

    def test_returns_none_when_no_zip_in_output(self) -> None:
        assert _extract_package_path("Random success message with no zip path") is None

    def test_picks_first_zip_when_multiple(self) -> None:
        out = "Created package: /tmp/first.zip\nlater unrelated /tmp/other.zip mention"
        assert _extract_package_path(out) == "/tmp/first.zip"


# ---------------------------------------------------------------------------
# apply_publish_plan
# ---------------------------------------------------------------------------


class TestApplyPublish:
    def test_calls_mutator_with_planned_argv(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="inbox-helper"))
        mutator = FakeMutator()
        apply_publish_plan(plan, mutator=mutator)
        assert mutator.calls == [["a365", "publish", "--agent-name", "inbox-helper"]]

    def test_surfaces_package_path_when_visible(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        mutator = FakeMutator(
            scripted=[
                RunResult(
                    argv=["a365"],
                    returncode=0,
                    stdout="…\nCreated package: /tmp/x.zip\n",
                    stderr="",
                )
            ]
        )
        result = apply_publish_plan(plan, mutator=mutator)
        assert isinstance(result, PublishResult)
        assert result.package_path == "/tmp/x.zip"
        assert any("/tmp/x.zip" in m for m in result.messages)

    def test_messages_always_include_admin_centre_url(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        result = apply_publish_plan(plan, mutator=FakeMutator())
        assert any(ADMIN_CENTRE_URL in m for m in result.messages)

    def test_no_package_path_when_cli_silent(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        result = apply_publish_plan(plan, mutator=FakeMutator())
        assert result.package_path is None

    def test_aadsts_error_propagates(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        mutator = FakeMutator(scripted=[AADSTSError("AADSTS65001", "no perms")])
        with pytest.raises(AADSTSError) as excinfo:
            apply_publish_plan(plan, mutator=mutator)
        assert excinfo.value.code == "AADSTS65001"

    def test_cli_invocation_error_propagates(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        mutator = FakeMutator(scripted=[CliInvocationError(["a365"], 7, "boom")])
        with pytest.raises(CliInvocationError):
            apply_publish_plan(plan, mutator=mutator)


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_publish_plan_dataclass_basic() -> None:
    plan = build_publish_plan(PublishInputs(agent_name="x"))
    assert isinstance(plan, PublishPlan)
    assert plan.step.description.startswith("package")


def test_admin_centre_url_pinned() -> None:
    # Pin the URL we surface to the operator after a successful publish.
    assert ADMIN_CENTRE_URL == "https://admin.microsoft.com/"
