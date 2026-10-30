"""PR #277 r1 — delegated-user JWT evidence and handoff identity binding."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_activity_bridge import _client_for, _inbound_message_activity
from test_agent365_plugin import (
    _HANDOFF_TENANT,
    _HANDOFF_USER,
    _make_adapter,
    _make_inbound,
    _StubPlatformConfig,
    adapter_mod,
)
from test_hardening_096_channel_identity import (
    _TENANT,
    _VICTIM_AAD,
    _bridge_allowlist_cfg,
    _plugin_allowlist_adapter,
)

from hermes_a365 import invoke as invoke_mod
from hermes_a365.invoke import jwt_delegated_user_oid

_V2_APP_ONLY = {
    "oid": _VICTIM_AAD,
    "roles": ["SomeRole"],
    "ver": "2.0",
    "tid": _TENANT,
}
_V1_APP_ONLY = {
    "appid": "app-guid",
    "roles": ["SomeRole"],
    "ver": "1.0",
    "oid": _VICTIM_AAD,
}


class TestJwtDelegatedUserHelper:
    def test_v2_oid_roles_without_scp_or_idtyp_is_not_delegated(self) -> None:
        assert jwt_delegated_user_oid(_V2_APP_ONLY) == ""

    def test_v1_appid_roles_without_scp_is_not_delegated(self) -> None:
        assert jwt_delegated_user_oid(_V1_APP_ONLY) == ""

    def test_scp_makes_oid_available(self) -> None:
        claims = {**_V2_APP_ONLY, "scp": "User.Read"}
        assert jwt_delegated_user_oid(claims) == _VICTIM_AAD


class TestV2AppOnlyNotAllowlistedBothRuntimes:
    def test_bridge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import hermes_a365.activity_bridge as bridge

        cfg = _bridge_allowlist_cfg(tmp_path, monkeypatch)
        cfg.skip_jwt_validation = False
        capture: dict[str, list[Any]] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity()
        activity["from"] = {"id": "29:other"}
        monkeypatch.setattr(bridge, "peek_unverified_iss", lambda _t: None)
        monkeypatch.setattr(bridge, "validate_inbound_jwt", AsyncMock(return_value=_V2_APP_ONLY))
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
        monkeypatch.setattr(bridge, "validate_inbound_jwt", AsyncMock(return_value=_V2_APP_ONLY))
        client = TestClient(adapter.build_app())
        body = _make_inbound(path="A")
        body["from"] = {"id": "29:other"}
        response = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )
        assert response.status_code == 403
        assert adapter._handled_events == []


class TestHandoffAppOnlyOidRejectedOnMsteams:
    @pytest.mark.asyncio
    async def test_handoff_identity_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = adapter_mod.Agent365Adapter(
            _StubPlatformConfig(extra={"tenant_id": _HANDOFF_TENANT})
        )
        record = {
            "tenant_id": _HANDOFF_TENANT,
            "user_aad": _HANDOFF_USER,
        }
        ctx = invoke_mod.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "channelId": "msteams",
                "serviceUrl": "https://smba.trafficmanager.net/amer/x/",
                "value": {"continuation": "tok"},
                "conversation": {"id": "conv-1", "tenantId": _HANDOFF_TENANT},
                "from": {"id": _HANDOFF_USER},
            },
            claims={"oid": _HANDOFF_USER, "tid": _HANDOFF_TENANT, "idtyp": "app"},
            path_tag="B",
        )
        assert ctx.token_user_oid == ""
        assert adapter._handoff_identity_matches(record, ctx) is False


class TestDirectLineHandoffNoBodyFallback:
    @pytest.mark.asyncio
    async def test_body_aadObjectId_does_not_satisfy_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = _plugin_allowlist_adapter(monkeypatch, tmp_path, allow_client=True)
        adapter._allow_all_users = True
        record = {
            "tenant_id": _HANDOFF_TENANT,
            "user_aad": _HANDOFF_USER,
        }
        ctx = invoke_mod.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "channelId": "directline",
                "serviceUrl": "https://directline.botframework.com/",
                "value": {"continuation": "tok"},
                "conversation": {"id": "conv-dl", "tenantId": _HANDOFF_TENANT},
                "from": {"id": "dl-user", "aadObjectId": _HANDOFF_USER},
            },
            claims={"tid": _HANDOFF_TENANT},
            path_tag="B",
        )
        assert adapter._handoff_identity_matches(record, ctx) is False


class TestDelegatedUserHandoffStillWorks:
    @pytest.mark.asyncio
    async def test_msteams_handoff_with_scp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from test_agent365_plugin import (
            _bind_origin_db,
            _seed_copilot_origin,
            _settle_handoff,
        )
        from test_hardening_0961_part1 import _FakeOriginDb

        adapter = _make_adapter(monkeypatch)
        _seed_copilot_origin(adapter)
        row = {
            "id": "sess-cc",
            "source": "agent365",
            "chat_id": "conv-cc",
            "user_id": _HANDOFF_USER,
            "session_key": f"agent:main:agent365:group:conv-cc:{_HANDOFF_USER}",
            "ended_at": None,
            "end_reason": None,
        }
        _bind_origin_db(adapter, _FakeOriginDb({"sess-cc": row}))
        link = adapter._mint_handoff_link("conv-cc", reason="test")
        assert link is not None
        token = link.split("continuation=")[1]
        ctx = invoke_mod.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "channelId": "msteams",
                "serviceUrl": "https://smba.trafficmanager.net/amer/x/",
                "value": {"continuation": token},
                "conversation": {
                    "id": "conv-teams",
                    "conversationType": "personal",
                    "tenantId": _HANDOFF_TENANT,
                },
                "from": {"id": "29:user", "aadObjectId": _HANDOFF_USER},
            },
            claims={
                "oid": _HANDOFF_USER,
                "tid": _HANDOFF_TENANT,
                "scp": "User.Read",
            },
            path_tag="B",
        )
        assert ctx.token_user_oid == _HANDOFF_USER
        response = await adapter._handle_handoff_action(ctx)
        assert response.status == 200
        await _settle_handoff(adapter)
        assert len(adapter._handled_events) == 1
