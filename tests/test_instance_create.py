"""Tests for hermes_a365.instance_create — v0.2 local-only runtime .env writer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hermes_a365.instance_create import (
    InstanceCreateError,
    InstanceCreateInputs,
    InstanceCreateResult,
    InstancePlan,
    apply_instance_plan,
    build_instance_plan,
    write_text_atomic,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_skill_env(hermes_home: Path, **overrides: str) -> None:
    """Plant a minimal ~/.hermes/.env that satisfies parent-env requirements."""
    base = {
        "A365_APP_ID": "00000000-0000-0000-0000-00000000aaa1",
        "A365_TENANT_ID": "contoso.onmicrosoft.com",
        "HERMES_OTLP_ENDPOINT": "https://contoso.otel.agent365.microsoft.com",
    }
    base.update(overrides)
    text = "".join(f"{k}={v}\n" for k, v in sorted(base.items()))
    (hermes_home / ".env").write_text(text)


def _inputs(**overrides: Any) -> InstanceCreateInputs:
    base: dict[str, Any] = {
        "slug": "inbox-helper",
        "owner": "sadiq@contoso.com",
        "owner_aad_id": "00000000-0000-0000-0000-000000000001",
    }
    base.update(overrides)
    return InstanceCreateInputs(**base)


# ---------------------------------------------------------------------------
# InstanceCreateInputs validation
# ---------------------------------------------------------------------------


class TestInstanceCreateInputs:
    def test_minimal_valid(self) -> None:
        inp = _inputs()
        assert inp.slug == "inbox-helper"
        assert inp.otlp_endpoint is None

    @pytest.mark.parametrize("field_name", ["slug", "owner", "owner_aad_id"])
    def test_required_fields_must_be_nonempty(self, field_name: str) -> None:
        with pytest.raises(ValueError, match=field_name):
            _inputs(**{field_name: ""})


# ---------------------------------------------------------------------------
# Skill env preconditions
# ---------------------------------------------------------------------------


class TestSkillEnvPreconditions:
    def test_missing_skill_env_fails_clean(self, tmp_path: Path) -> None:
        with pytest.raises(InstanceCreateError, match=r"run `hermes a365 register`"):
            build_instance_plan(_inputs(), hermes_home=tmp_path)

    def test_skill_env_missing_required_keys_fails_clean(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("HERMES_OTLP_ENDPOINT=x\n")
        with pytest.raises(InstanceCreateError, match="missing required keys"):
            build_instance_plan(_inputs(), hermes_home=tmp_path)

    def test_missing_otlp_endpoint_with_no_override_fails(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path, HERMES_OTLP_ENDPOINT="")
        with pytest.raises(InstanceCreateError, match="HERMES_OTLP_ENDPOINT"):
            build_instance_plan(_inputs(), hermes_home=tmp_path)

    def test_otlp_endpoint_override_accepted(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path, HERMES_OTLP_ENDPOINT="")
        plan = build_instance_plan(
            _inputs(otlp_endpoint="https://override"),
            hermes_home=tmp_path,
        )
        assert plan.desired_env_inputs.hermes_otlp_endpoint == "https://override"


# ---------------------------------------------------------------------------
# Plan content
# ---------------------------------------------------------------------------


class TestBuildInstancePlan:
    def test_defers_uuid_generation_when_absent(self, tmp_path: Path) -> None:
        # Slice 18n (bug #10): the plan no longer mints a UUID at
        # build time — apply does. Stops dry-run from showing one
        # value while a later --apply mints another.
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.aa_instance_id is None
        assert plan.aa_instance_id_was_existing is False

    def test_preserves_existing_aa_instance_id(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\n")
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.aa_instance_id == "550e8400-e29b-41d4-a716-446655440000"
        assert plan.aa_instance_id_was_existing is True
        assert plan.will_create is False  # .env already exists

    def test_will_create_true_when_agent_env_absent(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.will_create is True

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
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.desired_env_inputs.business_hours_tz == "Europe/London"
        assert plan.desired_env_inputs.business_hours_start == "09:00"
        assert plan.desired_env_inputs.business_hours_end == "17:00"

    def test_business_hours_override_wins(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(
            "AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\nBUSINESS_HOURS_TZ=Europe/London\n"
        )
        plan = build_instance_plan(
            _inputs(business_hours_tz="UTC"),
            hermes_home=tmp_path,
        )
        assert plan.desired_env_inputs.business_hours_tz == "UTC"

    def test_a365_cli_variant_field_is_gone(self, tmp_path: Path) -> None:
        # Slice 18n (bug #9): no more A365_CLI_VARIANT in the rendered .env.
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert not hasattr(plan.desired_env_inputs, "a365_cli_variant")

    def test_path_b_identity_inherited_from_parent_env(self, tmp_path: Path) -> None:
        _seed_skill_env(
            tmp_path,
            A365_BF_APP_ID="11111111-1111-1111-1111-111111111111",
            A365_BF_CLIENT_SECRET="bf-secret",
        )
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.desired_env_inputs.a365_bf_app_id == (
            "11111111-1111-1111-1111-111111111111"
        )
        assert plan.desired_env_inputs.a365_bf_client_secret == "bf-secret"

    def test_path_b_identity_absent_when_parent_env_absent(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.desired_env_inputs.a365_bf_app_id is None
        assert plan.desired_env_inputs.a365_bf_client_secret is None

    @pytest.mark.parametrize(
        ("key", "expected_app_id", "expected_secret"),
        [
            ("A365_BF_APP_ID", "11111111-1111-1111-1111-111111111111", None),
            ("A365_BF_CLIENT_SECRET", None, "bf-secret"),
        ],
    )
    def test_half_configured_path_b_identity_inherited(
        self,
        tmp_path: Path,
        key: str,
        expected_app_id: str | None,
        expected_secret: str | None,
    ) -> None:
        value = "11111111-1111-1111-1111-111111111111"
        if key == "A365_BF_CLIENT_SECRET":
            value = "bf-secret"
        _seed_skill_env(tmp_path, **{key: value})
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.desired_env_inputs.a365_bf_app_id == expected_app_id
        assert plan.desired_env_inputs.a365_bf_client_secret == expected_secret

    def test_preserves_user_managed_existing_env_keys(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(
            "AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\n"
            "CUSTOM_OPERATOR_FLAG=1\n"
            "Z_SIDE_SETTING=kept\n"
        )
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.desired_env_inputs.preserved_env == {
            "CUSTOM_OPERATOR_FLAG": "1",
            "Z_SIDE_SETTING": "kept",
        }

    def test_managed_existing_env_keys_are_not_preserved(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(
            "AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\n"
            "A365_BF_APP_ID=stale-bf-app\n"
            "A365_BF_CLIENT_SECRET=stale-bf-secret\n"
            "CUSTOM_OPERATOR_FLAG=1\n"
        )
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        assert plan.desired_env_inputs.a365_bf_app_id is None
        assert plan.desired_env_inputs.a365_bf_client_secret is None
        assert plan.desired_env_inputs.preserved_env == {"CUSTOM_OPERATOR_FLAG": "1"}


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------


class TestPlanRender:
    def test_human_says_no_cloud_step(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        text = plan.render_human()
        assert "[plan] hermes a365 instance create inbox-helper" in text
        # Slice 18n (bug #10): new agent shows the deferred-generation marker
        # rather than a UUID that --apply would discard.
        assert "(generated at apply)" in text
        assert "cloud step:    none" in text

    def test_human_marks_existing_id_as_preserved(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=abc-123\n")
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        text = plan.render_human()
        assert "abc-123" in text
        assert "preserved" in text


# ---------------------------------------------------------------------------
# write_text_atomic
# ---------------------------------------------------------------------------


class TestWriteTextAtomic:
    def test_creates_parents_and_no_tmp_remnant(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c.env"
        write_text_atomic(target, "K=V\n")
        assert target.read_text() == "K=V\n"
        assert not (tmp_path / "a" / "b" / "c.env.tmp").exists()

    def test_default_mode_is_owner_only(self, tmp_path: Path) -> None:
        """Slice 18x: defensive 0600 default. Output files (currently the
        per-agent .env, future activity-bridge config) shouldn't inherit
        world-readable umask defaults."""
        target = tmp_path / "secret.env"
        write_text_atomic(target, "K=V\n")
        # Mask off the file-type bits and check just the perm bits.
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_explicit_mode_override(self, tmp_path: Path) -> None:
        target = tmp_path / "shared.env"
        write_text_atomic(target, "K=V\n", mode=0o644)
        assert (target.stat().st_mode & 0o777) == 0o644


# ---------------------------------------------------------------------------
# apply_instance_plan
# ---------------------------------------------------------------------------


class TestApplyInstance:
    def test_writes_env_with_inputs_and_inherited(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        result = apply_instance_plan(plan)

        assert isinstance(result, InstanceCreateResult)
        assert result.env_written is True
        env_path = tmp_path / "agents" / "inbox-helper" / ".env"
        assert env_path.exists()
        text = env_path.read_text()
        assert "AGENT_IDENTITY=inbox-helper" in text
        # Slice 18n: plan.aa_instance_id is None for new agents — apply
        # mints the UUID, threads it through the result, and writes it
        # to the .env. The result's id is the source of truth.
        assert f"AA_INSTANCE_ID={result.aa_instance_id}" in text
        assert "A365_APP_ID=00000000-0000-0000-0000-00000000aaa1" in text
        # Secrets policy: never write the blueprint client secret to disk.
        assert "A365_APP_PASSWORD" not in text
        # Bug #9: no v0.1 leftover field.
        assert "A365_CLI_VARIANT" not in text

    def test_writes_path_b_identity_when_parent_env_has_it(self, tmp_path: Path) -> None:
        _seed_skill_env(
            tmp_path,
            A365_BF_APP_ID="11111111-1111-1111-1111-111111111111",
            A365_BF_CLIENT_SECRET="bf-secret",
        )
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        apply_instance_plan(plan)

        text = (tmp_path / "agents" / "inbox-helper" / ".env").read_text()
        assert "A365_BF_APP_ID=11111111-1111-1111-1111-111111111111\n" in text
        assert "A365_BF_CLIENT_SECRET=bf-secret\n" in text

    @pytest.mark.parametrize(
        ("key", "expected_line", "unexpected_key"),
        [
            (
                "A365_BF_APP_ID",
                "A365_BF_APP_ID=11111111-1111-1111-1111-111111111111\n",
                "A365_BF_CLIENT_SECRET",
            ),
            (
                "A365_BF_CLIENT_SECRET",
                "A365_BF_CLIENT_SECRET=bf-secret\n",
                "A365_BF_APP_ID",
            ),
        ],
    )
    def test_writes_half_configured_path_b_identity(
        self,
        tmp_path: Path,
        key: str,
        expected_line: str,
        unexpected_key: str,
    ) -> None:
        value = "11111111-1111-1111-1111-111111111111"
        if key == "A365_BF_CLIENT_SECRET":
            value = "bf-secret"
        _seed_skill_env(tmp_path, **{key: value})
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        apply_instance_plan(plan)

        text = (tmp_path / "agents" / "inbox-helper" / ".env").read_text()
        assert expected_line in text
        assert unexpected_key not in text

    def test_re_run_preserves_user_managed_env_keys(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(
            "AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\n"
            "CUSTOM_OPERATOR_FLAG=1\n"
            "Z_SIDE_SETTING=kept\n"
        )
        plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
        apply_instance_plan(plan)

        text = agent_env.read_text()
        assert "CUSTOM_OPERATOR_FLAG=1\n" in text
        assert "Z_SIDE_SETTING=kept\n" in text

    def test_idempotent_re_run_preserves_aa_instance_id(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        plan1 = build_instance_plan(_inputs(), hermes_home=tmp_path)
        first = apply_instance_plan(plan1)

        plan2 = build_instance_plan(_inputs(), hermes_home=tmp_path)
        # Plan now sees the existing UUID and pins it in plan.aa_instance_id.
        assert plan2.aa_instance_id == first.aa_instance_id
        assert plan2.aa_instance_id_was_existing is True
        apply_instance_plan(plan2)

        # AA_INSTANCE_ID stayed put across the round-trip.
        env_text = (tmp_path / "agents" / "inbox-helper" / ".env").read_text()
        assert f"AA_INSTANCE_ID={first.aa_instance_id}" in env_text


# ---------------------------------------------------------------------------
# Sanity on InstancePlan dataclass
# ---------------------------------------------------------------------------


def test_instance_plan_is_a_dataclass(tmp_path: Path) -> None:
    _seed_skill_env(tmp_path)
    plan = build_instance_plan(_inputs(), hermes_home=tmp_path)
    assert isinstance(plan, InstancePlan)
    assert plan.slug == "inbox-helper"
