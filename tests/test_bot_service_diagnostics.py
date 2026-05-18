"""Tests for read-only Path B Bot Service diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_a365.bot_service import BotServiceConfig, CommandResult
from hermes_a365.bot_service_diagnostics import (
    DiagnosticResult,
    collect_bot_service_diagnostics,
)

BF_APP_ID = "11111111-1111-1111-1111-111111111111"
TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"


class FakeRunner:
    def __init__(
        self,
        *,
        bot_app_id: str = BF_APP_ID,
        endpoint: str = "https://example.test/api/messages",
        teams: dict[str, Any] | None = None,
        subscription_id: str = SUBSCRIPTION_ID,
    ) -> None:
        self.bot_app_id = bot_app_id
        self.endpoint = endpoint
        self.teams = teams or self._teams()
        self.subscription_id = subscription_id

    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        if argv[:3] == ["az", "account", "show"]:
            return self._ok(argv, {"id": self.subscription_id})
        if argv[:3] == ["az", "bot", "show"]:
            return self._ok(
                argv,
                {
                    "properties": {
                        "msaAppId": self.bot_app_id,
                        "endpoint": self.endpoint,
                    }
                },
            )
        if argv[:4] == ["az", "bot", "msteams", "show"]:
            if self.teams is None:
                return CommandResult(argv, 3, stderr="Channel not found")
            return self._ok(argv, self.teams)
        raise AssertionError(f"unexpected command: {argv}")

    @staticmethod
    def _teams(*, accepted: bool = True, enabled: bool = True) -> dict[str, Any]:
        return {
            "properties": {
                "properties": {
                    "acceptedTerms": accepted,
                    "isEnabled": enabled,
                }
            }
        }

    @staticmethod
    def _ok(argv: list[str], data: dict[str, Any]) -> CommandResult:
        return CommandResult(argv, 0, stdout=json.dumps(data))


def _write_sidecar(tmp_path: Path, *, app_id: str = BF_APP_ID) -> Path:
    sidecar = tmp_path / "a365.bot-service.config.json"
    sidecar.write_text(
        BotServiceConfig(
            schemaVersion=1,
            subscriptionId=SUBSCRIPTION_ID,
            resourceGroup="hermes-a365-bots",
            botName="hermes-inbox-helper-bot",
            armResourceId=(
                f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/hermes-a365-bots"
                "/providers/Microsoft.BotService/botServices/hermes-inbox-helper-bot"
            ),
            msaAppId=app_id,
            tenantId=TENANT_ID,
            messagingEndpoint="https://example.test/api/messages",
            channelsEnabled=["msteams"],
            createdAt="2026-05-18T12:30:00Z",
        ).to_json()
    )
    return sidecar


def _by_name(results: list[DiagnosticResult]) -> dict[str, DiagnosticResult]:
    return {result.name: result for result in results}


def test_absent_sidecar_skips_all_doctor_path_b_probes(tmp_path: Path) -> None:
    results = collect_bot_service_diagnostics(
        sidecar_path=tmp_path / "missing.json",
        generated_config_path=tmp_path / "a365.generated.config.json",
        no_network=True,
    )

    assert results == []


def test_partial_sidecar_reports_config_error(tmp_path: Path) -> None:
    sidecar = tmp_path / "a365.bot-service.config.json"
    sidecar.write_text(json.dumps({"schemaVersion": 1, "botName": "only-this"}))

    results = collect_bot_service_diagnostics(
        sidecar_path=sidecar,
        generated_config_path=tmp_path / "a365.generated.config.json",
        no_network=True,
    )

    assert results[0].name == "bot_service_config"
    assert results[0].state == "error"
    assert "missing required fields" in results[0].detail


def test_detects_msa_app_id_drift_from_generated_path_b_identity(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path, app_id="99999999-9999-9999-9999-999999999999")
    generated = tmp_path / "a365.generated.config.json"
    generated.write_text(json.dumps({"botMsaAppId": BF_APP_ID}))

    results = collect_bot_service_diagnostics(
        sidecar_path=sidecar,
        generated_config_path=generated,
        no_network=True,
    )

    drift = _by_name(results)["bot_service_msa_app_id"]
    assert drift.state == "error"
    assert "expected" in drift.detail
    assert drift.data["expected_source"] == "a365.generated.config.json::botMsaAppId"


def test_accepts_operator_env_bf_app_id_when_generated_config_lacks_bot_id(
    tmp_path: Path,
) -> None:
    sidecar = _write_sidecar(tmp_path)
    generated = tmp_path / "a365.generated.config.json"
    generated.write_text(json.dumps({"agentBlueprintId": "legacy-blueprint-id"}))

    results = collect_bot_service_diagnostics(
        sidecar_path=sidecar,
        generated_config_path=generated,
        no_network=True,
        operator_env={"A365_BF_APP_ID": BF_APP_ID},
    )

    app_id = _by_name(results)["bot_service_msa_app_id"]
    assert app_id.state == "ok"
    assert app_id.data["expected_source"] == "~/.hermes/.env A365_BF_APP_ID"


def test_channel_disabled_surfaces_warn(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)

    results = collect_bot_service_diagnostics(
        sidecar_path=sidecar,
        generated_config_path=tmp_path / "a365.generated.config.json",
        runner=FakeRunner(teams=FakeRunner._teams(accepted=False, enabled=False)),
        operator_env={"A365_BF_APP_ID": BF_APP_ID},
        runtime_auth_probe=lambda _config: DiagnosticResult(
            "bot_service_runtime_auth", "ok", "runtime ok"
        ),
    )

    channel = _by_name(results)["bot_service_channel_msteams"]
    assert channel.state == "warn"
    assert "acceptedTerms/isEnabled" in channel.detail


def test_runtime_auth_failure_points_to_standalone_bridge_requirement(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)

    results = collect_bot_service_diagnostics(
        sidecar_path=sidecar,
        generated_config_path=tmp_path / "a365.generated.config.json",
        runner=FakeRunner(),
        operator_env={"A365_BF_APP_ID": BF_APP_ID},
        runtime_auth_probe=lambda _config: DiagnosticResult(
            "bot_service_runtime_auth",
            "error",
            "standalone activity-bridge serve must dispatch Path B via BF-token auth",
        ),
    )

    runtime = _by_name(results)["bot_service_runtime_auth"]
    assert runtime.state == "error"
    assert "activity-bridge serve" in runtime.detail
    assert "BF-token" in runtime.detail
