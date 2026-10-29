"""Regression tests for 0.9.6.1 hardening follow-ups."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_activity_bridge import _cfg, _client_for, _inbound_message_activity
from test_bot_service import SUBSCRIPTION_ID, TENANT_ID, FakeRunner

from hermes_a365._common import SidecarBackupError, write_owner_only_bytes_exclusive
from hermes_a365.activity_bridge import (
    ReplyPostMayHaveLandedError,
    _is_trusted_service_url,
    _reply_failure_forgets_idempotency,
)
from hermes_a365.bot_service import (
    BotServiceCreateInputs,
    BotServiceError,
    apply_create_plan,
    build_create_plan,
)
from hermes_a365.mutator import _ANSI_ESCAPE_RE, _strip_ansi
from hermes_a365.plugin.conversations import ConversationRegistry
from hermes_a365.sidecar_schema import SidecarSchemaError, validate_bot_service_sidecar_schema


class TestTrustedServiceUrlTyping:
    @pytest.mark.parametrize("value", [{}, ["https://smba.trafficmanager.net"], 1])
    def test_non_string_service_url_rejected(self, value: Any) -> None:
        assert _is_trusted_service_url(value, (".botframework.com",)) is False

    def test_serve_non_string_service_url_returns_403(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity()
        activity["serviceUrl"] = {}
        with _client_for(cfg, capture=capture) as client:
            r = client.post("/api/messages", json=activity)
        assert r.status_code == 403
        assert "Traceback" not in r.text


class TestServeOpenApiHidden:
    def test_docs_and_openapi_hidden(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            assert client.get("/docs").status_code == 404
            assert client.get("/redoc").status_code == 404
            assert client.get("/openapi.json").status_code == 404
            assert client.get("/healthz").json() == {"ok": True}


class TestReplyIdempotencyContract:
    def test_post_send_timeout_does_not_forget(self) -> None:
        assert _reply_failure_forgets_idempotency(ReplyPostMayHaveLandedError("x")) is False

    def test_pre_send_error_forgets(self) -> None:
        assert _reply_failure_forgets_idempotency(RuntimeError("mint failed")) is True


class TestAnsiRegexTidy:
    def test_csi_sequences_stripped_identically(self) -> None:
        for sample in ("\x1b[31mred\x1b[0m", "\x1b[2Jclear", "\x1b[?25lhide"):
            assert _strip_ansi(sample) == _ANSI_ESCAPE_RE.sub("", sample)


class TestBotServiceSidecarSchemaMessages:
    def test_schema_version_sentinel_not_in_error(self) -> None:
        sentinel = "SENTINEL-NOT-A-SECRET-1"
        with pytest.raises(SidecarSchemaError) as exc:
            validate_bot_service_sidecar_schema({"schemaVersion": sentinel}, source="cfg")
        assert sentinel not in str(exc.value)


class TestBotServiceCreateExistingSidecar:
    def test_unsupported_schema_refuses_before_mutations(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "a365.bot-service.config.json"
        original = json.dumps(
            {
                "schemaVersion": 99,
                "subscriptionId": SUBSCRIPTION_ID,
                "resourceGroup": "rg",
                "botName": "bot",
                "armResourceId": (
                    "/subscriptions/x/resourceGroups/rg/providers/"
                    "Microsoft.BotService/botServices/bot"
                ),
                "msaAppId": "00000000-0000-0000-0000-000000000001",
                "tenantId": TENANT_ID,
                "messagingEndpoint": "https://example.test/api/messages",
                "channelsEnabled": ["msteams"],
                "createdAt": "2020-01-01T00:00:00Z",
                "resourceGroupManaged": False,
                "unknown": "keep",
            }
        ).encode()
        sidecar.write_bytes(original)
        os.chmod(sidecar, 0o600)
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError):
            apply_create_plan(plan, runner=runner, operator_env=env)
        assert sidecar.read_bytes() == original
        assert not any(c[:3] == ["az", "group", "create"] for c in runner.calls)


class TestConversationsInvalidUtf8:
    def test_load_empty_and_save_quarantines(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "conversations.json"
        sentinel = b"\xff\xfeSENTINEL-NOT-A-SECRET-1"
        path.write_bytes(sentinel)
        with caplog.at_level("WARNING"):
            reg = ConversationRegistry.load(path)
        assert reg.to_payload()["conversations"] == []
        assert "SENTINEL" not in caplog.text
        ConversationRegistry.write_payload(path, {"schemaVersion": 1, "conversations": {}})
        quarantine = next(p for p in tmp_path.iterdir() if ".corrupt-" in p.name)
        assert quarantine.read_bytes() == sentinel
        assert oct(quarantine.stat().st_mode & 0o777) == "0o600"


class TestBackupOSErrorNormalized:
    def test_open_eacces_becomes_sidecar_backup_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "sidecar.json.backup-test"

        def fail_open(*args: Any, **kwargs: Any) -> int:
            raise OSError("denied")

        monkeypatch.setattr(os, "open", fail_open)
        with pytest.raises(SidecarBackupError):
            write_owner_only_bytes_exclusive(target, b"x")
