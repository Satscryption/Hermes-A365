"""0.9.6.1 hardening part 1 — plugin/auth surface regressions."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_activity_bridge import (
    BF_TEST_SERVICE_URL,
    TEST_APX_AZP,
    TEST_TENANT_ID,
    _bf_jwks_transport,
    _cfg,
    _client_for,
    _inbound_message_activity,
    _jwks_transport,
    _make_token,
)
from test_agent365_plugin import (
    _HANDOFF_OTHER_USER,
    _HANDOFF_TENANT,
    _HANDOFF_USER,
    _bind_origin_db,
    _FakeOriginDb,
    _handoff_ctx,
    _make_adapter,
    _make_inbound,
    _seed_copilot_origin,
    _StubPlatformConfig,
)

from hermes_a365 import activity_bridge
from hermes_a365 import invoke as invoke_mod
from hermes_a365.activity_bridge import (
    BF_ISSUER,
    JwtValidationError,
    _JwksCache,
    make_app,
    validate_inbound_jwt,
    validate_inbound_jwt_bf,
)
from hermes_a365.plugin import adapter as adapter_mod


class TestServeOpenApiHidden:
    @pytest.mark.parametrize("kwarg", ["docs_url", "redoc_url", "openapi_url"])
    def test_bridge_fastapi_ctor_passes_none(
        self, kwarg: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, Any] = {}

        class RecordingFastAPI:
            def __init__(self, **kwargs: Any) -> None:
                recorded.update(kwargs)

            def add_middleware(self, *args: Any, **kwargs: Any) -> None:
                pass

            def get(self, *args: Any, **kwargs: Any) -> Any:
                def deco(fn: Any) -> Any:
                    return fn

                return deco

            def post(self, *args: Any, **kwargs: Any) -> Any:
                def deco(fn: Any) -> Any:
                    return fn

                return deco

        monkeypatch.setattr(activity_bridge, "_FastAPI", RecordingFastAPI)
        cfg = _cfg()
        make_app(cfg, http_client=httpx.AsyncClient())
        assert kwarg in recorded
        assert recorded[kwarg] is None

    @pytest.mark.parametrize("route", ["/docs", "/redoc", "/openapi.json"])
    def test_bridge_each_openapi_route_hidden(self, route: str) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            assert client.get(route).status_code == 404

    def test_bridge_healthz_ok_shape(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            assert client.get("/healthz").json() == {"ok": True}

    @pytest.mark.parametrize("kwarg", ["docs_url", "redoc_url", "openapi_url"])
    def test_plugin_fastapi_ctor_passes_none(
        self, kwarg: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, Any] = {}

        class RecordingFastAPI:
            def __init__(self, **kwargs: Any) -> None:
                recorded.update(kwargs)

            def add_middleware(self, *args: Any, **kwargs: Any) -> None:
                pass

            def get(self, *args: Any, **kwargs: Any) -> Any:
                def deco(fn: Any) -> Any:
                    return fn

                return deco

            def post(self, *args: Any, **kwargs: Any) -> Any:
                def deco(fn: Any) -> Any:
                    return fn

                return deco

        import fastapi

        monkeypatch.setattr(fastapi, "FastAPI", RecordingFastAPI)
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig())
        adapter.build_app()
        assert kwarg in recorded
        assert recorded[kwarg] is None

    @pytest.mark.parametrize("route", ["/docs", "/redoc", "/openapi.json"])
    def test_plugin_each_openapi_route_hidden(self, route: str) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig())
        app = adapter.build_app()
        with TestClient(app) as client:
            assert client.get(route).status_code == 404

    def test_plugin_healthz_ok_shape(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig())
        app = adapter.build_app()
        with TestClient(app) as client:
            assert client.get("/healthz").json() == {"ok": True}


class TestJwtRequiresExpAndNbf:
    def _complete_token(
        self,
        rsa_keypair: tuple[Any, dict[str, Any]],
        *,
        aud: str,
    ) -> tuple[str, dict[str, Any]]:
        priv, jwk = rsa_keypair
        return _make_token(priv, aud=aud, azp=TEST_APX_AZP), jwk

    def _token_missing_claim(
        self,
        rsa_keypair: tuple[Any, dict[str, Any]],
        *,
        aud: str,
        drop: str,
    ) -> tuple[str, dict[str, Any]]:
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization

        priv, jwk = rsa_keypair
        token = _make_token(priv, aud=aud, azp=TEST_APX_AZP)
        header = pyjwt.get_unverified_header(token)
        payload = pyjwt.decode(token, options={"verify_signature": False})
        payload.pop(drop, None)
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return (
            pyjwt.encode(payload, pem, algorithm="RS256", headers=header),
            jwk,
        )

    @pytest.mark.asyncio
    async def test_validate_inbound_jwt_accepts_complete_token(
        self, rsa_keypair: tuple[Any, dict[str, Any]]
    ) -> None:
        _, jwk = rsa_keypair
        token, _ = self._complete_token(rsa_keypair, aud="bot-app-id")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            claims = await validate_inbound_jwt(
                token=token,
                tenant_id=TEST_TENANT_ID,
                expected_app_id="bot-app-id",
                azp_allowlist=(TEST_APX_AZP,),
                client=client,
                cache=_JwksCache(),
            )
        assert claims["exp"]
        assert claims["nbf"]

    @pytest.mark.asyncio
    async def test_validate_inbound_jwt_requires_exp(
        self, rsa_keypair: tuple[Any, dict[str, Any]]
    ) -> None:
        _, jwk = rsa_keypair
        token, _ = self._token_missing_claim(rsa_keypair, aud="bot-app-id", drop="exp")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="exp"):
                await validate_inbound_jwt(
                    token=token,
                    tenant_id=TEST_TENANT_ID,
                    expected_app_id="bot-app-id",
                    azp_allowlist=(TEST_APX_AZP,),
                    client=client,
                    cache=_JwksCache(),
                )

    @pytest.mark.asyncio
    async def test_validate_inbound_jwt_requires_nbf(
        self, rsa_keypair: tuple[Any, dict[str, Any]]
    ) -> None:
        _, jwk = rsa_keypair
        token, _ = self._token_missing_claim(rsa_keypair, aud="bot-app-id", drop="nbf")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="nbf"):
                await validate_inbound_jwt(
                    token=token,
                    tenant_id=TEST_TENANT_ID,
                    expected_app_id="bot-app-id",
                    azp_allowlist=(TEST_APX_AZP,),
                    client=client,
                    cache=_JwksCache(),
                )

    @pytest.mark.asyncio
    async def test_validate_inbound_jwt_bf_accepts_complete_token(
        self, rsa_keypair: tuple[Any, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(
            priv,
            aud="bot-app-id",
            iss=BF_ISSUER,
            extra={"serviceUrl": BF_TEST_SERVICE_URL},
        )
        async with httpx.AsyncClient(transport=_bf_jwks_transport(jwk)) as client:
            claims = await validate_inbound_jwt_bf(
                token=token,
                expected_app_id="bot-app-id",
                expected_service_url=BF_TEST_SERVICE_URL,
                client=client,
                cache=_JwksCache(),
            )
        assert claims["exp"]
        assert claims["nbf"]

    @pytest.mark.asyncio
    async def test_validate_inbound_jwt_bf_requires_exp(
        self, rsa_keypair: tuple[Any, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(
            priv,
            aud="bot-app-id",
            iss=BF_ISSUER,
            extra={"serviceUrl": BF_TEST_SERVICE_URL},
        )
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization

        header = pyjwt.get_unverified_header(token)
        payload = pyjwt.decode(token, options={"verify_signature": False})
        payload.pop("exp", None)
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        token = pyjwt.encode(payload, pem, algorithm="RS256", headers=header)
        async with httpx.AsyncClient(transport=_bf_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="exp"):
                await validate_inbound_jwt_bf(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url=BF_TEST_SERVICE_URL,
                    client=client,
                    cache=_JwksCache(),
                )

    @pytest.mark.asyncio
    async def test_validate_inbound_jwt_bf_requires_nbf(
        self, rsa_keypair: tuple[Any, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(
            priv,
            aud="bot-app-id",
            iss=BF_ISSUER,
            extra={"serviceUrl": BF_TEST_SERVICE_URL},
        )
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization

        header = pyjwt.get_unverified_header(token)
        payload = pyjwt.decode(token, options={"verify_signature": False})
        payload.pop("nbf", None)
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        token = pyjwt.encode(payload, pem, algorithm="RS256", headers=header)
        async with httpx.AsyncClient(transport=_bf_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="nbf"):
                await validate_inbound_jwt_bf(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url=BF_TEST_SERVICE_URL,
                    client=client,
                    cache=_JwksCache(),
                )

    def _serve_cfg(self) -> Any:
        cfg = _cfg()
        cfg.tenant_id = TEST_TENANT_ID
        return cfg

    def _serve_activity(self) -> dict[str, Any]:
        activity = _inbound_message_activity()
        activity["conversation"]["tenantId"] = TEST_TENANT_ID
        activity["recipient"]["tenantId"] = TEST_TENANT_ID
        return activity

    def test_serve_accepts_complete_token(self, rsa_keypair: tuple[Any, dict[str, Any]]) -> None:
        cfg = self._serve_cfg()
        token, jwk = self._complete_token(rsa_keypair, aud=cfg.blueprint_client_id)
        cfg.skip_jwt_validation = False
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture, jwk=jwk) as client:
            r = client.post(
                "/api/messages",
                json=self._serve_activity(),
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        assert capture["webhook"]

    def test_serve_rejects_token_without_exp(self, rsa_keypair: tuple[Any, dict[str, Any]]) -> None:
        cfg = self._serve_cfg()
        token, jwk = self._token_missing_claim(rsa_keypair, aud=cfg.blueprint_client_id, drop="exp")
        cfg.skip_jwt_validation = False
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture, jwk=jwk) as client:
            r = client.post(
                "/api/messages",
                json=self._serve_activity(),
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 403
        assert capture["webhook"] == []

    def test_serve_rejects_token_without_nbf(self, rsa_keypair: tuple[Any, dict[str, Any]]) -> None:
        cfg = self._serve_cfg()
        token, jwk = self._token_missing_claim(rsa_keypair, aud=cfg.blueprint_client_id, drop="nbf")
        cfg.skip_jwt_validation = False
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture, jwk=jwk) as client:
            r = client.post(
                "/api/messages",
                json=self._serve_activity(),
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 403
        assert capture["webhook"] == []


class TestHandoffIdentityThroughHandler:
    def _plant_token(self, adapter: Any, *, user_aad: str = _HANDOFF_USER) -> str:
        token = "handoff-tok-test"
        adapter._handoff_tokens[token] = {
            "conversation_id": "conv-cc",
            "chat_type": "groupChat",
            "tenant_id": _HANDOFF_TENANT,
            "user_aad": user_aad,
            "hermes_session_id": "sess-cc",
            "reason": "test",
            "minted_at": time.time(),
        }
        return token

    def _ready(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        adapter = _make_adapter(monkeypatch)
        _seed_copilot_origin(adapter)
        _bind_origin_db(
            adapter,
            _FakeOriginDb(
                {
                    "sess-cc": {
                        "id": "sess-cc",
                        "source": "agent365",
                        "chat_id": "conv-cc",
                        "user_id": _HANDOFF_USER,
                        "session_key": f"agent:main:agent365:group:conv-cc:{_HANDOFF_USER}",
                        "ended_at": None,
                        "end_reason": None,
                    }
                }
            ),
        )
        return adapter

    @pytest.mark.asyncio
    async def test_matching_token_oid_pins_and_consumes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from test_agent365_plugin import _settle_handoff

        adapter = self._ready(monkeypatch)
        token = self._plant_token(adapter)
        response = await adapter._handle_handoff_action(_handoff_ctx(token))
        assert response.status == 200
        assert token not in adapter._handoff_tokens
        await _settle_handoff(adapter)
        assert len(adapter._handled_events) == 1

    @pytest.mark.asyncio
    async def test_mismatched_token_oid_keeps_continuation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from test_agent365_plugin import _settle_handoff

        adapter = self._ready(monkeypatch)
        token = self._plant_token(adapter)
        response = await adapter._handle_handoff_action(
            _handoff_ctx(token, claims_user=_HANDOFF_OTHER_USER)
        )
        assert response.status == 200
        assert token in adapter._handoff_tokens
        await _settle_handoff(adapter)
        assert adapter._handled_events == []

    @pytest.mark.asyncio
    async def test_absent_oid_with_matching_body_pins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from test_agent365_plugin import _settle_handoff

        adapter = self._ready(monkeypatch)
        token = self._plant_token(adapter)
        ctx = invoke_mod.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "channelId": "msteams",
                "id": "inv-1",
                "serviceUrl": "https://smba.trafficmanager.net/amer/x/",
                "value": {"continuation": token},
                "conversation": {
                    "id": "conv-teams",
                    "conversationType": "personal",
                    "tenantId": _HANDOFF_TENANT,
                },
                "from": {
                    "id": "29:user",
                    "aadObjectId": _HANDOFF_USER,
                },
            },
            claims={"tid": _HANDOFF_TENANT},
            path_tag="B",
        )
        response = await adapter._handle_handoff_action(ctx)
        assert response.status == 200
        assert token not in adapter._handoff_tokens
        await _settle_handoff(adapter)
        assert len(adapter._handled_events) == 1

    @pytest.mark.asyncio
    async def test_empty_oid_and_empty_body_keeps_continuation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from test_agent365_plugin import _settle_handoff

        adapter = self._ready(monkeypatch)
        token = self._plant_token(adapter)
        ctx = invoke_mod.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "value": {"continuation": token},
                "conversation": {"id": "conv-teams", "tenantId": _HANDOFF_TENANT},
                "from": {"id": "29:user"},
            },
            claims={"tid": _HANDOFF_TENANT},
            path_tag="B",
        )
        response = await adapter._handle_handoff_action(ctx)
        assert response.status == 200
        assert token in adapter._handoff_tokens
        await _settle_handoff(adapter)
        assert adapter._handled_events == []


class TestHandoffTokenUserOid:
    def _ctx(
        self,
        *,
        claims: dict[str, Any] | None,
        body_user: str,
    ) -> invoke_mod.InvokeContext:
        return invoke_mod.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "channelId": "msteams",
                "from": {"aadObjectId": body_user},
                "conversation": {"id": "conv-teams", "tenantId": "tid"},
                "value": {"continuation": "tok"},
            },
            claims=claims,
            path_tag="B",
        )

    def test_build_invoke_context_without_oid_leaves_token_user_oid_empty(self) -> None:
        ctx = self._ctx(claims={"tid": "tid"}, body_user="record-user")
        assert ctx.token_user_oid == ""
        assert ctx.user_oid == "record-user"

    def test_absent_oid_matches_record_when_body_names_user(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        record = {"tenant_id": "tid", "user_aad": "record-user"}
        ctx = self._ctx(claims={"tid": "tid"}, body_user="record-user")
        assert adapter._handoff_identity_matches(record, ctx) is True

    def test_empty_body_with_absent_oid_refused(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        record = {"tenant_id": "tid", "user_aad": "record-user"}
        ctx = self._ctx(claims={"tid": "tid"}, body_user="")
        assert adapter._handoff_identity_matches(record, ctx) is False

    def test_empty_oid_claim_does_not_match(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        record = {"tenant_id": "tid", "user_aad": "record-user"}
        ctx = self._ctx(claims={"tid": "tid", "oid": ""}, body_user="record-user")
        assert adapter._handoff_identity_matches(record, ctx) is True

    def test_mismatched_oid_refused(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        record = {"tenant_id": "tid", "user_aad": "record-user"}
        ctx = self._ctx(
            claims={"tid": "tid", "oid": "other-user", "idtyp": "user"},
            body_user="record-user",
        )
        assert adapter._handoff_identity_matches(record, ctx) is False

    def test_matching_oid_dispatches_identity(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        record = {"tenant_id": "tid", "user_aad": "record-user"}
        ctx = self._ctx(
            claims={"tid": "tid", "oid": "record-user", "idtyp": "user"},
            body_user="record-user",
        )
        assert adapter._handoff_identity_matches(record, ctx) is True


class TestHandoffActionContinuation:
    def _live_origin_row(self) -> dict[str, Any]:
        return {
            "id": "sess-cc",
            "source": "agent365",
            "chat_id": "conv-cc",
            "user_id": _HANDOFF_USER,
            "session_key": f"agent:main:agent365:group:conv-cc:{_HANDOFF_USER}",
            "ended_at": None,
            "end_reason": None,
        }

    @pytest.mark.asyncio
    async def test_connector_without_oid_pins_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from test_agent365_plugin import _settle_handoff

        adapter = _make_adapter(monkeypatch)
        _seed_copilot_origin(adapter)
        _bind_origin_db(adapter, _FakeOriginDb({"sess-cc": self._live_origin_row()}))
        link = adapter._mint_handoff_link("conv-cc", reason="test")
        assert link is not None
        token = link.split("continuation=")[1]
        ctx = invoke_mod.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "channelId": "msteams",
                "id": "inv-1",
                "serviceUrl": "https://smba.trafficmanager.net/amer/x/",
                "value": {"continuation": token},
                "conversation": {
                    "id": "conv-teams",
                    "conversationType": "personal",
                    "tenantId": _HANDOFF_TENANT,
                },
                "from": {
                    "id": "29:user",
                    "name": "Sadiq",
                    "aadObjectId": _HANDOFF_USER,
                },
            },
            claims={"tid": _HANDOFF_TENANT},
            path_tag="B",
        )
        response = await adapter._handle_handoff_action(ctx)
        assert response.status == 200
        assert token not in adapter._handoff_tokens
        await _settle_handoff(adapter)
        assert len(adapter._handled_events) == 1
        again = await adapter._handle_handoff_action(ctx)
        assert again.status == 200
        await _settle_handoff(adapter)
        assert len(adapter._handled_events) == 1

    @pytest.mark.asyncio
    async def test_mismatched_oid_keeps_continuation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from test_agent365_plugin import _settle_handoff

        adapter = _make_adapter(monkeypatch)
        _seed_copilot_origin(adapter)
        _bind_origin_db(adapter, _FakeOriginDb({"sess-cc": self._live_origin_row()}))
        link = adapter._mint_handoff_link("conv-cc", reason="test")
        assert link is not None
        token = link.split("continuation=")[1]
        response = await adapter._handle_handoff_action(
            _handoff_ctx(token, claims_user=_HANDOFF_OTHER_USER)
        )
        assert response.status == 200
        assert token in adapter._handoff_tokens
        await _settle_handoff(adapter)
        assert adapter._handled_events == []

    @pytest.mark.asyncio
    async def test_matching_oid_consumes_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from test_agent365_plugin import _settle_handoff

        adapter = _make_adapter(monkeypatch)
        _seed_copilot_origin(adapter)
        _bind_origin_db(adapter, _FakeOriginDb({"sess-cc": self._live_origin_row()}))
        link = adapter._mint_handoff_link("conv-cc", reason="test")
        assert link is not None
        token = link.split("continuation=")[1]
        response = await adapter._handle_handoff_action(_handoff_ctx(token))
        assert response.status == 200
        assert token not in adapter._handoff_tokens
        await _settle_handoff(adapter)
        assert len(adapter._handled_events) == 1
        again = await adapter._handle_handoff_action(_handoff_ctx(token))
        assert again.status == 200
        await _settle_handoff(adapter)
        assert len(adapter._handled_events) == 1


class TestFileConsentDeclineChecks:
    @pytest.mark.asyncio
    async def test_decline_missing_pending_dismisses_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        remove = AsyncMock()
        notice = AsyncMock()
        monkeypatch.setattr(adapter, "_remove_file_consent_card", remove)
        monkeypatch.setattr(adapter, "_send_file_consent_decline_notice", notice)
        activity = {
            "conversation": {"id": "conv-a"},
            "from": {"id": "user-a"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {"action": "decline", "context": {"consentId": "missing"}},
        }
        await adapter._handle_file_consent(activity, validated_path="A")
        await adapter._drain_file_consent_upload_tasks()
        remove.assert_awaited()
        notice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_decline_expired_pending_dismisses_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        remove = AsyncMock()
        notice = AsyncMock()
        monkeypatch.setattr(adapter, "_remove_file_consent_card", remove)
        monkeypatch.setattr(adapter, "_send_file_consent_decline_notice", notice)
        adapter._pending_file_uploads["cid"] = {
            "conversation_id": "conv-a",
            "user_id": "user-a",
            "service_url": "https://smba.trafficmanager.net/teams/",
            "lifecycle_generation": adapter._lifecycle_generation,
            "chat_generation": adapter._chat_generation("conv-a"),
            "created_at": time.time() - adapter_mod._PENDING_UPLOAD_TTL_SEC - 1.0,
        }
        activity = {
            "conversation": {"id": "conv-a"},
            "from": {"id": "user-a"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {"action": "decline", "context": {"consentId": "cid"}},
        }
        await adapter._handle_file_consent(activity, validated_path="A")
        await adapter._drain_file_consent_upload_tasks()
        assert "cid" in adapter._pending_file_uploads
        remove.assert_awaited()
        notice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_decline_live_mismatch_keeps_pending(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        adapter._pending_file_uploads["cid"] = {
            "conversation_id": "conv-a",
            "user_id": "user-a",
            "service_url": "https://smba.trafficmanager.net/teams/",
            "lifecycle_generation": adapter._lifecycle_generation,
            "chat_generation": adapter._chat_generation("conv-a"),
            "created_at": time.time(),
        }
        activity = {
            "conversation": {"id": "conv-b"},
            "from": {"id": "user-a"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {"action": "decline", "context": {"consentId": "cid"}},
        }
        await adapter._handle_file_consent(activity, validated_path="A")
        assert "cid" in adapter._pending_file_uploads

    @pytest.mark.asyncio
    async def test_valid_decline_consumes_pending_once(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        adapter._pending_file_uploads["cid"] = {
            "conversation_id": "conv-a",
            "user_id": "user-a",
            "service_url": "https://smba.trafficmanager.net/teams/",
            "lifecycle_generation": adapter._lifecycle_generation,
            "chat_generation": adapter._chat_generation("conv-a"),
            "created_at": time.time(),
        }
        activity = {
            "conversation": {"id": "conv-a"},
            "from": {"id": "user-a"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {"action": "decline", "context": {"consentId": "cid"}},
        }
        await adapter._handle_file_consent(activity, validated_path="A")
        assert "cid" not in adapter._pending_file_uploads
        await adapter._handle_file_consent(activity, validated_path="A")
        assert "cid" not in adapter._pending_file_uploads

    @pytest.mark.asyncio
    async def test_decline_wrong_user_leaves_card_notice_and_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        adapter._pending_file_uploads["cid"] = {
            "conversation_id": "conv-a",
            "user_id": "user-a",
            "service_url": "https://smba.trafficmanager.net/teams/",
            "lifecycle_generation": adapter._lifecycle_generation,
            "chat_generation": adapter._chat_generation("conv-a"),
            "created_at": time.time(),
        }
        remove = AsyncMock()
        notice = AsyncMock()
        monkeypatch.setattr(adapter, "_remove_file_consent_card", remove)
        monkeypatch.setattr(adapter, "_send_file_consent_decline_notice", notice)
        wrong = {
            "conversation": {"id": "conv-a"},
            "from": {"id": "user-b"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "replyToId": "card-activity-1",
            "value": {"action": "decline", "context": {"consentId": "cid"}},
        }
        await adapter._handle_file_consent(wrong, validated_path="A")
        await adapter._drain_file_consent_upload_tasks()
        assert "cid" in adapter._pending_file_uploads
        assert "cid" not in adapter._file_consent_decline_notified
        remove.assert_not_awaited()
        notice.assert_not_awaited()
        right = {
            "conversation": {"id": "conv-a"},
            "from": {"id": "user-a"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "replyToId": "card-activity-1",
            "value": {"action": "decline", "context": {"consentId": "cid"}},
        }
        await adapter._handle_file_consent(right, validated_path="A")
        await adapter._drain_file_consent_upload_tasks()
        assert "cid" not in adapter._pending_file_uploads
        assert "cid" in adapter._file_consent_decline_notified
        remove.assert_awaited()
        notice.assert_awaited()


class TestMintHandoffGuard:
    def test_mint_returns_none_without_aad_object_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-cc")
        adapter._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        assert adapter._mint_handoff_link("conv-cc", reason="test") is None

    def test_mint_returns_none_without_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-cc")
        inbound["from"]["aadObjectId"] = _HANDOFF_USER
        inbound["conversation"].pop("tenantId", None)
        inbound["recipient"].pop("tenantId", None)
        adapter._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        assert adapter._mint_handoff_link("conv-cc", reason="test") is None
