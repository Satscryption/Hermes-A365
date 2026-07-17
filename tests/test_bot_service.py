"""Tests for hermes_a365.bot_service — Path B Azure Bot Service wrapper."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hermes_a365.bot_service import (
    BotServiceCleanupInputs,
    BotServiceConfig,
    BotServiceCreateInputs,
    BotServiceEnableChannelInputs,
    BotServiceError,
    BotServiceUpdateEndpointInputs,
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

BF_APP_ID = "11111111-1111-1111-1111-111111111111"
TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"


class FakeRunner:
    def __init__(
        self,
        *,
        bot: dict[str, Any] | None = None,
        teams: dict[str, Any] | None = None,
        group_exists: bool = True,
        provider_state: str = "Registered",
        default_location: str | None = None,
    ) -> None:
        self.bot = bot
        self.teams = teams
        self.group_exists = group_exists
        self.provider_state = provider_state
        self.default_location = default_location
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        self.calls.append(list(argv))
        if argv[:3] == ["az", "config", "get"]:
            if self.default_location is None:
                return CommandResult(argv, 1, stderr="defaults.location is not set")
            return CommandResult(argv, 0, stdout=f"{self.default_location}\n")
        if argv[:3] == ["az", "account", "show"]:
            return self._ok({"id": SUBSCRIPTION_ID, "tenantId": TENANT_ID}, argv)
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


def test_verify_missing_sidecar_fails_cleanly(tmp_path: Path) -> None:
    with pytest.raises(BotServiceError, match="bot-service create --apply"):
        verify_bot_service(tmp_path / "a365.bot-service.config.json", runner=FakeRunner())


def _write_sidecar(tmp_path: Path, *, resource_group_managed: bool = False) -> Path:
    path = tmp_path / "a365.bot-service.config.json"
    cfg = BotServiceConfig(
        schemaVersion=1,
        subscriptionId=SUBSCRIPTION_ID,
        resourceGroup="hermes-a365-bots",
        botName="hermes-inbox-helper-bot",
        armResourceId="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.BotService/botServices/bot",
        msaAppId=BF_APP_ID,
        tenantId=TENANT_ID,
        messagingEndpoint="https://example.test/api/messages",
        channelsEnabled=["webchat", "directline", "msteams"],
        createdAt="2026-05-18T12:30:00Z",
        resourceGroupManaged=resource_group_managed,
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
    assert result.sidecar_removed is True
    assert not sidecar.exists()
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
    assert result.sidecar_removed is True
    assert not sidecar.exists()
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

    assert result.resource_group_deleted is True
    assert any(call[:3] == ["az", "group", "delete"] for call in runner.calls)


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
