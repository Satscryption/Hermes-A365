"""Tests for scripts/instance_create.py.

Every test uses an in-memory FakeMutator + FakeQuerySource and a tmp_path-
based ``HERMES_HOME``; nothing here ever calls the real ``a365`` CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from instance_create import (
    InstanceCreateError,
    InstanceCreateInputs,
    InstanceCreateResult,
    InstancePlan,
    apply_instance_plan,
    build_instance_plan,
    write_text_atomic,
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
    create_instance_response: dict[str, Any] = field(default_factory=lambda: {"instanceId": "ok"})
    create_instance_error: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # Stubs — not exercised here.
    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def fic_configure(self, *, app_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def setup_blueprint(self, *, file_path: Path) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def create_instance(self, *, blueprint_slug: str, instance_id: str) -> dict[str, Any]:
        self.calls.append(
            ("create_instance", {"blueprint_slug": blueprint_slug, "instance_id": instance_id})
        )
        if self.create_instance_error is not None:
            err, self.create_instance_error = self.create_instance_error, None
            raise err
        return self.create_instance_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_skill_env(hermes_home: Path, **overrides: str) -> None:
    """Plant a minimal ~/.hermes/.env that satisfies parent-env requirements."""
    base = {
        "A365_APP_ID": "00000000-0000-0000-0000-00000000aaa1",
        "A365_TENANT_ID": "contoso.onmicrosoft.com",
        "A365_CLI_VARIANT": "a365-dotnet",
        "HERMES_OTLP_ENDPOINT": "https://contoso.otel.agent365.microsoft.com",
    }
    base.update(overrides)
    text = "".join(f"{k}={v}\n" for k, v in sorted(base.items()))
    (hermes_home / ".env").write_text(text)


def _inputs(**overrides: Any) -> InstanceCreateInputs:
    base = {
        "slug": "inbox-helper",
        "owner": "sadiq@contoso.com",
        "owner_aad_id": "00000000-0000-0000-0000-000000000001",
    }
    base.update(overrides)
    return InstanceCreateInputs(**base)


# ---------------------------------------------------------------------------
# Inputs validation
# ---------------------------------------------------------------------------


class TestInstanceCreateInputs:
    def test_valid(self) -> None:
        inp = _inputs()
        assert inp.slug == "inbox-helper"

    @pytest.mark.parametrize("field_name", ["slug", "owner", "owner_aad_id"])
    def test_required_fields_must_be_nonempty(self, field_name: str) -> None:
        kwargs: dict[str, Any] = {field_name: ""}
        with pytest.raises(ValueError, match=field_name):
            _inputs(**kwargs)


# ---------------------------------------------------------------------------
# Skill env preconditions
# ---------------------------------------------------------------------------


class TestSkillEnvPreconditions:
    def test_missing_skill_env_fails_clean(self, tmp_path: Path) -> None:
        with pytest.raises(InstanceCreateError, match="run `hermes a365 register`"):
            build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())

    def test_skill_env_missing_required_keys_fails_clean(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("HERMES_OTLP_ENDPOINT=x\n")  # no APP_ID/TENANT_ID
        with pytest.raises(InstanceCreateError, match="missing required keys"):
            build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())

    def test_missing_otlp_endpoint_with_no_override_fails(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path, HERMES_OTLP_ENDPOINT="")
        with pytest.raises(InstanceCreateError, match="HERMES_OTLP_ENDPOINT"):
            build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())

    def test_otlp_endpoint_override_is_accepted(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path, HERMES_OTLP_ENDPOINT="")
        plan = build_instance_plan(
            _inputs(otlp_endpoint="https://override"),
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
        )
        assert plan.desired_env_inputs.hermes_otlp_endpoint == "https://override"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestBuildInstancePlan:
    def test_create_when_no_local_no_cloud(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        assert plan.action == "create"
        assert plan.aa_instance_id_was_existing is False
        # Generated UUID looks like a UUID.
        assert len(plan.aa_instance_id) == 36
        assert plan.cloud_actual is None

    def test_noop_when_cloud_already_has_instance(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        # Plant an agent .env with an existing AA_INSTANCE_ID.
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\n")
        # And tell the cloud it already exists.
        qs = FakeQuerySource(instances={"550e8400-e29b-41d4-a716-446655440000": {"id": "..."}})
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=qs)
        assert plan.action == "noop"
        assert plan.aa_instance_id == "550e8400-e29b-41d4-a716-446655440000"
        assert plan.aa_instance_id_was_existing is True
        assert plan.cloud_actual is not None

    def test_create_cloud_only_when_local_id_but_cloud_missing(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\n")
        # Empty cloud → instance not registered yet.
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        assert plan.action == "create-cloud-only"
        assert plan.aa_instance_id == "550e8400-e29b-41d4-a716-446655440000"
        assert plan.aa_instance_id_was_existing is True

    def test_business_hours_inherited_from_existing_agent_env(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(
            "AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\n"
            "BUSINESS_HOURS_TZ=Europe/London\n"
            "BUSINESS_HOURS_START=09:00\n"
            "BUSINESS_HOURS_END=17:00\n"
        )
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        assert plan.desired_env_inputs.business_hours_tz == "Europe/London"
        assert plan.desired_env_inputs.business_hours_start == "09:00"
        assert plan.desired_env_inputs.business_hours_end == "17:00"

    def test_business_hours_override_wins_over_existing(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(
            "AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\nBUSINESS_HOURS_TZ=Europe/London\n"
        )
        plan = build_instance_plan(
            _inputs(business_hours_tz="UTC"),
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
        )
        assert plan.desired_env_inputs.business_hours_tz == "UTC"

    def test_cli_variant_inherited_from_skill_env(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path, A365_CLI_VARIANT="atk-npm")
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        assert plan.desired_env_inputs.a365_cli_variant == "atk-npm"

    def test_cli_variant_falls_back_when_unset(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path, A365_CLI_VARIANT="")
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        assert plan.desired_env_inputs.a365_cli_variant == "a365-dotnet"


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------


class TestPlanRender:
    def test_create_action_renders_expected_phrases(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        text = plan.render_human()
        assert "[plan] hermes a365 instance create inbox-helper" in text
        assert "AA_INSTANCE_ID:" in text
        assert "(new)" in text
        assert "would call a365 create-instance" in text

    def test_noop_action_renders_already_registered(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\n")
        qs = FakeQuerySource(instances={"550e8400-e29b-41d4-a716-446655440000": {}})
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=qs)
        assert "(existing)" in plan.render_human()
        assert "already registered" in plan.render_human()


# ---------------------------------------------------------------------------
# write_text_atomic
# ---------------------------------------------------------------------------


class TestWriteTextAtomic:
    def test_creates_parent_dirs_and_no_tmp_remnant(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c.env"
        write_text_atomic(target, "K=V\n")
        assert target.read_text() == "K=V\n"
        assert not (tmp_path / "a" / "b" / "c.env.tmp").exists()


# ---------------------------------------------------------------------------
# apply_instance_plan
# ---------------------------------------------------------------------------


class TestApplyCreate:
    def test_create_writes_env_and_calls_mutator(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        mutator = FakeMutator()
        result = apply_instance_plan(plan, mutator=mutator, hermes_home=tmp_path)

        assert isinstance(result, InstanceCreateResult)
        assert result.env_written is True
        assert result.cloud_registered is True

        env_path = tmp_path / "agents" / "inbox-helper" / ".env"
        assert env_path.exists()
        env_text = env_path.read_text()
        assert "AGENT_IDENTITY=inbox-helper" in env_text
        assert f"AA_INSTANCE_ID={plan.aa_instance_id}" in env_text
        assert "A365_APP_PASSWORD" not in env_text  # secrets policy

        assert mutator.calls == [
            (
                "create_instance",
                {"blueprint_slug": "inbox-helper", "instance_id": plan.aa_instance_id},
            )
        ]


class TestApplyNoop:
    def test_noop_writes_env_but_does_not_call_mutator(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        existing_id = "550e8400-e29b-41d4-a716-446655440000"
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(f"AA_INSTANCE_ID={existing_id}\n")
        qs = FakeQuerySource(instances={existing_id: {"id": existing_id}})
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=qs)

        mutator = FakeMutator()
        result = apply_instance_plan(plan, mutator=mutator, hermes_home=tmp_path)

        assert mutator.calls == []
        assert result.cloud_registered is False
        assert result.env_written is True
        # The fully-rendered .env still preserves the existing AA_INSTANCE_ID.
        assert f"AA_INSTANCE_ID={existing_id}" in agent_env.read_text()


class TestApplyCreateCloudOnly:
    def test_calls_mutator_and_keeps_existing_id(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        existing_id = "550e8400-e29b-41d4-a716-446655440000"
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(f"AA_INSTANCE_ID={existing_id}\n")
        # cloud is empty
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        assert plan.action == "create-cloud-only"

        mutator = FakeMutator()
        apply_instance_plan(plan, mutator=mutator, hermes_home=tmp_path)
        assert mutator.calls == [
            (
                "create_instance",
                {"blueprint_slug": "inbox-helper", "instance_id": existing_id},
            )
        ]


class TestApplyAADSTSPropagation:
    def test_aadsts_error_propagates_after_env_write(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        mutator = FakeMutator(create_instance_error=AADSTSError("AADSTS65001", "no perms"))
        with pytest.raises(AADSTSError) as excinfo:
            apply_instance_plan(plan, mutator=mutator, hermes_home=tmp_path)
        assert excinfo.value.code == "AADSTS65001"
        # The .env was already written before the cloud call — so it does
        # exist on disk. The apply path is structured so the local artefact
        # converges first; the cloud call is the only failure point.
        env_path = tmp_path / "agents" / "inbox-helper" / ".env"
        assert env_path.exists()


# ---------------------------------------------------------------------------
# End-to-end idempotency
# ---------------------------------------------------------------------------


class TestIdempotentReRun:
    def test_second_apply_preserves_aa_instance_id(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        # First run: create
        plan1 = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=FakeQuerySource())
        first_id = plan1.aa_instance_id
        mutator = FakeMutator()
        apply_instance_plan(plan1, mutator=mutator, hermes_home=tmp_path)

        # Second run, cloud now reports the instance.
        qs = FakeQuerySource(instances={first_id: {"id": first_id}})
        plan2 = build_instance_plan(_inputs(), hermes_home=tmp_path, query_source=qs)
        assert plan2.aa_instance_id == first_id
        assert plan2.action == "noop"
        apply_instance_plan(plan2, mutator=FakeMutator(), hermes_home=tmp_path)
        # AA_INSTANCE_ID stayed put.
        env_text = (tmp_path / "agents" / "inbox-helper" / ".env").read_text()
        assert f"AA_INSTANCE_ID={first_id}" in env_text


# Verify InstancePlan render+attrs survive round-trip through the Path.
def test_plan_dataclass_fields_present() -> None:
    inputs = _inputs()
    p = InstancePlan(
        slug=inputs.slug,
        aa_instance_id="x",
        aa_instance_id_was_existing=False,
        action="create",
        desired_env_inputs=None,  # type: ignore[arg-type]
    )
    assert p.cloud_actual is None
