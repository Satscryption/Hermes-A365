from __future__ import annotations

from pathlib import Path

import pytest

from hermes_a365.render_instance_env import InstanceEnvInputs, render_instance_env

GOLDEN_DIR = Path(__file__).parent / "golden" / "instance_env"

# Pinned values so goldens are stable.
FIXED_INSTANCE_ID = "550e8400-e29b-41d4-a716-446655440000"


def _check_golden(name: str, actual: str, *, update: bool) -> None:
    path = GOLDEN_DIR / name
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    expected = path.read_text()
    assert actual == expected, (
        f"golden mismatch: {name}\n--- expected ---\n{expected}\n--- actual ---\n{actual}"
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


def _base_inputs(**overrides: object) -> InstanceEnvInputs:
    base: dict[str, object] = dict(
        agent_identity="inbox-helper",
        owner="sadiq@contoso.com",
        owner_aad_id="00000000-0000-0000-0000-000000000001",
        a365_app_id="00000000-0000-0000-0000-00000000aaa1",
        a365_tenant_id="contoso.onmicrosoft.com",
        hermes_otlp_endpoint="https://contoso.otel.agent365.microsoft.com",
        aa_instance_id=FIXED_INSTANCE_ID,
    )
    base.update(overrides)
    return InstanceEnvInputs(**base)  # type: ignore[arg-type]


def test_render_instance_env_minimal(update_golden: bool) -> None:
    actual = render_instance_env(_base_inputs())
    _check_golden("minimal.env", actual, update=update_golden)


def test_render_instance_env_with_business_hours(update_golden: bool) -> None:
    actual = render_instance_env(
        _base_inputs(
            business_hours_tz="Europe/London",
            business_hours_start="09:00",
            business_hours_end="18:00",
        )
    )
    _check_golden("with_business_hours.env", actual, update=update_golden)


def test_render_instance_env_with_path_b_identity() -> None:
    text = render_instance_env(
        _base_inputs(
            a365_bf_app_id="11111111-1111-1111-1111-111111111111",
            a365_bf_client_secret="bf-secret",
        )
    )
    assert "A365_BF_APP_ID=11111111-1111-1111-1111-111111111111\n" in text
    assert "A365_BF_CLIENT_SECRET=bf-secret\n" in text


def test_render_instance_env_half_configured_path_b_identity() -> None:
    text = render_instance_env(
        _base_inputs(
            a365_bf_app_id="11111111-1111-1111-1111-111111111111",
            a365_bf_client_secret="",
        )
    )
    assert "A365_BF_APP_ID=11111111-1111-1111-1111-111111111111\n" in text
    assert "A365_BF_CLIENT_SECRET" not in text


def test_render_instance_env_preserves_user_managed_env() -> None:
    text = render_instance_env(
        _base_inputs(
            preserved_env={
                "CUSTOM_OPERATOR_FLAG": "1",
                "Z_SIDE_SETTING": "kept",
            }
        )
    )
    assert text.endswith("CUSTOM_OPERATOR_FLAG=1\nZ_SIDE_SETTING=kept\n")


def test_render_instance_env_generates_uuid_when_missing() -> None:
    inputs = _base_inputs(aa_instance_id=None)
    assert inputs.aa_instance_id is not None
    assert len(inputs.aa_instance_id) == 36  # UUID length


def test_render_instance_env_omits_unset_business_hours() -> None:
    text = render_instance_env(_base_inputs())
    assert "BUSINESS_HOURS_TZ" not in text
    assert "BUSINESS_HOURS_START" not in text
    assert "BUSINESS_HOURS_END" not in text


def test_render_instance_env_never_emits_app_password() -> None:
    """Spec §6.5: A365_APP_PASSWORD must never be written to the per-agent .env."""
    text = render_instance_env(_base_inputs())
    assert "A365_APP_PASSWORD" not in text
    assert "PASSWORD" not in text


def test_render_instance_env_does_not_emit_a365_cli_variant() -> None:
    """Slice 18n (bug #9): the v0.1 A365_CLI_VARIANT field is gone."""
    text = render_instance_env(_base_inputs())
    assert "A365_CLI_VARIANT" not in text
    assert "VARIANT" not in text
