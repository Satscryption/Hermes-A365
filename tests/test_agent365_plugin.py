"""Tests for plugins/agent365 — slice 19m skeleton.

The plugin imports ``gateway.platforms.base`` and ``gateway.config``
from the Hermes harness at module level. These aren't installed in
this repo's venv (the harness lives at ``~/.hermes/hermes-agent/``),
so we install minimal stubs into ``sys.modules`` *before* importing
the plugin module — same trick upstream Hermes uses for its own
unit tests of platform plugins.

Slice 19m proves the registration plumbing is right; slice 19n's
tests exercise the actual runtime. We deliberately don't test
real connect/send semantics here — those methods are documented
stubs.
"""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Stub harness modules. Installed once at module import; removed in
# the final cleanup fixture so other test modules don't see them.
# ---------------------------------------------------------------------------


@dataclass
class _StubSendResult:
    success: bool
    message_id: Any = None


class _StubBasePlatformAdapter:
    def __init__(self, config: Any, platform: Any) -> None:
        self.config = config
        self.platform = platform
        self._running = False

    def _mark_connected(self) -> None:
        self._running = True

    def _mark_disconnected(self) -> None:
        self._running = False


class _StubMessageEvent:
    pass


class _StubMessageType:
    pass


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


def _install_gateway_stubs() -> None:
    if "gateway.platforms.base" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    config = types.ModuleType("gateway.config")

    base.BasePlatformAdapter = _StubBasePlatformAdapter
    base.SendResult = _StubSendResult
    base.MessageEvent = _StubMessageEvent
    base.MessageType = _StubMessageType
    config.Platform = _StubPlatform
    config.PlatformConfig = _StubPlatformConfig

    sys.modules["gateway"] = gateway
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.base"] = base
    sys.modules["gateway.config"] = config


_install_gateway_stubs()


# Make the plugins/ directory importable. We append rather than insert
# so a real Hermes install on the path still wins.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(_REPO_ROOT))

# Now safe to import the plugin.
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
# Tests
# ---------------------------------------------------------------------------


class TestPluginManifest:
    def test_plugin_yaml_present_and_parseable(self) -> None:
        path = _REPO_ROOT / "plugins" / "agent365" / "PLUGIN.yaml"
        assert path.exists()
        # Don't add a yaml dependency just for this — the loader is the
        # authoritative parser. Smoke-check that the keys we depend on
        # are textually present.
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
        # The factory must be callable; we don't invoke it here.
        assert callable(kwargs["adapter_factory"])
        # Plugin loader uses these env-name strings to wire authorization
        # without requiring core changes.
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
        # Smart-chunking gate. 0 would mean "no chunking" — A365 does
        # have a practical limit, so this must be non-zero.
        assert ctx.platforms[0]["max_message_length"] > 0

    def test_platform_hint_mentions_a365(self) -> None:
        ctx = _FakeCtx()
        adapter_mod.register(ctx)
        hint = ctx.platforms[0]["platform_hint"].lower()
        assert "agent 365" in hint or "a365" in hint


class TestCheckRequirements:
    def test_returns_true_when_extras_installed(self) -> None:
        # The bridge extras (httpx, fastapi, jwt) ARE installed in this
        # repo's dev venv (used by the existing bridge tests). So the
        # probe should report True.
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


class TestAdapterConstruction:
    def test_init_pulls_slug_and_port_from_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENT_IDENTITY", raising=False)
        monkeypatch.delenv("HERMES_BRIDGE_PORT", raising=False)
        monkeypatch.delenv("A365_TENANT_ID", raising=False)
        monkeypatch.delenv("A365_APP_ID", raising=False)
        monkeypatch.delenv("HERMES_BRIDGE_WEBHOOK", raising=False)
        cfg = _StubPlatformConfig(
            extra={
                "slug": "inbox-helper",
                "port": 3978,
                "tenant_id": "tenant-1",
                "app_id": "app-1",
                "webhook_url": "http://hook",
            }
        )
        a = adapter_mod.Agent365Adapter(cfg)
        assert a.slug == "inbox-helper"
        assert a.port == 3978
        assert a.tenant_id == "tenant-1"
        assert a.blueprint_app_id == "app-1"
        assert a.webhook_url == "http://hook"
        # Platform identity is preserved via the stub Platform class.
        assert a.platform.value == "agent365"

    def test_env_vars_override_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_BRIDGE_PORT", "4000")
        monkeypatch.setenv("A365_TENANT_ID", "env-tenant")
        monkeypatch.setenv("A365_APP_ID", "env-app")
        cfg = _StubPlatformConfig(
            extra={"port": 3978, "tenant_id": "ignored", "app_id": "ignored"}
        )
        a = adapter_mod.Agent365Adapter(cfg)
        assert a.port == 4000
        assert a.tenant_id == "env-tenant"
        assert a.blueprint_app_id == "env-app"

    @pytest.mark.asyncio
    async def test_connect_disconnect_marks_running_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_TENANT_ID", "t")
        monkeypatch.setenv("A365_APP_ID", "a")
        cfg = _StubPlatformConfig(extra={"slug": "x", "port": 3978})
        a = adapter_mod.Agent365Adapter(cfg)
        ok = await a.connect()
        assert ok is True
        assert a._running is True
        await a.disconnect()
        assert a._running is False

    @pytest.mark.asyncio
    async def test_send_returns_successful_stub(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_TENANT_ID", "t")
        monkeypatch.setenv("A365_APP_ID", "a")
        cfg = _StubPlatformConfig(extra={})
        a = adapter_mod.Agent365Adapter(cfg)
        result = await a.send(chat_id="conv-1", content="hi", metadata={})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_get_chat_info_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_TENANT_ID", "t")
        monkeypatch.setenv("A365_APP_ID", "a")
        cfg = _StubPlatformConfig(extra={})
        a = adapter_mod.Agent365Adapter(cfg)
        info = await a.get_chat_info("conv-42")
        # ADDING_A_PLATFORM.md:31-46 mandates the {name, type, chat_id}
        # contract — slice 19o will replace the placeholder name with a
        # session-table lookup.
        assert set(info.keys()) >= {"name", "type", "chat_id"}
        assert info["chat_id"] == "conv-42"
