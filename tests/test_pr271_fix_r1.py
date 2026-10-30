"""PR #271 fix round 1 — regression tests with named killing coverage."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_activity_bridge import (
    TEST_TENANT_ID,
    _cfg,
    _client_for,
    _inbound_message_activity,
)
from test_agent365_plugin import _StubPlatformConfig
from test_bot_service import SUBSCRIPTION_ID, TENANT_ID, FakeRunner

from hermes_a365 import activity_bridge
from hermes_a365 import invoke as invoke_mod
from hermes_a365.activity_bridge import (
    ReplyPostError,
    ReplyPostMayHaveLandedError,
    _reply_failure_forgets_idempotency,
    make_app,
)
from hermes_a365.bot_service import (
    BotServiceCreateInputs,
    BotServiceError,
    apply_create_plan,
    build_create_plan,
)
from hermes_a365.mutator import _strip_ansi
from hermes_a365.plugin import adapter as adapter_mod
from hermes_a365.plugin.conversations import ConversationRegistry
from hermes_a365.sidecar_schema import SidecarSchemaError, validate_bot_service_sidecar_schema


class TestA1WebhookReplyOnlyRetry:
    def test_reply_502_retry_does_not_reinvoke_webhook(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        reply_calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if url.startswith("https://hook.test/responder"):
                capture["webhook"].append(1)
                return httpx.Response(200, json={"text": "hi back"})
            if "/v3/conversations/" in url:
                reply_calls["n"] += 1
                return httpx.Response(502, text="bad gateway")
            if "/oauth2/v2.0/token" in url:
                return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
            return httpx.Response(404)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = make_app(cfg, http_client=http_client)
        activity = _inbound_message_activity()
        with TestClient(app) as client:
            r1 = client.post("/api/messages", json=activity)
            assert r1.status_code == 502
            r2 = client.post("/api/messages", json=activity)
            assert r2.status_code == 502
        assert len(capture["webhook"]) == 1
        assert reply_calls["n"] == 2

    def test_webhook_non_dict_body_retry_is_duplicate_not_second_webhook(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if url.startswith("https://hook.test/responder"):
                capture["webhook"].append(1)
                return httpx.Response(200, json=["not", "a", "dict"])
            if "/oauth2/v2.0/token" in url:
                return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
            return httpx.Response(404)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = make_app(cfg, http_client=http_client)
        activity = _inbound_message_activity()
        with TestClient(app) as client:
            client.post("/api/messages", json=activity)
            r2 = client.post("/api/messages", json=activity)
        assert len(capture["webhook"]) == 1
        assert r2.json().get("status") == "duplicate"


class TestA2ReplyIdempotencyClassifier:
    @pytest.mark.parametrize(
        "error,forgets",
        [
            (ReplyPostMayHaveLandedError("x"), False),
            (ReplyPostError(status_code=502, url="u", body_excerpt=""), False),
            (ReplyPostError(status_code=400, url="u", body_excerpt=""), True),
            (ReplyPostError(status_code=429, url="u", body_excerpt=""), False),
        ],
    )
    def test_classifier(self, error: BaseException, forgets: bool) -> None:
        assert _reply_failure_forgets_idempotency(error) is forgets

    def test_read_error_on_reply_retry_single_webhook(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        fail_once = {"done": False}

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if url.startswith("https://hook.test/responder"):
                capture["webhook"].append(1)
                return httpx.Response(200, json={"text": "hi"})
            if "/v3/conversations/" in url and not fail_once["done"]:
                fail_once["done"] = True
                raise httpx.ReadError("simulated")
            if "/oauth2/v2.0/token" in url:
                return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
            if "/v3/conversations/" in url:
                return httpx.Response(200, json={})
            return httpx.Response(404)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = make_app(cfg, http_client=http_client)
        activity = _inbound_message_activity()
        with TestClient(app) as client:
            client.post("/api/messages", json=activity)
            r2 = client.post("/api/messages", json=activity)
        assert len(capture["webhook"]) == 1
        assert r2.status_code == 200


class TestE1JwtRequiresExpAndNbf:
    def _token_missing_claim(
        self, rsa_keypair: tuple[Any, dict[str, Any]], *, drop: str
    ) -> tuple[str, dict[str, Any]]:
        import time as _time

        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization

        priv, jwk = rsa_keypair
        payload = {
            "aud": "bot-app-id",
            "iss": f"https://login.microsoftonline.com/{TEST_TENANT_ID}/v2.0",
            "azp": "00000000-0000-0000-0000-000000000001",
            "tid": TEST_TENANT_ID,
            "nbf": int(_time.time()),
            "exp": int(_time.time()) + 600,
            "iat": int(_time.time()),
        }
        payload.pop(drop, None)
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return (
            pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": "test-kid-1"}),
            jwk,
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


class TestE2HandoffTokenUserOid:
    def test_empty_claim_oid_does_not_match_body_aad_object_id(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig(extra={"tenant_id": "tid"}))
        record = {"tenant_id": "tid", "user_aad": "record-user"}
        ctx = invoke_mod.InvokeContext(
            name="handoff/action",
            activity={"from": {"aadObjectId": "body-user"}},
            value={},
            user_oid="body-user",
            token_user_oid="",
            user_upn="",
            tenant_id="tid",
            service_url="https://example.test",
            conv={"tenantId": "tid"},
            chat_type="group",
            path_tag="A",
        )
        assert adapter._handoff_identity_matches(record, ctx) is False


class TestE3ServeOpenApiKwargs:
    @pytest.mark.parametrize("kwarg", ["docs_url", "redoc_url", "openapi_url"])
    def test_fastapi_ctor_passes_none(self, kwarg: str, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert recorded.get(kwarg) is None


class TestE7AnsiStripFixedExpectations:
    @pytest.mark.parametrize(
        "sample,expected",
        [
            ("\x1b[31mred\x1b[0m", "red"),
            ("\x1b[2Jclear", "clear"),
            ("\x1b[?25lhide", "hide"),
        ],
    )
    def test_strip_ansi_matches_fixed_output(self, sample: str, expected: str) -> None:
        assert _strip_ansi(sample) == expected


class TestC1ConversationsStrictUtf8:
    def test_utf16_registry_roundtrip_via_save(self, tmp_path: Path) -> None:
        path = tmp_path / "conversations.json"
        path.write_bytes('{"schemaVersion": 1, "conversations": {}}'.encode("utf-16-le"))
        reg = ConversationRegistry.load(path)
        ConversationRegistry.write_payload(path, reg.to_payload())
        assert path.read_bytes().decode("utf-8").startswith("{")


class TestC2CreateRefusesExistingSidecar:
    def test_valid_v1_sidecar_bytes_unchanged_no_azure(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "a365.bot-service.config.json"
        original = json.dumps(
            {
                "schemaVersion": 1,
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
            }
        ).encode()
        sidecar.write_bytes(original)
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError, match="already exists"):
            apply_create_plan(plan, runner=runner)
        assert sidecar.read_bytes() == original
        assert runner.calls == []


class TestC3AgentNameTypeValidation:
    def test_dict_agent_name_error_names_type_not_value(self) -> None:
        from hermes_a365.bot_service import BotServiceConfig

        sentinel = "SENTINEL-NOT-A-SECRET-C3"
        payload = {
            "schemaVersion": 2,
            "agentName": {sentinel: 1},
            "subscriptionId": SUBSCRIPTION_ID,
            "resourceGroup": "rg",
            "botName": "bot",
            "armResourceId": (
                "/subscriptions/x/resourceGroups/rg/providers/Microsoft.BotService/botServices/bot"
            ),
            "msaAppId": "00000000-0000-0000-0000-000000000001",
            "tenantId": TENANT_ID,
            "messagingEndpoint": "https://example.test/api/messages",
            "channelsEnabled": ["msteams"],
            "createdAt": "2020-01-01T00:00:00Z",
            "resourceGroupManaged": False,
        }
        with pytest.raises(BotServiceError) as exc:
            BotServiceConfig.from_json_text(json.dumps(payload), source="cfg")
        assert sentinel not in str(exc.value)
        assert "dict" in str(exc.value).lower() or "unsupported type" in str(exc.value).lower()


class TestE3PluginOpenApiHidden:
    @pytest.mark.parametrize("route", ["/docs", "/redoc", "/openapi.json"])
    def test_each_openapi_route_hidden(self, route: str) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig())
        app = adapter.build_app()
        with TestClient(app) as client:
            assert client.get(route).status_code == 404

    def test_plugin_healthz_ok_shape(self) -> None:
        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig())
        app = adapter.build_app()
        with TestClient(app) as client:
            assert client.get("/healthz").json() == {"ok": True}


class TestC4SchemaVersionOutOfRangeMessage:
    def test_int_99_is_unsupported_version_not_type(self) -> None:
        with pytest.raises(SidecarSchemaError, match="unsupported version"):
            validate_bot_service_sidecar_schema({"schemaVersion": 99}, source="cfg")


class TestB1FileConsentDeclineChecks:
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
    async def test_valid_decline_consumes_pending(self) -> None:
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
