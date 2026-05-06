"""Tests for plugins/agent365 — slices 19m skeleton + 19n runtime port.

The plugin imports ``gateway.platforms.base``, ``gateway.config``, and
``gateway.session`` from the Hermes harness at module level. Those
aren't installed in this repo's venv (the harness lives at
``~/.hermes/hermes-agent/``), so we install minimal stubs into
``sys.modules`` *before* importing the plugin module — same trick
upstream Hermes uses for its own unit tests of platform plugins.
"""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub harness modules. Installed once at module import.
# ---------------------------------------------------------------------------


@dataclass
class _StubSendResult:
    success: bool
    message_id: Any = None
    error: str | None = None


class _StubMessageType(Enum):
    TEXT = "text"
    PHOTO = "photo"
    DOCUMENT = "document"


@dataclass
class _StubMessageEvent:
    text: str
    message_type: Any = None
    source: Any = None
    raw_message: Any = None
    message_id: str | None = None
    timestamp: Any = None


class _StubPlatform:
    """Mimics ``gateway.config.Platform``'s "accept any name" behaviour
    that the plugin loader relies on (``Platform._missing_()`` upstream)."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _StubPlatform) and self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:
        return f"Platform({self.value!r})"


@dataclass
class _StubPlatformConfig:
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _StubSessionSource:
    platform: Any
    chat_id: str
    chat_name: str | None = None
    chat_type: str = "dm"
    user_id: str | None = None
    user_name: str | None = None
    thread_id: str | None = None
    chat_topic: str | None = None
    user_id_alt: str | None = None
    chat_id_alt: str | None = None
    is_bot: bool = False
    guild_id: str | None = None
    parent_chat_id: str | None = None
    message_id: str | None = None


class _StubBasePlatformAdapter:
    """Just enough of BasePlatformAdapter for the adapter tests.

    Stores any event passed to ``handle_message`` on
    ``self._handled_events`` so route tests can assert dispatch
    happened with the right shape.
    """

    def __init__(self, config: Any, platform: Any) -> None:
        self.config = config
        self.platform = platform
        self._running = False
        self._fatal: tuple[str, str, bool] | None = None
        self._handled_events: list[Any] = []

    def _mark_connected(self) -> None:
        self._running = True

    def _mark_disconnected(self) -> None:
        self._running = False

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._fatal = (code, message, retryable)

    async def handle_message(self, event: Any) -> None:
        self._handled_events.append(event)


def _install_gateway_stubs() -> None:
    if "gateway.platforms.base" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    config = types.ModuleType("gateway.config")
    session = types.ModuleType("gateway.session")

    base.BasePlatformAdapter = _StubBasePlatformAdapter
    base.SendResult = _StubSendResult
    base.MessageEvent = _StubMessageEvent
    base.MessageType = _StubMessageType
    config.Platform = _StubPlatform
    config.PlatformConfig = _StubPlatformConfig
    session.SessionSource = _StubSessionSource

    sys.modules["gateway"] = gateway
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.base"] = base
    sys.modules["gateway.config"] = config
    sys.modules["gateway.session"] = session


_install_gateway_stubs()

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(_REPO_ROOT))

agent365 = importlib.import_module("plugins.agent365")
adapter_mod = importlib.import_module("plugins.agent365.adapter")


# ---------------------------------------------------------------------------
# Fake plugin context — captures the register_platform() call.
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self) -> None:
        self.platforms: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []

    def register_platform(self, **kwargs: Any) -> None:
        self.platforms.append(kwargs)

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_adapter(monkeypatch: pytest.MonkeyPatch, **extra_overrides: Any) -> Any:
    """Build an Agent365Adapter with sensible defaults for route tests."""
    monkeypatch.setenv("A365_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("A365_APP_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", "fake-secret")
    extra = {"slug": "test-agent", "port": 0}
    extra.update(extra_overrides)
    cfg = _StubPlatformConfig(extra=extra)
    return adapter_mod.Agent365Adapter(cfg)


def _make_inbound(
    *,
    text: str = "hello",
    conv_id: str = "conv-1",
    activity_id: str = "act-1",
    service_url: str = "https://smba.trafficmanager.net/amer/x/",
) -> dict[str, Any]:
    """Synthesise a BF activity in the shape the bridge sees from A365."""
    return {
        "type": "message",
        "id": activity_id,
        "channelId": "msteams",
        "serviceUrl": service_url,
        "conversation": {"id": conv_id, "conversationType": "personal"},
        "from": {"id": "user-1", "name": "Sadiq"},
        "recipient": {"id": "agent-1", "name": "Inbox Helper"},
        "text": text,
    }


# ---------------------------------------------------------------------------
# Manifest + register (carried over from 19m)
# ---------------------------------------------------------------------------


class TestPluginManifest:
    def test_plugin_yaml_present_and_parseable(self) -> None:
        path = _REPO_ROOT / "plugins" / "agent365" / "PLUGIN.yaml"
        assert path.exists()
        text = path.read_text()
        for key in ("name:", "version:", "description:", "requires_env:"):
            assert key in text, f"PLUGIN.yaml missing {key!r}"
        assert "name: agent365" in text

    def test_init_reexports_register(self) -> None:
        assert hasattr(agent365, "register")
        assert agent365.register is adapter_mod.register


class TestRegister:
    def test_calls_ctx_register_platform_with_required_keys(self) -> None:
        ctx = _FakeCtx()
        adapter_mod.register(ctx)
        assert len(ctx.platforms) == 1
        kwargs = ctx.platforms[0]
        assert kwargs["name"] == "agent365"
        assert kwargs["label"] == "Microsoft Agent 365"
        assert callable(kwargs["adapter_factory"])
        assert kwargs["allowed_users_env"] == "A365_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "A365_ALLOW_ALL_USERS"
        assert kwargs["required_env"] == ["A365_TENANT_ID", "A365_APP_ID"]

    def test_register_platform_advertises_check_and_validate(self) -> None:
        ctx = _FakeCtx()
        adapter_mod.register(ctx)
        kwargs = ctx.platforms[0]
        assert callable(kwargs["check_fn"])
        assert callable(kwargs["validate_config"])

    def test_max_message_length_is_set(self) -> None:
        ctx = _FakeCtx()
        adapter_mod.register(ctx)
        assert ctx.platforms[0]["max_message_length"] > 0

    def test_platform_hint_mentions_a365(self) -> None:
        ctx = _FakeCtx()
        adapter_mod.register(ctx)
        hint = ctx.platforms[0]["platform_hint"].lower()
        assert "agent 365" in hint or "a365" in hint


class TestCheckRequirements:
    def test_returns_true_when_extras_installed(self) -> None:
        # Bridge extras (httpx, fastapi, jwt, uvicorn) are in the dev
        # venv per the existing bridge tests.
        assert adapter_mod.check_requirements() is True


class TestValidateConfig:
    def test_accepts_extra_with_tenant_and_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("A365_TENANT_ID", raising=False)
        monkeypatch.delenv("A365_APP_ID", raising=False)
        cfg = _StubPlatformConfig(extra={"tenant_id": "t", "app_id": "a"})
        assert adapter_mod.validate_config(cfg) is True

    def test_accepts_env_when_extra_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_TENANT_ID", "tenant-1")
        monkeypatch.setenv("A365_APP_ID", "app-1")
        cfg = _StubPlatformConfig(extra={})
        assert adapter_mod.validate_config(cfg) is True

    def test_rejects_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("A365_TENANT_ID", raising=False)
        monkeypatch.delenv("A365_APP_ID", raising=False)
        cfg = _StubPlatformConfig(extra={})
        assert adapter_mod.validate_config(cfg) is False


# ---------------------------------------------------------------------------
# Adapter construction (env / extra plumbing)
# ---------------------------------------------------------------------------


class TestAdapterConstruction:
    def test_init_pulls_slug_and_port_from_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for k in (
            "AGENT_IDENTITY",
            "HERMES_BRIDGE_PORT",
            "A365_TENANT_ID",
            "A365_APP_ID",
            "HERMES_BRIDGE_WEBHOOK",
            "A365_BLUEPRINT_CLIENT_SECRET",
        ):
            monkeypatch.delenv(k, raising=False)
        cfg = _StubPlatformConfig(
            extra={
                "slug": "inbox-helper",
                "port": 3978,
                "tenant_id": "tenant-1",
                "app_id": "app-1",
                "blueprint_client_secret": "extra-secret",
            }
        )
        a = adapter_mod.Agent365Adapter(cfg)
        assert a.slug == "inbox-helper"
        assert a.port == 3978
        assert a.tenant_id == "tenant-1"
        assert a.blueprint_app_id == "app-1"
        assert a.blueprint_client_secret == "extra-secret"
        assert a.platform.value == "agent365"

    def test_env_vars_override_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_BRIDGE_PORT", "4000")
        monkeypatch.setenv("A365_TENANT_ID", "env-tenant")
        monkeypatch.setenv("A365_APP_ID", "env-app")
        monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", "env-secret")
        cfg = _StubPlatformConfig(
            extra={
                "port": 3978,
                "tenant_id": "ignored",
                "app_id": "ignored",
                "blueprint_client_secret": "ignored",
            }
        )
        a = adapter_mod.Agent365Adapter(cfg)
        assert a.port == 4000
        assert a.tenant_id == "env-tenant"
        assert a.blueprint_app_id == "env-app"
        assert a.blueprint_client_secret == "env-secret"

    def test_secret_loaded_from_generated_config_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "a365.generated.config.json"
        cfg_path.write_text('{"agentBlueprintClientSecret": "from-disk"}')
        monkeypatch.setenv("A365_TENANT_ID", "t")
        monkeypatch.setenv("A365_APP_ID", "a")
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        cfg = _StubPlatformConfig(
            extra={"generated_config_path": str(cfg_path)}
        )
        a = adapter_mod.Agent365Adapter(cfg)
        # Lazy-loaded only when the bridge config is built.
        assert a.blueprint_client_secret == ""
        assert a._ensure_secret() == "from-disk"
        assert a.blueprint_client_secret == "from-disk"

    def test_make_bridge_config_raises_without_secret(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("A365_TENANT_ID", "t")
        monkeypatch.setenv("A365_APP_ID", "a")
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        # Generated config exists but has no secret.
        cfg_path = tmp_path / "a365.generated.config.json"
        cfg_path.write_text("{}")
        cfg = _StubPlatformConfig(
            extra={"generated_config_path": str(cfg_path)}
        )
        a = adapter_mod.Agent365Adapter(cfg)
        with pytest.raises(RuntimeError, match="missing"):
            a._make_bridge_config()


# ---------------------------------------------------------------------------
# /api/messages route — drive via FastAPI TestClient.
# ---------------------------------------------------------------------------


class TestMessagesRoute:
    def test_untrusted_service_url_returns_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        client = TestClient(a.build_app())
        body = _make_inbound(service_url="https://attacker.example/")
        r = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 403
        assert "untrusted serviceUrl" in r.json()["detail"]
        assert a._handled_events == []

    def test_missing_authorization_returns_401(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        client = TestClient(a.build_app())
        r = client.post("/api/messages", json=_make_inbound())
        assert r.status_code == 401

    def test_valid_jwt_dispatches_message_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Patch the bridge's validator + http client so we can drive
        the route end-to-end without a real Microsoft JWKS / token."""
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        # Patch validate_inbound_jwt to always succeed.
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        a._http_client = MagicMock()  # never actually called in the JWT path

        client = TestClient(a.build_app())
        body = _make_inbound(text="hello there", conv_id="conv-X", activity_id="aaa")
        r = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "dispatched"
        # MessageEvent landed in handle_message.
        assert len(a._handled_events) == 1
        evt = a._handled_events[0]
        assert evt.text == "hello there"
        assert evt.source.chat_id == "conv-X"
        assert evt.source.chat_type == "dm"  # personal → dm mapping
        assert evt.source.user_id == "user-1"
        assert evt.source.user_name == "Sadiq"
        # Cached for outbound lookup via the durable registry (slice 19o).
        assert "conv-X" in a._conversations
        ref = a._conversations.get("conv-X")
        assert ref is not None
        assert ref.last_inbound_activity_id == "aaa"
        assert ref.raw["id"] == "aaa"

    def test_duplicate_delivery_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        a._http_client = MagicMock()
        client = TestClient(a.build_app())
        body = _make_inbound()
        headers = {"Authorization": "Bearer pretend"}
        r1 = client.post("/api/messages", json=body, headers=headers)
        r2 = client.post("/api/messages", json=body, headers=headers)
        assert r1.json()["status"] == "dispatched"
        assert r2.json()["status"] == "duplicate"
        # Only one dispatch despite two POSTs.
        assert len(a._handled_events) == 1

    def test_conversation_update_acked_no_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        a._http_client = MagicMock()
        client = TestClient(a.build_app())
        body = {**_make_inbound(), "type": "conversationUpdate"}
        r = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "acked"
        assert a._handled_events == []


# ---------------------------------------------------------------------------
# send() — outbound via cached inbound + send_reply
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_send_with_no_cached_inbound_returns_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        result = await a.send(chat_id="missing", content="hi")
        assert result.success is False
        assert "no cached inbound" in (result.error or "")

    @pytest.mark.asyncio
    async def test_send_with_cached_inbound_invokes_send_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        result = await a.send(chat_id="conv-1", content="hi back")
        assert result.success is True
        assert send_reply_mock.await_count == 1
        kwargs = send_reply_mock.await_args.kwargs
        assert kwargs["inbound"]["id"] == "act-1"
        # Reply activity carries our text.
        assert kwargs["reply"]["text"] == "hi back"
        # Reply mirrors BF reply convention.
        assert kwargs["reply"]["replyToId"] == "act-1"

    @pytest.mark.asyncio
    async def test_send_reply_failure_surfaces_in_send_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        boom = AsyncMock(side_effect=RuntimeError("token mint failed"))
        monkeypatch.setattr(bridge, "send_reply", boom)

        result = await a.send(chat_id="conv-1", content="x")
        assert result.success is False
        assert "token mint failed" in (result.error or "")


# ---------------------------------------------------------------------------
# get_chat_info — pulls metadata from cached inbound
# ---------------------------------------------------------------------------


class TestGetChatInfo:
    @pytest.mark.asyncio
    async def test_returns_default_shape_when_no_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        info = await a.get_chat_info("unknown")
        assert info == {"name": "unknown", "type": "personal", "chat_id": "unknown"}

    @pytest.mark.asyncio
    async def test_resolves_name_and_type_from_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        cached = _make_inbound(conv_id="conv-G")
        cached["conversation"]["conversationType"] = "groupChat"
        cached["conversation"]["name"] = "team-room"
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(cached))
        info = await a.get_chat_info("conv-G")
        assert info["name"] == "team-room"
        assert info["type"] == "group"
        assert info["chat_id"] == "conv-G"


# ---------------------------------------------------------------------------
# Slice 19o — durable session table
# ---------------------------------------------------------------------------


class TestConversationRef:
    def test_from_activity_extracts_required_fields(self) -> None:
        ref = adapter_mod.ConversationRef.from_activity(_make_inbound())
        assert ref is not None
        assert ref.conversation_id == "conv-1"
        assert ref.service_url.startswith("https://smba.trafficmanager.net/")
        assert ref.chat_type == "personal"
        assert ref.user_id == "user-1"
        assert ref.user_name == "Sadiq"
        assert ref.last_inbound_activity_id == "act-1"
        assert ref.raw["id"] == "act-1"

    def test_from_activity_returns_none_without_conversation_id(self) -> None:
        bad = _make_inbound()
        bad["conversation"] = {}
        assert adapter_mod.ConversationRef.from_activity(bad) is None

    def test_round_trip_through_dict(self) -> None:
        ref = adapter_mod.ConversationRef.from_activity(_make_inbound())
        round_tripped = adapter_mod.ConversationRef.from_dict(ref.to_dict())
        assert round_tripped == ref

    def test_from_dict_tolerates_extra_keys(self) -> None:
        # Future-schema fields shouldn't break round-trip; they land in
        # `raw` so we don't lose them.
        payload = adapter_mod.ConversationRef.from_activity(
            _make_inbound()
        ).to_dict()
        payload["future_field_we_dont_know_about"] = "ok"
        ref = adapter_mod.ConversationRef.from_dict(payload)
        assert ref.conversation_id == "conv-1"


class TestConversationRegistry:
    def test_upsert_merges_and_preserves_existing_fields(self) -> None:
        from plugins.agent365.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(ConversationRef(
            conversation_id="conv-X",
            service_url="https://svc.trafficmanager.net/",
            chat_name="original",
        ))
        # Second upsert with empty chat_name must not wipe the existing one.
        reg.upsert(ConversationRef(
            conversation_id="conv-X",
            service_url="https://svc.trafficmanager.net/",
            chat_name=None,
            last_inbound_activity_id="act-2",
        ))
        ref = reg.get("conv-X")
        assert ref is not None
        assert ref.chat_name == "original"
        assert ref.last_inbound_activity_id == "act-2"

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        from plugins.agent365.conversations import ConversationRegistry

        reg = ConversationRegistry.load(tmp_path / "nope.json")
        assert len(reg) == 0

    def test_load_unparseable_returns_empty(self, tmp_path: Path) -> None:
        from plugins.agent365.conversations import ConversationRegistry

        path = tmp_path / "convs.json"
        path.write_text("not json {{{")
        reg = ConversationRegistry.load(path)
        assert len(reg) == 0

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        from plugins.agent365.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        ref = ConversationRef.from_activity(_make_inbound(conv_id="conv-A"))
        reg.upsert(ref)
        path = tmp_path / "convs.json"
        reg.save(path)

        # File on disk is well-formed JSON.
        import json

        payload = json.loads(path.read_text())
        assert payload["schema"] == ConversationRegistry.SCHEMA_VERSION
        assert len(payload["conversations"]) == 1

        # Round-trips back into a registry.
        reloaded = ConversationRegistry.load(path)
        assert "conv-A" in reloaded
        assert reloaded.get("conv-A").user_name == "Sadiq"

    def test_save_is_atomic_with_no_tmpfile_residue(self, tmp_path: Path) -> None:
        """Atomic write means no leftover .tmp files after a successful save."""
        from plugins.agent365.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(ConversationRef(conversation_id="x", service_url="https://x/"))
        path = tmp_path / "convs.json"
        reg.save(path)
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
        assert leftovers == []


class TestAdapterPersistsRegistry:
    def test_inbound_writes_registry_to_disk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from fastapi.testclient import TestClient

        conv_path = tmp_path / "convs.json"
        a = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x"}),
        )
        a._http_client = MagicMock()
        client = TestClient(a.build_app())
        client.post(
            "/api/messages",
            json=_make_inbound(conv_id="conv-D", activity_id="act-Z"),
            headers={"Authorization": "Bearer pretend"},
        )
        assert conv_path.exists()
        # Reload independently to confirm durability.
        from plugins.agent365.conversations import ConversationRegistry

        reloaded = ConversationRegistry.load(conv_path)
        ref = reloaded.get("conv-D")
        assert ref is not None
        assert ref.last_inbound_activity_id == "act-Z"

    def test_constructor_loads_existing_registry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from plugins.agent365.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "convs.json"
        seed = ConversationRegistry()
        seed.upsert(
            ConversationRef(
                conversation_id="conv-survived",
                service_url="https://smba.trafficmanager.net/",
                chat_name="across-restart",
            )
        )
        seed.save(conv_path)

        a = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        ref = a._conversations.get("conv-survived")
        assert ref is not None
        assert ref.chat_name == "across-restart"


# ---------------------------------------------------------------------------
# Slice 19o — send_typing + send_image
# ---------------------------------------------------------------------------


class TestSendTyping:
    @pytest.mark.asyncio
    async def test_no_op_when_no_cached_inbound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        # Should swallow silently — gateway typing pulse must not throw.
        await a.send_typing("missing")

    @pytest.mark.asyncio
    async def test_posts_typing_activity_to_conversation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(
                _make_inbound(conv_id="conv-T", activity_id="t1")
            )
        )
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        # Mock the token mint + the actual POST.
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_outbound_token",
            AsyncMock(return_value="bearer-xyz"),
        )
        post_mock = AsyncMock(
            return_value=MagicMock(status_code=200, text="")
        )
        a._http_client.post = post_mock

        await a.send_typing("conv-T")
        assert post_mock.await_count == 1
        url = post_mock.await_args.kwargs.get("url") or post_mock.await_args.args[0]
        assert "/v3/conversations/conv-T/activities" in url
        # No activity-id suffix on a typing post — different from
        # replyToActivity, intentionally.
        assert "/activities/" not in url
        body = post_mock.await_args.kwargs["json"]
        assert body["type"] == "typing"
        assert body["conversation"]["id"] == "conv-T"
        # Auth header carries our minted bearer.
        headers = post_mock.await_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer bearer-xyz"

    @pytest.mark.asyncio
    async def test_typing_failure_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_outbound_token",
            AsyncMock(side_effect=RuntimeError("token mint failed")),
        )
        # Must not raise — gateway typing pulse runs in a hot path.
        await a.send_typing("conv-1")


class TestSendImage:
    @pytest.mark.asyncio
    async def test_renders_adaptive_card_with_image_and_caption(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        result = await a.send_image(
            "conv-1",
            "https://example.test/cat.jpg",
            caption="my cat",
        )
        assert result.success is True
        kwargs = send_reply_mock.await_args.kwargs
        attachments = kwargs["reply"]["attachments"]
        assert len(attachments) == 1
        card = attachments[0]["content"]
        assert card["type"] == "AdaptiveCard"
        body = card["body"]
        # First element is the Image, second is the TextBlock caption.
        assert body[0]["type"] == "Image"
        assert body[0]["url"] == "https://example.test/cat.jpg"
        assert body[1]["type"] == "TextBlock"
        assert body[1]["text"] == "my cat"

    @pytest.mark.asyncio
    async def test_no_caption_omits_textblock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
        result = await a.send_image("conv-1", "https://example.test/x.png")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_no_cached_inbound_returns_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        result = await a.send_image("missing", "https://example.test/x.png")
        assert result.success is False
        assert "no cached inbound" in (result.error or "")
