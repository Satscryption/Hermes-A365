"""0.9.6.1 hardening part 1 — plugin/auth surface regressions."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_activity_bridge import (
    TEST_APX_AZP,
    TEST_TENANT_ID,
    _cfg,
    _client_for,
    _inbound_message_activity,
    _jwks_transport,
    _make_token,
)
from test_agent365_plugin import _StubPlatformConfig

from hermes_a365 import activity_bridge
from hermes_a365 import invoke as invoke_mod
from hermes_a365.activity_bridge import (
    JwtValidationError,
    _JwksCache,
    make_app,
    validate_inbound_jwt,
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
    def _token_missing_claim(
        self, rsa_keypair: tuple[Any, dict[str, Any]], *, drop: str
    ) -> tuple[str, dict[str, Any]]:
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization

        priv, jwk = rsa_keypair
        token = _make_token(priv, aud="bot-app-id", azp=TEST_APX_AZP)
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
    async def test_validate_inbound_jwt_requires_exp(
        self, rsa_keypair: tuple[Any, dict[str, Any]]
    ) -> None:
        _, jwk = rsa_keypair
        token, _ = self._token_missing_claim(rsa_keypair, drop="exp")
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
        token, _ = self._token_missing_claim(rsa_keypair, drop="nbf")
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

    def test_serve_rejects_token_without_exp(self, rsa_keypair: tuple[Any, dict[str, Any]]) -> None:
        token, jwk = self._token_missing_claim(rsa_keypair, drop="exp")
        cfg = _cfg()
        cfg.skip_jwt_validation = False
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture, jwk=jwk) as client:
            activity = _inbound_message_activity()
            r = client.post(
                "/api/messages",
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 403
        assert capture["webhook"] == []

    def test_serve_rejects_token_without_nbf(self, rsa_keypair: tuple[Any, dict[str, Any]]) -> None:
        token, jwk = self._token_missing_claim(rsa_keypair, drop="nbf")
        cfg = _cfg()
        cfg.skip_jwt_validation = False
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture, jwk=jwk) as client:
            activity = _inbound_message_activity()
            r = client.post(
                "/api/messages",
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 403
        assert capture["webhook"] == []


class TestHandoffTokenUserOid:
    def test_empty_claim_oid_does_not_match_body_aad_object_id(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        record = {"tenant_id": "tid", "user_aad": "record-user"}
        ctx = invoke_mod.InvokeContext(
            name="handoff/action",
            activity={"from": {"aadObjectId": "record-user"}},
            value={},
            user_oid="record-user",
            token_user_oid="",
            user_upn="",
            tenant_id="tid",
            service_url="https://example.test",
            conv={"tenantId": "tid"},
            chat_type="group",
            path_tag="A",
        )
        assert adapter._handoff_identity_matches(record, ctx) is False


class TestFileConsentDeclineChecks:
    @pytest.mark.asyncio
    async def test_decline_mismatch_keeps_pending(self) -> None:
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
    async def test_decline_expired_keeps_pending(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
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
