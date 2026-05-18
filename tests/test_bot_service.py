"""Tests for hermes_a365.bot_service — Path B Azure Bot Service wrapper."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hermes_a365.bot_service import (
    BotServiceConfig,
    BotServiceCreateInputs,
    BotServiceError,
    CommandResult,
    ProbeResult,
    apply_create_plan,
    build_create_plan,
    derive_bot_name,
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
        provider_state: str = "Registered",
    ) -> None:
        self.bot = bot
        self.teams = teams
        self.provider_state = provider_state
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        self.calls.append(list(argv))
        if argv[:3] == ["az", "account", "show"]:
            return self._ok({"id": SUBSCRIPTION_ID, "tenantId": TENANT_ID}, argv)
        if argv[:3] == ["az", "provider", "register"]:
            self.provider_state = "Registered"
            return self._ok({}, argv)
        if argv[:3] == ["az", "provider", "show"]:
            return CommandResult(argv, 0, stdout=self.provider_state)
        if argv[:3] == ["az", "group", "create"]:
            return self._ok({"name": self._arg(argv, "--name")}, argv)
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


def test_create_apply_writes_0600_sidecar_and_enables_teams(tmp_path: Path) -> None:
    runner = FakeRunner()
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

    with pytest.raises(BotServiceError, match="cannot change --appid"):
        apply_create_plan(
            plan,
            runner=runner,
            operator_env={"A365_BF_APP_ID": BF_APP_ID, "A365_TENANT_ID": TENANT_ID},
            now=_now,
        )

    assert not (tmp_path / "a365.bot-service.config.json").exists()


def test_create_requires_path_b_app_id(tmp_path: Path) -> None:
    runner = FakeRunner()
    plan = build_create_plan(_inputs(tmp_path), operator_env={})

    with pytest.raises(BotServiceError, match="separate non-agentic BF app id"):
        apply_create_plan(plan, runner=runner, operator_env={}, now=_now)


def test_verify_missing_sidecar_fails_cleanly(tmp_path: Path) -> None:
    with pytest.raises(BotServiceError, match="bot-service create --apply"):
        verify_bot_service(tmp_path / "a365.bot-service.config.json", runner=FakeRunner())


def _write_sidecar(tmp_path: Path) -> Path:
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
    assert statuses["runtime_auth"] == "WARN"


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


def test_verify_errors_when_teams_terms_not_accepted(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams(accepted=False))

    report = verify_bot_service(sidecar, runner=runner)

    assert report.ok is False
    teams = next(result for result in report.results if result.name == "msteams_channel")
    assert teams.status == "ERROR"
    assert "acceptedTerms" in teams.detail
