"""Tests for hermes_a365.bot_service — Path B Azure Bot Service wrapper."""

from __future__ import annotations

import json
import shlex
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hermes_a365.bot_service import (
    SIDECAR_SCHEMA_VERSION,
    BotServiceConfig,
    BotServiceCreateInputs,
    BotServiceError,
    CommandResult,
    ProbeResult,
    _extract_directline_secret,
    apply_cleanup_plan,
    apply_create_plan,
    apply_enable_channel_plan,
    apply_update_endpoint_plan,
    build_cleanup_plan,
    build_create_plan,
    build_enable_channel_plan,
    build_parser,
    build_update_endpoint_plan,
    derive_bot_name,
    directline_runtime_probe,
    resolve_default_region,
    verify_bot_service,
)
from hermes_a365.bot_service import (
    BotServiceCleanupInputs as _BotServiceCleanupInputs,
)
from hermes_a365.bot_service import (
    BotServiceEnableChannelInputs as _BotServiceEnableChannelInputs,
)
from hermes_a365.bot_service import (
    BotServiceUpdateEndpointInputs as _BotServiceUpdateEndpointInputs,
)
from hermes_a365.bot_service import (
    run as run_bot_service,
)

BF_APP_ID = "11111111-1111-1111-1111-111111111111"
TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
ARM_RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/hermes-a365-bots/providers/"
    "Microsoft.BotService/botServices/hermes-inbox-helper-bot"
)


def BotServiceCleanupInputs(
    *,
    agent_name: str,
    sidecar_path: Path,
    purge_resource_group: bool = False,
    target_confirmation: str | None = None,
    legacy_binding_confirmation: str | None = None,
) -> _BotServiceCleanupInputs:
    """Build a confirmed cleanup input; refusal tests override confirmation."""
    if target_confirmation is None and sidecar_path.exists():
        target_confirmation = str(json.loads(sidecar_path.read_text())["armResourceId"])
    return _BotServiceCleanupInputs(
        agent_name=agent_name,
        sidecar_path=sidecar_path,
        purge_resource_group=purge_resource_group,
        target_confirmation=target_confirmation,
        legacy_binding_confirmation=legacy_binding_confirmation,
    )


def _sidecar_target(path: Path) -> str | None:
    if not path.exists():
        return None
    return str(json.loads(path.read_text())["armResourceId"])


def BotServiceEnableChannelInputs(
    *,
    agent_name: str,
    sidecar_path: Path,
    channel: str = "msteams",
    legacy_binding_confirmation: str | None = None,
    target_confirmation: str | None = None,
) -> _BotServiceEnableChannelInputs:
    return _BotServiceEnableChannelInputs(
        agent_name=agent_name,
        sidecar_path=sidecar_path,
        channel=channel,
        legacy_binding_confirmation=legacy_binding_confirmation,
        target_confirmation=target_confirmation or _sidecar_target(sidecar_path),
    )


def BotServiceUpdateEndpointInputs(
    *,
    agent_name: str,
    url: str,
    sidecar_path: Path,
    allow_local: bool = False,
    legacy_binding_confirmation: str | None = None,
    target_confirmation: str | None = None,
) -> _BotServiceUpdateEndpointInputs:
    return _BotServiceUpdateEndpointInputs(
        agent_name=agent_name,
        url=url,
        sidecar_path=sidecar_path,
        allow_local=allow_local,
        legacy_binding_confirmation=legacy_binding_confirmation,
        target_confirmation=target_confirmation or _sidecar_target(sidecar_path),
    )


class FakeRunner:
    def __init__(
        self,
        *,
        bot: dict[str, Any] | None = None,
        teams: dict[str, Any] | None = None,
        group_exists: bool = True,
        provider_state: str = "Registered",
        default_location: str | None = None,
        group_resources: list[dict[str, Any]] | None = None,
        resource_list_fails: bool = False,
        account_tenant_id: str = TENANT_ID,
    ) -> None:
        self.bot = bot
        self.teams = teams
        self.group_exists = group_exists
        self.provider_state = provider_state
        self.default_location = default_location
        # #102 M5: what `az resource list -g` reports as the group's top-level
        # contents. Default empty == "only ever held the (now-deleted) bot".
        self.group_resources = list(group_resources or [])
        self.resource_list_fails = resource_list_fails
        self.account_tenant_id = account_tenant_id
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        self.calls.append(list(argv))
        if argv[:3] == ["az", "config", "get"]:
            if self.default_location is None:
                return CommandResult(argv, 1, stderr="defaults.location is not set")
            return CommandResult(argv, 0, stdout=f"{self.default_location}\n")
        if argv[:3] == ["az", "account", "show"]:
            selected = (
                self._arg(argv, "--subscription")
                if "--subscription" in argv
                else SUBSCRIPTION_ID
            )
            return self._ok({"id": selected, "tenantId": self.account_tenant_id}, argv)
        if argv[:3] == ["az", "provider", "register"]:
            self.provider_state = "Registered"
            return self._ok({}, argv)
        if argv[:3] == ["az", "provider", "show"]:
            return CommandResult(argv, 0, stdout=self.provider_state)
        if argv[:3] == ["az", "group", "show"]:
            if not self.group_exists:
                return CommandResult(argv, 3, stderr="Resource group not found")
            return self._ok({"name": self._arg(argv, "--name")}, argv)
        if argv[:3] == ["az", "group", "create"]:
            self.group_exists = True
            return self._ok({"name": self._arg(argv, "--name")}, argv)
        if argv[:3] == ["az", "group", "delete"]:
            self.group_exists = False
            return self._ok({}, argv)
        if argv[:3] == ["az", "resource", "list"]:
            if self.resource_list_fails:
                return CommandResult(argv, 1, stderr="listing failed")
            # `az resource list` returns a JSON ARRAY, not an object.
            return CommandResult(argv, 0, stdout=json.dumps(self.group_resources))
        if argv[:3] == ["az", "bot", "show"]:
            if self.bot is None:
                return CommandResult(argv, 3, stderr="BotService not found")
            return self._ok(self.bot, argv)
        if argv[:3] == ["az", "bot", "create"]:
            endpoint = self._arg(argv, "--endpoint")
            app_id = self._arg(argv, "--appid")
            name = self._arg(argv, "--name")
            rg = self._arg(argv, "--resource-group")
            self.bot = self._bot(name=name, resource_group=rg, app_id=app_id, endpoint=endpoint)
            return self._ok(self.bot, argv)
        if argv[:3] == ["az", "bot", "delete"]:
            if self.bot is None:
                return CommandResult(argv, 3, stderr="BotService not found")
            self.bot = None
            return self._ok({}, argv)
        if argv[:3] == ["az", "bot", "update"]:
            assert self.bot is not None
            self.bot["properties"]["endpoint"] = self._arg(argv, "--endpoint")
            return self._ok(self.bot, argv)
        if argv[:4] == ["az", "bot", "msteams", "show"]:
            if self.teams is None:
                return CommandResult(argv, 3, stderr="Channel not found")
            return self._ok(self.teams, argv)
        if argv[:4] == ["az", "bot", "msteams", "create"]:
            self.teams = self._teams(accepted=False)
            return self._ok(self.teams, argv)
        if argv[:4] == ["az", "bot", "msteams", "delete"]:
            if self.teams is None:
                return CommandResult(argv, 3, stderr="Channel not found")
            self.teams = None
            return self._ok({}, argv)
        if argv[:3] == ["az", "rest", "--method"]:
            self.teams = self._teams(accepted=True)
            return self._ok(self.teams, argv)
        raise AssertionError(f"unexpected command: {argv}")

    def _ok(self, data: dict[str, Any], argv: list[str]) -> CommandResult:
        return CommandResult(argv, 0, stdout=json.dumps(data))

    @staticmethod
    def _arg(argv: list[str], name: str) -> str:
        return argv[argv.index(name) + 1]

    @staticmethod
    def _bot(
        *,
        name: str = "hermes-inbox-helper-bot",
        resource_group: str = "hermes-a365-bots",
        app_id: str = BF_APP_ID,
        endpoint: str = "https://example.test/api/messages",
    ) -> dict[str, Any]:
        return {
            "id": (
                f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{resource_group}"
                f"/providers/Microsoft.BotService/botServices/{name}"
            ),
            "properties": {
                "endpoint": endpoint,
                "msaAppId": app_id,
                "enabledChannels": ["webchat", "directline"],
            },
        }

    @staticmethod
    def _teams(*, accepted: bool = True) -> dict[str, Any]:
        return {
            "properties": {
                "properties": {
                    "acceptedTerms": accepted,
                    "isEnabled": True,
                    "deploymentEnvironment": "CommercialDeployment",
                }
            }
        }


def _inputs(tmp_path: Path, **overrides: Any) -> BotServiceCreateInputs:
    base: dict[str, Any] = {
        "agent_name": "Hermes Inbox Helper",
        "resource_group": "hermes-a365-bots",
        "endpoint": "https://example.test",
        "sidecar_path": tmp_path / "a365.bot-service.config.json",
    }
    base.update(overrides)
    return BotServiceCreateInputs(**base)


def _now() -> datetime:
    return datetime(2026, 5, 18, 12, 30, tzinfo=UTC)


def test_derive_bot_name_matches_playbook_shape() -> None:
    assert derive_bot_name("Hermes Inbox Helper") == "hermes-inbox-helper-bot"
    assert len(derive_bot_name("A" * 80)) <= 42


def test_resolve_default_region_prefers_az_config() -> None:
    region, source = resolve_default_region(runner=FakeRunner(default_location="uksouth"))

    assert region == "uksouth"
    assert source == "az config defaults.location"


def test_resolve_default_region_falls_back_when_az_config_empty() -> None:
    region, source = resolve_default_region(runner=FakeRunner(default_location=None))

    assert region == "westeurope"
    assert source == "built-in fallback"


def test_create_apply_writes_0600_sidecar_and_enables_teams(tmp_path: Path) -> None:
    runner = FakeRunner(group_exists=False)
    plan = build_create_plan(_inputs(tmp_path), operator_env={"A365_BF_APP_ID": BF_APP_ID})

    result = apply_create_plan(
        plan,
        runner=runner,
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
        now=_now,
    )

    assert result.created_bot is True
    assert result.created_teams_channel is True
    assert result.patched_teams_terms is True
    mode = stat.S_IMODE(result.sidecar_path.stat().st_mode)
    assert mode == 0o600
    data = json.loads(result.sidecar_path.read_text())
    assert data["schemaVersion"] == SIDECAR_SCHEMA_VERSION == 2
    assert data["msaAppId"] == BF_APP_ID
    assert data["tenantId"] == TENANT_ID
    assert data["messagingEndpoint"] == "https://example.test/api/messages"
    assert data["channelsEnabled"] == ["directline", "msteams", "webchat"]
    assert data["resourceGroupManaged"] is True
    assert any(call[:3] == ["az", "provider", "register"] for call in runner.calls)
    assert any(call[:4] == ["az", "bot", "msteams", "create"] for call in runner.calls)
    assert any(call[:3] == ["az", "rest", "--method"] for call in runner.calls)


def test_rerun_create_apply_is_noop_when_bot_and_teams_match(tmp_path: Path) -> None:
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_create_plan(_inputs(tmp_path), operator_env={"A365_BF_APP_ID": BF_APP_ID})

    result = apply_create_plan(
        plan,
        runner=runner,
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
        now=_now,
    )

    assert result.created_bot is False
    assert result.created_teams_channel is False
    assert result.patched_teams_terms is False
    assert not any(call[:3] == ["az", "bot", "create"] for call in runner.calls)
    assert not any(call[:3] == ["az", "rest", "--method"] for call in runner.calls)


def test_create_detects_msa_app_id_mismatch_without_autofix(tmp_path: Path) -> None:
    stale_bot = FakeRunner._bot(app_id="99999999-9999-9999-9999-999999999999")
    runner = FakeRunner(bot=stale_bot, teams=FakeRunner._teams())
    plan = build_create_plan(_inputs(tmp_path), operator_env={"A365_BF_APP_ID": BF_APP_ID})

    with pytest.raises(BotServiceError, match="cannot change --appid") as exc:
        apply_create_plan(
            plan,
            runner=runner,
            operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
            now=_now,
        )

    message = str(exc.value)
    assert "Paste-ready recovery:" in message
    assert "az bot msteams delete --resource-group hermes-a365-bots" in message
    assert "az bot delete --resource-group hermes-a365-bots" in message
    assert "hermes-a365 bot-service create" in message
    assert "--appid 11111111-1111-1111-1111-111111111111" in message
    assert "--apply" in message
    assert not (tmp_path / "a365.bot-service.config.json").exists()


def test_create_requires_path_b_app_id(tmp_path: Path) -> None:
    runner = FakeRunner()
    plan = build_create_plan(_inputs(tmp_path), operator_env={})

    with pytest.raises(BotServiceError, match="separate non-agentic BF app id"):
        apply_create_plan(plan, runner=runner, operator_env={}, now=_now)


def test_create_supports_sidecar_in_new_parent_directory(tmp_path: Path) -> None:
    sidecar = tmp_path / "new" / "nested" / "bot.json"
    runner = FakeRunner(bot=None, teams=None, group_exists=False)
    plan = build_create_plan(
        _inputs(tmp_path, sidecar_path=sidecar),
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
    )

    apply_create_plan(
        plan,
        runner=runner,
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
        now=_now,
    )

    assert sidecar.exists()
    assert not sidecar.with_name(f"{sidecar.name}.lock").exists()


def test_verify_missing_sidecar_fails_cleanly(tmp_path: Path) -> None:
    with pytest.raises(BotServiceError, match="bot-service create --apply"):
        verify_bot_service(tmp_path / "a365.bot-service.config.json", runner=FakeRunner())


def _write_sidecar(
    tmp_path: Path,
    *,
    resource_group_managed: bool = False,
    agent_name: str | None = "Hermes Inbox Helper",
) -> Path:
    path = tmp_path / "a365.bot-service.config.json"
    cfg = BotServiceConfig(
        schemaVersion=SIDECAR_SCHEMA_VERSION,
        subscriptionId=SUBSCRIPTION_ID,
        resourceGroup="hermes-a365-bots",
        botName="hermes-inbox-helper-bot",
        armResourceId=(
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/hermes-a365-bots/"
            "providers/Microsoft.BotService/botServices/hermes-inbox-helper-bot"
        ),
        msaAppId=BF_APP_ID,
        tenantId=TENANT_ID,
        messagingEndpoint="https://example.test/api/messages",
        channelsEnabled=["webchat", "directline", "msteams"],
        createdAt="2026-05-18T12:30:00Z",
        resourceGroupManaged=resource_group_managed,
        agentName=agent_name,
    )
    path.write_text(cfg.to_json())
    return path


def test_verify_reports_green_resource_and_channel_state(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())

    report = verify_bot_service(sidecar, runner=runner)

    assert report.ok is True
    statuses = {result.name: result.status for result in report.results}
    assert statuses["provider"] == "OK"
    assert statuses["bot_msa_app_id"] == "OK"
    assert statuses["msteams_channel"] == "OK"
    assert statuses["path_endpoint_parity"] == "OK"
    assert statuses["runtime_auth"] == "WARN"


def test_enable_channel_apply_creates_teams_and_updates_sidecar(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=None)
    cfg = BotServiceConfig.from_file(sidecar)
    cfg.channelsEnabled = ["webchat", "directline"]
    sidecar.write_text(cfg.to_json())
    plan = build_enable_channel_plan(
        BotServiceEnableChannelInputs(
            agent_name="Hermes Inbox Helper",
            channel="msteams",
            sidecar_path=sidecar,
        )
    )

    result = apply_enable_channel_plan(plan, runner=runner)

    assert result.channel_created is True
    assert result.patched_teams_terms is True
    assert BotServiceConfig.from_file(sidecar).channelsEnabled == [
        "directline",
        "msteams",
        "webchat",
    ]
    assert any(call[:4] == ["az", "bot", "msteams", "create"] for call in runner.calls)
    assert any(call[:3] == ["az", "rest", "--method"] for call in runner.calls)


def test_enable_channel_requires_exact_target_before_azure_read(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_enable_channel_plan(
        _BotServiceEnableChannelInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            target_confirmation="/subscriptions/wrong/bot",
        )
    )

    with pytest.raises(BotServiceError, match="--confirm-bot-target"):
        apply_enable_channel_plan(plan, runner=runner)

    assert runner.calls == []


def test_enable_channel_apply_is_noop_when_teams_enabled(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_enable_channel_plan(
        BotServiceEnableChannelInputs(agent_name="Hermes Inbox Helper", sidecar_path=sidecar)
    )

    result = apply_enable_channel_plan(plan, runner=runner)

    assert result.channel_created is False
    assert result.patched_teams_terms is False
    assert "already enabled" in "\n".join(result.messages)
    assert not any(call[:4] == ["az", "bot", "msteams", "create"] for call in runner.calls)
    assert not any(call[:3] == ["az", "rest", "--method"] for call in runner.calls)


def test_update_endpoint_apply_updates_bot_and_sidecar_without_disabling_channels(
    tmp_path: Path,
) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://new-tunnel.example",
            sidecar_path=sidecar,
        )
    )
    result = apply_update_endpoint_plan(plan, runner=runner)

    assert result.endpoint_updated is True
    updated = BotServiceConfig.from_file(sidecar)
    assert updated.messagingEndpoint == "https://new-tunnel.example/api/messages"
    assert updated.channelsEnabled == ["directline", "msteams", "webchat"]
    assert any(call[:3] == ["az", "bot", "update"] for call in runner.calls)


def test_update_endpoint_requires_exact_target_before_azure_read(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        _BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://new.example/api/messages",
            sidecar_path=sidecar,
            target_confirmation="/subscriptions/wrong/bot",
        )
    )

    with pytest.raises(BotServiceError, match="--confirm-bot-target"):
        apply_update_endpoint_plan(plan, runner=runner)

    assert runner.calls == []


def test_competing_endpoint_update_cannot_mutate_after_lock_claim(
    tmp_path: Path,
) -> None:
    from hermes_a365 import bot_service as bot_service_module

    sidecar = _write_sidecar(tmp_path)
    first = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://first.example/api/messages",
            sidecar_path=sidecar,
        )
    )
    second = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://second.example/api/messages",
            sidecar_path=sidecar,
        )
    )
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())

    with (
        bot_service_module._exclusive_sidecar_lock(sidecar),
        pytest.raises(BotServiceError, match="another Bot Service operation"),
    ):
        apply_update_endpoint_plan(second, runner=runner)

    assert runner.calls == []
    assert not sidecar.with_name(f"{sidecar.name}.lock").exists()
    assert first.sidecar_binding == second.sidecar_binding


def test_update_endpoint_apply_noops_when_endpoint_current(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://example.test/api/messages",
            sidecar_path=sidecar,
        )
    )

    result = apply_update_endpoint_plan(plan, runner=runner)

    assert result.endpoint_updated is False
    assert not any(call[:3] == ["az", "bot", "update"] for call in runner.calls)
    assert BotServiceConfig.from_file(sidecar).messagingEndpoint == (
        "https://example.test/api/messages"
    )


def test_cleanup_apply_deletes_bot_and_backs_up_sidecar(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    a365_config = tmp_path / "a365.config.json"
    a365_config.write_text('{"tenantId":"t"}\n')
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(agent_name="Hermes Inbox Helper", sidecar_path=sidecar)
    )

    result = apply_cleanup_plan(
        plan,
        runner=runner,
        now=lambda: datetime(2026, 5, 18, 13, 0, tzinfo=UTC),
    )

    assert result.bot_deleted is True
    assert result.sidecar_removed is False
    assert sidecar.exists()
    assert result.sidecar_backup_path == (
        tmp_path / "a365.bot-service.config.backup-20260518-130000.json"
    )
    assert result.sidecar_backup_path.exists()
    assert a365_config.exists()
    assert any(call[:3] == ["az", "bot", "delete"] for call in runner.calls)
    assert result.blueprint_preserved is True
    assert result.blueprint_preserved_message is not None
    assert any("Blueprint Entra app" in message for message in result.messages)


def test_cleanup_calls_az_bot_delete_without_yes_flag(tmp_path: Path) -> None:
    # `az bot delete` only accepts `--name` and `--resource-group`; it
    # rejects `--yes`. The v0.7.0 release walk hit this against the live
    # tenant after `az bot msteams delete` had already succeeded, leaving
    # the install half-cleaned (msteams gone, bot alive, sidecar drifted).
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(agent_name="Hermes Inbox Helper", sidecar_path=sidecar)
    )

    apply_cleanup_plan(plan, runner=runner)

    bot_delete_calls = [
        call for call in runner.calls if call[:3] == ["az", "bot", "delete"]
    ]
    assert bot_delete_calls, "az bot delete was never invoked"
    for call in bot_delete_calls:
        assert "--yes" not in call, (
            f"az bot delete must not be invoked with --yes; got {call}"
        )


def test_verify_generated_config_help_documents_cwd_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["verify", "--help"])

    assert excinfo.value.code == 0
    help_text = capsys.readouterr().out
    help_flat = " ".join(help_text.split())
    assert "--generated-config" in help_text
    assert "./a365.generated.config.json" in help_text
    assert "current working" in help_flat
    assert "another cwd" in help_flat


def test_cleanup_apply_is_noop_when_sidecar_missing(tmp_path: Path) -> None:
    sidecar = tmp_path / "a365.bot-service.config.json"
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(agent_name="Hermes Inbox Helper", sidecar_path=sidecar)
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.bot_deleted is False
    assert result.sidecar_removed is False
    assert runner.calls == []
    assert "nothing to clean up" in "\n".join(result.messages)


def test_cleanup_cli_reports_missing_sidecar_as_partial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sidecar = tmp_path / "missing-sidecar.json"
    args = build_parser().parse_args(
        [
            "cleanup",
            "--agent-name",
            "Hermes Inbox Helper",
            "--sidecar",
            str(sidecar),
            "--apply",
            "--confirm",
            "Hermes Inbox Helper",
        ]
    )

    assert run_bot_service(args) == 1
    assert "nothing to clean up" in capsys.readouterr().out


def test_cleanup_apply_missing_bot_still_backs_up_sidecar(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=None, teams=None)
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(agent_name="Hermes Inbox Helper", sidecar_path=sidecar)
    )

    result = apply_cleanup_plan(
        plan,
        runner=runner,
        now=lambda: datetime(2026, 5, 18, 13, 0, tzinfo=UTC),
    )

    assert result.bot_deleted is False
    assert result.sidecar_removed is False
    assert sidecar.exists()
    assert any("no bot resource found" in message for message in result.messages)


def test_cleanup_purge_resource_group_requires_managed_sidecar(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path, resource_group_managed=False)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_deleted is False
    assert not any(call[:3] == ["az", "group", "delete"] for call in runner.calls)


def test_cleanup_purge_resource_group_deletes_when_managed(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_deleted is False
    assert not any(call[:3] == ["az", "group", "delete"] for call in runner.calls)
    assert "cannot make the inventory check and group deletion atomic" in "\n".join(
        result.messages
    )


def test_cleanup_completes_requested_purge_after_group_absence_readback(
    tmp_path: Path,
) -> None:
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner(bot=None, teams=None, group_exists=False)
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_purge_pending is False
    assert result.sidecar_removed is False
    assert sidecar.exists()
    assert "already absent" in "\n".join(result.messages)


# ---------------------------------------------------------------------------
# #102 WP1 — subscription pinning (H3 provisioning + L5 cleanup)
# ---------------------------------------------------------------------------


def test_create_apply_pins_subscription_on_every_az_call(tmp_path: Path) -> None:
    # H3 (#102): provisioning must never act on the CLI's ambient default
    # subscription. Every ARM read/mutate in the create flow carries the
    # RESOLVED --subscription; `az account show` is the sole exception (it is
    # what resolution reads).
    runner = FakeRunner(bot=None, teams=None, group_exists=False)
    plan = build_create_plan(
        _inputs(tmp_path),
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
    )

    apply_create_plan(
        plan,
        runner=runner,
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
        now=_now,
    )

    az_calls = [c for c in runner.calls if c[:1] == ["az"]]
    assert az_calls, "no az calls captured"
    for call in az_calls:
        if call[:3] == ["az", "account", "show"]:
            # The first call resolves ambient defaults; the second validates
            # the resolved subscription and tenant before mutation.
            if "--subscription" in call:
                assert FakeRunner._arg(call, "--subscription") == SUBSCRIPTION_ID
            continue
        if call[:3] == ["az", "rest", "--method"]:
            # Pinned via the absolute management URL, not a flag.
            url = FakeRunner._arg(call, "--url")
            assert f"/subscriptions/{SUBSCRIPTION_ID}/" in url
            continue
        assert "--subscription" in call, f"unpinned az call: {call}"
        assert FakeRunner._arg(call, "--subscription") == SUBSCRIPTION_ID


def test_create_apply_pins_explicit_subscription_over_account(tmp_path: Path) -> None:
    # H3 (#102): an explicit --subscription-id wins over the account default —
    # and is what every provisioning call pins.
    explicit = "44444444-4444-4444-4444-444444444444"
    runner = FakeRunner(bot=None, teams=None, group_exists=False)
    plan = build_create_plan(
        _inputs(tmp_path, subscription_id=explicit),
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
    )

    apply_create_plan(
        plan,
        runner=runner,
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
        now=_now,
    )

    group_create = next(c for c in runner.calls if c[:3] == ["az", "group", "create"])
    bot_create = next(c for c in runner.calls if c[:3] == ["az", "bot", "create"])
    assert FakeRunner._arg(group_create, "--subscription") == explicit
    assert FakeRunner._arg(bot_create, "--subscription") == explicit


def test_create_refuses_subscription_tenant_mismatch_before_mutation(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        bot=None,
        teams=None,
        group_exists=False,
        account_tenant_id="99999999-9999-9999-9999-999999999999",
    )
    plan = build_create_plan(
        _inputs(tmp_path, subscription_id=SUBSCRIPTION_ID),
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
    )

    with pytest.raises(BotServiceError, match="refusing before mutation"):
        apply_create_plan(
            plan,
            runner=runner,
            operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
        )

    assert runner.calls
    assert all(call[:3] == ["az", "account", "show"] for call in runner.calls)


def test_cleanup_apply_pins_sidecar_subscription_on_deletes(tmp_path: Path) -> None:
    # L5 (#102): deletes bind to the sidecar's persisted subscriptionId, not
    # the ambient az default — bot delete, msteams delete, and the purge's
    # resource inventory all carry it. Group deletion is deliberately manual.
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    apply_cleanup_plan(plan, runner=runner)

    for prefix in (
        ["az", "bot", "show"],
        ["az", "bot", "msteams", "delete"],
        ["az", "bot", "delete"],
        ["az", "resource", "list"],
    ):
        matching = [c for c in runner.calls if c[: len(prefix)] == prefix]
        assert matching, f"expected a {' '.join(prefix)} call"
        for call in matching:
            assert FakeRunner._arg(call, "--subscription") == SUBSCRIPTION_ID, (
                f"unpinned cleanup call: {call}"
            )


# ---------------------------------------------------------------------------
# #102 WP2 — resource-group blast-radius guard (M5)
# ---------------------------------------------------------------------------


def test_cleanup_purge_skips_when_group_holds_foreign_resources(tmp_path: Path) -> None:
    # M5 (#102): a later-added unrelated resource must not be destroyed by the
    # purge. The purge (not the whole cleanup) is skipped and the leftovers
    # are named; bot deletion and sidecar backup still complete.
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner(
        bot=FakeRunner._bot(),
        teams=FakeRunner._teams(),
        group_resources=[
            {
                "id": (
                    "/subscriptions/sub/resourceGroups/rg/providers"
                    "/Microsoft.Storage/storageAccounts/prodlogs"
                ),
                "type": "Microsoft.Storage/storageAccounts",
                "name": "prodlogs",
            }
        ],
    )
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_deleted is False
    assert not any(call[:3] == ["az", "group", "delete"] for call in runner.calls)
    joined = "\n".join(result.messages)
    assert "Microsoft.Storage/storageAccounts/prodlogs" in joined
    assert "non-Hermes-managed" in joined
    # Bot deletion completed, but the requested group purge remains explicit
    # partial work and keeps the sidecar available for retry/readback.
    assert result.bot_deleted is True
    assert result.resource_group_purge_pending is True
    assert result.sidecar_removed is False
    assert sidecar.exists()


def test_cleanup_purge_proceeds_when_only_managed_bot_listed(tmp_path: Path) -> None:
    # M5 (#102): the managed bot itself (e.g. its deletion still settling) is
    # not a foreign resource — matched by the sidecar's armResourceId.
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner(
        bot=FakeRunner._bot(),
        teams=FakeRunner._teams(),
        group_resources=[
            {
                # Case differs from the sidecar's armResourceId on purpose —
                # ARM ids are compared case-insensitively.
                "id": ARM_RESOURCE_ID.upper(),
                "type": "Microsoft.BotService/botServices",
                "name": "hermes-inbox-helper-bot",
            }
        ],
    )
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_deleted is False
    assert not any(call[:3] == ["az", "group", "delete"] for call in runner.calls)
    assert "review and run manually" in "\n".join(result.messages)
    assert result.resource_group_purge_pending is True
    assert sidecar.exists()


def test_cleanup_purge_fails_closed_when_listing_fails(tmp_path: Path) -> None:
    # M5 (#102): unknown contents == do not delete. A failed listing skips the
    # purge rather than treating the group as empty.
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner(
        bot=FakeRunner._bot(), teams=FakeRunner._teams(), resource_list_fails=True
    )
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_deleted is False
    assert not any(call[:3] == ["az", "group", "delete"] for call in runner.calls)
    assert "could not enumerate" in "\n".join(result.messages)


class _RawResourceListRunner(FakeRunner):
    def __init__(self, raw_resource_list: str) -> None:
        super().__init__(bot=FakeRunner._bot(), teams=FakeRunner._teams())
        self.raw_resource_list = raw_resource_list

    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        if argv[:3] == ["az", "resource", "list"]:
            self.calls.append(list(argv))
            return CommandResult(argv, 0, stdout=self.raw_resource_list)
        return super().run(argv, timeout=timeout)


@pytest.mark.parametrize("raw_resource_list", ["", "[null]", '[{"name":"ok"}, null]'])
def test_cleanup_purge_fails_closed_on_untrustworthy_success_output(
    tmp_path: Path, raw_resource_list: str
) -> None:
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = _RawResourceListRunner(raw_resource_list)
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_deleted is False
    assert not any(call[:3] == ["az", "group", "delete"] for call in runner.calls)
    assert "could not enumerate" in "\n".join(result.messages)


def test_cleanup_plan_enumerates_group_contents_for_purge_dry_run(tmp_path: Path) -> None:
    # M5 (#102): with a runner, the PLAN (dry-run) lists the group's current
    # contents so the operator sees the blast radius before --apply.
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner(
        group_resources=[
            {
                "id": (
                    "/subscriptions/sub/resourceGroups/rg/providers"
                    "/Microsoft.KeyVault/vaults/prodkv"
                ),
                "type": "Microsoft.KeyVault/vaults",
                "name": "prodkv",
            }
        ]
    )
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        ),
        runner=runner,
    )

    rendered = plan.render_human()
    assert "Microsoft.KeyVault/vaults/prodkv" in rendered
    assert "never auto-delete the group" in rendered


def test_cleanup_plan_makes_no_az_calls_without_purge(tmp_path: Path) -> None:
    # The plan stays offline unless a managed-group purge is actually
    # requested — a plain cleanup dry-run must not start issuing az calls.
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner()
    build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper", sidecar_path=sidecar
        ),
        runner=runner,
    )
    assert runner.calls == []


# ---------------------------------------------------------------------------
# #102 WP3 — sidecar target binding (M6)
# ---------------------------------------------------------------------------


def test_cleanup_refuses_sidecar_bound_to_other_agent(tmp_path: Path) -> None:
    # M6 (#102): running cleanup for agent X against agent Y's sidecar must
    # refuse BEFORE any deletion, naming the sidecar's real targets.
    sidecar = _write_sidecar(
        tmp_path, resource_group_managed=True, agent_name="Other Agent"
    )
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    with pytest.raises(BotServiceError) as excinfo:
        apply_cleanup_plan(plan, runner=runner)

    message = str(excinfo.value)
    assert "Other Agent" in message
    assert "hermes-inbox-helper-bot" in message
    assert "hermes-a365-bots" in message
    # Refused before ANY az mutation ran.
    assert runner.calls == []
    assert sidecar.exists()


def test_cleanup_requires_exact_arm_target_before_any_azure_read(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        _BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            target_confirmation="/subscriptions/wrong/resource",
        )
    )

    with pytest.raises(BotServiceError, match="--confirm-bot-target"):
        apply_cleanup_plan(plan, runner=runner)

    assert runner.calls == []
    assert sidecar.exists()


def test_cleanup_refuses_legacy_sidecar_without_binding_confirmation(
    tmp_path: Path,
) -> None:
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = 1
    raw.pop("agentName")
    sidecar.write_text(json.dumps(raw))
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        _BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            target_confirmation=ARM_RESOURCE_ID,
        )
    )

    with pytest.raises(BotServiceError, match="--confirm-legacy-binding"):
        apply_cleanup_plan(plan, runner=runner)

    assert runner.calls == []


def test_cleanup_refuses_sidecar_changed_after_plan(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
        )
    )
    raw = json.loads(sidecar.read_text())
    raw["messagingEndpoint"] = "https://changed.example/api/messages"
    sidecar.write_text(json.dumps(raw))

    with pytest.raises(BotServiceError, match="changed after planning"):
        apply_cleanup_plan(plan, runner=runner)

    assert runner.calls == []


def test_cleanup_plan_rejects_a_b_a_sidecar_interleaving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _write_sidecar(tmp_path)
    snapshot_a = sidecar.read_bytes()
    raw_b = json.loads(snapshot_a)
    raw_b["botName"] = "hermes-inbox-helper-box"
    raw_b["armResourceId"] = raw_b["armResourceId"][:-1] + "x"
    snapshot_b = (json.dumps(raw_b, indent=2, sort_keys=True) + "\n").encode()
    assert len(snapshot_a) == len(snapshot_b)
    original_read_bytes = Path.read_bytes
    reads = 0

    def racing_read_bytes(path: Path) -> bytes:
        nonlocal reads
        if path == sidecar:
            reads += 1
            return snapshot_b if reads == 1 else snapshot_a
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", racing_read_bytes)

    with pytest.raises(BotServiceError, match="changed while the cleanup plan was read"):
        build_cleanup_plan(
            _BotServiceCleanupInputs(
                agent_name="Hermes Inbox Helper",
                sidecar_path=sidecar,
                target_confirmation=ARM_RESOURCE_ID,
            )
        )


def test_cleanup_preserves_new_sidecar_published_after_final_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_a365 import bot_service as bot_service_module

    sidecar = _write_sidecar(tmp_path)
    snapshot_a = sidecar.read_bytes()
    raw_b = json.loads(snapshot_a)
    raw_b["messagingEndpoint"] = "https://new-writer.example/api/messages"
    snapshot_b = (json.dumps(raw_b, indent=2, sort_keys=True) + "\n").encode()
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
        )
    )
    original_binding = bot_service_module._sidecar_file_binding
    checks = 0

    def publish_after_binding(path: Path) -> tuple[int, int, int, str]:
        nonlocal checks
        binding = original_binding(path)
        checks += 1
        if checks == 2:
            path.write_bytes(snapshot_b)
        return binding

    monkeypatch.setattr(
        bot_service_module, "_sidecar_file_binding", publish_after_binding
    )
    result = apply_cleanup_plan(
        plan,
        runner=FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams()),
        now=_now,
    )

    assert result.sidecar_backup_path is not None
    assert result.sidecar_backup_path.read_bytes() == snapshot_a
    assert sidecar.read_bytes() == snapshot_b
    assert result.sidecar_removed is False


def test_sidecar_rejects_non_boolean_resource_group_authority(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["resourceGroupManaged"] = "false"
    sidecar.write_text(json.dumps(raw))

    with pytest.raises(BotServiceError, match="must be true or false"):
        BotServiceConfig.from_file(sidecar)


def test_cleanup_legacy_sidecar_without_agent_name_uses_live_binding_and_confirm(
    tmp_path: Path,
) -> None:
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = 1
    del raw["agentName"]
    sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            legacy_binding_confirmation="Hermes Inbox Helper",
        )
    )

    result = apply_cleanup_plan(plan, runner=runner, now=_now)

    assert result.bot_deleted is True
    assert result.sidecar_removed is False
    assert result.sidecar_backup_path is not None
    assert result.sidecar_backup_path.exists()
    assert sidecar.exists()


def test_update_migrates_verified_legacy_sidecar_to_bound_v2(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = 1
    raw.pop("agentName")
    sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://new-tunnel.example",
            sidecar_path=sidecar,
            legacy_binding_confirmation="Hermes Inbox Helper",
        )
    )
    assert "--confirm-legacy-binding='Hermes Inbox Helper'" in plan.render_human()

    result = apply_update_endpoint_plan(plan, runner=runner)

    updated = BotServiceConfig.from_file(sidecar)
    assert updated.schemaVersion == SIDECAR_SCHEMA_VERSION
    assert updated.agentName == "Hermes Inbox Helper"
    assert any("migrated legacy sidecar" in message for message in result.messages)
    backups = list(tmp_path.glob("a365.bot-service.config.backup-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["schemaVersion"] == 1


@pytest.mark.parametrize("confirmation", [None, "Other Agent"])
def test_update_refuses_legacy_binding_without_exact_confirmation_before_mutation(
    tmp_path: Path, confirmation: str | None
) -> None:
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = 1
    raw.pop("agentName")
    sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://new-tunnel.example",
            sidecar_path=sidecar,
            legacy_binding_confirmation=confirmation,
        )
    )

    with pytest.raises(BotServiceError, match="--confirm-legacy-binding"):
        apply_update_endpoint_plan(plan, runner=runner)

    assert not any(call[:3] == ["az", "bot", "update"] for call in runner.calls)
    assert json.loads(sidecar.read_text())["schemaVersion"] == 1


def test_parser_accepts_explicit_legacy_binding_confirmation() -> None:
    args = build_parser().parse_args(
        [
            "update-endpoint",
            "--agent-name",
            "Hermes Inbox Helper",
            "--url",
            "https://example.test/api/messages",
            "--confirm-legacy-binding",
            "Hermes Inbox Helper",
        ]
    )

    assert args.confirm_legacy_binding == "Hermes Inbox Helper"


def test_cleanup_parser_accepts_target_and_legacy_confirmations() -> None:
    args = build_parser().parse_args(
        [
            "cleanup",
            "--agent-name",
            "Hermes Inbox Helper",
            "--confirm-bot-target",
            ARM_RESOURCE_ID,
            "--confirm-legacy-binding",
            "Hermes Inbox Helper",
        ]
    )

    assert args.confirm_bot_target == ARM_RESOURCE_ID
    assert args.confirm_legacy_binding == "Hermes Inbox Helper"


def test_cleanup_legacy_dry_run_prints_complete_apply_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = 1
    raw.pop("agentName")
    sidecar.write_text(json.dumps(raw))
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(
        ["cleanup", "--agent-name", "Hermes Inbox Helper", "--sidecar", str(sidecar)]
    )

    assert run_bot_service(args) == 0
    output = capsys.readouterr().out
    rendered = output.split("Re-run with ", maxsplit=1)[1].split(
        " to clean up Bot Service.", maxsplit=1
    )[0]
    assert shlex.split(rendered) == [
        "--apply",
        "--confirm=Hermes Inbox Helper",
        f"--confirm-bot-target={ARM_RESOURCE_ID}",
        "--confirm-legacy-binding=Hermes Inbox Helper",
    ]


def test_create_apply_writes_agent_name_into_sidecar(tmp_path: Path) -> None:
    # M6 (#102): provisioning stamps the binding the cleanup check reads.
    runner = FakeRunner(bot=None, teams=None, group_exists=False)
    plan = build_create_plan(
        _inputs(tmp_path),
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
    )

    result = apply_create_plan(
        plan,
        runner=runner,
        operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
        now=_now,
    )

    assert result.config.agentName == "Hermes Inbox Helper"
    on_disk = json.loads(result.sidecar_path.read_text())
    assert on_disk["agentName"] == "Hermes Inbox Helper"


def test_sidecar_from_file_accepts_legacy_without_agent_name(tmp_path: Path) -> None:
    # M6 (#102): the v2 reader retains an explicit compatibility path for a v1
    # sidecar written before the binding existed (agentName=None).
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = 1
    del raw["agentName"]
    sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")

    cfg = BotServiceConfig.from_file(sidecar)
    assert cfg.agentName is None
    assert cfg.schemaVersion == 1


@pytest.mark.parametrize("agent_name", [None, "", "  "])
def test_v2_sidecar_requires_non_empty_agent_binding(
    tmp_path: Path, agent_name: str | None
) -> None:
    sidecar = _write_sidecar(tmp_path, agent_name=agent_name)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = SIDECAR_SCHEMA_VERSION
    if agent_name is None:
        raw.pop("agentName", None)
    sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")

    with pytest.raises(BotServiceError, match="requires a non-empty agentName"):
        BotServiceConfig.from_file(sidecar)


def test_cleanup_plan_render_surfaces_target_and_binding(tmp_path: Path) -> None:
    # M6 (#102): the plan (shown before the operator is told what to type for
    # --confirm) names the subscription and which agent the sidecar was
    # provisioned for.
    sidecar = _write_sidecar(tmp_path)
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(agent_name="Hermes Inbox Helper", sidecar_path=sidecar)
    )
    rendered = plan.render_human()
    assert SUBSCRIPTION_ID in rendered
    assert "provisioned for: Hermes Inbox Helper" in rendered


def test_verify_pins_sidecar_subscription_on_every_probe(tmp_path: Path) -> None:
    # #102 review: the Teams-channel probe was the one verify read left
    # unpinned — in a multi-subscription setup the bot probe queried the
    # sidecar's subscription while the msteams probe queried the ambient one,
    # yielding an incoherent (or falsely failing) verify report.
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())

    verify_bot_service(sidecar, runner=runner)

    for call in runner.calls:
        assert "--subscription" in call, f"unpinned verify call: {call}"
        assert FakeRunner._arg(call, "--subscription") == SUBSCRIPTION_ID


def test_sidecar_with_blank_subscription_id_refuses_to_load(tmp_path: Path) -> None:
    # #102 review: a blank subscriptionId would silently UN-pin every az call
    # (_sub_args('') emits no flag) — cleanup would then delete against the
    # ambient az subscription. Fail closed at load instead.
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["subscriptionId"] = "  "
    sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")

    with pytest.raises(BotServiceError, match="blank subscriptionId"):
        BotServiceConfig.from_file(sidecar)


class _RaisingListRunner(FakeRunner):
    """az resource list RAISES (missing binary / timeout), like
    SubprocessRunner does — not just a nonzero exit."""

    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        if argv[:3] == ["az", "resource", "list"]:
            self.calls.append(list(argv))
            raise BotServiceError("failed to run 'az': No such file or directory")
        return super().run(argv, timeout=timeout)


def test_cleanup_purge_fails_closed_when_listing_raises(tmp_path: Path) -> None:
    # #102 review: SubprocessRunner RAISES BotServiceError for a missing az
    # binary or timeout. The purge guard must treat that as contents-unknown
    # (skip the purge) — not abort the cleanup mid-way after the bot was
    # already deleted.
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = _RaisingListRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_deleted is False
    assert not any(call[:3] == ["az", "group", "delete"] for call in runner.calls)
    assert "could not enumerate" in "\n".join(result.messages)
    # Bot deletion completed, but unknown group state preserves retry evidence.
    assert result.bot_deleted is True
    assert result.resource_group_purge_pending is True
    assert result.sidecar_removed is False


def test_cleanup_plan_survives_raising_listing(tmp_path: Path) -> None:
    # #102 review: the CLI dry-run now passes a real SubprocessRunner for
    # plan-time enumeration. On a machine without az, that runner RAISES —
    # the plan must degrade to "no listing" (as before PR-D), not crash a
    # dry-run that used to work offline.
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = _RaisingListRunner()
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        ),
        runner=runner,
    )
    assert plan.resource_group_contents is None
    assert "never auto-delete the group" in plan.render_human()


def test_validate_confirm_rejects_missing_and_mismatch() -> None:
    # #102 review gap: bot_service._validate_confirm had no direct tests (the
    # tested one belongs to cleanup.py).
    from hermes_a365.bot_service import _validate_confirm

    with pytest.raises(BotServiceError):
        _validate_confirm("Hermes Inbox Helper", None)
    with pytest.raises(BotServiceError):
        _validate_confirm("Hermes Inbox Helper", "hermes inbox helper")
    _validate_confirm("Hermes Inbox Helper", "Hermes Inbox Helper")


def test_verify_warns_when_path_a_and_path_b_endpoints_drift(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    generated_config = tmp_path / "a365.generated.config.json"
    generated_config.write_text(
        json.dumps({"messagingEndpoint": "https://path-a.example/api/messages"})
    )
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())

    report = verify_bot_service(sidecar, runner=runner, generated_config_path=generated_config)

    assert report.ok is True
    parity = next(result for result in report.results if result.name == "path_endpoint_parity")
    assert parity.status == "WARN"
    assert "activity-bridge update-endpoint" in parity.detail


def test_verify_detects_runtime_auth_probe_rejection(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())

    def rejected_probe(config: BotServiceConfig, runner: FakeRunner) -> ProbeResult:
        return ProbeResult(
            "runtime_auth",
            "ERROR",
            "configured endpoint rejected a Path B BF Connector token (HTTP 403)",
        )

    report = verify_bot_service(sidecar, runner=runner, runtime_probe=rejected_probe)

    assert report.ok is False
    runtime = next(result for result in report.results if result.name == "runtime_auth")
    assert runtime.status == "ERROR"
    assert "BF Connector token" in runtime.detail


# Structure mirrors a real `az bot directline show --with-secrets` response
# captured during the v0.7.0 release walk (2026-05-19). The `properties` key
# is doubly-nested, and a sibling `resource.properties` repeats the channel.
# All key material is placeholder.
_AZ_DIRECTLINE_REAL_SHAPE: dict[str, Any] = {
    "changedTime": "0001-01-01T00:00:00Z",
    "etag": None,
    "id": (
        "/subscriptions/00000000-0000-0000-0000-000000000000"
        "/resourceGroups/rg/providers/Microsoft.BotService/botServices"
        "/bot/channels/DirectLineChannel"
    ),
    "location": "global",
    "name": None,
    "properties": {
        "channelName": "DirectLineChannel",
        "etag": "W/\"x\"",
        "location": "global",
        "properties": {
            "directLineEmbedCode": None,
            "extensionKey1": "EXT_KEY_1_PLACEHOLDER",
            "extensionKey2": "EXT_KEY_2_PLACEHOLDER",
            "isEnabled": True,
            "sites": [
                {
                    "isEnabled": True,
                    "isV1Enabled": True,
                    "isV3Enabled": True,
                    "key": "PRIMARY_SITE_KEY_PLACEHOLDER",
                    "key2": "SECONDARY_SITE_KEY_PLACEHOLDER",
                    "siteId": "SITE_ID_PLACEHOLDER",
                    "siteName": "Default Site",
                    "trustedOrigins": [],
                }
            ],
        },
        "provisioningState": None,
    },
    "resource": {
        "channelName": "DirectLineChannel",
        "etag": "W/\"x\"",
        "location": "global",
        "properties": {
            "isEnabled": True,
            "sites": [
                {
                    "isEnabled": True,
                    "key": "PRIMARY_SITE_KEY_PLACEHOLDER",
                    "key2": "SECONDARY_SITE_KEY_PLACEHOLDER",
                    "siteId": "SITE_ID_PLACEHOLDER",
                    "siteName": "Default Site",
                }
            ],
        },
        "provisioningState": None,
    },
    "resourceGroup": "rg",
}


def test_extract_directline_secret_walks_real_az_double_nested_shape() -> None:
    # Live `az bot directline show --with-secrets` nests the channel sites at
    # `data.properties.properties.sites[]`. Regression: pre-fix code only
    # checked the single-nested `data.properties.sites[]` and failed against
    # real az output during the v0.7.0 release walk.
    assert (
        _extract_directline_secret(_AZ_DIRECTLINE_REAL_SHAPE)
        == "PRIMARY_SITE_KEY_PLACEHOLDER"
    )


def test_extract_directline_secret_handles_single_nested_legacy_shape() -> None:
    legacy = {
        "properties": {
            "sites": [
                {"key": "LEGACY_KEY_PLACEHOLDER"},
            ],
        },
    }
    assert _extract_directline_secret(legacy) == "LEGACY_KEY_PLACEHOLDER"


def test_extract_directline_secret_falls_back_to_resource_properties() -> None:
    # If az ever drops the double-nested `properties.properties` but keeps the
    # `resource.properties.sites[]` copy, the probe should still succeed.
    resource_only = {
        "properties": {"channelName": "DirectLineChannel"},
        "resource": {
            "properties": {
                "sites": [{"key": "RESOURCE_KEY_PLACEHOLDER"}],
            },
        },
    }
    assert (
        _extract_directline_secret(resource_only) == "RESOURCE_KEY_PLACEHOLDER"
    )


def test_extract_directline_secret_raises_when_no_secret_anywhere() -> None:
    with pytest.raises(BotServiceError, match="not present in az output"):
        _extract_directline_secret({"properties": {"sites": [{"siteName": "x"}]}})


def test_directline_probe_percent_encodes_conversation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #103/L8: the conversationId comes from the Direct Line
    # start-conversation response and is interpolated into the activities
    # probe URL. Percent-encode it so a hostile id can't inject path
    # structure, a query, or a fragment.
    from hermes_a365 import bot_service

    cfg = BotServiceConfig(
        schemaVersion=1,
        subscriptionId=SUBSCRIPTION_ID,
        resourceGroup="hermes-a365-bots",
        botName="hermes-inbox-helper-bot",
        armResourceId=(
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.BotService/botServices/bot"
        ),
        msaAppId=BF_APP_ID,
        tenantId=TENANT_ID,
        messagingEndpoint="https://example.test/api/messages",
        channelsEnabled=["webchat", "directline"],
        createdAt="2026-05-18T12:30:00Z",
        resourceGroupManaged=False,
    )

    class _SecretRunner:
        def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
            return CommandResult(
                argv, 0, stdout=json.dumps({"properties": {"key": "DL_SECRET"}})
            )

    urls: list[str] = []

    def _fake_http_json(
        url: str,
        *,
        token: str,
        body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, dict[str, Any]]:
        urls.append(url)
        if url.endswith("/v3/directline/conversations"):
            return 200, {"conversationId": "abc?../x#y", "token": "conv-token"}
        return 200, {}

    monkeypatch.setattr(bot_service, "_http_json", _fake_http_json)
    result = directline_runtime_probe(cfg, _SecretRunner())

    assert result.status == "OK"
    # Second call is the activities POST carrying the conversation id.
    activities_url = urls[1]
    assert "abc%3F..%2Fx%23y" in activities_url
    after = activities_url.split("/conversations/", 1)[1]
    assert "?" not in after
    assert "#" not in after
    assert "../" not in after


def test_verify_errors_when_teams_terms_not_accepted(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams(accepted=False))

    report = verify_bot_service(sidecar, runner=runner)

    assert report.ok is False
    teams = next(result for result in report.results if result.name == "msteams_channel")
    assert teams.status == "ERROR"
    assert "acceptedTerms" in teams.detail


# ── #71: endpoint URL validation (HTTPS + localhost guard) ─────────────


def test_endpoint_https_happy_path_unchanged(tmp_path: Path) -> None:
    """The v0.7.5-validated HTTPS path normalizes exactly as before."""
    create = _inputs(tmp_path, endpoint="https://example.test")
    assert create.endpoint == "https://example.test/api/messages"
    update = BotServiceUpdateEndpointInputs(
        agent_name="x",
        url="https://new-tunnel.example/api/messages",
        sidecar_path=tmp_path / "s.json",
    )
    assert update.url == "https://new-tunnel.example/api/messages"


def test_endpoint_empty_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(BotServiceError, match="non-empty"):
        _inputs(tmp_path, endpoint="   ")


def test_endpoint_non_absolute_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(BotServiceError, match="absolute http"):
        _inputs(tmp_path, endpoint="example.test/api/messages")


def test_endpoint_refuses_plain_http_remote_host(tmp_path: Path) -> None:
    with pytest.raises(BotServiceError, match="HTTPS"):
        _inputs(tmp_path, endpoint="http://example.test")
    with pytest.raises(BotServiceError, match="HTTPS"):
        BotServiceUpdateEndpointInputs(
            agent_name="x", url="http://example.test", sidecar_path=tmp_path / "s.json"
        )


# ``[::1]`` is the IPv6 loopback in URL form — urlparse strips the brackets
# so the hostname compares equal to "::1"; exercise that branch explicitly.
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_endpoint_refuses_loopback_without_allow_local(tmp_path: Path, host: str) -> None:
    with pytest.raises(BotServiceError, match="--allow-local"):
        _inputs(tmp_path, endpoint=f"https://{host}:3978")
    with pytest.raises(BotServiceError, match="--allow-local"):
        BotServiceUpdateEndpointInputs(
            agent_name="x", url=f"https://{host}:3978", sidecar_path=tmp_path / "s.json"
        )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost:3978", "http://localhost:3978/api/messages"),
        ("127.0.0.1:3978", "http://127.0.0.1:3978/api/messages"),
        ("[::1]:3978", "http://[::1]:3978/api/messages"),
    ],
)
def test_endpoint_allow_local_permits_http_loopback(
    tmp_path: Path, host: str, expected: str
) -> None:
    create = _inputs(tmp_path, endpoint=f"http://{host}", allow_local=True)
    assert create.endpoint == expected
    update = BotServiceUpdateEndpointInputs(
        agent_name="x",
        url=f"http://{host}",
        sidecar_path=tmp_path / "s.json",
        allow_local=True,
    )
    assert update.url == expected


def test_allow_local_does_not_relax_https_for_remote_host(tmp_path: Path) -> None:
    """--allow-local only relaxes loopback — a remote http:// stays refused."""
    with pytest.raises(BotServiceError, match="HTTPS"):
        _inputs(tmp_path, endpoint="http://example.test", allow_local=True)


@pytest.mark.parametrize("sub", ["create", "update-endpoint"])
def test_allow_local_cli_flag_wiring(sub: str) -> None:
    url_flag = "--endpoint" if sub == "create" else "--url"
    common = [sub, "--agent-name", "x", url_flag, "https://h.example"]
    if sub == "create":
        common += ["--resource-group", "rg"]
    assert build_parser().parse_args(common).allow_local is False
    assert build_parser().parse_args([*common, "--allow-local"]).allow_local is True


@pytest.mark.parametrize("drift", ["arm", "app"])
def test_mutation_refuses_live_bot_identity_drift(tmp_path: Path, drift: str) -> None:
    sidecar = _write_sidecar(tmp_path)
    bot = FakeRunner._bot()
    if drift == "arm":
        bot["id"] = (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/hermes-a365-bots/"
            "providers/Microsoft.BotService/botServices/different-bot"
        )
    else:
        bot["properties"]["msaAppId"] = "99999999-9999-9999-9999-999999999999"
    runner = FakeRunner(bot=bot, teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://new-tunnel.example",
            sidecar_path=sidecar,
        )
    )

    with pytest.raises(BotServiceError, match="does not match"):
        apply_update_endpoint_plan(plan, runner=runner)

    assert not any(call[:3] == ["az", "bot", "update"] for call in runner.calls)


def test_mutation_refuses_live_bot_without_arm_resource_id(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    bot = FakeRunner._bot()
    bot.pop("id")
    runner = FakeRunner(bot=bot, teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://new-tunnel.example",
            sidecar_path=sidecar,
        )
    )

    with pytest.raises(BotServiceError, match="omitted its ARM resource id"):
        apply_update_endpoint_plan(plan, runner=runner)

    assert not any(call[:3] == ["az", "bot", "update"] for call in runner.calls)


def test_cleanup_never_purges_group_when_bound_bot_is_missing(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path, resource_group_managed=True)
    runner = FakeRunner(bot=None, teams=None, group_resources=[])
    plan = build_cleanup_plan(
        BotServiceCleanupInputs(
            agent_name="Hermes Inbox Helper",
            sidecar_path=sidecar,
            purge_resource_group=True,
        )
    )

    result = apply_cleanup_plan(plan, runner=runner)

    assert result.resource_group_deleted is False
    assert not any(call[:3] == ["az", "group", "delete"] for call in runner.calls)
    assert result.resource_group_purge_pending is True
    assert sidecar.exists()
    assert any("review and run manually" in message for message in result.messages)
