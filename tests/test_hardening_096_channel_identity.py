"""Release hardening — inbound channel admission and sender identity binding."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_activity_bridge import _client_for, _inbound_message_activity
from test_agent365_plugin import (
    _make_inbound,
    _StubPlatformConfig,
    adapter_mod,
)

from hermes_a365.activity_bridge import BF_ISSUER, load_bridge_config

_VICTIM_AAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_VICTIM_BF_ID = "29:spoofed-victim"
_TENANT = "11111111-1111-1111-1111-111111111111"


def _write_allowlist_profile(home: Path, *, users: str) -> None:
    path = home / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"A365_ALLOWED_USERS={users}\n")
    path.chmod(0o600)


def _client_channel_activity(
    channel_id: str,
    *,
    service_url: str = "https://directline.botframework.com/",
) -> dict[str, Any]:
    activity = _inbound_message_activity()
    activity["channelId"] = channel_id
    activity["serviceUrl"] = service_url
    activity["from"] = {
        "id": _VICTIM_BF_ID,
        "aadObjectId": _VICTIM_AAD,
        "name": "Attacker",
    }
    activity["recipient"] = {"id": "bot-1", "name": "Bot"}
    activity["conversation"] = {"id": "conv-dl", "conversationType": "personal"}
    return activity


def _bridge_allowlist_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import types

    home = tmp_path / "hermes"
    _write_allowlist_profile(home, users=_VICTIM_AAD)
    agent_dir = home / "agents" / "inbox-helper"
    agent_dir.mkdir(parents=True)
    env_path = agent_dir / ".env"
    env_path.write_text(f"A365_TENANT_ID={_TENANT}\n")
    env_path.chmod(0o600)
    generated = home / "a365.generated.config.json"
    generated.write_text(
        '{"agentBlueprintId":"blueprint-app-id","agentBlueprintClientSecret":"sek"}\n'
    )
    generated.chmod(0o600)
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda bound=home: bound
    monkeypatch.setitem(__import__("sys").modules, "hermes_constants", hermes_constants)
    monkeypatch.setenv("A365_TENANT_ID", _TENANT)
    monkeypatch.setenv("A365_APP_ID", "blueprint-app-id")
    monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", "sek")
    monkeypatch.setenv("A365_ALLOWED_USERS", _VICTIM_AAD)
    monkeypatch.delenv("A365_ALLOW_ALL_USERS", raising=False)
    return load_bridge_config(
        slug="inbox-helper",
        webhook_url="https://hook.test/responder",
        hermes_home=home,
        generated_config_path=generated,
        skip_jwt_validation=True,
    )


def _plugin_allowlist_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, allow_client: bool = False
) -> Any:
    import types

    home = tmp_path / "hermes"
    _write_allowlist_profile(home, users=_VICTIM_AAD)
    if allow_client:
        (home / ".env").write_text(
            f"A365_ALLOWED_USERS={_VICTIM_AAD}\nA365_ALLOW_CLIENT_BF_CHANNELS=true\n"
        )
        (home / ".env").chmod(0o600)
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda bound=home: bound
    monkeypatch.setitem(__import__("sys").modules, "hermes_constants", hermes_constants)
    monkeypatch.delenv("A365_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("A365_ALLOWED_USERS", _VICTIM_AAD)
    if allow_client:
        monkeypatch.setenv("A365_ALLOW_CLIENT_BF_CHANNELS", "true")
    else:
        monkeypatch.delenv("A365_ALLOW_CLIENT_BF_CHANNELS", raising=False)
    monkeypatch.setenv("A365_TENANT_ID", _TENANT)
    monkeypatch.setenv("A365_APP_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", "fake-secret")
    extra: dict[str, Any] = {
        "slug": "test-agent",
        "port": 0,
        "conversations_path": str(tmp_path / "conversations.json"),
    }
    if allow_client:
        extra["allow_client_bf_channels"] = True
    return adapter_mod.Agent365Adapter(_StubPlatformConfig(extra=extra))


@pytest.mark.parametrize("channel_id", ["directline", "webchat"])
class TestClientChannelRejectedBothRuntimes:
    def test_bridge_rejects_before_webhook(
        self,
        channel_id: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _bridge_allowlist_cfg(tmp_path, monkeypatch)
        capture: dict[str, list[Any]] = {"webhook": [], "reply": [], "token": []}
        activity = _client_channel_activity(channel_id)
        with _client_for(cfg, capture=capture) as client:
            response = client.post("/api/messages", json=activity)
        assert response.status_code == 403
        assert response.json()["detail"] == "end user is not authorized"
        assert capture["webhook"] == []
        assert capture["token"] == []

    def test_plugin_rejects_before_dispatch_and_persist(
        self,
        channel_id: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        adapter = _plugin_allowlist_adapter(monkeypatch, tmp_path)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt_bf",
            AsyncMock(return_value={"aud": "x", "iss": BF_ISSUER}),
        )
        monkeypatch.setattr(bridge, "peek_unverified_iss", lambda _t: BF_ISSUER)
        persist = AsyncMock(return_value=None)
        monkeypatch.setattr(adapter, "_persist_conversations", persist)
        client = TestClient(adapter.build_app())
        activity = _client_channel_activity(channel_id)
        response = client.post(
            "/api/messages",
            json=activity,
            headers={"Authorization": "Bearer a.b.c"},
        )
        assert response.status_code == 403
        assert adapter._handled_events == []
        persist.assert_not_called()


class TestMsteamsRegression:
    def test_bridge_admits_listed_m365_sender(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _bridge_allowlist_cfg(tmp_path, monkeypatch)
        capture: dict[str, list[Any]] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity()
        activity["from"] = {"id": "29:user", "aadObjectId": _VICTIM_AAD}
        with _client_for(cfg, capture=capture) as client:
            response = client.post("/api/messages", json=activity)
        assert response.status_code == 200
        assert len(capture["webhook"]) == 1

    def test_plugin_admits_listed_m365_sender(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = _plugin_allowlist_adapter(monkeypatch, tmp_path)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z", "oid": _VICTIM_AAD}),
        )
        client = TestClient(adapter.build_app())
        body = _make_inbound(path="A")
        body["from"] = {"id": "29:user", "aadObjectId": _VICTIM_AAD}
        response = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )
        assert response.status_code == 200
        assert len(adapter._handled_events) == 1


class TestEnabledClientChannelBodyAliasesStillRejected:
    @pytest.mark.parametrize("channel_id", ["directline", "webchat"])
    def test_bridge(self, channel_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _bridge_allowlist_cfg(tmp_path, monkeypatch)
        cfg.allow_client_bf_channels = True
        capture: dict[str, list[Any]] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            response = client.post("/api/messages", json=_client_channel_activity(channel_id))
        assert response.status_code == 403
        assert capture["webhook"] == []

    def test_plugin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _plugin_allowlist_adapter(monkeypatch, tmp_path, allow_client=True)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt_bf",
            AsyncMock(return_value={"aud": "x", "iss": BF_ISSUER}),
        )
        monkeypatch.setattr(bridge, "peek_unverified_iss", lambda _t: BF_ISSUER)
        client = TestClient(adapter.build_app())
        response = client.post(
            "/api/messages",
            json=_client_channel_activity("directline"),
            headers={"Authorization": "Bearer a.b.c"},
        )
        assert response.status_code == 403
        assert adapter._handled_events == []


class TestAppOnlyTokenOidNotEndUser:
    def test_bridge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import hermes_a365.activity_bridge as bridge

        cfg = _bridge_allowlist_cfg(tmp_path, monkeypatch)
        cfg.skip_jwt_validation = False
        capture: dict[str, list[Any]] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity()
        activity["from"] = {"id": "29:unlisted-user"}
        monkeypatch.setattr(bridge, "peek_unverified_iss", lambda _t: None)
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(
                return_value={
                    "aud": "x",
                    "iss": "y",
                    "azp": "z",
                    "oid": _VICTIM_AAD,
                    "idtyp": "app",
                }
            ),
        )
        with _client_for(cfg, capture=capture) as client:
            response = client.post(
                "/api/messages",
                json=activity,
                headers={"Authorization": "Bearer a.b.c"},
            )
        assert response.status_code == 403
        assert capture["webhook"] == []

    def test_plugin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _plugin_allowlist_adapter(monkeypatch, tmp_path)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(
                return_value={
                    "aud": "x",
                    "iss": "y",
                    "azp": "z",
                    "oid": _VICTIM_AAD,
                    "idtyp": "app",
                }
            ),
        )
        client = TestClient(adapter.build_app())
        body = _make_inbound(path="A")
        body["from"] = {"id": "29:unlisted-user"}
        response = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )
        assert response.status_code == 403
        assert adapter._handled_events == []
