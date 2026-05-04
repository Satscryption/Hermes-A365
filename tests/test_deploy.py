"""Tests for scripts/deploy.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from deploy import (
    SUPPORTED_CHANNELS,
    DeployError,
    DeployPlan,
    DeployResult,
    apply_deploy_plan,
    build_deploy_plan,
    normalize_channels,
)
from register import AADSTSError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeQuerySource:
    available: bool = True
    instances: dict[str, dict[str, Any]] = field(default_factory=dict)

    def query_license(self) -> dict[str, Any] | None:
        return None

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None:
        return None

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return None

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return self.instances.get(instance_id)

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return None


@dataclass
class FakeMutator:
    available: bool = True
    deploy_response: dict[str, Any] = field(default_factory=dict)
    deploy_error: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # Stubs — not exercised here.
    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def fic_configure(self, *, app_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def setup_blueprint(self, *, file_path: Path) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def create_instance(  # pragma: no cover
        self, *, blueprint_slug: str, instance_id: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    def deploy(self, *, instance_id: str, channels: list[str]) -> dict[str, Any]:
        self.calls.append(("deploy", {"instance_id": instance_id, "channels": list(channels)}))
        if self.deploy_error is not None:
            err, self.deploy_error = self.deploy_error, None
            raise err
        return self.deploy_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_INSTANCE_ID = "550e8400-e29b-41d4-a716-446655440000"


def _seed_agent_env(
    tmp_path: Path,
    slug: str = "inbox-helper",
    instance_id: str | None = None,
) -> Path:
    """Plant a minimal per-agent .env at ~/.hermes/agents/<slug>/.env."""
    iid = instance_id if instance_id is not None else _INSTANCE_ID
    path = tmp_path / "agents" / slug / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"AA_INSTANCE_ID={iid}\n")
    return path


# ---------------------------------------------------------------------------
# normalize_channels
# ---------------------------------------------------------------------------


class TestNormalizeChannels:
    def test_empty_means_unbind_everything(self) -> None:
        assert normalize_channels([]) == []
        assert normalize_channels(None) == []

    def test_dedupes_and_sorts(self) -> None:
        assert normalize_channels(["outlook", "teams", "outlook"]) == ["outlook", "teams"]

    def test_rejects_unknown_channel(self) -> None:
        with pytest.raises(ValueError, match="unsupported channel"):
            normalize_channels(["teams", "slack"])

    def test_supported_channels_constant_matches_spec(self) -> None:
        assert frozenset({"teams", "outlook", "m365copilot"}) == SUPPORTED_CHANNELS


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


class TestAgentEnvPreconditions:
    def test_missing_agent_env_fails_clean(self, tmp_path: Path) -> None:
        with pytest.raises(DeployError, match="instance create"):
            build_deploy_plan(
                "inbox-helper", ["teams"], hermes_home=tmp_path, query_source=FakeQuerySource()
            )

    def test_missing_aa_instance_id_fails_clean(self, tmp_path: Path) -> None:
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("OWNER=sadiq@contoso.com\n")  # no AA_INSTANCE_ID
        with pytest.raises(DeployError, match="no AA_INSTANCE_ID"):
            build_deploy_plan(
                "inbox-helper", ["teams"], hermes_home=tmp_path, query_source=FakeQuerySource()
            )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestBuildDeployPlan:
    def test_create_when_no_channels_currently_bound(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        plan = build_deploy_plan(
            "inbox-helper",
            ["teams", "outlook"],
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
        )
        assert plan.action == "create"
        assert plan.additions == ["outlook", "teams"]
        assert plan.removals == []
        assert plan.current == []
        assert plan.aa_instance_id == _INSTANCE_ID

    def test_noop_when_desired_matches_current(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(
            instances={_INSTANCE_ID: {"channels": {"teams": "ok", "outlook": "ok"}}}
        )
        plan = build_deploy_plan(
            "inbox-helper",
            ["teams", "outlook"],
            hermes_home=tmp_path,
            query_source=qs,
        )
        assert plan.action == "noop"
        assert plan.additions == []
        assert plan.removals == []

    def test_patch_addition_only(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_deploy_plan(
            "inbox-helper", ["teams", "outlook"], hermes_home=tmp_path, query_source=qs
        )
        assert plan.action == "patch"
        assert plan.additions == ["outlook"]
        assert plan.removals == []

    def test_patch_removal_only(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(
            instances={_INSTANCE_ID: {"channels": {"teams": "ok", "outlook": "ok"}}}
        )
        plan = build_deploy_plan("inbox-helper", ["teams"], hermes_home=tmp_path, query_source=qs)
        assert plan.action == "patch"
        assert plan.additions == []
        assert plan.removals == ["outlook"]

    def test_patch_mixed_diff(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(
            instances={_INSTANCE_ID: {"channels": {"teams": "ok", "outlook": "ok"}}}
        )
        plan = build_deploy_plan(
            "inbox-helper",
            ["teams", "m365copilot"],
            hermes_home=tmp_path,
            query_source=qs,
        )
        assert plan.action == "patch"
        assert plan.additions == ["m365copilot"]
        assert plan.removals == ["outlook"]

    def test_unbind_everything(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_deploy_plan("inbox-helper", [], hermes_home=tmp_path, query_source=qs)
        assert plan.action == "patch"
        assert plan.additions == []
        assert plan.removals == ["teams"]

    def test_missing_state_does_not_count_as_bound(self, tmp_path: Path) -> None:
        # Channel state "missing" should not be treated as currently bound.
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(
            instances={_INSTANCE_ID: {"channels": {"teams": "ok", "outlook": "missing"}}}
        )
        plan = build_deploy_plan("inbox-helper", ["teams"], hermes_home=tmp_path, query_source=qs)
        assert plan.current == ["teams"]
        assert plan.action == "noop"

    def test_unavailable_query_source_assumes_no_current(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        plan = build_deploy_plan(
            "inbox-helper",
            ["teams"],
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        assert plan.action == "create"
        assert plan.current == []


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------


class TestPlanRender:
    def test_create_renders_additions(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        plan = build_deploy_plan(
            "inbox-helper",
            ["teams", "outlook"],
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
        )
        text = plan.render_human()
        assert "[plan] deploy inbox-helper" in text
        assert "current channels:  (none)" in text
        assert "desired channels:  outlook, teams" in text
        assert "+outlook" in text and "+teams" in text

    def test_noop_renders_already_converged(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_deploy_plan("inbox-helper", ["teams"], hermes_home=tmp_path, query_source=qs)
        assert "already converged" in plan.render_human()

    def test_patch_renders_signed_delta(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"outlook": "ok"}}})
        plan = build_deploy_plan("inbox-helper", ["teams"], hermes_home=tmp_path, query_source=qs)
        text = plan.render_human()
        assert "+teams" in text
        assert "-outlook" in text


# ---------------------------------------------------------------------------
# apply_deploy_plan
# ---------------------------------------------------------------------------


class TestApplyDeploy:
    def test_create_calls_mutator_with_desired_set(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        plan = build_deploy_plan(
            "inbox-helper",
            ["teams", "outlook"],
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
        )
        mutator = FakeMutator(deploy_response={"channels": {"teams": "ok", "outlook": "ok"}})
        result = apply_deploy_plan(plan, mutator=mutator)

        assert isinstance(result, DeployResult)
        assert result.mutator_called is True
        assert mutator.calls == [
            ("deploy", {"instance_id": _INSTANCE_ID, "channels": ["outlook", "teams"]})
        ]
        assert result.channel_results == {"teams": "ok", "outlook": "ok"}

    def test_noop_does_not_call_mutator(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_deploy_plan("inbox-helper", ["teams"], hermes_home=tmp_path, query_source=qs)
        mutator = FakeMutator()
        result = apply_deploy_plan(plan, mutator=mutator)
        assert mutator.calls == []
        assert result.mutator_called is False
        assert any("already matches" in m for m in result.messages)

    def test_deep_links_surfaced_in_messages(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        plan = build_deploy_plan(
            "inbox-helper", ["teams"], hermes_home=tmp_path, query_source=FakeQuerySource()
        )
        link = "https://teams.microsoft.com/l/chat/0/0?bot=abc"
        mutator = FakeMutator(
            deploy_response={"channels": {"teams": "ok"}, "deep_links": {"teams": link}}
        )
        result = apply_deploy_plan(plan, mutator=mutator)
        assert result.deep_links == {"teams": link}
        assert any(link in m for m in result.messages)

    def test_removals_surfaced_in_messages(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(
            instances={_INSTANCE_ID: {"channels": {"teams": "ok", "outlook": "ok"}}}
        )
        plan = build_deploy_plan("inbox-helper", ["teams"], hermes_home=tmp_path, query_source=qs)
        mutator = FakeMutator(deploy_response={"channels": {"teams": "ok"}})
        result = apply_deploy_plan(plan, mutator=mutator)
        assert any("outlook: unbound" in m for m in result.messages)

    def test_aadsts_error_propagates(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        plan = build_deploy_plan(
            "inbox-helper", ["m365copilot"], hermes_home=tmp_path, query_source=FakeQuerySource()
        )
        mutator = FakeMutator(deploy_error=AADSTSError("AADSTS65001", "no copilot license"))
        with pytest.raises(AADSTSError) as excinfo:
            apply_deploy_plan(plan, mutator=mutator)
        assert excinfo.value.code == "AADSTS65001"


# Smoke check on DeployPlan dataclass.
def test_deploy_plan_dataclass_basic() -> None:
    p = DeployPlan(
        slug="x",
        aa_instance_id="i",
        desired=["teams"],
        current=[],
        additions=["teams"],
        removals=[],
        action="create",
    )
    assert p.action == "create"
