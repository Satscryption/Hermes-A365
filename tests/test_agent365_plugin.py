"""Tests for hermes_a365.plugin — slices 19m skeleton + 19n runtime port.

The plugin imports ``gateway.platforms.base``, ``gateway.config``, and
``gateway.session`` from the Hermes harness at module level. Those
aren't installed in this repo's venv (the harness lives at
``~/.hermes/hermes-agent/``), so we install minimal stubs into
``sys.modules`` *before* importing the plugin module — same trick
upstream Hermes uses for its own unit tests of platform plugins.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
import time
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
    media_urls: list = field(default_factory=list)
    media_types: list = field(default_factory=list)


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
        import asyncio as _asyncio

        self.config = config
        self.platform = platform
        self._running = False
        self._fatal: tuple[str, str, bool] | None = None
        self._handled_events: list[Any] = []
        # Slice 19x-d (#4): mirror real BasePlatformAdapter's in-flight
        # state primitives so prune_conversations() can read
        # self._active_sessions without crashing the test fakes.
        self._active_sessions: dict[str, _asyncio.Event] = {}
        self._session_tasks: dict[str, _asyncio.Task] = {}

    def _mark_connected(self) -> None:
        self._running = True

    def _mark_disconnected(self) -> None:
        self._running = False

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._fatal = (code, message, retryable)

    async def handle_message(self, event: Any) -> None:
        self._handled_events.append(event)

    @staticmethod
    def validate_media_delivery_path(path: str) -> str | None:
        """Mirror BasePlatformAdapter.validate_media_delivery_path enough for
        the #76c outbound-file tests: accept an existing absolute regular file,
        else None (the real one also applies a credential/system denylist)."""
        if not path:
            return None
        p = Path(path)
        if not p.is_absolute() or not p.is_file():
            return None
        return str(p.resolve())


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

agent365 = importlib.import_module("hermes_a365.plugin")
adapter_mod = importlib.import_module("hermes_a365.plugin.adapter")


# ---------------------------------------------------------------------------
# Fake plugin context — captures the register_platform() call.
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self) -> None:
        self.platforms: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []

    def register_platform(self, **kwargs: Any) -> None:
        self.platforms.append(kwargs)

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_cli_command(self, **kwargs: Any) -> None:
        self.cli_commands.append(kwargs)


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
    path: str = "A",
) -> dict[str, Any]:
    """Synthesise a BF activity in the shape the bridge sees.

    Default is Path A (A365 agentic-user routing): recipient carries
    ``agenticAppId`` + ``agenticUserId`` + tenantId. Pass ``path="B"``
    for a classic Bot Framework shape with no agentic identifiers
    (used for #33 dispatch tests). The default shape is Path A
    because most route-level tests want to exercise the legacy A365
    outbound chain.
    """
    recipient: dict[str, Any] = {"id": "agent-1", "name": "Inbox Helper"}
    conv: dict[str, Any] = {"id": conv_id, "conversationType": "personal"}
    if path == "A":
        recipient["agenticAppId"] = "agentic-app-1"
        recipient["agenticUserId"] = "agentic-user-1"
        recipient["tenantId"] = "11111111-1111-1111-1111-111111111111"
        conv["tenantId"] = "11111111-1111-1111-1111-111111111111"
    return {
        "type": "message",
        "id": activity_id,
        "channelId": "msteams",
        "serviceUrl": service_url,
        "conversation": conv,
        "from": {"id": "user-1", "name": "Sadiq"},
        "recipient": recipient,
        "text": text,
    }


# ---------------------------------------------------------------------------
# Manifest + register (carried over from 19m)
# ---------------------------------------------------------------------------


class TestPluginManifest:
    def test_plugin_yaml_present_and_parseable(self) -> None:
        # Bundled as package data; resolves under either an editable
        # install or an installed wheel.
        from importlib import resources

        path = Path(str(resources.files("hermes_a365.plugin").joinpath("plugin.yaml")))
        assert path.exists()
        text = path.read_text()
        for key in ("name:", "version:", "description:", "requires_env:"):
            assert key in text, f"plugin.yaml missing {key!r}"
        assert "name: agent365" in text

    def test_uppercase_manifest_not_present(self) -> None:
        # Regression guard: macOS APFS is case-insensitive by default
        # so Path.exists() can't distinguish — list the directory
        # and check the actual on-disk name. On Linux the loader is
        # case-sensitive and an uppercase variant would be skipped.
        from importlib import resources

        plugin_dir = Path(str(resources.files("hermes_a365.plugin")))
        names = {p.name for p in plugin_dir.iterdir()}
        assert "plugin.yaml" in names
        assert "PLUGIN.yaml" not in names, (
            "PLUGIN.yaml re-introduced — harness loader globs for lowercase"
        )

    def test_init_register_is_a_wrapper(self) -> None:
        # Slice 19x-a: __init__.register is now a wrapper that calls
        # both adapter.register AND register_cli_command, so it is no
        # longer the same object as adapter_mod.register.
        assert callable(agent365.register)
        assert agent365.register is not adapter_mod.register


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

    def test_setup_fn_is_wired(self) -> None:
        # Slice 19r-a: setup_fn must point at interactive_setup so
        # `hermes gateway setup --platform agent365` finds the wizard.
        ctx = _FakeCtx()
        adapter_mod.register(ctx)
        kwargs = ctx.platforms[0]
        assert kwargs.get("setup_fn") is adapter_mod.interactive_setup
        assert callable(kwargs["setup_fn"])

    def test_interactive_setup_signature_is_no_args(self) -> None:
        # Hermes' setup harness calls setup_fn() with no arguments
        # (per gateway/platforms/irc/adapter.py reference).
        import inspect

        sig = inspect.signature(adapter_mod.interactive_setup)
        assert len(sig.parameters) == 0


class TestDetectDrift:
    """Slice 19r-b: _detect_drift() returns operator-config issues."""

    def _make_home(self, tmp_path: Path, *, env: str = "", agents: list[str] | None = None,
                   a365_config: dict[str, Any] | None = None,
                   generated: dict[str, Any] | None = None,
                   generated_filename: str = "a365.generated.config.json") -> Path:
        """Build a fake home dir with the bits _detect_drift reads."""
        (tmp_path / ".hermes").mkdir()
        (tmp_path / ".hermes" / ".env").write_text(env)
        agents_root = tmp_path / ".hermes" / "agents"
        agents_root.mkdir()
        for slug in agents or []:
            (agents_root / slug).mkdir()
        if a365_config is not None:
            import json as _json
            (tmp_path / "a365.config.json").write_text(_json.dumps(a365_config))
        if generated is not None:
            import json as _json
            (tmp_path / generated_filename).write_text(_json.dumps(generated))
        return tmp_path

    def test_no_drift_on_clean_home(self, tmp_path: Path) -> None:
        home = self._make_home(tmp_path)
        drift = adapter_mod._detect_drift(home=home, config={})
        assert drift == []

    def test_app_id_stale_detected(self, tmp_path: Path) -> None:
        # Operator .env app id != generated config blueprint id.
        home = self._make_home(
            tmp_path,
            env="A365_APP_ID=00000000-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n",
            generated={"agentBlueprintId": "11111111-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
        )
        drift = adapter_mod._detect_drift(home=home, config={})
        keys = [d["key"] for d in drift]
        assert "app_id_stale" in keys
        msg = next(d["message"] for d in drift if d["key"] == "app_id_stale")
        assert "00000000" in msg
        assert "11111111" in msg

    def test_app_id_matching_no_drift(self, tmp_path: Path) -> None:
        home = self._make_home(
            tmp_path,
            env="A365_APP_ID=11111111-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n",
            generated={"agentBlueprintId": "11111111-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
        )
        # Seed the XDG symlink so slice 19r-bis (#25)'s drift check
        # doesn't surface xdg_symlink_missing here.
        xdg_dir = home / ".config" / "a365"
        xdg_dir.mkdir(parents=True)
        (xdg_dir / "a365.generated.config.json").symlink_to(
            home / "a365.generated.config.json"
        )
        drift = adapter_mod._detect_drift(home=home, config={})
        assert [d["key"] for d in drift] == []

    def test_slug_orphan_detected(self, tmp_path: Path) -> None:
        # Stanza points at a slug not present under ~/.hermes/agents/.
        home = self._make_home(tmp_path, agents=["inbox-helper-r8"])
        cfg = {
            "gateway": {
                "platforms": {
                    "agent365": {
                        "enabled": True,
                        "extra": {"slug": "old-slug-that-doesnt-exist"},
                    }
                }
            }
        }
        drift = adapter_mod._detect_drift(home=home, config=cfg)
        keys = [d["key"] for d in drift]
        assert "slug_orphan" in keys

    def test_slug_present_no_drift(self, tmp_path: Path) -> None:
        home = self._make_home(tmp_path, agents=["inbox-helper-r8", "test-agent"])
        cfg = {
            "gateway": {
                "platforms": {
                    "agent365": {"extra": {"slug": "inbox-helper-r8"}}
                }
            }
        }
        drift = adapter_mod._detect_drift(home=home, config=cfg)
        assert "slug_orphan" not in [d["key"] for d in drift]

    def test_a365_config_empty_detected(self, tmp_path: Path) -> None:
        home = self._make_home(
            tmp_path,
            a365_config={"tenantId": "", "clientAppId": ""},
        )
        drift = adapter_mod._detect_drift(home=home, config={})
        keys = [d["key"] for d in drift]
        assert "a365_config_empty" in keys

    def test_a365_config_empty_fixer_reseeds(self, tmp_path: Path) -> None:
        # Fixer should fill in clientAppId; tenant fill depends on
        # whether `az account show` is available in the test env.
        # We test the unambiguous half here.
        home = self._make_home(
            tmp_path,
            env="A365_TENANT_ID=22222222-cccc-cccc-cccc-cccccccccccc\n",
            a365_config={"tenantId": "", "clientAppId": ""},
        )
        drift = adapter_mod._detect_drift(home=home, config={})
        item = next(d for d in drift if d["key"] == "a365_config_empty")
        fixer = item.get("fixer")
        if fixer is None:
            # az not available in test env — skip the reseed assertion.
            import pytest as _pytest
            _pytest.skip("az not in PATH; fixer was not constructed")
        fixer()
        import json as _json
        cur = _json.loads((home / "a365.config.json").read_text())
        # clientAppId always reseeds to the well-known GUID.
        assert cur["clientAppId"] == adapter_mod._AGENT365_CLI_APP_ID
        # tenantId may have come from operator env (preferred) or detected.
        assert cur["tenantId"] != ""

    def test_a365_config_present_no_drift(self, tmp_path: Path) -> None:
        home = self._make_home(
            tmp_path,
            a365_config={"tenantId": "abc", "clientAppId": "def"},
        )
        drift = adapter_mod._detect_drift(home=home, config={})
        assert "a365_config_empty" not in [d["key"] for d in drift]

    def test_generated_config_missing_detected(self, tmp_path: Path) -> None:
        # Stanza points at a path that doesn't exist on disk.
        home = self._make_home(tmp_path)
        bad_path = str(tmp_path / "nope.json")
        cfg = {
            "gateway": {
                "platforms": {
                    "agent365": {"extra": {"generated_config_path": bad_path}}
                }
            }
        }
        drift = adapter_mod._detect_drift(home=home, config=cfg)
        keys = [d["key"] for d in drift]
        assert "generated_config_missing" in keys

    def test_generated_config_blank_detected(self, tmp_path: Path) -> None:
        # Path exists but agentBlueprintId is empty.
        gen_path = tmp_path / "stale.json"
        import json as _json
        gen_path.write_text(_json.dumps({"agentBlueprintId": ""}))
        home = self._make_home(tmp_path)
        cfg = {
            "gateway": {
                "platforms": {
                    "agent365": {"extra": {"generated_config_path": str(gen_path)}}
                }
            }
        }
        drift = adapter_mod._detect_drift(home=home, config=cfg)
        keys = [d["key"] for d in drift]
        assert "generated_config_blank" in keys

    def test_drift_keys_are_unique_per_run(self, tmp_path: Path) -> None:
        # Each drift item is reported at most once.
        home = self._make_home(
            tmp_path,
            env="A365_APP_ID=00000000-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n",
            agents=["inbox-helper-r8"],
            a365_config={"tenantId": "", "clientAppId": ""},
            generated={"agentBlueprintId": "11111111-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
        )
        cfg = {
            "gateway": {
                "platforms": {
                    "agent365": {"extra": {"slug": "orphan-slug"}}
                }
            }
        }
        drift = adapter_mod._detect_drift(home=home, config=cfg)
        keys = [d["key"] for d in drift]
        assert len(set(keys)) == len(keys)


class TestEnsureXdgGeneratedConfigSymlink:
    """Slice 19r-bis (#25): GA CLI XDG-path symlink helper."""

    def _make_home_with_xdg_root(self, tmp_path: Path) -> Path:
        (tmp_path / ".config").mkdir()
        return tmp_path

    def test_noop_when_target_is_xdg_path(self, tmp_path: Path) -> None:
        home = self._make_home_with_xdg_root(tmp_path)
        xdg_dir = home / ".config" / "a365"
        xdg_dir.mkdir()
        target = xdg_dir / "a365.generated.config.json"
        target.write_text("{}")
        result = adapter_mod._ensure_xdg_generated_config_symlink(target, home=home)
        assert result["status"] == "noop"
        # Still a regular file, no symlink overlay.
        assert target.is_file() and not target.is_symlink()

    def test_creates_symlink_when_xdg_path_missing(self, tmp_path: Path) -> None:
        home = self._make_home_with_xdg_root(tmp_path)
        target = home / "a365.generated.config.json"
        target.write_text("{}")
        result = adapter_mod._ensure_xdg_generated_config_symlink(target, home=home)
        xdg = home / ".config" / "a365" / "a365.generated.config.json"
        assert result["status"] == "created"
        assert xdg.is_symlink()
        assert xdg.resolve() == target.resolve()

    def test_noop_when_correct_symlink_exists(self, tmp_path: Path) -> None:
        home = self._make_home_with_xdg_root(tmp_path)
        target = home / "a365.generated.config.json"
        target.write_text("{}")
        xdg_dir = home / ".config" / "a365"
        xdg_dir.mkdir()
        xdg = xdg_dir / "a365.generated.config.json"
        xdg.symlink_to(target)
        result = adapter_mod._ensure_xdg_generated_config_symlink(target, home=home)
        assert result["status"] == "noop"
        assert xdg.is_symlink()
        assert xdg.resolve() == target.resolve()

    def test_repairs_symlink_pointing_at_wrong_target(self, tmp_path: Path) -> None:
        home = self._make_home_with_xdg_root(tmp_path)
        wrong = home / "wrong-target.json"
        wrong.write_text("{}")
        right = home / "a365.generated.config.json"
        right.write_text("{}")
        xdg_dir = home / ".config" / "a365"
        xdg_dir.mkdir()
        xdg = xdg_dir / "a365.generated.config.json"
        xdg.symlink_to(wrong)
        result = adapter_mod._ensure_xdg_generated_config_symlink(right, home=home)
        assert result["status"] == "repaired"
        assert xdg.is_symlink()
        assert xdg.resolve() == right.resolve()

    def test_skipped_when_xdg_path_is_real_file(self, tmp_path: Path) -> None:
        home = self._make_home_with_xdg_root(tmp_path)
        target = home / "a365.generated.config.json"
        target.write_text("{}")
        xdg_dir = home / ".config" / "a365"
        xdg_dir.mkdir()
        xdg = xdg_dir / "a365.generated.config.json"
        # Operator-seeded real file — wizard must not clobber.
        xdg.write_text('{"operator": "data"}')
        result = adapter_mod._ensure_xdg_generated_config_symlink(target, home=home)
        assert result["status"] == "skipped_real_file"
        assert not xdg.is_symlink()
        assert xdg.read_text() == '{"operator": "data"}'

    def test_creates_xdg_parent_dir(self, tmp_path: Path) -> None:
        # ~/.config/a365 doesn't exist yet — helper should create it.
        home = tmp_path  # no .config/a365 setup
        target = home / "a365.generated.config.json"
        target.write_text("{}")
        result = adapter_mod._ensure_xdg_generated_config_symlink(target, home=home)
        assert result["status"] == "created"
        assert (home / ".config" / "a365").is_dir()


class TestDetectDriftXdgSymlink:
    """Slice 19r-bis (#25): _detect_drift surfaces XDG-symlink gaps."""

    def _make_home(
        self,
        tmp_path: Path,
        *,
        generated_at: str = "a365.generated.config.json",
    ) -> Path:
        (tmp_path / ".hermes").mkdir()
        (tmp_path / ".hermes" / ".env").write_text("")
        (tmp_path / ".hermes" / "agents").mkdir()
        (tmp_path / generated_at).write_text('{"agentBlueprintId": "x"}')
        return tmp_path

    def test_xdg_symlink_missing_detected(self, tmp_path: Path) -> None:
        home = self._make_home(tmp_path)
        # No ~/.config/a365/ at all.
        drift = adapter_mod._detect_drift(home=home, config={})
        keys = [d["key"] for d in drift]
        assert "xdg_symlink_missing" in keys

    def test_xdg_symlink_wrong_target_detected(self, tmp_path: Path) -> None:
        home = self._make_home(tmp_path)
        # XDG symlink points at a stale generated config.
        other = tmp_path / "other-generated.json"
        other.write_text('{"agentBlueprintId": "stale"}')
        xdg_dir = home / ".config" / "a365"
        xdg_dir.mkdir(parents=True)
        xdg = xdg_dir / "a365.generated.config.json"
        xdg.symlink_to(other)
        drift = adapter_mod._detect_drift(home=home, config={})
        keys = [d["key"] for d in drift]
        assert "xdg_symlink_wrong_target" in keys

    def test_no_drift_when_xdg_symlink_correct(self, tmp_path: Path) -> None:
        home = self._make_home(tmp_path)
        xdg_dir = home / ".config" / "a365"
        xdg_dir.mkdir(parents=True)
        xdg = xdg_dir / "a365.generated.config.json"
        xdg.symlink_to(home / "a365.generated.config.json")
        drift = adapter_mod._detect_drift(home=home, config={})
        keys = [d["key"] for d in drift]
        assert "xdg_symlink_missing" not in keys
        assert "xdg_symlink_wrong_target" not in keys

    def test_no_drift_when_generated_is_xdg_itself(self, tmp_path: Path) -> None:
        # Operator keeps the generated config directly at the XDG path.
        (tmp_path / ".hermes").mkdir()
        (tmp_path / ".hermes" / ".env").write_text(
            f"A365_GENERATED_CONFIG_PATH={tmp_path}/.config/a365/a365.generated.config.json\n"
        )
        (tmp_path / ".hermes" / "agents").mkdir()
        xdg_dir = tmp_path / ".config" / "a365"
        xdg_dir.mkdir(parents=True)
        (xdg_dir / "a365.generated.config.json").write_text(
            '{"agentBlueprintId": "x"}'
        )
        drift = adapter_mod._detect_drift(home=tmp_path, config={})
        keys = [d["key"] for d in drift]
        assert "xdg_symlink_missing" not in keys
        assert "xdg_symlink_wrong_target" not in keys

    def test_xdg_drift_fixer_repairs_symlink(self, tmp_path: Path) -> None:
        home = self._make_home(tmp_path)
        drift = adapter_mod._detect_drift(home=home, config={})
        item = next(d for d in drift if d["key"] == "xdg_symlink_missing")
        assert callable(item["fixer"])
        item["fixer"]()
        xdg = home / ".config" / "a365" / "a365.generated.config.json"
        assert xdg.is_symlink()
        assert xdg.resolve() == (home / "a365.generated.config.json").resolve()


class TestCheckRequirements:
    def test_returns_true_when_extras_installed(self) -> None:
        # Bridge extras (httpx, fastapi, jwt, uvicorn) are in the dev
        # venv per the existing bridge tests.
        assert adapter_mod.check_requirements() is True


class TestIsConnected:
    """Slice 19o follow-up — `is_connected(config)` signature must
    match `gateway/platform_registry.py:64` (`Callable[[Any], bool]`).
    Earlier 19m drafts had a 0-arg version that would have crashed
    the loader's status check at first call."""

    def test_takes_config_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_TENANT_ID", "t")
        monkeypatch.setenv("A365_APP_ID", "a")
        cfg = _StubPlatformConfig(extra={})
        assert adapter_mod.is_connected(cfg) is True

    def test_returns_false_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("A365_TENANT_ID", raising=False)
        monkeypatch.delenv("A365_APP_ID", raising=False)
        cfg = _StubPlatformConfig(extra={})
        assert adapter_mod.is_connected(cfg) is False


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
    def test_connect_accepts_is_reconnect_kwarg(self) -> None:
        # Contract guard: BasePlatformAdapter.connect is
        # ``connect(self, *, is_reconnect: bool = False)`` and the gateway
        # always calls ``adapter.connect(is_reconnect=...)`` (gateway/run.py).
        # An override that drops the kwarg breaks every connect against the
        # current gateway core ("unexpected keyword argument 'is_reconnect'").
        import inspect

        params = inspect.signature(adapter_mod.Agent365Adapter.connect).parameters
        assert "is_reconnect" in params, "connect() must accept is_reconnect"
        p = params["is_reconnect"]
        assert p.kind == inspect.Parameter.KEYWORD_ONLY
        assert p.default is False

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


class TestMessagesRoutePathBDispatch:
    """#34 — route handler peeks the unverified ``iss`` claim and
    dispatches to ``validate_inbound_jwt_bf`` for Path B (classic Bot
    Framework) tokens, or ``validate_inbound_jwt`` for Path A (A365 /
    AAD-v2) tokens. The peek is a routing hint only — both validators
    still do real signature checks, so a malformed ``Bearer pretend``
    falls through to the A365 path (preserved pre-#34 behaviour)."""

    @staticmethod
    def _make_unverifiable_token(iss: str) -> str:
        """Build a JWT that's parseable enough for ``peek_unverified_iss``
        to read the iss claim, but whose signature won't verify
        against any real key. Tests monkeypatch the *real* validators
        so the signature never actually matters."""
        import base64
        import json

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT", "kid": "fake"}).encode()
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"iss": iss, "aud": "bot-app-id", "exp": 9999999999}).encode()
        ).rstrip(b"=").decode()
        # Padded fake signature — adapter doesn't decode it; only
        # validator branches care, and those are monkeypatched.
        return f"{header}.{payload}.AAAA"

    def test_bf_iss_dispatches_to_bf_validator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BF-issued token → adapter calls ``validate_inbound_jwt_bf``
        with the activity's serviceUrl + bot app id, NOT the A365
        validator. With ``bf_app_id`` unset (default), the expected
        audience falls back to ``blueprint_app_id`` — preserves
        pre-#36 behaviour for operators on Path A only or for the
        provisional bot resource registered against the blueprint
        app id."""
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()

        a365_validator = AsyncMock(return_value={"iss": "should-not-be-called"})
        bf_validator = AsyncMock(return_value={"iss": bridge.BF_ISSUER})
        monkeypatch.setattr(bridge, "validate_inbound_jwt", a365_validator)
        monkeypatch.setattr(bridge, "validate_inbound_jwt_bf", bf_validator)
        a._http_client = MagicMock()

        token = self._make_unverifiable_token(bridge.BF_ISSUER)
        client = TestClient(a.build_app())
        body = _make_inbound(text="hello path B")
        r = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "dispatched"
        # BF validator called with the right args.
        bf_validator.assert_awaited_once()
        kwargs = bf_validator.await_args.kwargs
        # bf_app_id is unset by default → falls back to blueprint.
        assert kwargs["expected_app_id"] == a.blueprint_app_id
        assert kwargs["expected_service_url"] == body["serviceUrl"]
        assert kwargs["cache"] is a._bf_jwks_cache
        # A365 validator NOT called.
        a365_validator.assert_not_awaited()
        # MessageEvent landed in handle_message.
        assert len(a._handled_events) == 1
        assert a._handled_events[0].text == "hello path B"

    def test_bf_iss_uses_bf_app_id_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#36: when the adapter is configured with a separate Path B
        identity (``bf_app_id``), inbound BF JWTs are validated
        against THAT app id rather than the blueprint. Mirrors the
        operator's bot-resource rewire to the non-agentic identity —
        Microsoft signs inbound JWTs with `aud = bf_app_id` after
        the rewire."""
        from fastapi.testclient import TestClient

        monkeypatch.setenv("A365_BF_APP_ID", "path-b-app-id")
        monkeypatch.setenv("A365_BF_CLIENT_SECRET", "path-b-secret")
        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()

        bf_validator = AsyncMock(return_value={"iss": bridge.BF_ISSUER})
        monkeypatch.setattr(bridge, "validate_inbound_jwt_bf", bf_validator)
        a._http_client = MagicMock()

        token = self._make_unverifiable_token(bridge.BF_ISSUER)
        client = TestClient(a.build_app())
        r = client.post(
            "/api/messages",
            json=_make_inbound(),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        bf_validator.assert_awaited_once()
        # Critical: expected_app_id = bf_app_id, NOT blueprint.
        assert bf_validator.await_args.kwargs["expected_app_id"] == "path-b-app-id"
        assert a.bf_app_id == "path-b-app-id"
        assert a.bf_client_secret == "path-b-secret"

    def test_aad_iss_dispatches_to_a365_validator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path A token (AAD-v2 issuer) → adapter calls ``validate_inbound_jwt``,
        NOT the BF validator."""
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()

        a365_validator = AsyncMock(return_value={"iss": "ok"})
        bf_validator = AsyncMock(return_value={"iss": "should-not-be-called"})
        monkeypatch.setattr(bridge, "validate_inbound_jwt", a365_validator)
        monkeypatch.setattr(bridge, "validate_inbound_jwt_bf", bf_validator)
        a._http_client = MagicMock()

        aad_iss = f"https://login.microsoftonline.com/{a.tenant_id}/v2.0"
        token = self._make_unverifiable_token(aad_iss)
        client = TestClient(a.build_app())
        r = client.post(
            "/api/messages",
            json=_make_inbound(),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        a365_validator.assert_awaited_once()
        bf_validator.assert_not_awaited()

    def test_unparseable_token_defaults_to_a365(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Bearer pretend`` (not even a JWT) — peek returns None, so
        the dispatcher falls through to the A365 path. Pins the
        pre-#34 behaviour that other ``TestMessagesRoute`` cases
        already rely on (they pass ``Bearer pretend`` + monkeypatched
        A365 validator)."""
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()

        a365_validator = AsyncMock(return_value={"iss": "ok"})
        bf_validator = AsyncMock(return_value={"iss": "should-not-be-called"})
        monkeypatch.setattr(bridge, "validate_inbound_jwt", a365_validator)
        monkeypatch.setattr(bridge, "validate_inbound_jwt_bf", bf_validator)
        a._http_client = MagicMock()

        client = TestClient(a.build_app())
        r = client.post(
            "/api/messages",
            json=_make_inbound(),
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200, r.text
        a365_validator.assert_awaited_once()
        bf_validator.assert_not_awaited()

    def test_bf_validator_failure_returns_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BF-issued token where the validator raises → 403 with the
        validator's reason in the detail. Pins the actual route
        behaviour against the Direct Line probe failure mode that
        was documented in §11.10 finding 11."""
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()

        async def _reject(**_kwargs: Any) -> dict[str, Any]:
            raise bridge.JwtValidationError("BF signature/aud/iss check failed: bad")

        monkeypatch.setattr(bridge, "validate_inbound_jwt_bf", _reject)
        a._http_client = MagicMock()

        token = self._make_unverifiable_token(bridge.BF_ISSUER)
        client = TestClient(a.build_app())
        r = client.post(
            "/api/messages",
            json=_make_inbound(),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403
        assert "BF signature/aud/iss" in r.json()["detail"]
        assert a._handled_events == []


# ---------------------------------------------------------------------------
# Slice 19q — filter agents-channel synthetic events
# ---------------------------------------------------------------------------


class TestShouldDispatch:
    """Pure-function classifier for which inbound activities reach
    ``handle_message``. Round-5 §9d walkthrough surfaced
    ``agents``-channel onboarding probes spamming the agent loop —
    these tests pin the matrix."""

    def test_real_msteams_message_dispatches(self) -> None:
        assert adapter_mod._should_dispatch(_make_inbound()) is True

    def test_conversation_update_acks(self) -> None:
        body = {**_make_inbound(), "type": "conversationUpdate"}
        assert adapter_mod._should_dispatch(body) is False

    def test_typing_acks(self) -> None:
        body = {**_make_inbound(), "type": "typing"}
        assert adapter_mod._should_dispatch(body) is False

    def test_end_of_conversation_acks(self) -> None:
        body = {**_make_inbound(), "type": "endOfConversation"}
        assert adapter_mod._should_dispatch(body) is False

    def test_agents_channel_event_acks(self) -> None:
        # The exact shape Microsoft sends for `agentLifecycle` probes
        # during the AI Teammate activation flow.
        body = {
            **_make_inbound(),
            "channelId": "agents",
            "type": "event",
            "name": "agentLifecycle",
            "from": {"id": "system", "name": "System"},
        }
        assert adapter_mod._should_dispatch(body) is False

    def test_agents_channel_message_from_system_acks(self) -> None:
        # Synthetic lifecycle render activities arrive on `agents`
        # channel as `type=message` from `from.id=system`.
        body = {
            **_make_inbound(),
            "channelId": "agents",
            "from": {"id": "system", "name": "System"},
        }
        assert adapter_mod._should_dispatch(body) is False

    def test_agents_channel_message_from_no_reply_acks(self) -> None:
        # The exact shape that slipped through the original `system`-only
        # filter during the §9d round-5 walkthrough — Teams ships these
        # email-template render activities (a "you have a new Copilot
        # notification" HTML blob) on the `agents` channel from a
        # no-reply mail address. Captured in conversations.json
        # post-walkthrough.
        body = {
            **_make_inbound(),
            "channelId": "agents",
            "from": {
                "id": "no-reply@teams.mail.microsoft",
                "name": "Microsoft Teams",
            },
        }
        assert adapter_mod._should_dispatch(body) is False

    def test_msteams_channel_no_reply_still_dispatches(self) -> None:
        # The no-reply filter is gated on `channelId=agents` —
        # we never want to drop a real msteams message just because
        # it happens to share a sender prefix.
        body = {
            **_make_inbound(),
            "from": {"id": "no-reply@teams.mail.microsoft", "name": "x"},
        }
        # channelId stays "msteams" via _make_inbound's default.
        assert adapter_mod._should_dispatch(body) is True

    def test_agents_channel_message_from_real_user_dispatches(self) -> None:
        # If a real user message ever lands on the `agents` channel
        # (e.g., a future Copilot Chat path), don't drop it on the
        # floor. ``from.id=system`` is the load-bearing filter.
        body = {
            **_make_inbound(),
            "channelId": "agents",
            "from": {"id": "user-1", "name": "Sadiq"},
        }
        assert adapter_mod._should_dispatch(body) is True

    def test_missing_from_field_does_not_crash(self) -> None:
        body = {**_make_inbound(), "channelId": "agents"}
        body.pop("from", None)
        # No `from.id=system`, so we treat it as user-routable.
        assert adapter_mod._should_dispatch(body) is True


class TestLifecycleRegistryAction:
    """#79 — pure classifier mapping BF lifecycle activities to a registry
    action (capture for proactive / evict on uninstall / leave alone)."""

    def test_installation_add_upserts(self) -> None:
        body = {"type": "installationUpdate", "action": "add"}
        assert adapter_mod._lifecycle_registry_action(body) == "upsert"

    def test_installation_default_action_upserts(self) -> None:
        # Missing/empty action is treated as add (capture, don't evict).
        assert adapter_mod._lifecycle_registry_action(
            {"type": "installationUpdate"}
        ) == "upsert"

    def test_installation_remove_evicts(self) -> None:
        body = {"type": "installationUpdate", "action": "remove"}
        assert adapter_mod._lifecycle_registry_action(body) == "evict"

    def test_conversation_update_bot_added_upserts(self) -> None:
        # Capture only when the BOT (recipient.id) is among membersAdded.
        body = {
            "type": "conversationUpdate",
            "recipient": {"id": "bot-1"},
            "membersAdded": [{"id": "user-9"}, {"id": "bot-1"}],
        }
        assert adapter_mod._lifecycle_registry_action(body) == "upsert"

    def test_conversation_update_user_added_without_bot_is_none(self) -> None:
        # An ordinary user joining a still-live group must not churn the
        # registry — leave it to _should_dispatch's ack-and-bail.
        body = {
            "type": "conversationUpdate",
            "recipient": {"id": "bot-1"},
            "membersAdded": [{"id": "user-9"}],
        }
        assert adapter_mod._lifecycle_registry_action(body) is None

    def test_conversation_update_members_added_without_recipient_is_none(
        self,
    ) -> None:
        # No bot id to match against → cannot fire.
        body = {"type": "conversationUpdate", "membersAdded": [{"id": "x"}]}
        assert adapter_mod._lifecycle_registry_action(body) is None

    def test_conversation_update_without_members_is_none(self) -> None:
        # Plain conversationUpdate (topic rename etc.) — leave to
        # _should_dispatch's existing ack-and-bail.
        assert adapter_mod._lifecycle_registry_action(
            {"type": "conversationUpdate", "recipient": {"id": "bot-1"}}
        ) is None

    def test_conversation_update_bot_removed_evicts(self) -> None:
        # The bot being kicked from a group is a real uninstall signal on
        # surfaces that don't send installationUpdate(remove).
        body = {
            "type": "conversationUpdate",
            "recipient": {"id": "bot-1"},
            "membersRemoved": [{"id": "bot-1"}],
        }
        assert adapter_mod._lifecycle_registry_action(body) == "evict"

    def test_conversation_update_user_removed_is_none(self) -> None:
        # A user leaving a still-live group must NOT evict — only the bot's
        # own removal does.
        body = {
            "type": "conversationUpdate",
            "recipient": {"id": "bot-1"},
            "membersRemoved": [{"id": "user-9"}],
        }
        assert adapter_mod._lifecycle_registry_action(body) is None

    def test_agents_channel_synthetic_install_is_none(self) -> None:
        # Synthetic agents-channel probes must not reach the registry —
        # they bypass _should_dispatch's screen (lifecycle runs first).
        for sender in ("system", "no-reply@teams.mail.microsoft"):
            body = {
                "type": "installationUpdate",
                "action": "add",
                "channelId": "agents",
                "from": {"id": sender},
            }
            assert adapter_mod._lifecycle_registry_action(body) is None

    def test_real_msteams_install_still_upserts(self) -> None:
        body = {
            "type": "installationUpdate",
            "action": "add",
            "channelId": "msteams",
        }
        assert adapter_mod._lifecycle_registry_action(body) == "upsert"

    def test_real_message_is_none(self) -> None:
        assert adapter_mod._lifecycle_registry_action(_make_inbound()) is None

    def test_typing_is_none(self) -> None:
        assert adapter_mod._lifecycle_registry_action({"type": "typing"}) is None


class TestConversationRegistryEvict:
    """#79 — explicit tenant-driven removal (uninstall hygiene)."""

    @staticmethod
    def _reg():
        from hermes_a365.plugin.conversations import ConversationRegistry

        return ConversationRegistry()

    def test_evict_present_returns_true(self) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        reg = self._reg()
        reg.upsert(ConversationRef(conversation_id="c1", service_url="u"))
        assert reg.evict("c1") is True
        assert "c1" not in reg

    def test_evict_absent_returns_false(self) -> None:
        assert self._reg().evict("nope") is False

    def test_evict_removes_pinned_entry(self) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        reg = self._reg()
        reg.upsert(ConversationRef(conversation_id="c2", service_url="u"))
        assert reg.pin("c2") is True
        # An uninstall is a harder signal than a pin.
        assert reg.evict("c2") is True
        assert "c2" not in reg


class TestLifecycleCapture:
    """#79 — route-level: lifecycle activities capture/evict the
    conversation reference for proactive delivery and never reach the
    agent loop. Driven via the FastAPI TestClient with the JWT validator
    patched, same harness as ``TestMessagesRoute``."""

    @staticmethod
    def _client(a, monkeypatch):
        from fastapi.testclient import TestClient

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        a._http_client = MagicMock()
        return TestClient(a.build_app())

    @staticmethod
    def _lifecycle_body(conv_id="conv-install", **overrides):
        # Path B (classic BF: no agentic ids, trafficmanager serviceUrl)
        # so the captured ref classifies as a Path B proactive target.
        body = {**_make_inbound(path="B", conv_id=conv_id), **overrides}
        body.pop("text", None)  # lifecycle activities carry no user text
        return body

    def test_route_logs_inbound_activity_shape(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Request-level observability: every inbound logs its shape-defining
        # fields before any gate, so the log shows whether e.g. an
        # installationUpdate ever reaches the endpoint (closes finding #5).
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        caplog.set_level("INFO")
        body = self._lifecycle_body(type="installationUpdate", action="add")
        client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer pretend"}
        )
        assert "inbound activity type=installationUpdate action=add" in caplog.text
        assert "channelId=msteams" in caplog.text

    def test_installation_add_captures_ref_enables_proactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        body = self._lifecycle_body(type="installationUpdate", action="add")
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer pretend"}
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "acked", "lifecycle": "upsert"}
        # Never reached the agent loop (fixes the wasted-turn mishandling).
        assert a._handled_events == []
        # Captured into the registry...
        assert "conv-install" in a._conversations
        # ...but NOT marked seen-this-lifetime, so send() routes proactive.
        assert "conv-install" not in a._seen_inbounds_this_lifetime
        # And the captured ref is a usable Path B proactive target.
        spec = a._build_proactive_target_spec("conv-install")
        assert spec is not None
        assert spec["path"] == "B"
        assert spec["conversation_id"] == "conv-install"

    def test_lifecycle_capture_stamps_validated_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #106 review follow-up (L4): a lifecycle-captured ref must stamp the
        # JWT-validated path so a later proactive mint off it binds to the
        # validated path, not the untrusted activity body. Before the fix the
        # lifecycle branch left validated_path=None → proactive send went
        # body-derived. (In this harness the unverified-iss peek fails, so the
        # validator dispatch defaults to the Path A branch → validated_path "A".)
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        body = self._lifecycle_body(
            conv_id="conv-vp", type="installationUpdate", action="add"
        )
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer pretend"}
        )
        assert r.json() == {"status": "acked", "lifecycle": "upsert"}
        ref = a._conversations.get("conv-vp")
        assert ref is not None
        assert ref.validated_path == "A"
        # ...and the proactive target spec carries it (not None) so the mint binds.
        spec = a._build_proactive_target_spec("conv-vp")
        assert spec is not None
        assert spec["validated_path"] == "A"

    def test_conversation_update_bot_added_captures_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        # The bot is recipient.id from _make_inbound (= "agent-1").
        body = self._lifecycle_body(
            conv_id="conv-add",
            type="conversationUpdate",
            membersAdded=[{"id": "user-x"}, {"id": "agent-1"}],
        )
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer pretend"}
        )
        assert r.json() == {"status": "acked", "lifecycle": "upsert"}
        assert a._handled_events == []
        assert "conv-add" in a._conversations

    def test_lifecycle_does_not_clobber_active_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Clobber regression: a real user message captured this lifetime,
        # then a bot-add conversationUpdate for the SAME conv must NOT
        # overwrite the cached user-message raw / last_inbound id (which
        # would corrupt the replyToActivity target), and the chat must stay
        # seen-this-lifetime (keep its reply path, not flip to proactive).
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        msg = _make_inbound(
            path="B", conv_id="conv-live", activity_id="user-act-1", text="hi"
        )
        r1 = client.post(
            "/api/messages", json=msg, headers={"Authorization": "Bearer pretend"}
        )
        assert r1.json()["status"] == "dispatched"
        assert "conv-live" in a._seen_inbounds_this_lifetime
        assert a._conversations.get("conv-live").last_inbound_activity_id == "user-act-1"

        lc = self._lifecycle_body(
            conv_id="conv-live",
            id="cu-act-9",  # different id than the user message (no dedupe)
            type="conversationUpdate",
            membersAdded=[{"id": "agent-1"}],
        )
        r2 = client.post(
            "/api/messages", json=lc, headers={"Authorization": "Bearer pretend"}
        )
        assert r2.json() == {"status": "acked", "lifecycle": "upsert"}
        ref = a._conversations.get("conv-live")
        assert ref.last_inbound_activity_id == "user-act-1"  # not clobbered
        assert ref.raw.get("id") == "user-act-1"
        assert "conv-live" in a._seen_inbounds_this_lifetime

    def test_installation_remove_evicts_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        # Pre-seed as if the chat was captured earlier this lifetime.
        seed = _make_inbound(path="B", conv_id="conv-rm")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(seed))
        a._seen_inbounds_this_lifetime.add("conv-rm")
        client = self._client(a, monkeypatch)
        body = self._lifecycle_body(
            conv_id="conv-rm", type="installationUpdate", action="remove"
        )
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer pretend"}
        )
        assert r.json() == {"status": "acked", "lifecycle": "evict"}
        assert a._handled_events == []
        assert "conv-rm" not in a._conversations
        assert "conv-rm" not in a._seen_inbounds_this_lifetime

    def test_evict_tears_down_stream_and_coalesced_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # L3 (#105): uninstall must drop live stream / coalesced-reply /
        # coalesced-status slots for the chat AND cancel their watchdog tasks,
        # so no debounce later fires a doomed POST into the evicted chat. An
        # unrelated chat's state must survive.
        a = _make_adapter(monkeypatch)
        seed = _make_inbound(path="B", conv_id="conv-rm")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(seed))
        a._seen_inbounds_this_lifetime.add("conv-rm")

        a._active_stream_by_chat["conv-rm"] = "sid-1"
        a._streams["sid-1"] = {"bf_stream_id": "sid-1"}
        reply_task = MagicMock()
        a._active_coalesced_reply_by_chat["conv-rm"] = "mid-1"
        a._coalesced_replies["mid-1"] = {"content": "partial"}
        a._coalesced_reply_tasks["mid-1"] = reply_task
        status_task = MagicMock()
        a._coalesced_status["status:conv-rm:s1"] = {"chat_id": "conv-rm", "lines": []}
        a._coalesced_status_tasks["status:conv-rm:s1"] = status_task

        # Unrelated chat — must be untouched.
        other_task = MagicMock()
        a._active_coalesced_reply_by_chat["conv-other"] = "mid-other"
        a._coalesced_replies["mid-other"] = {"content": "keep"}
        a._coalesced_reply_tasks["mid-other"] = other_task

        client = self._client(a, monkeypatch)
        body = self._lifecycle_body(
            conv_id="conv-rm", type="installationUpdate", action="remove"
        )
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer pretend"}
        )
        assert r.json() == {"status": "acked", "lifecycle": "evict"}

        assert "conv-rm" not in a._active_stream_by_chat
        assert "sid-1" not in a._streams
        assert "conv-rm" not in a._active_coalesced_reply_by_chat
        assert "mid-1" not in a._coalesced_replies
        assert "mid-1" not in a._coalesced_reply_tasks
        reply_task.cancel.assert_called_once()
        assert "status:conv-rm:s1" not in a._coalesced_status
        assert "status:conv-rm:s1" not in a._coalesced_status_tasks
        status_task.cancel.assert_called_once()

        # Unrelated chat's coalesced state + watchdog survive.
        assert a._coalesced_replies["mid-other"] == {"content": "keep"}
        assert "conv-other" in a._active_coalesced_reply_by_chat
        other_task.cancel.assert_not_called()

    def test_seen_inbounds_set_stays_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # L2 (#105): the per-lifetime seen-set can't grow without limit on a
        # long-running gateway. With a low cap, posting more distinct chats
        # than the cap keeps the set bounded.
        monkeypatch.setattr(adapter_mod, "_MAX_SEEN_INBOUNDS", 3)
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        for i in range(8):
            body = _make_inbound(
                path="B", conv_id=f"conv-{i}", activity_id=f"act-{i}"
            )
            r = client.post(
                "/api/messages",
                json=body,
                headers={"Authorization": "Bearer pretend"},
            )
            assert r.status_code == 200, r.text
        assert len(a._seen_inbounds_this_lifetime) <= 3


class TestServeAppAgentsChannelFilter:
    """Route-level coverage for the slice 19q filter — same shape
    as ``test_conversation_update_acked_no_dispatch`` from 19n."""

    @staticmethod
    def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        from fastapi.testclient import TestClient

        # Isolated registry path — keeps tests from contaminating
        # ~/.hermes/agents/test-agent/ across runs.
        a = _make_adapter(
            monkeypatch,
            conversations_path=str(tmp_path / "convs.json"),
        )
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        a._http_client = MagicMock()
        return a, TestClient(a.build_app())

    def test_agents_event_acked_no_dispatch_no_registry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a, client = self._client(monkeypatch, tmp_path)
        body = {
            **_make_inbound(),
            "channelId": "agents",
            "type": "event",
            "name": "agentLifecycle",
        }
        r = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "acked"
        # No agent turn wasted on the synthetic event.
        assert a._handled_events == []
        # Registry semantics: synthetic events do NOT churn
        # `last_inbound_activity_id` — that field tracks user-replyable
        # messages only.
        assert len(a._conversations) == 0

    def test_agents_message_from_system_acked_no_dispatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a, client = self._client(monkeypatch, tmp_path)
        body = {
            **_make_inbound(),
            "channelId": "agents",
            "from": {"id": "system", "name": "System"},
        }
        r = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "acked"
        assert a._handled_events == []
        assert len(a._conversations) == 0

    def test_real_user_msteams_message_still_dispatches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Regression check for the happy path.
        a, client = self._client(monkeypatch, tmp_path)
        r = client.post(
            "/api/messages",
            json=_make_inbound(),
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "dispatched"
        assert len(a._handled_events) == 1
        assert "conv-1" in a._conversations


# ---------------------------------------------------------------------------
# Slice 19x-a (#4): _build_proactive_target_spec — pure registry read
# ---------------------------------------------------------------------------


class TestBuildProactiveTargetSpec:
    """Pure-function target-spec builder for cron-driven proactive sends."""

    def _seed_path_a_inbound(
        self,
        *,
        conv_id: str = "conv-proactive",
        service_url: str = "https://smba.trafficmanager.net/amer/x/",
        tenant_id: str = "11111111-2222-3333-4444-555555555555",
        agentic_app_id: str = "aa-app-id",
        agentic_user_id: str = "aa-user-id",
    ) -> dict[str, Any]:
        return {
            "type": "message",
            "id": "act-most-recent",
            "channelId": "msteams",
            "serviceUrl": service_url,
            "conversation": {
                "id": conv_id,
                "conversationType": "personal",
                "tenantId": tenant_id,
            },
            "from": {"id": "user-1", "name": "Sadiq"},
            "recipient": {
                "id": "agent-1",
                "name": "Inbox Helper",
                "tenantId": tenant_id,
                "agenticAppId": agentic_app_id,
                "agenticUserId": agentic_user_id,
            },
            "text": "hello",
        }

    def test_returns_none_when_chat_not_in_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        assert a._build_proactive_target_spec("never-seen") is None

    def test_returns_none_when_ref_has_no_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Registry entries can carry just metadata when persisted with
        # raw stripped — that's still un-routable for proactive.
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            ConversationRef(
                conversation_id="raw-stripped",
                service_url="https://smba.trafficmanager.net/",
                chat_type="personal",
                # raw deliberately empty
            )
        )
        assert a._build_proactive_target_spec("raw-stripped") is None

    def test_path_a_inbound_produces_complete_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        inbound = self._seed_path_a_inbound()
        a._conversations.upsert(ConversationRef.from_activity(inbound))

        spec = a._build_proactive_target_spec("conv-proactive")
        assert spec is not None
        assert spec["service_url"] == "https://smba.trafficmanager.net/amer/x/"
        assert spec["conversation_id"] == "conv-proactive"
        assert spec["channel_id"] == "msteams"
        assert spec["chat_type"] == "personal"
        assert spec["tenant_id"] == "11111111-2222-3333-4444-555555555555"
        assert spec["agentic_app_id"] == "aa-app-id"
        assert spec["agentic_user_id"] == "aa-user-id"
        assert spec["path"] == "A"
        # Outbound sender = inbound recipient (the agentic user).
        assert spec["from"]["id"] == "agent-1"
        assert spec["from"]["agenticAppId"] == "aa-app-id"
        # Outbound recipient = inbound sender (the user we're posting to).
        assert spec["recipient"]["id"] == "user-1"
        assert spec["recipient"]["name"] == "Sadiq"

    def test_path_tag_b_when_agentic_fields_missing_but_bf_service_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#33 refined the tagger: a classic-BF-shaped inbound (no
        agentic ids + serviceUrl on the BF host-suffix allowlist) is
        now tagged ``"B"`` instead of ``"unknown"``, so the proactive
        send-side hits the BF S2S outbound branch via
        ``acquire_reply_token``."""
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        inbound = self._seed_path_a_inbound()
        inbound["recipient"].pop("agenticAppId")
        inbound["recipient"].pop("agenticUserId")
        # serviceUrl default = smba.trafficmanager.net → Path B
        a._conversations.upsert(ConversationRef.from_activity(inbound))

        spec = a._build_proactive_target_spec("conv-proactive")
        assert spec is not None
        assert spec["path"] == "B"
        assert spec["agentic_app_id"] == ""
        assert spec["agentic_user_id"] == ""

    def test_path_tag_b_when_only_one_agentic_field_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed inbound with only one of the two agentic fields
        is not classifiable as Path A. If the serviceUrl is BF-shaped
        we fall through to Path B (#33); the BF S2S outbound bearer
        doesn't depend on either agentic field, so this is a safer
        recovery than refusing the send."""
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        inbound = self._seed_path_a_inbound()
        inbound["recipient"].pop("agenticUserId")
        a._conversations.upsert(ConversationRef.from_activity(inbound))

        spec = a._build_proactive_target_spec("conv-proactive")
        assert spec is not None
        assert spec["path"] == "B"

    def test_path_tag_unknown_when_agentic_missing_and_non_bf_service_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the serviceUrl host isn't on the BF allowlist either
        (e.g. somebody's posted a forged inbound through a tunnel),
        the tagger refuses to classify — the dispatcher will then
        raise rather than guess. Belt-and-braces against an attacker
        who could otherwise steer outbound traffic by claiming an
        unknown serviceUrl."""
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        inbound = self._seed_path_a_inbound()
        inbound["recipient"].pop("agenticAppId")
        inbound["recipient"].pop("agenticUserId")
        inbound["serviceUrl"] = "https://attacker.example/"
        a._conversations.upsert(ConversationRef.from_activity(inbound))

        spec = a._build_proactive_target_spec("conv-proactive")
        assert spec is not None
        assert spec["path"] == "unknown"

    def test_tenant_id_falls_back_through_conversation_then_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        # Recipient lacks tenantId, conversation has it.
        inbound = self._seed_path_a_inbound()
        inbound["recipient"].pop("tenantId")
        # Keep conversation.tenantId.
        a._conversations.upsert(ConversationRef.from_activity(inbound))
        spec = a._build_proactive_target_spec("conv-proactive")
        assert spec is not None
        assert spec["tenant_id"] == "11111111-2222-3333-4444-555555555555"

    def test_channel_id_default_when_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        inbound = self._seed_path_a_inbound()
        inbound.pop("channelId")
        a._conversations.upsert(ConversationRef.from_activity(inbound))
        spec = a._build_proactive_target_spec("conv-proactive")
        assert spec is not None
        assert spec["channel_id"] == "msteams"

    def test_chat_type_propagated_from_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        inbound = self._seed_path_a_inbound()
        inbound["conversation"]["conversationType"] = "groupChat"
        a._conversations.upsert(ConversationRef.from_activity(inbound))
        spec = a._build_proactive_target_spec("conv-proactive")
        assert spec is not None
        assert spec["chat_type"] == "groupChat"

    def test_handles_non_dict_recipient_and_from_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: malformed cached inbound where recipient/from
        # aren't dicts. Should still return a spec (empty dicts), not
        # crash.
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        ref = ConversationRef(
            conversation_id="malformed",
            service_url="https://x/",
            chat_type="personal",
            raw={
                "conversation": {"id": "malformed"},
                "from": "not-a-dict",
                "recipient": ["also", "not", "a", "dict"],
            },
        )
        a._conversations.upsert(ref)
        spec = a._build_proactive_target_spec("malformed")
        assert spec is not None
        assert spec["from"] == {}
        assert spec["recipient"] == {}
        assert spec["path"] == "unknown"

    def test_does_not_mutate_registry_or_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pure function — caller can't observe state changes.
        import copy as _copy

        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        inbound = self._seed_path_a_inbound()
        a._conversations.upsert(ConversationRef.from_activity(inbound))
        snapshot = _copy.deepcopy(a._conversations.get("conv-proactive"))
        _ = a._build_proactive_target_spec("conv-proactive")
        after = a._conversations.get("conv-proactive")
        assert after.to_dict() == snapshot.to_dict()


# ---------------------------------------------------------------------------
# send() — outbound via cached inbound + send_reply
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_send_with_no_cached_inbound_and_no_registry_entry_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Slice 19x-b: send() now falls through to _send_proactive when
        # there's no cached inbound. With no registry entry either, the
        # proactive path surfaces a clear "no registry entry" failure.
        a = _make_adapter(monkeypatch)
        result = await a.send(chat_id="missing", content="hi")
        assert result.success is False
        assert "no registry entry" in (result.error or "")

    @pytest.mark.asyncio
    async def test_send_with_cached_inbound_invokes_send_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        # Slice 19x-e (#27): production fills this set on inbound capture.
        a._seen_inbounds_this_lifetime.add("conv-1")
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
    async def test_send_binds_validated_path_not_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # L4 (#100, #106 review) — a BF-validated (Path B) inbound whose BODY
        # carries injected agentic ids (which would body-derive to Path A) must
        # mint via the validated path captured on the ConversationRef, NOT the
        # body. The adapter threads ref.validated_path into send_reply.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-B")  # path="A": body has agentic ids
        ref = adapter_mod.ConversationRef.from_activity(inbound)
        ref.validated_path = "B"  # ...but the JWT validated as Path B.
        a._conversations.upsert(ref)
        a._seen_inbounds_this_lifetime.add("conv-B")
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        result = await a.send(chat_id="conv-B", content="hi")
        assert result.success is True
        # send_reply received the VALIDATED path "B", not the body-derived "A".
        assert send_reply_mock.await_args.kwargs["validated_path"] == "B"

    @pytest.mark.asyncio
    async def test_send_reply_failure_surfaces_in_send_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._seen_inbounds_this_lifetime.add("conv-1")
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

    @pytest.mark.parametrize("status_code", [403, 500])
    async def test_send_reply_http_failure_surfaces_in_send_result(
        self, monkeypatch: pytest.MonkeyPatch, status_code: int
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._seen_inbounds_this_lifetime.add("conv-1")
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        failure = bridge.ReplyPostError(
            status_code=status_code,
            url="https://smba.test/v3/conversations/conv-1/activities/act-1",
            body_excerpt="denied by connector",
        )
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(side_effect=failure))

        result = await a.send(chat_id="conv-1", content="x")
        assert result.success is False
        assert f"HTTP {status_code}" in (result.error or "")
        assert "denied by connector" in (result.error or "")


# ---------------------------------------------------------------------------
# Slice 19x-e (#27): send() gate — per-lifetime inbound tracking
# ---------------------------------------------------------------------------


class TestSendGate:
    """`send()` routes via proactive when this lifetime hasn't captured
    an inbound for chat_id, regardless of registry raw."""

    @pytest.mark.asyncio
    async def test_fresh_lifetime_with_registry_entry_routes_proactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates a gateway restart: the registry has the chat
        # (raw populated), but the lifetime set is empty.
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(
                {
                    "type": "message",
                    "id": "act-prior",
                    "channelId": "msteams",
                    "serviceUrl": "https://smba.trafficmanager.net/x/",
                    "conversation": {
                        "id": "c1",
                        "conversationType": "personal",
                        "tenantId": "t",
                    },
                    "from": {"id": "u"},
                    "recipient": {
                        "id": "a",
                        "agenticAppId": "aa",
                        "agenticUserId": "au",
                    },
                }
            )
        )
        # Critical: lifetime set is empty — like a fresh gateway boot.
        assert a._seen_inbounds_this_lifetime == set()

        # Confirm _cached_inbound_for returns the persisted raw —
        # under the old gate this would have routed cached-inbound.
        assert a._cached_inbound_for("c1") is not None

        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json = MagicMock(return_value={"id": "proactive-id"})
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(return_value=mock_resp)

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "acquire_outbound_token", AsyncMock(return_value="tok")
        )
        # send_reply must NOT fire — gate routes us through proactive.
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        result = await a.send(chat_id="c1", content="proactive ping")
        assert result.success is True
        assert result.message_id == "proactive-id"
        # Wire-shape confirmation: sendToConversation URL, no replyToId.
        url = a._http_client.post.await_args.args[0]
        assert url.endswith("/v3/conversations/c1/activities")
        body = a._http_client.post.await_args.kwargs["json"]
        assert "replyToId" not in body
        # The reply-path mock should never have been called.
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_proactive_send_carries_ai_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #73(a): proactive (sendToConversation) messages are AI-generated
        # content too.
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(
                {
                    "type": "message",
                    "id": "act-prior",
                    "channelId": "msteams",
                    "serviceUrl": "https://smba.trafficmanager.net/x/",
                    "conversation": {
                        "id": "c1",
                        "conversationType": "personal",
                        "tenantId": "t",
                    },
                    "from": {"id": "u"},
                    "recipient": {"id": "a", "agenticAppId": "aa", "agenticUserId": "au"},
                }
            )
        )
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json = MagicMock(return_value={"id": "pid"})
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(return_value=mock_resp)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "acquire_reply_token", AsyncMock(return_value=("tok", "A"))
        )

        result = await a.send(chat_id="c1", content="proactive ping")
        assert result.success is True
        body = a._http_client.post.await_args.kwargs["json"]
        assert "replyToId" not in body
        assert body["entities"][0]["additionalType"] == ["AIGeneratedContent"]

    @pytest.mark.asyncio
    async def test_inbound_capture_populates_lifetime_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Drive a real inbound through the FastAPI route and confirm
        # the lifetime set picks it up — the production capture point.
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

        assert a._seen_inbounds_this_lifetime == set()

        client = TestClient(a.build_app())
        client.post(
            "/api/messages",
            json=_make_inbound(conv_id="conv-Z", activity_id="act-Z"),
            headers={"Authorization": "Bearer pretend"},
        )
        assert "conv-Z" in a._seen_inbounds_this_lifetime

    @pytest.mark.asyncio
    async def test_after_inbound_capture_send_uses_cached_inbound_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Drive an inbound, then call send() for the same chat —
        # the lifetime set is populated so the gate routes
        # cached-inbound (replyToActivity).
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
            json=_make_inbound(conv_id="conv-Y", activity_id="act-Y"),
            headers={"Authorization": "Bearer pretend"},
        )
        assert "conv-Y" in a._seen_inbounds_this_lifetime

        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        # acquire_outbound_token would be called by the proactive path;
        # if the gate is wrong and we go proactive, this mock catches it.
        proactive_token_mock = AsyncMock(return_value="should-not-fire")
        monkeypatch.setattr(
            bridge, "acquire_outbound_token", proactive_token_mock
        )

        result = await a.send(chat_id="conv-Y", content="reply")
        assert result.success is True
        # Cached-inbound path fires send_reply, NOT acquire_outbound_token.
        assert send_reply_mock.await_count == 1
        assert proactive_token_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_lifetime_set_is_per_adapter_not_persisted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Persist a registry entry to disk, construct a fresh adapter
        # against the same conversations_path — the new adapter's
        # lifetime set is empty. This is what a gateway restart looks
        # like.
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "convs.json"
        seed = ConversationRegistry()
        seed.upsert(
            ConversationRef.from_activity(_make_inbound(conv_id="conv-survive"))
        )
        seed.save(conv_path)

        # First adapter — pretend the inbound was processed in a
        # prior lifetime.
        a1 = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        a1._seen_inbounds_this_lifetime.add("conv-survive")
        # ... gateway restart simulated by constructing a fresh adapter
        a2 = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        # Registry has the entry from disk.
        assert a2._conversations.get("conv-survive") is not None
        # But the lifetime set starts empty.
        assert a2._seen_inbounds_this_lifetime == set()


# ---------------------------------------------------------------------------
# Slice 19x-b (#4): proactive send via target spec (sendToConversation)
# ---------------------------------------------------------------------------


class TestSendProactive:
    """send() falls through to _send_proactive when no cached inbound."""

    def _seed_registry_path_a(
        self, adapter, *, conv_id: str = "conv-proactive"
    ) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        adapter._conversations.upsert(
            ConversationRef.from_activity(
                {
                    "type": "message",
                    "id": "act-most-recent",
                    "channelId": "msteams",
                    "serviceUrl": "https://smba.trafficmanager.net/amer/x/",
                    "conversation": {
                        "id": conv_id,
                        "conversationType": "personal",
                        "tenantId": "11111111-2222-3333-4444-555555555555",
                    },
                    "from": {"id": "user-1", "name": "Sadiq"},
                    "recipient": {
                        "id": "agent-1",
                        "name": "Inbox Helper",
                        "tenantId": "11111111-2222-3333-4444-555555555555",
                        "agenticAppId": "aa-app-id",
                        "agenticUserId": "aa-user-id",
                    },
                    "text": "earlier message",
                }
            )
        )

    @pytest.mark.asyncio
    async def test_path_a_happy_posts_to_send_to_conversation_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._seed_registry_path_a(a)
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_outbound_token",
            AsyncMock(return_value="t1-bearer"),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json = MagicMock(return_value={"id": "new-activity-id"})
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(return_value=mock_resp)

        # Strip the cached inbound so send() falls through to proactive.
        ref = a._conversations.get("conv-proactive")
        ref.raw = {}  # registry has metadata but no usable raw -> proactive path
        # Re-upsert with the same metadata + populated raw so the target-spec
        # has the fields it needs.
        self._seed_registry_path_a(a)
        # Then null out the cached-inbound lookup by setting raw back to empty
        # — wait: _cached_inbound_for returns None when raw is falsy, but
        # _build_proactive_target_spec also requires raw. Both need a hit.
        # So we keep raw populated; to force the proactive path, monkeypatch
        # _cached_inbound_for to return None.
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _chat_id: None)

        result = await a.send(chat_id="conv-proactive", content="ping")

        assert result.success is True
        assert result.message_id == "new-activity-id"
        # POST went to sendToConversation URL (no /<activity_id> suffix).
        called_args = a._http_client.post.await_args
        url = called_args.args[0]
        assert url == (
            "https://smba.trafficmanager.net/amer/x/v3/conversations/conv-proactive/activities"
        )
        # Bearer token from acquire_outbound_token used verbatim.
        assert called_args.kwargs["headers"]["Authorization"] == "Bearer t1-bearer"
        # Activity body has no replyToId (this is a proactive send, not a reply).
        body = called_args.kwargs["json"]
        assert "replyToId" not in body
        assert body["type"] == "message"
        assert body["text"] == "ping"
        # Outbound from = inbound recipient (the agentic identity).
        assert body["from"]["agenticAppId"] == "aa-app-id"
        # Outbound recipient = inbound from (the user).
        assert body["recipient"]["id"] == "user-1"

    @pytest.mark.asyncio
    async def test_path_unknown_returns_classification_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#33 retired the Path B-specific "deferred error referencing
        #16" message. The remaining unknown-path case is now
        genuinely unclassifiable: no agentic ids AND non-BF
        serviceUrl. The wrapper refuses to mint a token rather than
        guess at an audience."""
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        # Inbound without agentic ids AND with a non-BF serviceUrl
        # (so the path tagger emits "unknown" rather than "B").
        a._conversations.upsert(
            ConversationRef.from_activity(
                {
                    "type": "message",
                    "id": "act-most-recent",
                    "serviceUrl": "https://attacker.example/",
                    "conversation": {"id": "conv-unknown", "conversationType": "personal"},
                    "from": {"id": "user-1"},
                    "recipient": {"id": "bot-1"},
                }
            )
        )
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _chat_id: None)

        result = await a.send(chat_id="conv-unknown", content="ping")
        assert result.success is False
        assert "cannot classify" in (result.error or "").lower() or (
            "unknown" in (result.error or "").lower()
        )

    def _seed_registry_path_b(
        self, adapter, *, conv_id: str = "conv-proactive-b"
    ) -> None:
        """#33: a classic Bot Framework inbound shape — no agentic
        identifiers, serviceUrl on the BF host-suffix allowlist."""
        from hermes_a365.plugin.conversations import ConversationRef

        adapter._conversations.upsert(
            ConversationRef.from_activity(
                {
                    "type": "message",
                    "id": "act-most-recent",
                    "channelId": "msteams",
                    "serviceUrl": "https://smba.trafficmanager.net/emea/x/",
                    "conversation": {
                        "id": conv_id,
                        "conversationType": "personal",
                        "tenantId": "11111111-2222-3333-4444-555555555555",
                    },
                    "from": {"id": "user-bf", "name": "BF User"},
                    "recipient": {
                        "id": "bot-app-id",
                        "name": "Inbox Helper R8 CC",
                    },
                    "text": "earlier message from Copilot Chat",
                }
            )
        )

    @pytest.mark.asyncio
    async def test_path_b_happy_mints_bf_s2s_and_posts_to_send_to_conversation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#33 (slice 20e): a Path B proactive send mints a BF S2S
        bearer via the dispatcher, then POSTs the same
        ``sendToConversation`` URL Path A uses (only the bearer
        differs)."""
        a = _make_adapter(monkeypatch)
        self._seed_registry_path_b(a, conv_id="conv-pb")
        a._bridge_cfg = MagicMock()
        a._bridge_cfg.tenant_id = "tenant-b"
        a._bridge_cfg.blueprint_client_id = "blueprint-app-id"
        a._bridge_cfg.blueprint_client_secret = "sek"
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("bf-bearer", "B")),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json = MagicMock(return_value={"id": "new-bf-activity-id"})
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(return_value=mock_resp)

        # Force the proactive path.
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _chat_id: None)

        result = await a.send(chat_id="conv-pb", content="hi from cron")

        assert result.success is True
        assert result.message_id == "new-bf-activity-id"
        called = a._http_client.post.await_args
        # Same sendToConversation URL shape as Path A.
        assert called.args[0] == (
            "https://smba.trafficmanager.net/emea/x/v3/conversations/conv-pb/activities"
        )
        # Bearer comes from the BF S2S dispatcher path.
        assert called.kwargs["headers"]["Authorization"] == "Bearer bf-bearer"
        body = called.kwargs["json"]
        assert "replyToId" not in body
        assert body["text"] == "hi from cron"
        # Dispatcher was passed the synthetic activity with serviceUrl
        # so it could classify Path B.
        dispatcher_call = bridge.acquire_reply_token.await_args.kwargs
        assert (
            dispatcher_call["activity"]["serviceUrl"]
            == "https://smba.trafficmanager.net/emea/x/"
        )
        assert dispatcher_call["bf_cache"] is a._bf_token_cache

    @pytest.mark.asyncio
    async def test_token_mint_failure_surfaces_in_send_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._seed_registry_path_a(a)
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_outbound_token",
            AsyncMock(side_effect=RuntimeError("AADSTS70011")),
        )
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _chat_id: None)

        result = await a.send(chat_id="conv-proactive", content="ping")
        assert result.success is False
        assert "token" in (result.error or "")
        assert "AADSTS70011" in (result.error or "")
        # No POST attempted when token mint fails.
        assert a._http_client.post.await_count == 0

    @pytest.mark.asyncio
    async def test_post_non_2xx_surfaces_status_in_send_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._seed_registry_path_a(a)
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_outbound_token",
            AsyncMock(return_value="t1-bearer"),
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _chat_id: None)

        result = await a.send(chat_id="conv-proactive", content="ping")
        assert result.success is False
        assert "403" in (result.error or "")

    @pytest.mark.asyncio
    async def test_post_exception_surfaces_in_send_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._seed_registry_path_a(a)
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_outbound_token",
            AsyncMock(return_value="t1-bearer"),
        )
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(side_effect=ConnectionError("ECONNRESET"))
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _chat_id: None)

        result = await a.send(chat_id="conv-proactive", content="ping")
        assert result.success is False
        assert "post" in (result.error or "")
        assert "ECONNRESET" in (result.error or "")

    @pytest.mark.asyncio
    async def test_proactive_no_op_when_adapter_not_connected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._seed_registry_path_a(a)
        # http_client / bridge_cfg left as None — adapter not connected.
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _chat_id: None)

        result = await a.send(chat_id="conv-proactive", content="ping")
        assert result.success is False
        assert "not connected" in (result.error or "")

    @pytest.mark.asyncio
    async def test_empty_response_body_still_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BF connector sometimes returns 200 with empty body — the
        # server-side activity id may not be echoed back.
        a = _make_adapter(monkeypatch)
        self._seed_registry_path_a(a)
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_outbound_token",
            AsyncMock(return_value="t1-bearer"),
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(side_effect=ValueError("no body"))
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _chat_id: None)

        result = await a.send(chat_id="conv-proactive", content="ping")
        assert result.success is True
        assert result.message_id == ""


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

    @pytest.mark.parametrize("bad_raw", ["oops", ["a", "b"], 42, None, True])
    def test_from_dict_coerces_non_dict_raw_to_empty(self, bad_raw: Any) -> None:
        # M10 (#105): a corrupted / hand-edited conversations.json may carry a
        # non-dict `raw`. It must not round-trip as-is — downstream send/edit/
        # proactive paths call `raw.get(...)` and would AttributeError,
        # permanently breaking that conversation. Coerced to {}.
        ref = adapter_mod.ConversationRef.from_dict(
            {"conversation_id": "c", "service_url": "https://x/", "raw": bad_raw}
        )
        assert ref.raw == {}

    def test_corrupt_raw_survives_registry_load(self, tmp_path: Path) -> None:
        # M10 (#105) end-to-end: a persisted registry with a non-dict `raw`
        # loads cleanly and the entry is usable — ref.raw is {} so the
        # `raw.get(...)` calls that used to crash are safe.
        import json

        from hermes_a365.plugin.conversations import ConversationRegistry

        path = tmp_path / "convs.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "conversations": [
                        {
                            "conversation_id": "conv-corrupt",
                            "service_url": "https://x/",
                            "raw": "not-a-dict",
                        }
                    ],
                }
            )
        )
        reg = ConversationRegistry.load(path)
        ref = reg.get("conv-corrupt")
        assert ref is not None
        assert ref.raw == {}
        assert ref.raw.get("conversation") is None  # the call that used to crash


class TestConversationRegistry:
    def test_upsert_merges_and_preserves_existing_fields(self) -> None:
        from hermes_a365.plugin.conversations import (
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
        from hermes_a365.plugin.conversations import ConversationRegistry

        reg = ConversationRegistry.load(tmp_path / "nope.json")
        assert len(reg) == 0

    def test_load_unparseable_returns_empty(self, tmp_path: Path) -> None:
        from hermes_a365.plugin.conversations import ConversationRegistry

        path = tmp_path / "convs.json"
        path.write_text("not json {{{")
        reg = ConversationRegistry.load(path)
        assert len(reg) == 0

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        from hermes_a365.plugin.conversations import (
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
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(ConversationRef(conversation_id="x", service_url="https://x/"))
        path = tmp_path / "convs.json"
        reg.save(path)
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
        assert leftovers == []


# ---------------------------------------------------------------------------
# Slice 19x-c (#4): prune_old_entries + pin/unpin + mark_used
# ---------------------------------------------------------------------------


class TestPruneOldEntries:
    """ConversationRegistry pruning semantics — mirrors SessionStore.prune_old_entries."""

    def _reg_with(self, entries: list[dict]) -> Any:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        for e in entries:
            ref = ConversationRef(
                conversation_id=e["id"],
                service_url=e.get("service_url", f"https://{e['id']}/"),
                chat_type=e.get("chat_type", "personal"),
                last_used_at=e.get("last_used_at"),
                pinned=e.get("pinned", False),
            )
            # Bypass upsert's auto-stamp by inserting directly so tests
            # can pin specific timestamps (including None).
            reg._by_id[ref.conversation_id] = ref
        return reg

    def test_drops_stale_keeps_recent(self) -> None:
        reg = self._reg_with(
            [
                {"id": "stale", "last_used_at": 1000.0},
                {"id": "recent", "last_used_at": 999_000.0},
            ]
        )
        # now = 1_000_000; max_age 10 days -> cutoff = 1_000_000 - 864_000 = 136_000.
        # stale=1000 < 136_000 → drop.
        # recent=999_000 >= 136_000 → keep.
        dropped = reg.prune_old_entries(max_age_days=10, now=1_000_000.0)
        assert dropped == 1
        assert "stale" not in reg
        assert "recent" in reg

    def test_skip_active_session_keys(self) -> None:
        reg = self._reg_with(
            [
                {"id": "active-conv", "last_used_at": 1000.0},  # ancient + active
            ]
        )
        dropped = reg.prune_old_entries(
            max_age_days=10,
            active_session_keys={"active-conv"},
            now=1_000_000.0,
        )
        assert dropped == 0
        assert "active-conv" in reg

    def test_skip_pinned(self) -> None:
        reg = self._reg_with(
            [
                {"id": "ancient-pinned", "last_used_at": 1000.0, "pinned": True},
                {"id": "ancient-unpinned", "last_used_at": 1000.0, "pinned": False},
            ]
        )
        dropped = reg.prune_old_entries(max_age_days=10, now=1_000_000.0)
        assert dropped == 1
        assert "ancient-pinned" in reg
        assert "ancient-unpinned" not in reg

    def test_skip_when_last_used_at_is_none(self) -> None:
        # Defensive: schema-migrated entries without a timestamp shouldn't
        # be insta-dropped on the first prune.
        reg = self._reg_with(
            [
                {"id": "no-stamp", "last_used_at": None},
            ]
        )
        dropped = reg.prune_old_entries(max_age_days=10, now=1_000_000.0)
        assert dropped == 0
        assert "no-stamp" in reg

    def test_active_session_keys_none_is_treated_as_empty(self) -> None:
        reg = self._reg_with([{"id": "stale", "last_used_at": 1000.0}])
        dropped = reg.prune_old_entries(
            max_age_days=10, active_session_keys=None, now=1_000_000.0
        )
        assert dropped == 1

    def test_returns_count_of_dropped(self) -> None:
        reg = self._reg_with(
            [
                {"id": "s1", "last_used_at": 1000.0},
                {"id": "s2", "last_used_at": 1000.0},
                {"id": "s3", "last_used_at": 1000.0},
                {"id": "keep", "last_used_at": 999_000.0},
            ]
        )
        assert reg.prune_old_entries(max_age_days=10, now=1_000_000.0) == 3
        # Idempotent: re-running drops nothing.
        assert reg.prune_old_entries(max_age_days=10, now=1_000_000.0) == 0

    def test_max_age_zero_drops_everything_with_stamp(self) -> None:
        # Useful as a "drop all timestamped" knob; entries without a
        # stamp still survive (defensive default).
        reg = self._reg_with(
            [
                {"id": "a", "last_used_at": 999_999.99},
                {"id": "b", "last_used_at": None},
            ]
        )
        dropped = reg.prune_old_entries(max_age_days=0, now=1_000_000.0)
        assert dropped == 1
        assert "a" not in reg
        assert "b" in reg


class TestPinUnpin:
    def test_pin_marks_entry_and_returns_true(self) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(ConversationRef(conversation_id="c1", service_url="https://x/"))
        assert reg.pin("c1") is True
        assert reg.get("c1").pinned is True

    def test_pin_returns_false_for_unknown(self) -> None:
        from hermes_a365.plugin.conversations import ConversationRegistry

        reg = ConversationRegistry()
        assert reg.pin("nope") is False

    def test_unpin_clears_flag(self) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(
            ConversationRef(conversation_id="c1", service_url="https://x/", pinned=True)
        )
        assert reg.unpin("c1") is True
        assert reg.get("c1").pinned is False

    def test_pinned_survives_round_trip_through_disk(self, tmp_path: Path) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(ConversationRef(conversation_id="c1", service_url="https://x/"))
        reg.pin("c1")
        path = tmp_path / "convs.json"
        reg.save(path)
        reloaded = ConversationRegistry.load(path)
        assert reloaded.get("c1").pinned is True

    def test_old_payload_without_pinned_field_defaults_to_false(self) -> None:
        # Backward-compat: registries persisted before slice 19x-c had
        # no `pinned` / `last_used_at` keys. Load must tolerate that.
        from hermes_a365.plugin.conversations import ConversationRegistry

        old_payload = {
            "schema": 1,
            "conversations": [
                {
                    "conversation_id": "c1",
                    "service_url": "https://x/",
                    "chat_type": "personal",
                    "raw": {},
                    # No pinned, no last_used_at
                }
            ],
        }
        reg = ConversationRegistry.from_payload(old_payload)
        ref = reg.get("c1")
        assert ref is not None
        assert ref.pinned is False
        assert ref.last_used_at is None

    def test_upsert_preserves_existing_pinned_when_incoming_unpinned(self) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(
            ConversationRef(conversation_id="c1", service_url="https://x/", pinned=True)
        )
        # Re-upsert with pinned=False (default) — must NOT unpin.
        reg.upsert(
            ConversationRef(
                conversation_id="c1", service_url="https://x/", pinned=False
            )
        )
        assert reg.get("c1").pinned is True


class TestMarkUsedAndUpsertTimestamps:
    def test_upsert_sets_last_used_at_from_now_kwarg(self) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(
            ConversationRef(conversation_id="c1", service_url="https://x/"),
            now=42.0,
        )
        assert reg.get("c1").last_used_at == 42.0

    def test_upsert_merge_refreshes_last_used_at(self) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(
            ConversationRef(conversation_id="c1", service_url="https://x/"),
            now=100.0,
        )
        reg.upsert(
            ConversationRef(conversation_id="c1", service_url="https://x/"),
            now=200.0,
        )
        assert reg.get("c1").last_used_at == 200.0

    def test_mark_used_bumps_timestamp_without_other_changes(self) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(
            ConversationRef(
                conversation_id="c1",
                service_url="https://x/",
                chat_name="original",
            ),
            now=100.0,
        )
        result = reg.mark_used("c1", now=500.0)
        ref = reg.get("c1")
        assert result is True
        assert ref.last_used_at == 500.0
        assert ref.chat_name == "original"

    def test_mark_used_returns_false_for_unknown(self) -> None:
        from hermes_a365.plugin.conversations import ConversationRegistry

        reg = ConversationRegistry()
        assert reg.mark_used("nope") is False

    def test_last_used_at_round_trips_through_disk(self, tmp_path: Path) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        reg.upsert(
            ConversationRef(conversation_id="c1", service_url="https://x/"),
            now=12345.6789,
        )
        path = tmp_path / "convs.json"
        reg.save(path)
        reloaded = ConversationRegistry.load(path)
        assert reloaded.get("c1").last_used_at == 12345.6789


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
        from hermes_a365.plugin.conversations import ConversationRegistry

        reloaded = ConversationRegistry.load(conv_path)
        ref = reloaded.get("conv-D")
        assert ref is not None
        assert ref.last_inbound_activity_id == "act-Z"

    def test_constructor_loads_existing_registry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hermes_a365.plugin.conversations import (
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

    @pytest.mark.asyncio
    async def test_active_stream_blocks_separate_image_activity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-1")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._active_stream_by_chat["conv-1"] = "m1"
        a._streams["m1"] = {
            "bf_stream_id": "bf-1",
            "sequence": 1,
            "last_emit_ts": 0.0,
        }
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        result = await a.send_image("conv-1", "https://example.test/x.png")
        assert result.success is False
        assert "active stream" in (result.error or "")
        assert send_reply_mock.await_count == 0
        assert a._http_client.post.await_count == 0

    @pytest.mark.parametrize("status_code", [401, 503])
    async def test_reply_http_failure_surfaces_in_send_result(
        self, monkeypatch: pytest.MonkeyPatch, status_code: int
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
        failure = bridge.ReplyPostError(
            status_code=status_code,
            url="https://smba.test/v3/conversations/conv-1/activities/act-1",
            body_excerpt="connector said no",
        )
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(side_effect=failure))

        result = await a.send_image("conv-1", "https://example.test/x.png")
        assert result.success is False
        assert f"HTTP {status_code}" in (result.error or "")
        assert "connector said no" in (result.error or "")


# ---------------------------------------------------------------------------
# Slice 19x-a — `hermes a365 <verb>` CLI surface via plugin
# ---------------------------------------------------------------------------


cli_mod = importlib.import_module("hermes_a365.plugin.cli")


def _build_a365_parser():
    """Build a top-level parser with `register_cli` attached as the
    `a365` subparser. Mirrors what the Hermes harness does at load
    time when the plugin's `register_cli_command` callback fires."""
    import argparse

    parent = argparse.ArgumentParser(prog="hermes")
    subs = parent.add_subparsers(dest="cmd")
    a365_p = subs.add_parser("a365")
    cli_mod.register_cli(a365_p)
    return parent


class TestEditMessage:
    """Slice 19s — BF streaming-response protocol via edit_message."""

    @staticmethod
    def _wire_adapter(
        a: Any,
        *,
        inbound: dict[str, Any],
        post_responses: list[Any] | Any | None = None,
    ) -> Any:
        """Register the inbound + stub the http client + token mint.

        ``post_responses`` may be a single response, a list (one per
        successive POST), or ``None`` (defaults to a 202 OK).
        """
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()  # #33: dispatcher needs a cache to pass through

        if post_responses is None:
            post_responses = MagicMock(status_code=202, text="", json=lambda: {})
        if not isinstance(post_responses, list):
            post_responses = [post_responses]

        post_mock = AsyncMock(side_effect=post_responses)
        a._http_client.post = post_mock
        return post_mock

    @staticmethod
    def _patch_token_mint(monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the dispatcher (#33). All five outbound surfaces in
        the adapter funnel through ``acquire_reply_token`` since #33,
        so a single monkeypatch covers everything that used to go
        directly to ``acquire_outbound_token`` (Path A) or
        ``acquire_bf_s2s_token`` (Path B)."""
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("bearer-test", "A")),
        )

    @staticmethod
    def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        """Replace asyncio.sleep with a recorder so throttle tests
        observe the requested duration without actually waiting."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", sleep_mock)
        return sleep_mock

    def test_class_sets_requires_edit_finalize(self) -> None:
        # endStream() is mandatory in BF streaming-ux; the flag tells
        # Hermes' stream consumer to route the final edit through even
        # if content didn't change.
        assert adapter_mod.Agent365Adapter.REQUIRES_EDIT_FINALIZE is True

    @pytest.mark.asyncio
    async def test_finalize_activity_carries_ai_label_with_streaminfo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #73(a): the finalized message activity carries BOTH the
        # streaminfo (final) entity AND the AI-generated-content label.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-S")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-stream-z"})
        ok = MagicMock(status_code=202, text="", json=lambda: {})
        post_mock = self._wire_adapter(a, inbound=inbound, post_responses=[first, ok])
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-S", "m1", "Hi", finalize=False)
        await a.edit_message("conv-S", "m1", "Hi, done.", finalize=True)

        final_body = post_mock.await_args.kwargs["json"]
        assert final_body["type"] == "message"
        types = [e["type"] for e in final_body["entities"]]
        assert "streaminfo" in types
        assert "https://schema.org/Message" in types
        streaminfo = next(e for e in final_body["entities"] if e["type"] == "streaminfo")
        assert streaminfo["streamType"] == "final"
        ai = next(
            e for e in final_body["entities"] if e["type"] == "https://schema.org/Message"
        )
        assert ai["additionalType"] == ["AIGeneratedContent"]

    @pytest.mark.asyncio
    async def test_intermediate_chunk_has_no_ai_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Intermediate (typing) chunks are NOT AI-content-labelled —
        # only the user-visible final message is.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-S")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-stream-y"})
        post_mock = self._wire_adapter(a, inbound=inbound, post_responses=first)
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-S", "m1", "Hi", finalize=False)
        body = post_mock.await_args.kwargs["json"]
        assert body["type"] == "typing"
        assert [e["type"] for e in body["entities"]] == ["streaminfo"]

    @pytest.mark.asyncio
    async def test_non_personal_reply_to_coalesces_until_finalize(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #54 branch walk: Copilot Chat/groupChat accepts BF streaming
        # activities but renders them silently. Keep the conversation on
        # normal send_reply, but emit only once when Hermes finalizes.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G")
        inbound["conversation"]["conversationType"] = "groupChat"
        post_mock = self._wire_adapter(a, inbound=inbound)

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        first = await a.send(
            chat_id="conv-G",
            content="Hello ▉",
            reply_to="act-1",
        )
        assert first.success is True
        assert str(first.message_id).startswith("coalesced:conv-G:")
        assert send_reply_mock.await_count == 0
        assert post_mock.await_count == 0
        assert a._coalesced_replies[first.message_id]["content"] == "Hello"

        progress = await a.send(
            chat_id="conv-G",
            content="interim progress",
            reply_to=None,
        )
        assert progress.success is True
        assert progress.message_id == first.message_id
        assert send_reply_mock.await_count == 0
        assert a._coalesced_replies[first.message_id]["content"] == "Hello"

        update = await a.edit_message(
            "conv-G",
            str(first.message_id),
            "Hello world ▉",
            finalize=False,
        )
        assert update.success is True
        assert send_reply_mock.await_count == 0
        assert a._coalesced_replies[first.message_id]["content"] == "Hello world"

        final = await a.edit_message(
            "conv-G",
            str(first.message_id),
            "Hello world!",
            finalize=True,
        )
        assert final.success is True
        assert send_reply_mock.await_count == 1
        kwargs = send_reply_mock.await_args.kwargs
        assert kwargs["reply"]["text"] == "Hello world!"
        assert first.message_id not in a._coalesced_replies
        assert "conv-G" not in a._active_coalesced_reply_by_chat

        duplicate = await a.edit_message(
            "conv-G",
            str(first.message_id),
            "Hello world!",
            finalize=True,
        )
        assert duplicate.success is True
        assert send_reply_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_stale_coalesced_reply_flushes_buffer_and_late_final_noops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-stale")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        first = await a.send(
            chat_id="conv-G-stale",
            content="Recovered reply ▉",
            reply_to="act-1",
        )
        message_id = str(first.message_id)
        state = a._coalesced_replies[message_id]
        loop_now = asyncio.get_event_loop().time()
        state["last_update_ts"] = (
            loop_now - adapter_mod._COALESCED_REPLY_FLUSH_AFTER_SEC - 1.0
        )

        flushed = await a._flush_stale_coalesced_reply(message_id)

        assert flushed is True
        assert send_reply_mock.await_count == 1
        kwargs = send_reply_mock.await_args.kwargs
        assert kwargs["reply"]["text"] == "Recovered reply"
        assert message_id not in a._coalesced_replies
        assert "conv-G-stale" not in a._active_coalesced_reply_by_chat
        assert message_id not in a._coalesced_reply_tasks
        assert message_id in a._recently_finalized

        late_final = await a.edit_message(
            "conv-G-stale",
            message_id,
            "Recovered reply",
            finalize=True,
        )
        assert late_final.success is True
        assert send_reply_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_coalesced_reply_watchdog_flushes_when_finalize_never_arrives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_COALESCED_REPLY_FLUSH_AFTER_SEC", 0.01)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-watch")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        first = await a.send(
            chat_id="conv-G-watch",
            content="Watchdog reply ▉",
            reply_to="act-1",
        )
        message_id = str(first.message_id)
        assert message_id in a._coalesced_reply_tasks

        await asyncio.sleep(0.05)

        assert send_reply_mock.await_count == 1
        kwargs = send_reply_mock.await_args.kwargs
        assert kwargs["reply"]["text"] == "Watchdog reply"
        assert message_id not in a._coalesced_replies
        assert "conv-G-watch" not in a._active_coalesced_reply_by_chat
        assert message_id not in a._coalesced_reply_tasks
        assert message_id in a._recently_finalized

    @pytest.mark.asyncio
    async def test_stale_coalesced_reply_flush_failure_logs_and_drops(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-fail")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(side_effect=RuntimeError("connector down"))
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        first = await a.send(
            chat_id="conv-G-fail",
            content="Will be dropped ▉",
            reply_to="act-1",
        )
        message_id = str(first.message_id)
        caplog.set_level("WARNING")

        flushed = await a._flush_stale_coalesced_reply(message_id)

        assert flushed is False
        assert send_reply_mock.await_count == 1
        assert message_id not in a._coalesced_replies
        assert "conv-G-fail" not in a._active_coalesced_reply_by_chat
        assert message_id not in a._coalesced_reply_tasks
        assert message_id not in a._recently_finalized
        assert any(
            "dropping stale coalesced reply after flush failure" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_first_call_starts_stream_with_sequence_one_no_streamid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-S")
        first_resp = MagicMock(
            status_code=201, text="",
            json=lambda: {"id": "bf-stream-abc"},
        )
        post_mock = self._wire_adapter(a, inbound=inbound, post_responses=first_resp)
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        r = await a.edit_message("conv-S", "hermes-msg-1", "Hi", finalize=False)
        assert r.success is True
        assert r.message_id == "bf-stream-abc"

        body = post_mock.await_args.kwargs["json"]
        assert body["type"] == "typing"  # intermediate
        assert body["text"] == "Hi"
        entity = body["entities"][0]
        assert entity["type"] == "streaminfo"
        assert entity["streamType"] == "streaming"
        assert entity["streamSequence"] == 1
        # First request must NOT include streamId.
        assert "streamId" not in entity
        # State now tracks the BF-side stream id.
        assert a._streams["hermes-msg-1"]["bf_stream_id"] == "bf-stream-abc"
        assert a._active_stream_by_chat["conv-S"] == "hermes-msg-1"

    @pytest.mark.asyncio
    async def test_subsequent_calls_include_streamid_and_monotonic_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-S")
        responses = [
            MagicMock(status_code=201, text="", json=lambda: {"id": "bf-stream-xyz"}),
            MagicMock(status_code=202, text="", json=lambda: {}),
            MagicMock(status_code=202, text="", json=lambda: {}),
        ]
        post_mock = self._wire_adapter(a, inbound=inbound, post_responses=responses)
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-S", "m1", "A", finalize=False)
        await a.edit_message("conv-S", "m1", "A B", finalize=False)
        r3 = await a.edit_message("conv-S", "m1", "A B C", finalize=False)

        assert r3.success is True
        assert post_mock.await_count == 3
        # Sequence 2 and 3 carry the captured streamId.
        body2 = post_mock.await_args_list[1].kwargs["json"]
        body3 = post_mock.await_args_list[2].kwargs["json"]
        assert body2["entities"][0]["streamId"] == "bf-stream-xyz"
        assert body2["entities"][0]["streamSequence"] == 2
        assert body3["entities"][0]["streamId"] == "bf-stream-xyz"
        assert body3["entities"][0]["streamSequence"] == 3

    @pytest.mark.asyncio
    async def test_finalize_swaps_type_to_message_and_omits_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-F")
        responses = [
            MagicMock(status_code=201, text="", json=lambda: {"id": "bf-fin"}),
            MagicMock(status_code=202, text="", json=lambda: {}),
        ]
        post_mock = self._wire_adapter(a, inbound=inbound, post_responses=responses)
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-F", "m1", "Hi", finalize=False)
        await a.edit_message("conv-F", "m1", "Hi, done.", finalize=True)

        final_body = post_mock.await_args_list[1].kwargs["json"]
        # Final activity: type=message (NOT typing).
        assert final_body["type"] == "message"
        entity = final_body["entities"][0]
        # streamType=final on the close.
        assert entity["streamType"] == "final"
        # streamSequence MUST NOT be set on the final activity per
        # Microsoft's REST API spec.
        assert "streamSequence" not in entity
        # streamId carries through.
        assert entity["streamId"] == "bf-fin"
        # State is dropped after finalize=True so a future stream on
        # the same message_id starts cleanly.
        assert "m1" not in a._streams
        assert "conv-F" not in a._active_stream_by_chat

    @pytest.mark.asyncio
    async def test_new_message_id_continues_active_stream_instead_of_starting_second(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #54: Hermes can segment a turn and call edit_message with a
        # fresh message_id before the prior stream has finalized. Copilot
        # Chat requires one stream per turn, so continue the active stream
        # rather than opening another 201-created sequence.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-CC")
        responses = [
            MagicMock(status_code=201, text="", json=lambda: {"id": "bf-cc"}),
            MagicMock(status_code=202, text="", json=lambda: {}),
            MagicMock(status_code=202, text="", json=lambda: {}),
        ]
        post_mock = self._wire_adapter(a, inbound=inbound, post_responses=responses)
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        r1 = await a.edit_message("conv-CC", "m1", "A", finalize=False)
        r2 = await a.edit_message("conv-CC", "m2", "A B", finalize=False)
        r3 = await a.edit_message("conv-CC", "m2", "A B C", finalize=True)

        assert r1.success and r2.success and r3.success
        assert post_mock.await_count == 3
        body2 = post_mock.await_args_list[1].kwargs["json"]
        body3 = post_mock.await_args_list[2].kwargs["json"]
        assert body2["entities"][0]["streamId"] == "bf-cc"
        assert body2["entities"][0]["streamSequence"] == 2
        assert body3["entities"][0]["streamId"] == "bf-cc"
        assert body3["entities"][0]["streamType"] == "final"
        # The second message id never opened its own stream slot.
        assert "m2" not in a._streams
        assert "conv-CC" not in a._active_stream_by_chat

    @pytest.mark.asyncio
    async def test_no_inbound_returns_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        r = await a.edit_message("missing-conv", "m1", "x")
        assert r.success is False
        assert "no cached inbound" in (r.error or "")

    @pytest.mark.asyncio
    async def test_disconnected_adapter_returns_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        # _http_client / _bridge_cfg deliberately left None.
        r = await a.edit_message("conv-1", "m1", "x")
        assert r.success is False
        assert "not connected" in (r.error or "")

    @pytest.mark.asyncio
    async def test_throttles_intermediate_chunks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-T")
        responses = [
            MagicMock(status_code=201, text="", json=lambda: {"id": "bf-t"}),
            MagicMock(status_code=202, text="", json=lambda: {}),
        ]
        self._wire_adapter(a, inbound=inbound, post_responses=responses)
        self._patch_token_mint(monkeypatch)
        sleep_mock = self._no_sleep(monkeypatch)

        # Two back-to-back edits.
        await a.edit_message("conv-T", "m1", "A", finalize=False)
        await a.edit_message("conv-T", "m1", "A B", finalize=False)

        # The throttle should have kicked in on the second call.
        # First call: state["last_emit_ts"] = 0.0, so no sleep.
        # Second call: state["last_emit_ts"] is recent → sleep close to MIN_GAP.
        sleeps = [c.args[0] for c in sleep_mock.await_args_list if c.args]
        # At least one sleep should be at or near the MIN_GAP threshold.
        assert any(
            0.0 < s <= adapter_mod._STREAMING_MIN_GAP_SEC + 0.01 for s in sleeps
        ), f"expected a throttle sleep, got {sleeps!r}"

    @pytest.mark.asyncio
    async def test_403_content_stream_timeout_returns_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Microsoft sends 403 ContentStreamNotAllowed with
        # "exceeded streaming time" after the 2-min cap.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-x"})
        timeout_resp = MagicMock(
            status_code=403,
            text="",
            json=lambda: {
                "error": {
                    "code": "ContentStreamNotAllowed",
                    "message": "Content stream finished due to exceeded streaming time.",
                }
            },
        )
        self._wire_adapter(a, inbound=inbound, post_responses=[first, timeout_resp])
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-X", "m1", "A", finalize=False)
        r = await a.edit_message("conv-X", "m1", "A B", finalize=False)
        assert r.success is False
        assert r.error == "streaming timeout"
        # State dropped on terminal 403.
        assert "m1" not in a._streams

    @pytest.mark.asyncio
    async def test_403_stop_button_returns_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-Y")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-y"})
        stop_resp = MagicMock(
            status_code=403,
            text="",
            json=lambda: {
                "error": {
                    "code": "ContentStreamNotAllowed",
                    "message": "Content stream was canceled by user.",
                }
            },
        )
        self._wire_adapter(a, inbound=inbound, post_responses=[first, stop_resp])
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-Y", "m1", "A")
        r = await a.edit_message("conv-Y", "m1", "A B")
        assert r.success is False
        assert r.error == "streaming canceled by user"

    @pytest.mark.asyncio
    async def test_429_returns_rate_limit_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-R")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-r"})
        rate_resp = MagicMock(status_code=429, text="", json=lambda: {})
        self._wire_adapter(a, inbound=inbound, post_responses=[first, rate_resp])
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-R", "m1", "A")
        r = await a.edit_message("conv-R", "m1", "A B")
        assert r.success is False
        assert "rate limited" in (r.error or "")

    @pytest.mark.asyncio
    async def test_202_sequence_order_failed_is_soft_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Out-of-order 202 ContentStreamSequenceOrderPreConditionFailed —
        # treated as soft success since the server keeps the most-recent
        # sequence anyway. We log + continue.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-O")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-o"})
        ooo = MagicMock(
            status_code=202,
            text="",
            json=lambda: {
                "error": {
                    "code": "ContentStreamSequenceOrderPreConditionFailed",
                    "message": "PreCondition failed.",
                }
            },
        )
        self._wire_adapter(a, inbound=inbound, post_responses=[first, ooo])
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-O", "m1", "A")
        r = await a.edit_message("conv-O", "m1", "A B")
        assert r.success is True

    @pytest.mark.asyncio
    async def test_first_201_without_id_is_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: if Microsoft returns 201 but no id (shouldn't
        # happen per spec, but the spec docs are sometimes wrong),
        # we surface a failure so Hermes falls back.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-N")
        bad_resp = MagicMock(status_code=201, text="", json=lambda: {})
        self._wire_adapter(a, inbound=inbound, post_responses=bad_resp)
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        r = await a.edit_message("conv-N", "m1", "x")
        assert r.success is False
        assert "no id" in (r.error or "").lower()
        # State cleaned up.
        assert "m1" not in a._streams

    @pytest.mark.asyncio
    async def test_activity_swaps_from_and_recipient_correctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Outbound: bot is the sender, user is the recipient — the
        # swap mirrors send_typing's pattern (slice 19o).
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-A")
        # Custom from/recipient values to verify the swap.
        inbound["from"] = {"id": "user-id-789", "name": "Alice"}
        inbound["recipient"] = {"id": "bot-id-123", "name": "InboxBot"}
        resp = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-a"})
        post_mock = self._wire_adapter(a, inbound=inbound, post_responses=resp)
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-A", "m1", "x")
        body = post_mock.await_args.kwargs["json"]
        assert body["from"]["id"] == "bot-id-123"
        assert body["recipient"]["id"] == "user-id-789"


class TestSendOrUpdateStatus:
    """#53 — gateway status/lifecycle callbacks routed through
    ``send_or_update_status``. Copilot Chat (groupChat) coalesces a burst
    of same-key status lines into one bubble; Teams 1:1 (personal) status
    passes straight through to ``send`` unchanged."""

    @staticmethod
    def _wire(a: Any, inbound: dict[str, Any]) -> None:
        """Register the inbound + stub the http/bridge plumbing the flush
        path needs (``_send_reply_activity`` POSTs through ``send_reply``)."""
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()

    @pytest.mark.asyncio
    async def test_personal_status_passes_through_to_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Path A: do not filter or coalesce — delegate to send() unchanged
        # (identical to the gateway's no-method plain-send fallback).
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-P")  # personal by default
        self._wire(a, inbound)
        sentinel = object()
        send_mock = AsyncMock(return_value=sentinel)
        monkeypatch.setattr(a, "send", send_mock)

        res = await a.send_or_update_status(
            "conv-P", "lifecycle", "⚠️ trying fallback", metadata={"thread_id": "t"}
        )
        assert res is sentinel
        send_mock.assert_awaited_once_with(
            "conv-P", "⚠️ trying fallback", metadata={"thread_id": "t"}
        )
        assert a._coalesced_status == {}

    @pytest.mark.asyncio
    async def test_unknown_chat_passes_through_to_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No cached inbound → mirror the gateway's plain-send fallback.
        a = _make_adapter(monkeypatch)
        sentinel = object()
        send_mock = AsyncMock(return_value=sentinel)
        monkeypatch.setattr(a, "send", send_mock)

        res = await a.send_or_update_status("conv-none", "lifecycle", "hi")
        assert res is sentinel
        send_mock.assert_awaited_once_with("conv-none", "hi", metadata=None)
        assert a._coalesced_status == {}

    @pytest.mark.asyncio
    async def test_groupchat_burst_coalesces_into_one_bubble(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The terminal-failure flush fires N lifecycle lines back-to-back.
        # Copilot Chat can't edit a bubble in place, so they buffer under
        # one key and the debounce watchdog emits a single combined bubble.
        monkeypatch.setattr(adapter_mod, "_STATUS_COALESCE_FLUSH_AFTER_SEC", 0.01)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-st")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        lines = [
            "⚠️ Non-retryable error (HTTP 403) — trying fallback...",
            "🔄 Primary model failed — switching to fallback: gpt-5.4",
            "❌ API failed after 3 retries — giving up.",
        ]
        results = [
            await a.send_or_update_status("conv-G-st", "lifecycle", line)
            for line in lines
        ]
        key = a._coalesced_status_key("conv-G-st", "lifecycle")
        # Buffered under one synthetic key; nothing sent during the burst.
        assert all(r.message_id == key for r in results)
        assert send_reply_mock.await_count == 0
        assert a._coalesced_status[key]["lines"] == lines

        await asyncio.sleep(0.05)  # let the debounce watchdog flush

        assert send_reply_mock.await_count == 1
        assert send_reply_mock.await_args.kwargs["reply"]["text"] == "\n".join(lines)
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

    @pytest.mark.asyncio
    async def test_groupchat_dedups_exact_repeat_of_last_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_STATUS_COALESCE_FLUSH_AFTER_SEC", 0.01)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-dup")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        await a.send_or_update_status("conv-G-dup", "lifecycle", "same line")
        await a.send_or_update_status("conv-G-dup", "lifecycle", "same line")
        await a.send_or_update_status("conv-G-dup", "lifecycle", "different")
        await asyncio.sleep(0.05)

        assert send_reply_mock.await_count == 1
        assert (
            send_reply_mock.await_args.kwargs["reply"]["text"]
            == "same line\ndifferent"
        )

    @pytest.mark.asyncio
    async def test_groupchat_status_suppressed_while_reply_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Never interleave a status bubble into an active turn (CEA ordering).
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-act")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        first = await a.send(
            chat_id="conv-G-act", content="partial ▉", reply_to="act-1"
        )
        assert "conv-G-act" in a._active_coalesced_reply_by_chat

        res = await a.send_or_update_status(
            "conv-G-act", "lifecycle", "⚠️ trying fallback"
        )
        # Suppressed: points at the active reply, never buffered as status,
        # never its own bubble.
        assert res.success is True
        assert res.message_id == first.message_id
        assert a._coalesced_status == {}
        assert send_reply_mock.await_count == 0

        # Finalize the reply so no watchdog lingers past the test.
        await a.edit_message(
            "conv-G-act", str(first.message_id), "partial done", finalize=True
        )

    @pytest.mark.asyncio
    async def test_groupchat_status_buffered_then_reply_opens_is_suppressed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reverse ordering of the suppress-while-active test: the status is
        # buffered BEFORE any turn exists, then a coalesced reply opens during
        # the debounce window. The entry guard cannot catch this (it only sees
        # calls that arrive after the turn opened), so the flush must re-check
        # active-turn state and suppress rather than interleave a stray status
        # bubble into the active turn.
        monkeypatch.setattr(adapter_mod, "_STATUS_COALESCE_FLUSH_AFTER_SEC", 0.01)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-buf")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        # 1) Status fires with no active turn → buffered, watchdog armed.
        res = await a.send_or_update_status(
            "conv-G-buf", "lifecycle", "⚠️ trying fallback"
        )
        key = a._coalesced_status_key("conv-G-buf", "lifecycle")
        assert res.message_id == key
        assert key in a._coalesced_status

        # 2) The turn's first reply chunk opens a coalesced reply for the chat.
        await a.send(chat_id="conv-G-buf", content="partial ▉", reply_to="act-1")
        assert "conv-G-buf" in a._active_coalesced_reply_by_chat
        send_reply_mock.reset_mock()

        # 3) The debounce watchdog fires — it must drop the buffered status,
        #    NOT POST it as its own bubble mid-turn.
        await asyncio.sleep(0.05)
        assert send_reply_mock.await_count == 0
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

        # Finalize the reply so no watchdog lingers past the test.
        await a.edit_message(
            "conv-G-buf",
            a._coalesced_reply_message_id("conv-G-buf", "act-1"),
            "partial done",
            finalize=True,
        )

    @pytest.mark.asyncio
    async def test_groupchat_line_appended_during_flush_is_not_lost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Append-during-flush race: a same-key status line arrives while the
        # flush is suspended inside the BF POST. The watchdog task is the one
        # running the flush, so _ensure_coalesced_status_task cannot re-arm.
        # The trailing line must still be delivered (status lines accumulate;
        # a dropped line is gone for good), not silently dropped.
        monkeypatch.setattr(adapter_mod, "_STATUS_COALESCE_FLUSH_AFTER_SEC", 0.01)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-race")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()

        sent_texts: list[str] = []
        release = asyncio.Event()
        in_send = asyncio.Event()

        async def blocking_send_reply(**kwargs: Any) -> None:
            sent_texts.append(kwargs["reply"]["text"])
            # Block the FIRST send mid-await so a second callback can land.
            if len(sent_texts) == 1:
                in_send.set()
                await release.wait()

        monkeypatch.setattr(bridge, "send_reply", AsyncMock(side_effect=blocking_send_reply))

        await a.send_or_update_status("conv-G-race", "lifecycle", "line-1")
        # Let the watchdog fire and suspend inside the (blocked) POST.
        await asyncio.wait_for(in_send.wait(), timeout=1.0)

        # A trailing same-key line lands while the flush is mid-await.
        res = await a.send_or_update_status(
            "conv-G-race", "lifecycle", "line-2-arrived-during-flush"
        )
        assert res.success is True

        # Unblock the in-flight POST; the flush should re-arm for the remainder.
        release.set()
        await asyncio.sleep(0.05)

        # Both lines delivered — line-1 in the first bubble, line-2 in a
        # second bubble emitted by the re-armed watchdog. Nothing lost.
        assert sent_texts == ["line-1", "line-2-arrived-during-flush"]
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

    @pytest.mark.asyncio
    async def test_empty_status_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-empty")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)

        res = await a.send_or_update_status("conv-G-empty", "lifecycle", "   ")
        assert res.success is True
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

    @pytest.mark.asyncio
    async def test_flush_while_disconnected_drops_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-disc")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        await a.send_or_update_status("conv-G-disc", "lifecycle", "noise")
        key = a._coalesced_status_key("conv-G-disc", "lifecycle")
        a._http_client = None  # simulate a disconnect before the flush fires

        ok = await a._flush_coalesced_status(key)
        assert ok is False
        assert send_reply_mock.await_count == 0
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

    @pytest.mark.asyncio
    async def test_groupchat_registry_only_falls_back_to_plain_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Post-restart / resume case (slice 19x-e / #27): the registry still
        # carries a cached inbound (persistent raw survives restarts) but the
        # chat was never seen *this lifetime* — a resumed turn is dispatched
        # from persisted origin, not through the webhook that populates
        # _seen_inbounds_this_lifetime. The coalesce flush would use
        # replyToActivity against a stale pre-restart activity_id (BF can
        # reject it, then the buffer is silently dropped). Must fall back to
        # plain send() so the robust _send_proactive / lifetime gate applies.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-resume")
        inbound["conversation"]["conversationType"] = "groupChat"
        # Wire the registry but DO NOT mark the chat as seen this lifetime.
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        assert "conv-G-resume" not in a._seen_inbounds_this_lifetime
        sentinel = object()
        send_mock = AsyncMock(return_value=sentinel)
        monkeypatch.setattr(a, "send", send_mock)

        res = await a.send_or_update_status(
            "conv-G-resume", "lifecycle", "❌ API failed after 3 retries"
        )
        assert res is sentinel
        send_mock.assert_awaited_once_with(
            "conv-G-resume", "❌ API failed after 3 retries", metadata=None
        )
        # Never buffered as coalesced status — it went straight through send().
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

    @pytest.mark.asyncio
    async def test_warn_key_passes_through_to_send_not_coalesced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "warn" is an always-substantive degraded-path notice ("the user
        # needs to know something important failed", run_agent.py:_emit_warning)
        # — never retry noise. Coalescing it risks the leading-notice silent
        # drop (a reply opens during the 2s debounce → flush-time active-turn
        # guard discards the buffer). Route it straight through send() so it
        # posts immediately (no buffer, no flush-time drop).
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-warn")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        sentinel = object()
        send_mock = AsyncMock(return_value=sentinel)
        monkeypatch.setattr(a, "send", send_mock)

        res = await a.send_or_update_status(
            "conv-G-warn", "warn", "⚠️ auxiliary compression failed"
        )
        assert res is sentinel
        send_mock.assert_awaited_once_with(
            "conv-G-warn", "⚠️ auxiliary compression failed", metadata=None
        )
        # Not buffered under a coalesce key — went straight to send().
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}


class TestSendStreamStart:
    """Slice 19s-bis: send() participates in the same BF stream as
    edit_message when in a streaming context (personal chat, no active
    stream for the conversation)."""

    @pytest.mark.asyncio
    async def test_send_starts_stream_in_personal_chat_with_no_active_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-S1")  # personal by default
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_outbound_token",
            AsyncMock(return_value="bearer-stream"),
        )
        # send_reply MUST NOT be called when the streaming path is taken.
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        # 201 with stream id → success.
        post_mock = AsyncMock(return_value=MagicMock(
            status_code=201, text="",
            json=lambda: {"id": "bf-stream-from-send"},
        ))
        a._http_client.post = post_mock

        result = await a.send(
            chat_id="conv-S1", content="Hello", reply_to="inbound-id-1",
        )
        assert result.success is True
        # The returned message_id is the BF stream id (Hermes will pass
        # this to subsequent edit_message calls).
        assert result.message_id == "bf-stream-from-send"
        # Activity shape: typing + streaminfo + streamSequence:1 + no streamId.
        assert post_mock.await_count == 1
        body = post_mock.await_args.kwargs["json"]
        assert body["type"] == "typing"
        assert body["text"] == "Hello"
        entity = body["entities"][0]
        assert entity["type"] == "streaminfo"
        assert entity["streamType"] == "streaming"
        assert entity["streamSequence"] == 1
        assert "streamId" not in entity
        # State registered for both lookup paths.
        assert "bf-stream-from-send" in a._streams
        assert a._active_stream_by_chat["conv-S1"] == "bf-stream-from-send"
        # send_reply NOT called — we took the streaming path.
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_subsequent_edit_message_continues_the_send_started_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The full streaming flow: send() opens the stream, edit_message
        # continues it without starting a new stream. Single growing bubble.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-S2")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "acquire_outbound_token",
            AsyncMock(return_value="bearer-stream"),
        )
        # send_reply must NOT be called.
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        responses = [
            MagicMock(status_code=201, text="", json=lambda: {"id": "bf-S2"}),
            MagicMock(status_code=202, text="", json=lambda: {}),
            MagicMock(status_code=202, text="", json=lambda: {}),
        ]
        post_mock = AsyncMock(side_effect=responses)
        a._http_client.post = post_mock
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        r1 = await a.send(chat_id="conv-S2", content="A", reply_to="inbound-id-1")
        r2 = await a.edit_message("conv-S2", r1.message_id, "A B", finalize=False)
        r3 = await a.edit_message("conv-S2", r1.message_id, "A B C", finalize=True)

        assert r1.success and r2.success and r3.success
        assert post_mock.await_count == 3
        # All three POSTs share the same streamId on entries 2+ and have
        # monotonic streamSequence on the non-final ones; final omits.
        body1 = post_mock.await_args_list[0].kwargs["json"]
        body2 = post_mock.await_args_list[1].kwargs["json"]
        body3 = post_mock.await_args_list[2].kwargs["json"]
        assert "streamId" not in body1["entities"][0]
        assert body1["entities"][0]["streamSequence"] == 1
        assert body2["entities"][0]["streamId"] == "bf-S2"
        assert body2["entities"][0]["streamSequence"] == 2
        assert body3["entities"][0]["streamId"] == "bf-S2"
        assert body3["entities"][0]["streamType"] == "final"
        assert body3["type"] == "message"  # type swap on final
        assert "streamSequence" not in body3["entities"][0]
        # State cleaned up after finalize.
        assert "bf-S2" not in a._streams
        assert "conv-S2" not in a._active_stream_by_chat
        # send_reply NEVER called — single growing bubble path.
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_send_with_no_reply_to_falls_back_to_non_streaming(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Slice 19s-bis correction: ``reply_to is None`` indicates
        # commentary / tool-progress / one-shot replies — none of which
        # are followed by ``edit_message``. Starting a stream for them
        # produces a typing-activity that never closes (stuck "thinking"
        # bubble). Only stream-consumer first-chunks pass
        # ``reply_to=event_message_id``.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-C")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        # The stream-start path's POST must NOT be reached.
        a._http_client.post = AsyncMock()

        result = await a.send(
            chat_id="conv-C", content="Using browser tool…", reply_to=None,
        )
        assert result.success is True
        assert send_reply_mock.await_count == 1
        # No stream registered; no streaming POST issued.
        assert "conv-C" not in a._active_stream_by_chat
        assert a._http_client.post.await_count == 0

    @pytest.mark.asyncio
    async def test_send_with_no_reply_to_suppresses_while_stream_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #54: commentary / tool-progress / fallback messages must not
        # interleave into an active CEA stream. Copilot Chat renders those
        # as separate bubbles, so we suppress them and let the stream
        # continue to its normal finalize=True close.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        # Pre-populate an active stream (the stale one).
        a._active_stream_by_chat["conv-X"] = "stale-stream"
        a._streams["stale-stream"] = {
            "bf_stream_id": "bf-stale-id",
            "sequence": 5,
            "last_emit_ts": 0.0,
        }

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("bearer-x", "A")),
        )
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        a._http_client.post = AsyncMock()

        result = await a.send(chat_id="conv-X", content="next segment", reply_to=None)
        assert result.success is True
        assert result.message_id == "stale-stream"
        assert a._http_client.post.await_count == 0
        assert send_reply_mock.await_count == 0
        assert "stale-stream" in a._streams
        assert a._active_stream_by_chat["conv-X"] == "stale-stream"

    @pytest.mark.asyncio
    async def test_new_stream_first_chunk_finalizes_prior_stream_before_starting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A new streaming first chunk may replace a stale stream, but only
        # after the adapter sends streamType=final for the previous one.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X2")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        a._active_stream_by_chat["conv-X2"] = "stale-stream"
        a._streams["stale-stream"] = {
            "bf_stream_id": "bf-stale-id",
            "sequence": 5,
            "last_emit_ts": 0.0,
            "last_content": "old content",
        }

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("bearer-x", "A")),
        )
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        a._http_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=202, text="", json=lambda: {}),
                MagicMock(status_code=201, text="", json=lambda: {"id": "bf-new"}),
            ]
        )

        result = await a.send(
            chat_id="conv-X2", content="new content", reply_to="inbound-id-1"
        )
        assert result.success is True
        assert result.message_id == "bf-new"
        assert a._http_client.post.await_count == 2
        final_body = a._http_client.post.await_args_list[0].kwargs["json"]
        start_body = a._http_client.post.await_args_list[1].kwargs["json"]
        assert final_body["type"] == "message"
        assert final_body["text"] == "old content"
        assert final_body["entities"][0]["streamId"] == "bf-stale-id"
        assert final_body["entities"][0]["streamType"] == "final"
        assert start_body["type"] == "typing"
        assert start_body["text"] == "new content"
        assert start_body["entities"][0]["streamSequence"] == 1
        assert "stale-stream" not in a._streams
        assert a._active_stream_by_chat["conv-X2"] == "bf-new"
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_new_stream_first_chunk_blocked_when_prior_finalize_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X3")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        a._active_stream_by_chat["conv-X3"] = "stale-stream"
        a._streams["stale-stream"] = {
            "bf_stream_id": "bf-stale-id",
            "sequence": 5,
            "last_emit_ts": 0.0,
            "last_content": "old content",
        }

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("bearer-x", "A")),
        )
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        a._http_client.post = AsyncMock(
            return_value=MagicMock(status_code=503, text="busy", json=lambda: {})
        )

        result = await a.send(
            chat_id="conv-X3", content="new content", reply_to="inbound-id-1"
        )
        assert result.success is False
        assert "active stream still open" in (result.error or "")
        assert a._http_client.post.await_count == 1
        assert "stale-stream" in a._streams
        assert a._active_stream_by_chat["conv-X3"] == "stale-stream"
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_repeated_stale_finalize_failure_force_drops_and_starts_new_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Liveness guard for #54 review feedback: a permanently dead BF
        # stream id must not wedge the chat forever.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X4")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        a._active_stream_by_chat["conv-X4"] = "stale-stream"
        a._streams["stale-stream"] = {
            "bf_stream_id": "bf-stale-id",
            "sequence": 5,
            "last_emit_ts": 0.0,
            "last_content": "old content",
        }

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("bearer-x", "A")),
        )
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        a._http_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=503, text="busy", json=lambda: {}),
                MagicMock(status_code=503, text="still busy", json=lambda: {}),
                MagicMock(status_code=201, text="", json=lambda: {"id": "bf-new"}),
            ]
        )

        first = await a.send(
            chat_id="conv-X4", content="new content", reply_to="inbound-id-1"
        )
        second = await a.send(
            chat_id="conv-X4", content="new content", reply_to="inbound-id-1"
        )

        assert first.success is False
        assert second.success is True
        assert second.message_id == "bf-new"
        assert a._http_client.post.await_count == 3
        assert "stale-stream" not in a._streams
        assert "stale-stream" in a._recently_finalized
        assert a._active_stream_by_chat["conv-X4"] == "bf-new"
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_expired_stale_stream_force_drops_on_first_finalize_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X5")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        loop_now = asyncio.get_event_loop().time()
        a._active_stream_by_chat["conv-X5"] = "stale-stream"
        a._streams["stale-stream"] = {
            "bf_stream_id": "bf-stale-id",
            "sequence": 5,
            "last_emit_ts": 0.0,
            "opened_ts": loop_now - adapter_mod._STREAMING_FORCE_DROP_AFTER_SEC - 1.0,
            "last_content": "old content",
        }

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("bearer-x", "A")),
        )
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
        a._http_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=503, text="expired", json=lambda: {}),
                MagicMock(status_code=201, text="", json=lambda: {"id": "bf-new"}),
            ]
        )

        result = await a.send(
            chat_id="conv-X5", content="new content", reply_to="inbound-id-1"
        )
        assert result.success is True
        assert result.message_id == "bf-new"
        assert a._http_client.post.await_count == 2
        assert "stale-stream" not in a._streams
        assert a._active_stream_by_chat["conv-X5"] == "bf-new"

    @pytest.mark.asyncio
    async def test_send_falls_back_to_non_streaming_when_chat_is_not_personal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Group/channel chats: never stream (BF streaming is DM-only).
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G")
        inbound["conversation"]["conversationType"] = "groupChat"
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add("conv-G")  # slice 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        # Stream-start POST shouldn't fire at all; this AsyncMock catches it
        # if our gate is wrong.
        a._http_client.post = AsyncMock()

        result = await a.send(chat_id="conv-G", content="hi group")
        assert result.success is True
        assert send_reply_mock.await_count == 1
        # No active stream registered for the group chat.
        assert "conv-G" not in a._active_stream_by_chat
        # No direct POST to _send_stream_start.
        assert a._http_client.post.await_count == 0

    @pytest.mark.asyncio
    async def test_send_falls_back_when_stream_start_returns_non_201(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stream start returns 4xx → fall through to non-streaming.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-F")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add("conv-F")  # slice 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "acquire_outbound_token",
            AsyncMock(return_value="bearer-fail"),
        )
        a._http_client.post = AsyncMock(return_value=MagicMock(
            status_code=503, text="upstream busy",
            json=lambda: {},
        ))
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        result = await a.send(chat_id="conv-F", content="hi")
        assert result.success is True
        # Non-streaming send_reply was called as fallback.
        assert send_reply_mock.await_count == 1
        # Active-stream slot stays empty so a retry can attempt streaming again.
        assert "conv-F" not in a._active_stream_by_chat

    @pytest.mark.asyncio
    async def test_send_falls_back_when_stream_start_returns_201_without_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: 201 with empty/missing id can't be used as streamId.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-N")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add("conv-N")  # slice 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "acquire_outbound_token",
            AsyncMock(return_value="bearer-x"),
        )
        a._http_client.post = AsyncMock(return_value=MagicMock(
            status_code=201, text="", json=lambda: {},
        ))
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        result = await a.send(chat_id="conv-N", content="hi")
        assert result.success is True
        assert send_reply_mock.await_count == 1
        assert "conv-N" not in a._active_stream_by_chat

    def test_drop_stream_state_clears_both_maps(self, monkeypatch) -> None:
        a = _make_adapter(monkeypatch)
        a._streams["m1"] = {"bf_stream_id": "m1", "sequence": 3, "last_emit_ts": 0.0}
        a._active_stream_by_chat["c1"] = "m1"
        a._drop_stream_state("c1", "m1")
        assert "m1" not in a._streams
        assert "c1" not in a._active_stream_by_chat

    def test_drop_stream_state_only_clears_chat_slot_when_id_matches(
        self, monkeypatch
    ) -> None:
        # Defensive: if a different stream is active in the chat slot,
        # don't clobber it.
        a = _make_adapter(monkeypatch)
        a._streams["m1"] = {"bf_stream_id": "m1", "sequence": 3, "last_emit_ts": 0.0}
        a._active_stream_by_chat["c1"] = "different-stream"
        a._drop_stream_state("c1", "m1")
        assert "m1" not in a._streams
        # Different stream wasn't cleared.
        assert a._active_stream_by_chat["c1"] == "different-stream"





class TestPluginRegisterCli:
    def test_register_calls_ctx_register_cli_command(self) -> None:
        ctx = _FakeCtx()
        agent365.register(ctx)
        # Both surfaces wired: platform adapter + CLI subcommand.
        assert len(ctx.platforms) == 1
        assert ctx.platforms[0]["name"] == "agent365"
        assert len(ctx.cli_commands) == 1
        cli = ctx.cli_commands[0]
        assert cli["name"] == "a365"
        assert callable(cli["setup_fn"])
        assert callable(cli["handler_fn"])
        assert cli["setup_fn"] is cli_mod.register_cli
        assert cli["handler_fn"] is cli_mod.a365_command


class TestRegisterCliParserShape:
    """`hermes a365 <verb> --help` must parse for every documented verb.

    Each script's `build_parser` is supposed to attach to the
    subparser we hand it; if any verb's wiring breaks, argparse will
    SystemExit with code 0 from --help (proving the parser was built)
    or 2 (proving the verb is missing). We catch SystemExit and
    inspect the code.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["a365", "doctor", "--help"],
            ["a365", "license", "--help"],
            ["a365", "register", "--help"],
            ["a365", "consent", "--help"],
            ["a365", "instance", "create", "--help"],
            ["a365", "publish", "--help"],
            ["a365", "status", "--help"],
            ["a365", "cleanup", "--help"],
            ["a365", "activity-bridge", "--help"],
            ["a365", "activity-bridge", "verify", "--help"],
            ["a365", "activity-bridge", "serve", "--help"],
            ["a365", "activity-bridge", "update-endpoint", "--help"],
        ],
    )
    def test_help_parses_for_each_verb(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        parser = _build_a365_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(argv)
        assert exc.value.code == 0
        out = capsys.readouterr().out
        # Each --help dump should at least mention `usage:`.
        assert "usage:" in out


class TestRegisterCliDispatch:
    """Spot-check that `hermes a365 <verb> ...` routes through to the
    matching script's `run` function with a Namespace shaped the way
    that script expects."""

    def test_doctor_dispatch_routes_to_doctor_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.doctor as _doctor

        captured: dict[str, Any] = {}

        def _fake_run(args):
            captured["args"] = args
            return 0

        monkeypatch.setattr(_doctor, "run", _fake_run)
        parser = _build_a365_parser()
        ns = parser.parse_args(["a365", "doctor", "--human"])
        rc = cli_mod.a365_command(ns)
        assert rc == 0
        assert captured["args"].human is True
        assert captured["args"].no_network is False

    def test_status_dispatch_carries_agent_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.status as _status

        captured: dict[str, Any] = {}

        def _fake_run(args):
            captured["args"] = args
            return 0

        monkeypatch.setattr(_status, "run", _fake_run)
        parser = _build_a365_parser()
        ns = parser.parse_args(["a365", "status", "inbox-helper", "--human"])
        rc = cli_mod.a365_command(ns)
        assert rc == 0
        assert captured["args"].agent_name == "inbox-helper"
        assert captured["args"].human is True

    def test_cleanup_dispatch_carries_required_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.cleanup as _cleanup

        captured: dict[str, Any] = {}

        def _fake_run(args):
            captured["args"] = args
            return 0

        monkeypatch.setattr(_cleanup, "run", _fake_run)
        parser = _build_a365_parser()
        ns = parser.parse_args(
            [
                "a365",
                "cleanup",
                "--agent-name",
                "foo",
                "--purge-orphans",
                "--orphan-instance-id",
                "11111111-1111-1111-1111-111111111111",
            ]
        )
        rc = cli_mod.a365_command(ns)
        assert rc == 0
        assert captured["args"].agent_name == "foo"
        assert captured["args"].purge_orphans is True
        assert captured["args"].orphan_instance_id == [
            "11111111-1111-1111-1111-111111111111"
        ]

    def test_register_dispatch_carries_apply_and_recover_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.register as _register

        captured: dict[str, Any] = {}

        def _fake_run(args):
            captured["args"] = args
            return 0

        monkeypatch.setattr(_register, "run", _fake_run)
        parser = _build_a365_parser()
        ns = parser.parse_args(
            [
                "a365",
                "register",
                "--agent-name",
                "Hermes Inbox Helper",
                "--apply",
                "--auto-recover-secret",
            ]
        )
        rc = cli_mod.a365_command(ns)
        assert rc == 0
        assert captured["args"].agent_name == "Hermes Inbox Helper"
        assert captured["args"].apply is True
        assert captured["args"].auto_recover_secret is True

    def test_instance_create_dispatch_routes_to_instance_create_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.instance_create as _instance_create

        captured: dict[str, Any] = {}

        def _fake_run(args):
            captured["args"] = args
            return 0

        monkeypatch.setattr(_instance_create, "run", _fake_run)
        parser = _build_a365_parser()
        ns = parser.parse_args(
            [
                "a365",
                "instance",
                "create",
                "inbox-helper",
                "--owner",
                "x@y.z",
                "--owner-aad-id",
                "11111111-1111-1111-1111-111111111111",
            ]
        )
        rc = cli_mod.a365_command(ns)
        assert rc == 0
        assert captured["args"].slug == "inbox-helper"
        assert captured["args"].owner == "x@y.z"

    def test_activity_bridge_verify_routes_to_bridge_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.activity_bridge as _activity_bridge

        captured: dict[str, Any] = {}

        def _fake_run(args):
            captured["args"] = args
            return 0

        monkeypatch.setattr(_activity_bridge, "run", _fake_run)
        parser = _build_a365_parser()
        ns = parser.parse_args(
            ["a365", "activity-bridge", "verify", "--slug", "inbox-helper"]
        )
        rc = cli_mod.a365_command(ns)
        assert rc == 0
        assert captured["args"].cmd == "verify"
        assert captured["args"].slug == "inbox-helper"

    def test_unknown_verb_returns_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No subcommand at all → usage + 2.
        ns_empty = type("NS", (), {})()
        rc = cli_mod.a365_command(ns_empty)  # type: ignore[arg-type]
        assert rc == 2
        out = capsys.readouterr().out
        assert "usage:" in out

    def test_instance_with_no_subcommand_returns_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parser = _build_a365_parser()
        ns = parser.parse_args(["a365", "instance"])
        rc = cli_mod.a365_command(ns)
        assert rc == 2
        assert "instance" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Slice 19x-d (#4): adapter lifecycle wiring — prune_conversations + mark_used
# ---------------------------------------------------------------------------


class TestConversationsPruneConfig:
    def test_default_max_age_is_30_days(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        assert a._conversations_prune_max_age_days == 30.0

    def test_extra_override_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        a = _make_adapter(monkeypatch, conversations_prune_max_age_days=7)
        assert a._conversations_prune_max_age_days == 7.0

    def test_extra_override_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        a = _make_adapter(monkeypatch, conversations_prune_max_age_days=0.5)
        assert a._conversations_prune_max_age_days == 0.5

    def test_extra_override_string_int(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # YAML may surface this as a string depending on quoting.
        a = _make_adapter(monkeypatch, conversations_prune_max_age_days="14")
        assert a._conversations_prune_max_age_days == 14.0

    def test_invalid_value_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(
            monkeypatch, conversations_prune_max_age_days="not-a-number"
        )
        assert a._conversations_prune_max_age_days == 30.0


class TestPruneConversations:
    @pytest.mark.asyncio
    async def test_invokes_registry_prune_with_active_session_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch, conversations_prune_max_age_days=10)
        # Seed both an active and an inactive entry, then mark one as
        # "active" via _active_sessions.
        a._conversations.upsert(
            ConversationRef(
                conversation_id="active-chat",
                service_url="https://x/",
                last_used_at=1000.0,  # ancient
            )
        )
        a._conversations.upsert(
            ConversationRef(
                conversation_id="stale-chat",
                service_url="https://x/",
                last_used_at=1000.0,  # ancient
            )
        )
        # Override last_used_at after upsert (which auto-stamps to now).
        a._conversations._by_id["active-chat"].last_used_at = 1000.0
        a._conversations._by_id["stale-chat"].last_used_at = 1000.0
        a._active_sessions["active-chat"] = asyncio.Event()

        # Patch registry.prune_old_entries to observe the args without
        # double-invoking the real prune. (Wrap rather than replace so
        # the actual logic still runs and we can assert outputs.)
        original = a._conversations.prune_old_entries
        captured: dict[str, Any] = {}

        def _spy(
            max_age_days: float, *, active_session_keys=None, now=None
        ) -> int:
            captured["max_age_days"] = max_age_days
            captured["active_session_keys"] = set(active_session_keys or [])
            captured["now"] = now
            return original(
                max_age_days,
                active_session_keys=active_session_keys,
                now=now,
            )

        a._conversations.prune_old_entries = _spy  # type: ignore[assignment]

        dropped = await a.prune_conversations()
        assert dropped == 1
        assert captured["max_age_days"] == 10.0
        assert captured["active_session_keys"] == {"active-chat"}

    @pytest.mark.asyncio
    async def test_saves_to_disk_when_anything_dropped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "convs.json"
        a = _make_adapter(
            monkeypatch,
            conversations_path=str(conv_path),
            conversations_prune_max_age_days=10,
        )
        a._conversations.upsert(
            ConversationRef(
                conversation_id="stale", service_url="https://x/"
            )
        )
        a._conversations._by_id["stale"].last_used_at = 1000.0  # ancient
        # Persist initial state so we can confirm the post-prune save.
        a._persist_conversations()

        dropped = await a.prune_conversations()
        assert dropped == 1
        # Round-trip from disk: the dropped entry isn't there.
        reloaded = ConversationRegistry.load(conv_path)
        assert "stale" not in reloaded

    @pytest.mark.asyncio
    async def test_does_not_save_when_nothing_dropped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        conv_path = tmp_path / "convs.json"
        a = _make_adapter(
            monkeypatch,
            conversations_path=str(conv_path),
            conversations_prune_max_age_days=30,
        )
        a._conversations.upsert(
            ConversationRef(conversation_id="fresh", service_url="https://x/")
        )
        # Don't seed an initial save -- if nothing drops, the prune
        # path should not write anything either.

        dropped = await a.prune_conversations()
        assert dropped == 0
        assert not conv_path.exists()

    @pytest.mark.asyncio
    async def test_empty_active_session_keys_when_no_active_sessions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Isolate from any leaked ~/.hermes/agents/test-agent/conversations.json
        # left by earlier sessions.
        a = _make_adapter(monkeypatch, conversations_path=str(tmp_path / "convs.json"))
        # No entries, nothing to drop, but the method should still run.
        assert await a.prune_conversations() == 0


class TestMarkUsedFromOutboundPaths:
    """Outbound paths bump last_used_at so prune respects send-active chats."""

    @pytest.mark.asyncio
    async def test_send_bumps_last_used_at_on_cached_inbound_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound()),
            now=100.0,
        )
        # Slice 19x-e (#27): tell the gate this lifetime has seen
        # an inbound for the chat — otherwise send() routes proactively.
        a._seen_inbounds_this_lifetime.add("conv-1")
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))

        before = a._conversations.get("conv-1").last_used_at
        await a.send(chat_id="conv-1", content="hi")
        after = a._conversations.get("conv-1").last_used_at
        assert after is not None
        assert before == 100.0
        assert after > before

    @pytest.mark.asyncio
    async def test_send_proactive_bumps_last_used_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        a = _make_adapter(monkeypatch)
        # Seed registry with Path A entry.
        a._conversations.upsert(
            ConversationRef.from_activity(
                {
                    "type": "message",
                    "id": "act-prior",
                    "channelId": "msteams",
                    "serviceUrl": "https://x/",
                    "conversation": {
                        "id": "c1",
                        "conversationType": "personal",
                        "tenantId": "t",
                    },
                    "from": {"id": "u"},
                    "recipient": {
                        "id": "a",
                        "agenticAppId": "aa",
                        "agenticUserId": "au",
                    },
                }
            ),
            now=100.0,
        )
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json = MagicMock(return_value={"id": "out-id"})
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(return_value=mock_resp)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "acquire_outbound_token", AsyncMock(return_value="tok")
        )
        # Force proactive path.
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _c: None)

        before = a._conversations.get("c1").last_used_at
        await a.send(chat_id="c1", content="hello")
        after = a._conversations.get("c1").last_used_at
        assert after is not None
        assert before == 100.0
        assert after > before

    @pytest.mark.asyncio
    async def test_send_typing_bumps_last_used_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound()),
            now=100.0,
        )
        # send_typing routes through _post_activity; stub it out so we
        # don't need a real http client.
        a._post_activity = AsyncMock(return_value=None)

        before = a._conversations.get("conv-1").last_used_at
        await a.send_typing(chat_id="conv-1")
        after = a._conversations.get("conv-1").last_used_at
        assert after > before

    @pytest.mark.asyncio
    async def test_send_image_bumps_last_used_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound()),
            now=100.0,
        )
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))

        before = a._conversations.get("conv-1").last_used_at
        await a.send_image(chat_id="conv-1", image_url="https://img/")
        after = a._conversations.get("conv-1").last_used_at
        assert after > before

    @pytest.mark.asyncio
    async def test_proactive_failure_no_registry_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the registry has no entry at all, mark_used is a no-op;
        # the proactive failure path returns cleanly without touching
        # anything that doesn't exist.
        a = _make_adapter(monkeypatch)
        result = await a.send(chat_id="never-seen", content="hi")
        assert result.success is False
        assert "no registry entry" in (result.error or "")


class TestActivityToEvent:
    """#78 — recipient @mention stripping in _activity_to_event.

    Shapes are taken from real captured raws (CC groupChat + Teams
    channel) in the v0.7.5 walk registry backup.
    """

    BOT_ID = "28:1c2b61bc-fa6a-4c7b-9656-a82b662dacfe"

    def _event(self, monkeypatch: pytest.MonkeyPatch, activity: dict[str, Any]) -> Any:
        return _make_adapter(monkeypatch)._activity_to_event(activity)

    def _channel_activity(self, *, text: str, entities: list[Any]) -> dict[str, Any]:
        return {
            "id": "act-1",
            "text": text,
            "from": {"id": "29:user", "name": "Sadiq"},
            "recipient": {"id": self.BOT_ID, "name": "hermes-inbox-helper-bot"},
            "conversation": {"id": "19:thread@thread.v2", "conversationType": "channel"},
            "entities": entities,
        }

    def _mention(self, text: str | None = None) -> dict[str, Any]:
        ent: dict[str, Any] = {
            "type": "mention",
            "mentioned": {"id": self.BOT_ID, "name": "Hermes Inbox Helper R8"},
        }
        if text is not None:
            ent["text"] = text
        return ent

    def test_teams_channel_mention_only_stripped_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real Teams-channel raw: text IS the mention markup; entity carries
        # the matching `text`; a clientInfo entity rides alongside.
        act = self._channel_activity(
            text="<at>Hermes Inbox Helper R8</at>",
            entities=[
                self._mention("<at>Hermes Inbox Helper R8</at>"),
                {"type": "clientInfo", "locale": "en-GB", "platform": "Mac"},
            ],
        )
        evt = self._event(monkeypatch, act)
        assert evt.text == ""
        # raw_message preserved verbatim (only event.text is cleaned).
        assert evt.raw_message["text"] == "<at>Hermes Inbox Helper R8</at>"
        assert evt.raw_message["entities"][0]["text"] == "<at>Hermes Inbox Helper R8</at>"

    def test_cc_groupchat_no_text_field_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real CC raw: text already clean; mention entity has NO `text` field.
        act = {
            "id": "act-2",
            "text": "Tell me about your runtime agent",
            "from": {"id": "29:user"},
            "recipient": {"id": self.BOT_ID, "name": "hermes-inbox-helper-bot"},
            "conversation": {"id": "19:x", "conversationType": "groupChat"},
            "entities": [self._mention()],
        }
        assert self._event(monkeypatch, act).text == "Tell me about your runtime agent"

    def test_mention_between_words_collapses_double_space(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        act = self._channel_activity(
            text="hi <at>Bot</at> there",
            entities=[self._mention("<at>Bot</at>")],
        )
        assert self._event(monkeypatch, act).text == "hi there"

    def test_mention_at_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        act = self._channel_activity(
            text="status please <at>Bot</at>",
            entities=[self._mention("<at>Bot</at>")],
        )
        assert self._event(monkeypatch, act).text == "status please"

    def test_non_recipient_mention_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = {
            "type": "mention",
            "mentioned": {"id": "29:someone-else", "name": "Alice"},
            "text": "<at>Alice</at>",
        }
        act = self._channel_activity(
            text="<at>Alice</at> ping <at>Bot</at>",
            entities=[other, self._mention("<at>Bot</at>")],
        )
        # Only the recipient mention is stripped; user-to-user mention stays.
        assert self._event(monkeypatch, act).text == "<at>Alice</at> ping"

    def test_multiple_recipient_mentions_all_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        act = self._channel_activity(
            text="<at>Bot</at> hi <at>Bot</at>",
            entities=[self._mention("<at>Bot</at>")],
        )
        assert self._event(monkeypatch, act).text == "hi"

    def test_multiline_body_not_reflowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        act = self._channel_activity(
            text="<at>Bot</at> line one\nline two",
            entities=[self._mention("<at>Bot</at>")],
        )
        assert self._event(monkeypatch, act).text == "line one\nline two"

    def test_no_entities_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        act = {
            "id": "a",
            "text": "plain dm message",
            "from": {"id": "29:u"},
            "recipient": {"id": self.BOT_ID},
            "conversation": {"id": "c", "conversationType": "personal"},
        }
        assert self._event(monkeypatch, act).text == "plain dm message"

    def test_recipient_missing_no_stripping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        act = {
            "id": "a",
            "text": "<at>Bot</at> hello",
            "from": {"id": "29:u"},
            "recipient": None,
            "conversation": {"id": "c", "conversationType": "channel"},
            "entities": [
                {"type": "mention", "mentioned": {"id": "x"}, "text": "<at>Bot</at>"}
            ],
        }
        # recipient_id == "" -> helper returns text unchanged.
        assert self._event(monkeypatch, act).text == "<at>Bot</at> hello"

    def test_interior_whitespace_preserved_on_removal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the mention seam is touched — deliberate interior spacing
        # elsewhere in the body survives (the collapse is not global).
        act = self._channel_activity(
            text="<at>Bot</at> keep   these   spaces",
            entities=[self._mention("<at>Bot</at>")],
        )
        assert self._event(monkeypatch, act).text == "keep   these   spaces"

    def test_mention_at_start_of_later_line_no_stray_space(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A mention beginning a continuation line must not leave a stray
        # leading space on that line, and newlines are preserved.
        act = self._channel_activity(
            text="line one\n<at>Bot</at> line two",
            entities=[self._mention("<at>Bot</at>")],
        )
        assert self._event(monkeypatch, act).text == "line one\nline two"

    def test_not_removed_path_is_byte_for_byte_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No recipient mention removed -> the tidy must not run, so even
        # messages with intentional double spaces / outer whitespace are
        # returned verbatim.
        act = {
            "id": "a",
            "text": "  keep   these   spaces  ",
            "from": {"id": "29:u"},
            "recipient": {"id": self.BOT_ID},
            "conversation": {"id": "c", "conversationType": "groupChat"},
            "entities": [self._mention()],  # no text field -> nothing removed
        }
        assert self._event(monkeypatch, act).text == "  keep   these   spaces  "


class TestInvokeRoute:
    """#18 / 19w-a — invoke activities are handled synchronously in the route,
    NOT dispatched to the fire-and-forget agent loop."""

    def _client(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"oid": "o1", "tid": "t1"}),
        )
        a._http_client = MagicMock()
        return a, TestClient(a.build_app())

    def _invoke_body(
        self, *, name: str = "task/fetch", value: Any = None, conv_id: str = "conv-I"
    ) -> dict[str, Any]:
        body = _make_inbound(conv_id=conv_id)
        body["type"] = "invoke"
        body["name"] = name
        body["value"] = {"commandId": "x"} if value is None else value
        return body

    def test_task_fetch_returns_sync_invoke_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a, client = self._client(monkeypatch)
        r = client.post(
            "/api/messages",
            json=self._invoke_body(),
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200, r.text
        # BF wire: the taskInfo is the top-level HTTP body (NOT a {status,body}
        # wrapper); the HTTP status carries the invoke status. (v0.8.0 walk fix.)
        assert r.json()["task"]["type"] == "continue"
        # Handled INLINE — never dispatched to the fire-and-forget agent loop.
        assert a._handled_events == []
        # No AI-generated content label on an invoke response.
        assert "AIGeneratedContent" not in r.text

    def test_unknown_invoke_name_is_501_not_dispatched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a, client = self._client(monkeypatch)
        r = client.post(
            "/api/messages",
            json=self._invoke_body(name="composeExtension/query"),
            headers={"Authorization": "Bearer pretend"},
        )
        # Unknown name -> HTTP 501 (BF "not implemented") with the error body.
        assert r.status_code == 501
        assert "error" in r.json()
        assert a._handled_events == []

    def test_non_dict_value_handled_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # task/fetch ignores value; a non-dict value must not 500.
        _a, client = self._client(monkeypatch)
        r = client.post(
            "/api/messages",
            json=self._invoke_body(value="not-a-dict"),
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200
        assert r.json()["task"]["type"] == "continue"

    def test_message_activity_still_dispatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: the invoke branch must not capture normal messages.
        a, client = self._client(monkeypatch)
        r = client.post(
            "/api/messages",
            json=_make_inbound(text="hi", conv_id="conv-M"),
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "dispatched"
        assert len(a._handled_events) == 1

    def test_deduped_invoke_retry_redispatches_not_duplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #96 — a BF retry of an invoke (same conversationId:activityId) must
        # re-render its taskInfo, NOT the {status:duplicate} dedupe marker (which
        # is not a valid invokeResponse body). The invoke branch is intercepted
        # BEFORE the idempotency dedupe; today's names (task/fetch) are local +
        # idempotent, so re-running on a retry is safe.
        a, client = self._client(monkeypatch)
        headers = {"Authorization": "Bearer pretend"}
        body = self._invoke_body()  # fixed activity id -> a repeat is a retry
        r1 = client.post("/api/messages", json=body, headers=headers)
        r2 = client.post("/api/messages", json=body, headers=headers)
        for r in (r1, r2):
            assert r.status_code == 200, r.text
            assert r.json()["task"]["type"] == "continue"
            assert r.json().get("status") != "duplicate"
        # Never dispatched to the fire-and-forget agent loop either.
        assert a._handled_events == []

    def test_handler_exception_returns_graceful_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A handler crash must degrade to a {status:500} invokeResponse, never
        # an unhandled HTTP 500.
        a, client = self._client(monkeypatch)

        async def boom(ctx: Any) -> Any:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(adapter_mod.invoke, "dispatch_invoke", boom)
        r = client.post(
            "/api/messages",
            json=self._invoke_body(),
            headers={"Authorization": "Bearer pretend"},
        )
        # Graceful degradation: a handler crash -> HTTP 500 with the error body,
        # never an unhandled exception.
        assert r.status_code == 500
        assert r.json() == {"error": "invoke handler error"}
        assert a._handled_events == []

    def test_claims_wired_into_invoke_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The load-bearing new behavior: the validated JWT claims (previously
        # discarded) feed InvokeContext identity.
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(
                return_value={
                    "oid": "claim-oid",
                    "tid": "claim-tid",
                    "preferred_username": "u@x",
                }
            ),
        )
        a._http_client = MagicMock()
        captured: dict[str, Any] = {}

        async def capture(ctx: Any, *, registry: Any = None) -> Any:
            captured["ctx"] = ctx
            captured["registry"] = registry
            return adapter_mod.invoke.InvokeResponse(200, {"ok": True})

        monkeypatch.setattr(adapter_mod.invoke, "dispatch_invoke", capture)
        client = TestClient(a.build_app())
        r = client.post(
            "/api/messages",
            json=self._invoke_body(),
            headers={"Authorization": "Bearer pretend"},
        )
        assert r.status_code == 200
        ctx = captured["ctx"]
        assert ctx.user_oid == "claim-oid"
        assert ctx.tenant_id == "claim-tid"
        assert ctx.user_upn == "u@x"


class _StreamCM:
    """Async-context-manager stand-in for ``httpx.AsyncClient.stream(...)`` — the
    streaming download path (R2-P1). ``async with client.stream(...) as resp``."""

    def __init__(self, resp: Any) -> None:
        self._resp = resp

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _stream_cm(content: bytes, *, status_code: int = 200, headers: dict | None = None) -> Any:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}

    async def _aiter() -> Any:
        yield content

    resp.aiter_bytes = _aiter
    return _StreamCM(resp)


class TestInboundMedia:
    """#76(a/b) — Teams inbound attachments downloaded into the media cache and
    surfaced on MessageEvent.media_urls / media_types."""

    def _adapter(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        a = _make_adapter(monkeypatch)
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        # Review-F1: real connector allowlist so the download URL validator runs.
        a._bridge_cfg.trusted_service_url_suffixes = (
            adapter_mod._import_bridge().DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES
        )
        # R2-P1: exact tenant host allowlist so inbound file downloads are allowed.
        a._file_host_allowlist = ("contoso.sharepoint.com",)
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        return a

    @staticmethod
    def _stream(
        a: Any, content: bytes, *, status_code: int = 200, headers: dict | None = None
    ) -> Any:
        """Wire the streaming download mock; return the stream MagicMock for
        call-arg assertions."""
        cm = _stream_cm(content, status_code=status_code, headers=headers)
        a._http_client.stream = MagicMock(return_value=cm)
        return a._http_client.stream

    @pytest.mark.asyncio
    async def test_no_attachments_is_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        out = await a._extract_inbound_media({"id": "act-1"}, validated_path="A")
        assert out == ([], [], adapter_mod.MessageType.TEXT)

    @pytest.mark.asyncio
    async def test_inbound_file_download_info_no_bearer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        stream = self._stream(a, b"PDFDATA")
        activity = {
            "id": "act:9",  # ':' must be sanitised out of the cache filename
            "attachments": [
                {
                    "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                    "name": "../../evil.pdf",  # user-controlled — must not reach the path
                    "content": {
                        "downloadUrl": "https://contoso.sharepoint.com/dl",
                        "fileType": "pdf",
                    },
                }
            ],
        }
        urls, types, mt = await a._extract_inbound_media(activity, validated_path="A")
        assert mt == adapter_mod.MessageType.DOCUMENT
        assert types == [adapter_mod._TEAMS_FILE_DOWNLOAD_INFO]
        p = Path(urls[0])
        assert p.read_bytes() == b"PDFDATA"
        assert p.suffix == ".pdf"
        # Path-traversal: the malicious name/id never escape the cache dir.
        assert p.parent == a._media_cache_dir()
        assert ".." not in p.name and "evil" not in p.name
        # Pre-authenticated downloadUrl fetched WITHOUT an auth header.
        assert not stream.call_args.kwargs.get("headers")

    @pytest.mark.asyncio
    async def test_inbound_image_downloads_with_reply_bearer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        stream = self._stream(a, b"PNGDATA")
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "acquire_reply_token", AsyncMock(return_value=("BEARER", "A"))
        )
        activity = {
            "id": "act-img",
            "attachments": [
                {"contentType": "image/png", "contentUrl": "https://smba.trafficmanager.net/att/1"}
            ],
        }
        urls, types, mt = await a._extract_inbound_media(activity, validated_path="A")
        assert mt == adapter_mod.MessageType.PHOTO
        assert types == ["image/png"]
        p = Path(urls[0])
        assert p.read_bytes() == b"PNGDATA"
        assert p.suffix == ".png"
        # contentUrl fetched WITH the reply bearer.
        assert (
            stream.call_args.kwargs["headers"]["Authorization"] == "Bearer BEARER"
        )

    @pytest.mark.asyncio
    async def test_oversized_media_dropped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        big = b"x" * (adapter_mod._MAX_INBOUND_MEDIA_BYTES + 1)
        self._stream(a, big)
        activity = {
            "id": "a",
            "attachments": [
                {
                    "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                    "content": {"downloadUrl": "https://contoso.sharepoint.com/x"},
                }
            ],
        }
        out = await a._extract_inbound_media(activity, validated_path="A")
        assert out == ([], [], adapter_mod.MessageType.TEXT)

    @pytest.mark.asyncio
    async def test_failed_download_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        a._http_client.stream = MagicMock(side_effect=RuntimeError("boom"))
        activity = {
            "id": "a",
            "attachments": [
                {
                    "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                    "content": {"downloadUrl": "https://contoso.sharepoint.com/x"},
                }
            ],
        }
        out = await a._extract_inbound_media(activity, validated_path="A")
        assert out == ([], [], adapter_mod.MessageType.TEXT)

    # ── Review-F1 / R2-P1: hostile download URLs rejected before any fetch ─

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://contoso.sharepoint.com/x",  # not https
            "https://evil.example.com/x",  # off-allowlist host
            "https://169.254.169.254/latest",  # link-local IP (SSRF)
            "https://127.0.0.1/x",  # loopback IP
            "https://sharepoint.com.attacker.net/x",  # suffix-spoof
            # R2-P1: a DIFFERENT tenant's SharePoint host (customer-registrable
            # zone) — rejected because the configured host is contoso.sharepoint.com.
            "https://attacker-tenant.sharepoint.com/x",
        ],
    )
    async def test_inbound_file_hostile_url_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_url: str
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        stream = self._stream(a, b"DATA")
        activity = {
            "id": "a",
            "attachments": [
                {
                    "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                    "content": {"downloadUrl": bad_url},
                }
            ],
        }
        out = await a._extract_inbound_media(activity, validated_path="A")
        assert out == ([], [], adapter_mod.MessageType.TEXT)
        # No fetch — the URL was rejected before the request.
        assert stream.call_count == 0

    @pytest.mark.asyncio
    async def test_inbound_image_offhost_never_mints_bearer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The reply bearer must NOT be minted/sent for an off-connector contentUrl.
        a = self._adapter(monkeypatch, tmp_path)
        stream = self._stream(a, b"PNG")
        bridge = adapter_mod._import_bridge()
        mint = AsyncMock(return_value=("BEARER", "A"))
        monkeypatch.setattr(bridge, "acquire_reply_token", mint)
        activity = {
            "id": "a",
            "attachments": [
                {"contentType": "image/png", "contentUrl": "https://evil.example.com/x"}
            ],
        }
        out = await a._extract_inbound_media(activity, validated_path="A")
        assert out == ([], [], adapter_mod.MessageType.TEXT)
        assert mint.await_count == 0  # bearer never minted for an off-allowlist host
        assert stream.call_count == 0

    @pytest.mark.asyncio
    async def test_inbound_download_redirect_not_followed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        stream = self._stream(a, b"", status_code=302)
        activity = {
            "id": "a",
            "attachments": [
                {
                    "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                    "content": {"downloadUrl": "https://contoso.sharepoint.com/x"},
                }
            ],
        }
        out = await a._extract_inbound_media(activity, validated_path="A")
        assert out == ([], [], adapter_mod.MessageType.TEXT)  # 3xx → dropped
        # follow_redirects disabled on the request.
        assert stream.call_args.kwargs.get("follow_redirects") is False

    @pytest.mark.asyncio
    async def test_inbound_oversized_content_length_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # R2-P1: an oversized Content-Length is rejected up front (no body read).
        monkeypatch.setattr(adapter_mod, "_MAX_INBOUND_MEDIA_BYTES", 100)
        a = self._adapter(monkeypatch, tmp_path)
        self._stream(a, b"x" * 10, headers={"Content-Length": "999999"})
        activity = {
            "id": "a",
            "attachments": [
                {
                    "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                    "content": {"downloadUrl": "https://contoso.sharepoint.com/x"},
                }
            ],
        }
        out = await a._extract_inbound_media(activity, validated_path="A")
        assert out == ([], [], adapter_mod.MessageType.TEXT)

    @pytest.mark.asyncio
    async def test_inbound_stream_aborts_early_over_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # R2-P1: a body exceeding the cap is aborted mid-stream, NOT fully consumed.
        monkeypatch.setattr(adapter_mod, "_MAX_INBOUND_MEDIA_BYTES", 100)
        a = self._adapter(monkeypatch, tmp_path)
        consumed = {"chunks": 0}

        async def _aiter() -> Any:
            for _ in range(10):  # would be 10 chunks (600 B) if fully consumed
                consumed["chunks"] += 1
                yield b"x" * 60

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}  # no Content-Length → streamed-bound path
        resp.aiter_bytes = _aiter
        a._http_client.stream = MagicMock(return_value=_StreamCM(resp))
        activity = {
            "id": "a",
            "attachments": [
                {
                    "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                    "content": {"downloadUrl": "https://contoso.sharepoint.com/x"},
                }
            ],
        }
        out = await a._extract_inbound_media(activity, validated_path="A")
        assert out == ([], [], adapter_mod.MessageType.TEXT)  # over cap → dropped
        assert consumed["chunks"] < 10  # aborted after MAX+1, not fully consumed

    def test_activity_to_event_populates_media(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        ev = a._activity_to_event(
            _make_inbound(text="see pic"),
            media=(["/cache/x.png"], ["image/png"], adapter_mod.MessageType.PHOTO),
        )
        assert ev.media_urls == ["/cache/x.png"]
        assert ev.media_types == ["image/png"]
        assert ev.message_type == adapter_mod.MessageType.PHOTO
        # No media → TEXT + empty lists (regression: text turns unaffected).
        ev2 = a._activity_to_event(_make_inbound(text="hi"))
        assert ev2.message_type == adapter_mod.MessageType.TEXT
        assert ev2.media_urls == []


# ---------------------------------------------------------------------------
# #76c — outbound file transfer (FileConsentCard → OneDrive upload)
# ---------------------------------------------------------------------------


class TestOutboundFiles:
    def _connect(self, a: Any) -> None:
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        # R2-P1: configured tenant OneDrive host so uploads to _GOOD_UPLOAD pass.
        a._file_host_allowlist = ("contoso-my.sharepoint.com",)

    # Review-F2: a SharePoint/OneDrive host that passes the upload-URL allowlist.
    _GOOD_UPLOAD = "https://contoso-my.sharepoint.com/personal/u/_layouts/upload"

    def _consent_activity(
        self,
        *,
        action: str = "accept",
        consent_id: str = "c1",
        upload_info: dict[str, Any] | None = None,
        conv_id: str = "conv-1",
    ) -> dict[str, Any]:
        act = _make_inbound(conv_id=conv_id, activity_id="fc-act-1")
        act["type"] = "invoke"
        act["name"] = adapter_mod._FILE_CONSENT_INVOKE
        val: dict[str, Any] = {"action": action, "context": {"consentId": consent_id}}
        if upload_info is not None:
            val["uploadInfo"] = upload_info
        act["value"] = val
        return act

    @staticmethod
    def _seed_pending(
        a: Any,
        f: Path,
        *,
        consent_id: str = "c1",
        conv_id: str = "conv-1",
        user_id: str = "user-1",
        service_url: str = "https://smba.trafficmanager.net/amer/x/",
        size: int | None = None,
        sha256: str | None = None,
        created_at: float | None = None,
    ) -> None:
        # Review-F2/F3 + R2-P2: a well-formed pending entry bound to the
        # _consent_activity default conversation/user/serviceUrl + the file digest.
        raw = f.read_bytes()
        a._pending_file_uploads[consent_id] = {
            "path": str(f),
            "name": f.name,
            "size": len(raw) if size is None else size,
            "sha256": hashlib.sha256(raw).hexdigest() if sha256 is None else sha256,
            "conversation_id": conv_id,
            "user_id": user_id,
            "service_url": service_url,
            "created_at": time.time() if created_at is None else created_at,
        }

    # ── outbound: FileConsentCard ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_send_document_personal_emits_consent_card(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.4 fake bytes")
        result = await a.send_document("conv-1", str(f), caption="Here you go")

        assert result.success is True
        assert send_reply.await_count == 1
        reply = send_reply.await_args.kwargs["reply"]
        att = reply["attachments"][0]
        assert att["contentType"] == adapter_mod._FILE_CONSENT_CONTENT_TYPE
        assert att["name"] == "report.pdf"
        assert att["content"]["sizeInBytes"] == f.stat().st_size
        cid = att["content"]["acceptContext"]["consentId"]
        # Accept + decline share one consentId (both route back to us).
        assert att["content"]["declineContext"]["consentId"] == cid
        # A file-transfer card is NOT AI-generated content — no #73(a) entity.
        assert "entities" not in reply
        assert reply["replyToId"] == "act-1"
        # Pending upload recorded under the consentId; message_id echoes it.
        assert a._pending_file_uploads[cid]["name"] == "report.pdf"
        assert result.message_id == cid

    @pytest.mark.asyncio
    async def test_send_image_file_personal_emits_consent_card(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG fake bytes")
        result = await a.send_image_file("conv-1", str(f))

        assert result.success is True
        att = send_reply.await_args.kwargs["reply"]["attachments"][0]
        assert att["contentType"] == adapter_mod._FILE_CONSENT_CONTENT_TYPE
        assert att["name"] == "pic.png"

    @pytest.mark.asyncio
    async def test_send_document_non_personal_text_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-grp")
        inbound["conversation"]["conversationType"] = "groupChat"
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        # Intercept the base text fallback path.
        fallback = AsyncMock(return_value=adapter_mod.SendResult(success=True))
        monkeypatch.setattr(a, "send", fallback)

        f = tmp_path / "doc.txt"
        f.write_text("hi")
        result = await a.send_document("conv-grp", str(f), caption="cap")

        assert result.success is True
        # Degraded to text — no consent card, no pending upload.
        assert send_reply.await_count == 0
        assert a._pending_file_uploads == {}
        assert fallback.await_count == 1
        assert "doc.txt" in fallback.await_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_send_document_missing_file_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        result = await a.send_document("conv-1", str(tmp_path / "nope.pdf"))
        assert result.success is False
        assert "unsafe or missing" in (result.error or "")
        assert send_reply.await_count == 0
        assert a._pending_file_uploads == {}

    @pytest.mark.asyncio
    async def test_send_document_oversized_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        monkeypatch.setattr(adapter_mod, "_MAX_OUTBOUND_FILE_BYTES", 4)

        f = tmp_path / "big.bin"
        f.write_bytes(b"more than four bytes")
        result = await a.send_document("conv-1", str(f))
        assert result.success is False
        assert "over-cap" in (result.error or "")
        assert send_reply.await_count == 0
        assert a._pending_file_uploads == {}

    @pytest.mark.asyncio
    async def test_send_document_no_cached_inbound_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "x.pdf"
        f.write_bytes(b"x")
        result = await a.send_document("ghost", str(f))
        assert result.success is False
        assert "no cached inbound" in (result.error or "")

    # ── inbound: fileConsent/invoke handler ───────────────────────────────

    @pytest.mark.asyncio
    async def test_handle_consent_accept_uploads_and_sends_info_card(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "report.pdf"
        payload = b"the-actual-pdf-bytes"
        f.write_bytes(payload)
        self._seed_pending(a, f)
        a._http_client.put = AsyncMock(return_value=MagicMock(status_code=201))
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        upload_info = {
            "uploadUrl": self._GOOD_UPLOAD,
            "contentUrl": "https://contoso.sharepoint.com/report.pdf",
            "name": "report.pdf",
            "uniqueId": "drive-item-1",
            "fileType": "pdf",
        }
        resp = await a._handle_file_consent(
            self._consent_activity(upload_info=upload_info), validated_path="A"
        )

        assert resp.status == 200
        # Bytes PUT to the pre-authenticated OneDrive session with the range header.
        assert a._http_client.put.await_count == 1
        assert a._http_client.put.await_args.args[0] == upload_info["uploadUrl"]
        assert a._http_client.put.await_args.kwargs["content"] == payload
        n = len(payload)
        assert (
            a._http_client.put.await_args.kwargs["headers"]["Content-Range"]
            == f"bytes 0-{n - 1}/{n}"
        )
        # FileInfoCard confirmation sent, pointing at the uploaded content.
        assert send_reply.await_count == 1
        info_att = send_reply.await_args.kwargs["reply"]["attachments"][0]
        assert info_att["contentType"] == adapter_mod._FILE_INFO_CONTENT_TYPE
        assert info_att["contentUrl"] == upload_info["contentUrl"]
        # Pending consumed → a BF retry acks idempotently, no double upload.
        assert "c1" not in a._pending_file_uploads

    @pytest.mark.asyncio
    async def test_handle_consent_decline_no_upload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "x.pdf"
        f.write_bytes(b"x")
        a._pending_file_uploads["c1"] = {"path": str(f), "name": "x.pdf"}
        a._http_client.put = AsyncMock()
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        resp = await a._handle_file_consent(
            self._consent_activity(action="decline"), validated_path="A"
        )
        assert resp.status == 200
        assert a._http_client.put.await_count == 0
        assert send_reply.await_count == 0
        # Pending dropped even on decline (the offer is spent).
        assert "c1" not in a._pending_file_uploads

    @pytest.mark.asyncio
    async def test_handle_consent_unknown_consent_acks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        a._http_client.put = AsyncMock()
        resp = await a._handle_file_consent(
            self._consent_activity(
                consent_id="nope", upload_info={"uploadUrl": "https://x/y"}
            ),
            validated_path="A",
        )
        assert resp.status == 200
        assert a._http_client.put.await_count == 0

    @pytest.mark.asyncio
    async def test_handle_consent_accept_missing_upload_url_acks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "x.pdf"
        f.write_bytes(b"x")
        self._seed_pending(a, f)
        a._http_client.put = AsyncMock()
        resp = await a._handle_file_consent(
            self._consent_activity(upload_info=None), validated_path="A"
        )
        assert resp.status == 200
        assert a._http_client.put.await_count == 0
        assert "c1" not in a._pending_file_uploads

    @pytest.mark.asyncio
    async def test_handle_consent_upload_http_error_still_acks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "x.pdf"
        f.write_bytes(b"data")
        self._seed_pending(a, f)
        a._http_client.put = AsyncMock(return_value=MagicMock(status_code=500))
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        resp = await a._handle_file_consent(
            self._consent_activity(
                upload_info={
                    "uploadUrl": self._GOOD_UPLOAD,
                    "contentUrl": "c",
                    "name": "x",
                }
            ),
            validated_path="A",
        )
        assert resp.status == 200
        # Upload failed → no confirmation card, pending consumed (no retry loop).
        assert send_reply.await_count == 0
        assert "c1" not in a._pending_file_uploads

    # ── route: fileConsent/invoke reaches the handler ─────────────────────

    def test_route_file_consent_invoke_dispatches_handler(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"oid": "o1", "tid": "t1"}),
        )
        self._connect(a)
        f = tmp_path / "r.pdf"
        f.write_bytes(b"pdf")
        # Route body arrives on conv-fc from the _make_inbound default user.
        self._seed_pending(a, f, conv_id="conv-fc")
        a._http_client.put = AsyncMock(return_value=MagicMock(status_code=201))
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        body = _make_inbound(conv_id="conv-fc", activity_id="fc-1")
        body["type"] = "invoke"
        body["name"] = adapter_mod._FILE_CONSENT_INVOKE
        body["value"] = {
            "action": "accept",
            "context": {"consentId": "c1"},
            "uploadInfo": {
                "uploadUrl": self._GOOD_UPLOAD,
                "contentUrl": "https://contoso.sharepoint.com/c",
                "name": "r.pdf",
                "uniqueId": "u",
                "fileType": "pdf",
            },
        }
        client = TestClient(a.build_app())
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer pretend"}
        )
        assert r.status_code == 200, r.text
        # Handled inline — never dispatched to the fire-and-forget agent loop.
        assert a._handled_events == []
        assert a._http_client.put.await_count == 1
        assert "c1" not in a._pending_file_uploads

    # ── Review-F2/F3: accept-path trust boundary + resource limits ────────

    async def _accept_and_assert_no_put(
        self, a: Any, monkeypatch: pytest.MonkeyPatch, activity: dict[str, Any]
    ) -> None:
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
        resp = await a._handle_file_consent(activity, validated_path="A")
        assert resp.status == 200
        assert a._http_client.put.await_count == 0

    @pytest.mark.asyncio
    async def test_accept_conversation_mismatch_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "r.pdf"
        f.write_bytes(b"data")
        # Pending bound to conv-1; the accept arrives on a DIFFERENT conversation.
        self._seed_pending(a, f, conv_id="conv-1")
        a._http_client.put = AsyncMock()
        act = self._consent_activity(
            upload_info={"uploadUrl": self._GOOD_UPLOAD}, conv_id="conv-OTHER"
        )
        await self._accept_and_assert_no_put(a, monkeypatch, act)
        assert "c1" not in a._pending_file_uploads  # spent even on refusal

    @pytest.mark.asyncio
    async def test_accept_offhost_upload_url_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "r.pdf"
        f.write_bytes(b"data")
        self._seed_pending(a, f)
        a._http_client.put = AsyncMock()
        # Attacker-controlled upload destination — bytes must never be POSTed.
        act = self._consent_activity(
            upload_info={"uploadUrl": "https://evil.example.com/collect"}
        )
        await self._accept_and_assert_no_put(a, monkeypatch, act)

    @pytest.mark.asyncio
    async def test_accept_private_ip_upload_url_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "r.pdf"
        f.write_bytes(b"data")
        self._seed_pending(a, f)
        a._http_client.put = AsyncMock()
        act = self._consent_activity(
            upload_info={"uploadUrl": "https://169.254.169.254/latest/meta-data"}
        )
        await self._accept_and_assert_no_put(a, monkeypatch, act)

    @pytest.mark.asyncio
    async def test_accept_file_grew_since_offer_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "r.pdf"
        f.write_bytes(b"small")
        self._seed_pending(a, f, size=5)
        # File grew after the offer (size mismatch → refuse; user consented to 5B).
        f.write_bytes(b"a much larger payload than offered")
        a._http_client.put = AsyncMock()
        act = self._consent_activity(upload_info={"uploadUrl": self._GOOD_UPLOAD})
        await self._accept_and_assert_no_put(a, monkeypatch, act)

    @pytest.mark.asyncio
    async def test_accept_expired_consent_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "r.pdf"
        f.write_bytes(b"data")
        # Offered well beyond the TTL.
        self._seed_pending(
            a, f, created_at=time.time() - adapter_mod._PENDING_UPLOAD_TTL_SEC - 10
        )
        a._http_client.put = AsyncMock()
        act = self._consent_activity(upload_info={"uploadUrl": self._GOOD_UPLOAD})
        await self._accept_and_assert_no_put(a, monkeypatch, act)

    @pytest.mark.asyncio
    async def test_accept_same_size_content_swap_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # R2-P2: a same-size replacement passes the size check but the SHA-256
        # binding catches it — the offered content is not uploaded.
        a = _make_adapter(monkeypatch)
        self._connect(a)
        f = tmp_path / "r.pdf"
        f.write_bytes(b"AAAAA")
        self._seed_pending(a, f)  # digest bound to b"AAAAA"
        f.write_bytes(b"BBBBB")  # same size, different bytes
        a._http_client.put = AsyncMock()
        act = self._consent_activity(upload_info={"uploadUrl": self._GOOD_UPLOAD})
        await self._accept_and_assert_no_put(a, monkeypatch, act)

    @pytest.mark.asyncio
    async def test_accept_attacker_tenant_upload_url_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # R2-P1: a DIFFERENT tenant's SharePoint host is refused even though it
        # matches the *.sharepoint.com shape — configured host is contoso-my.
        a = _make_adapter(monkeypatch)
        self._connect(a)  # allowlist = contoso-my.sharepoint.com
        f = tmp_path / "r.pdf"
        f.write_bytes(b"data")
        self._seed_pending(a, f)
        a._http_client.put = AsyncMock()
        act = self._consent_activity(
            upload_info={"uploadUrl": "https://attacker-tenant.sharepoint.com/up"}
        )
        await self._accept_and_assert_no_put(a, monkeypatch, act)

    @pytest.mark.asyncio
    async def test_send_file_consent_bounds_pending_map(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_CORRELATOR_ENTRIES", 3)
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
        f = tmp_path / "r.pdf"
        f.write_bytes(b"data")
        for _ in range(6):
            await a.send_document("conv-1", str(f))
        assert len(a._pending_file_uploads) == 3

    # ── R3-P1: profile-scoped host allowlist + fail-before-offer ──────────

    def test_file_host_allowlist_from_profile_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # extra.file_host_allowlist (list) is read + normalised (lower/strip).
        monkeypatch.delenv("A365_FILE_HOST_ALLOWLIST", raising=False)
        a = _make_adapter(
            monkeypatch, file_host_allowlist=["Contoso.SharePoint.com", " x ", ""]
        )
        assert a._file_host_allowlist == ("contoso.sharepoint.com", "x")

    def test_file_host_allowlist_env_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_FILE_HOST_ALLOWLIST", "env.sharepoint.com")
        a = _make_adapter(monkeypatch)  # no profile extra
        assert a._file_host_allowlist == ("env.sharepoint.com",)

    def test_profile_config_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A365_FILE_HOST_ALLOWLIST", "env.sharepoint.com")
        a = _make_adapter(monkeypatch, file_host_allowlist=["profile.sharepoint.com"])
        assert a._file_host_allowlist == ("profile.sharepoint.com",)

    @pytest.mark.parametrize("bad", [5, True, {"a": 1}, 3.2])
    def test_file_host_allowlist_scalar_misconfig_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, bad: Any
    ) -> None:
        # Red-team catch: a non-str/non-list value must NOT crash plugin load
        # (`list(<scalar>)` → TypeError); it fails closed to an empty allowlist.
        monkeypatch.delenv("A365_FILE_HOST_ALLOWLIST", raising=False)
        a = _make_adapter(monkeypatch, file_host_allowlist=bad)
        assert a._file_host_allowlist == ()

    def test_two_profiles_reject_each_others_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Multiplex safety: profile A's pin must not accept profile B's tenant host.
        monkeypatch.delenv("A365_FILE_HOST_ALLOWLIST", raising=False)
        a = _make_adapter(monkeypatch, file_host_allowlist=["tenant-a.sharepoint.com"])
        b = _make_adapter(monkeypatch, file_host_allowlist=["tenant-b.sharepoint.com"])
        url_a = "https://tenant-a.sharepoint.com/up"
        url_b = "https://tenant-b.sharepoint.com/up"
        assert adapter_mod._is_allowed_file_host(url_a, a._file_host_allowlist)
        assert not adapter_mod._is_allowed_file_host(url_b, a._file_host_allowlist)
        assert adapter_mod._is_allowed_file_host(url_b, b._file_host_allowlist)
        assert not adapter_mod._is_allowed_file_host(url_a, b._file_host_allowlist)

    @pytest.mark.asyncio
    async def test_send_document_empty_allowlist_text_fallback_no_card(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # R3-P1: no pinned tenant host → text fallback, and NO FileConsentCard is
        # offered (a consent flow that could never complete is never presented).
        monkeypatch.delenv("A365_FILE_HOST_ALLOWLIST", raising=False)
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._seen_inbounds_this_lifetime.add("conv-1")
        self._connect(a)
        a._file_host_allowlist = ()  # empty ⇒ fail-closed
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        f = tmp_path / "r.pdf"
        f.write_bytes(b"data")

        result = await a.send_document("conv-1", str(f))
        assert result.success is True  # text fallback delivered
        assert a._pending_file_uploads == {}  # no consent recorded
        reply = send_reply.await_args.kwargs["reply"]
        atts = reply.get("attachments") or []
        assert all(
            att.get("contentType") != adapter_mod._FILE_CONSENT_CONTENT_TYPE
            for att in atts
        )
        assert "r.pdf" in (reply.get("text") or "")


# ---------------------------------------------------------------------------
# #73(b/c) — citations + feedback loop (plugin send path + invoke children)
# ---------------------------------------------------------------------------


class TestFeedbackAndCitations:
    def _connected_send(self, monkeypatch: pytest.MonkeyPatch, a: Any) -> Any:
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._seen_inbounds_this_lifetime.add("conv-1")
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        return send_reply

    @pytest.mark.asyncio
    async def test_send_stamps_feedback_channel_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        send_reply = self._connected_send(monkeypatch, a)
        await a.send(chat_id="conv-1", content="hi")
        reply = send_reply.await_args.kwargs["reply"]
        assert reply["channelData"] == {"feedbackLoop": {"type": "default"}}

    @pytest.mark.asyncio
    async def test_feedback_disabled_by_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_FEEDBACK_LOOP", "0")
        a = _make_adapter(monkeypatch)
        send_reply = self._connected_send(monkeypatch, a)
        await a.send(chat_id="conv-1", content="hi")
        assert "channelData" not in send_reply.await_args.kwargs["reply"]

    @pytest.mark.asyncio
    async def test_send_threads_citations_from_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        send_reply = self._connected_send(monkeypatch, a)
        await a.send(
            chat_id="conv-1",
            content="See [1].",
            metadata={"citations": [{"title": "Doc", "url": "https://d"}]},
        )
        entity = send_reply.await_args.kwargs["reply"]["entities"][0]
        assert entity["citation"][0]["appearance"]["name"] == "Doc"

    def _feedback_ctx(
        self, *, action_name: str = "feedback", reaction: str = "like", msg_id: str = "msg-1"
    ) -> Any:
        activity = {
            "type": "invoke",
            "name": "message/submitAction",
            "replyToId": msg_id,
            "conversation": {"id": "conv-1"},
            "from": {"id": "user-1"},
            "value": {
                "actionName": action_name,
                "actionValue": {"reaction": reaction, "feedback": "great"},
            },
        }
        return adapter_mod.invoke.build_invoke_context(
            activity, claims={"oid": "o1", "tid": "t1"}, path_tag="A"
        )

    @pytest.mark.asyncio
    async def test_feedback_submit_records_reaction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        resp = await a._handle_feedback_submit(self._feedback_ctx())
        assert resp.status == 200
        rec = a._feedback_by_message_id["msg-1"]
        assert rec["reaction"] == "like"
        assert rec["conversation_id"] == "conv-1"

    @pytest.mark.asyncio
    async def test_feedback_submit_ignores_non_feedback_action(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        resp = await a._handle_feedback_submit(
            self._feedback_ctx(action_name="somethingElse")
        )
        assert resp.status == 200
        assert a._feedback_by_message_id == {}

    @pytest.mark.asyncio
    async def test_message_fetch_task_acks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        ctx = adapter_mod.invoke.build_invoke_context(
            {"type": "invoke", "name": "message/fetchTask", "conversation": {}},
            claims=None,
            path_tag="A",
        )
        resp = await a._handle_message_fetch_task(ctx)
        assert resp.status == 200

    def test_invoke_registry_has_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        for name in ("task/fetch", "message/submitAction", "message/fetchTask", "handoff/action"):
            assert name in a._invoke_registry


# ---------------------------------------------------------------------------
# #77 — interactive-UI cards (approval / confirm / clarify)
# ---------------------------------------------------------------------------


class TestInteractiveCards:
    def _connect(self, a: Any) -> None:
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()

    @pytest.mark.asyncio
    async def test_send_exec_approval_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        result = await a.send_exec_approval("conv-1", "rm -rf /tmp/x", "sess-1")
        assert result.success is True
        reply = send_reply.await_args.kwargs["reply"]
        att = reply["attachments"][0]
        assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
        actions = att["content"]["actions"]
        assert [act["data"]["choice"] for act in actions] == [
            "once", "session", "always", "deny",
        ]
        assert all(act["data"]["hermes_kind"] == "exec_approval" for act in actions)
        assert all(act["data"]["session_key"] == "sess-1" for act in actions)
        # A system approval card is NOT AI-generated content — no #73(a) entity.
        assert "entities" not in reply

    @pytest.mark.asyncio
    async def test_send_slash_confirm_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        result = await a.send_slash_confirm(
            "conv-1", "Confirm", "Run it?", "sess-1", "cfm-9"
        )
        assert result.success is True
        actions = send_reply.await_args.kwargs["reply"]["attachments"][0]["content"][
            "actions"
        ]
        assert [act["data"]["choice"] for act in actions] == ["once", "always", "cancel"]
        assert all(act["data"]["confirm_id"] == "cfm-9" for act in actions)

    @pytest.mark.asyncio
    async def test_send_clarify_with_choices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        result = await a.send_clarify(
            "conv-1", "Which one?", ["Alpha", "Beta"], "clr-1", "sess-1"
        )
        assert result.success is True
        actions = send_reply.await_args.kwargs["reply"]["attachments"][0]["content"][
            "actions"
        ]
        assert [act["title"] for act in actions] == ["Alpha", "Beta", "Something else"]
        assert actions[0]["data"]["choice_text"] == "Alpha"
        assert actions[-1]["data"]["choice"] == "other"

    @pytest.mark.asyncio
    async def test_send_clarify_open_ended_arms_text_intercept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        a._seen_inbounds_this_lifetime.add("conv-1")
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        mark = MagicMock()
        monkeypatch.setattr(a, "_gw_mark_awaiting_text", mark)

        result = await a.send_clarify("conv-1", "Say more?", None, "clr-2", "sess-1")
        assert result.success is True
        # Question sent as a plain text reply; intercept armed for the answer.
        assert send_reply.await_args.kwargs["reply"]["text"] == "Say more?"
        mark.assert_called_once_with("clr-2")

    @pytest.mark.asyncio
    async def test_send_exec_approval_no_inbound_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        result = await a.send_exec_approval("ghost", "cmd", "sess-1")
        # No cached inbound → success=False so the gateway text-fallback fires.
        assert result.success is False

    def test_extract_card_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        a = _make_adapter(monkeypatch)
        good = {"type": "message", "value": {"hermes_kind": "exec_approval", "choice": "once"}}
        assert a._extract_card_action(good) == good["value"]
        # Not ours / not a card submit.
        assert a._extract_card_action({"type": "message", "text": "hi"}) is None
        assert a._extract_card_action({"type": "message", "value": "x"}) is None
        assert (
            a._extract_card_action({"type": "message", "value": {"hermes_kind": "nope"}})
            is None
        )
        # An invoke (not a message) carrying the tag is not a card submit.
        invoke_shaped = {"type": "invoke", "value": {"hermes_kind": "exec_approval"}}
        assert a._extract_card_action(invoke_shaped) is None

    @pytest.mark.asyncio
    async def test_handle_card_action_exec_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        resolver = MagicMock(return_value=1)
        monkeypatch.setattr(a, "_gw_resolve_approval", resolver)
        activity = {"type": "message", "conversation": {"id": "conv-1"}}
        value = {"hermes_kind": "exec_approval", "session_key": "sess-1", "choice": "always"}
        resp = await a._handle_card_action(activity, value)
        assert resp.status_code == 200
        assert json.loads(resp.body)["kind"] == "exec_approval"
        resolver.assert_called_once_with("sess-1", "always")

    @pytest.mark.asyncio
    async def test_handle_card_action_slash_confirm_posts_followup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        monkeypatch.setattr(
            a, "_gw_resolve_slash_confirm", AsyncMock(return_value="Command ran.")
        )
        activity = _make_inbound(conv_id="conv-1")
        value = {
            "hermes_kind": "slash_confirm",
            "session_key": "sess-1",
            "confirm_id": "cfm-1",
            "choice": "once",
        }
        resp = await a._handle_card_action(activity, value)
        assert resp.status_code == 200
        # The resolver's follow-up text is posted as a reply.
        assert send_reply.await_args.kwargs["reply"]["text"] == "Command ran."

    @pytest.mark.asyncio
    async def test_handle_card_action_clarify_numeric(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        resolver = MagicMock(return_value=True)
        monkeypatch.setattr(a, "_gw_resolve_clarify", resolver)
        value = {"hermes_kind": "clarify", "clarify_id": "clr-1", "choice_text": "Beta"}
        resp = await a._handle_card_action({"type": "message"}, value)
        assert resp.status_code == 200
        resolver.assert_called_once_with("clr-1", "Beta")

    @pytest.mark.asyncio
    async def test_handle_card_action_clarify_other_arms_intercept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        mark = MagicMock()
        monkeypatch.setattr(a, "_gw_mark_awaiting_text", mark)
        value = {"hermes_kind": "clarify", "clarify_id": "clr-1", "choice": "other"}
        resp = await a._handle_card_action({"type": "message"}, value)
        assert resp.status_code == 200
        mark.assert_called_once_with("clr-1")

    def test_route_card_submit_not_dispatched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "validate_inbound_jwt", AsyncMock(return_value={"oid": "o1"})
        )
        a._http_client = MagicMock()
        resolver = MagicMock(return_value=1)
        monkeypatch.setattr(a, "_gw_resolve_approval", resolver)

        body = _make_inbound(conv_id="conv-card", activity_id="ca-1", text="")
        body["value"] = {
            "hermes_kind": "exec_approval",
            "session_key": "sess-1",
            "choice": "deny",
        }
        client = TestClient(a.build_app())
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer pretend"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "card_action"
        # Routed to the resolver, NOT the agent loop.
        resolver.assert_called_once_with("sess-1", "deny")
        assert a._handled_events == []


# ---------------------------------------------------------------------------
# #82 — Copilot→Teams handoff
# ---------------------------------------------------------------------------


class TestHandoff:
    def test_mint_handoff_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(
                _make_inbound(conv_id="conv-cc")
            )
        )
        link = a._mint_handoff_link("conv-cc", reason="test")
        assert link is not None
        assert "continuation=" in link
        token = link.split("continuation=")[1]
        assert a._handoff_tokens[token]["conversation_id"] == "conv-cc"
        # #89 walk fix: the deep link must target the Teams-routable BF/messaging
        # bot id (the app that owns the Teams channel), not the CEA blueprint.
        assert f"28:{a.bf_app_id}" in link

    def test_mint_handoff_link_falls_back_to_blueprint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Single-identity deployment (no separate BF app) → blueprint id is used.
        a = _make_adapter(monkeypatch)
        a.bf_app_id = ""
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound(conv_id="conv-cc2"))
        )
        link = a._mint_handoff_link("conv-cc2", reason="test")
        assert f"28:{a.blueprint_app_id}" in link

    def test_mint_handoff_link_unknown_conv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        assert a._mint_handoff_link("ghost", reason="x") is None

    @pytest.mark.asyncio
    async def test_handle_handoff_action_known_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._handoff_tokens["tok-1"] = {"conversation_id": "conv-cc", "chat_type": "groupChat"}
        ctx = adapter_mod.invoke.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "value": {"continuation": "tok-1"},
                "conversation": {"id": "conv-teams"},
            },
            claims=None,
            path_tag="A",
        )
        resp = await a._handle_handoff_action(ctx)
        assert resp.status == 200
        # Token consumed; linkage recorded.
        assert "tok-1" not in a._handoff_tokens

    @pytest.mark.asyncio
    async def test_handle_handoff_action_unknown_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        ctx = adapter_mod.invoke.build_invoke_context(
            {
                "type": "invoke",
                "name": "handoff/action",
                "value": {"continuation": "nope"},
                "conversation": {"id": "conv-teams"},
            },
            claims=None,
            path_tag="A",
        )
        resp = await a._handle_handoff_action(ctx)
        assert resp.status == 200

    def test_append_handoff_link_disabled_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-cc")
        inbound["conversation"]["conversationType"] = "groupChat"
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        assert a._maybe_append_handoff_link("conv-cc", "body") == "body"

    def test_append_handoff_link_enabled_nonpersonal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_HANDOFF_LINK", "1")
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-cc")
        inbound["conversation"]["conversationType"] = "groupChat"
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        out = a._maybe_append_handoff_link("conv-cc", "body")
        assert "Continue in Teams" in out
        assert "body" in out

    def test_append_handoff_link_personal_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_HANDOFF_LINK", "1")
        a = _make_adapter(monkeypatch)
        # _make_inbound default is personal.
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound(conv_id="conv-dm"))
        )
        assert a._maybe_append_handoff_link("conv-dm", "body") == "body"


# ---------------------------------------------------------------------------
# v0.8.4 review follow-ups — bounded correlator maps, upload zero-guard
# ---------------------------------------------------------------------------


class TestCorrelatorBounds:
    def test_bound_map_drops_oldest(self) -> None:
        m = {str(i): i for i in range(5)}
        adapter_mod._bound_map(m, cap=3)
        assert list(m) == ["2", "3", "4"]

    @pytest.mark.asyncio
    async def test_feedback_map_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_CORRELATOR_ENTRIES", 3)
        a = _make_adapter(monkeypatch)
        for i in range(6):
            ctx = adapter_mod.invoke.build_invoke_context(
                {
                    "type": "invoke",
                    "name": "message/submitAction",
                    "replyToId": f"msg-{i}",
                    "conversation": {"id": "conv-1"},
                    "value": {"actionName": "feedback", "actionValue": {"reaction": "like"}},
                },
                claims=None,
                path_tag="A",
            )
            await a._handle_feedback_submit(ctx)
        assert len(a._feedback_by_message_id) == 3

    @pytest.mark.asyncio
    async def test_handoff_upload_zero_byte_file_acks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # #76c review nit: a file truncated to empty between offer and Accept
        # must not build a malformed Content-Range — ack gracefully instead.
        a = _make_adapter(monkeypatch)
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        a._file_host_allowlist = ("contoso-my.sharepoint.com",)
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        a._pending_file_uploads["c1"] = {
            "path": str(f),
            "name": "empty.bin",
            "size": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "conversation_id": "conv-1",
            "user_id": "",
            "service_url": "",
            "created_at": time.time(),
        }
        a._http_client.put = AsyncMock()
        activity = {
            "type": "invoke",
            "name": adapter_mod._FILE_CONSENT_INVOKE,
            "value": {
                "action": "accept",
                "context": {"consentId": "c1"},
                "uploadInfo": {
                    "uploadUrl": "https://contoso-my.sharepoint.com/up"
                },
            },
            "conversation": {"id": "conv-1"},
        }
        resp = await a._handle_file_consent(activity, validated_path="A")
        assert resp.status == 200
        # No malformed PUT attempted.
        assert a._http_client.put.await_count == 0

    @pytest.mark.asyncio
    async def test_disconnect_clears_correlator_maps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._feedback_by_message_id["m"] = {"reaction": "like"}
        a._handoff_tokens["t"] = {"conversation_id": "c"}
        a._pending_file_uploads["c1"] = {"path": "/x", "name": "x"}
        await a.disconnect()
        assert a._feedback_by_message_id == {}
        assert a._handoff_tokens == {}
        assert a._pending_file_uploads == {}


# ---------------------------------------------------------------------------
# v0.8.5 — #103 M9/M4: slug safety + outbound URL path-segment encoding
# ---------------------------------------------------------------------------


class TestSlugIngestion:
    """#103 / M9 + review P2 — an EXPLICITLY configured traversal-shaped
    slug is rejected fail-closed (the adapter refuses to construct) rather
    than silently routing to the shared 'default' profile state. A
    genuinely absent slug still resolves to 'default'."""

    def test_benign_slug_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        a = _make_adapter(monkeypatch, slug="inbox-helper")
        assert a.slug == "inbox-helper"

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "a\\b", ".", "x\x00y"])
    def test_explicit_traversal_slug_rejected(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        # Fail closed: an invalid configured slug must not instantiate an
        # adapter (and therefore cannot read/write the default profile).
        with pytest.raises(ValueError):
            _make_adapter(monkeypatch, slug=bad)

    def test_explicit_traversal_agent_identity_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_IDENTITY", "../../tmp/evil")
        with pytest.raises(ValueError):
            _make_adapter(monkeypatch, slug=None)

    def test_absent_slug_resolves_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No extra slug + no AGENT_IDENTITY → the supported single-profile
        # 'default' dir (missing-slug behaviour is preserved).
        monkeypatch.delenv("AGENT_IDENTITY", raising=False)
        a = _make_adapter(monkeypatch, slug=None)
        assert a.slug == ""
        assert a._conversations_path.parent.name == "default"

    def test_validate_config_rejects_explicit_invalid_slug(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_TENANT_ID", "11111111-1111-1111-1111-111111111111")
        monkeypatch.setenv("A365_APP_ID", "22222222-2222-2222-2222-222222222222")
        monkeypatch.delenv("AGENT_IDENTITY", raising=False)
        good = _StubPlatformConfig(extra={"slug": "inbox-helper"})
        bad = _StubPlatformConfig(extra={"slug": "../evil"})
        absent = _StubPlatformConfig(extra={})
        assert adapter_mod.validate_config(good) is True
        assert adapter_mod.validate_config(absent) is True
        assert adapter_mod.validate_config(bad) is False

    def test_two_invalid_profiles_cannot_share_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The multiplex hazard the review flags: neither invalid profile
        # may construct, so they can't collide on default/ durable state.
        for bad in ("../p1", "../p2"):
            with pytest.raises(ValueError):
                _make_adapter(monkeypatch, slug=bad)


class TestConversationsActivitiesUrl:
    """#103 / M4 — conversation ids are percent-encoded as single path
    segments in every outbound BF URL the adapter builds."""

    def test_teams_style_id_encoded(self) -> None:
        url = adapter_mod._conversations_activities_url(
            "https://smba.trafficmanager.net/amer", "19:abc@thread.tacv2"
        )
        assert url == (
            "https://smba.trafficmanager.net/amer/v3/conversations/"
            "19%3Aabc%40thread.tacv2/activities"
        )

    def test_hostile_id_cannot_shift_the_path(self) -> None:
        url = adapter_mod._conversations_activities_url(
            "https://smba.trafficmanager.net/amer", "../x?y=1#frag"
        )
        tail = url.split("/v3/conversations/", 1)[1]
        assert tail == "..%2Fx%3Fy%3D1%23frag/activities"
        assert "?" not in url
        assert "#" not in url

    def test_bare_dotdot_id_is_neutralised(self) -> None:
        # A conv id of exactly ".." must not render a live dot-segment that
        # URL normalisation collapses (…/conversations/../activities →
        # …/activities). quote(safe="") alone leaves ".." unchanged.
        url = adapter_mod._conversations_activities_url(
            "https://smba.trafficmanager.net/amer", ".."
        )
        assert url == (
            "https://smba.trafficmanager.net/amer/v3/conversations/%2E%2E/activities"
        )
        assert "/../" not in url

    @pytest.mark.asyncio
    async def test_proactive_send_uses_encoded_conv_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end through send()'s proactive fallback: the POSTed URL
        # carries the percent-encoded conversation id while the JSON body
        # keeps the raw id (only the URL is an injection surface).
        conv_id = "19:abc@thread.tacv2;messageid=1"
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(
                {
                    "type": "message",
                    "id": "act-prior",
                    "channelId": "msteams",
                    "serviceUrl": "https://smba.trafficmanager.net/x/",
                    "conversation": {
                        "id": conv_id,
                        "conversationType": "personal",
                        "tenantId": "t",
                    },
                    "from": {"id": "u"},
                    "recipient": {
                        "id": "a",
                        "agenticAppId": "aa",
                        "agenticUserId": "au",
                    },
                }
            )
        )
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json = MagicMock(return_value={"id": "pid"})
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock(return_value=mock_resp)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge, "acquire_reply_token", AsyncMock(return_value=("tok", "A"))
        )

        result = await a.send(chat_id=conv_id, content="proactive ping")
        assert result.success is True
        url = a._http_client.post.await_args.args[0]
        assert url.endswith(
            "/v3/conversations/19%3Aabc%40thread.tacv2%3Bmessageid%3D1/activities"
        )
        assert "19:abc@" not in url
        body = a._http_client.post.await_args.kwargs["json"]
        assert body["conversation"]["id"] == conv_id


# ---------------------------------------------------------------------------
# v0.8.5 — #110 CS-002: permissive parent .env cannot silently receive secret
# ---------------------------------------------------------------------------


class TestGateEnvSecretWrite:
    """#110 / CS-002 — the wizard checks (and repairs or refuses) the
    parent .env mode before A365_BLUEPRINT_CLIENT_SECRET is saved. The
    prompt/print hooks are injected, so no hermes_cli harness needed."""

    @staticmethod
    def _hooks(answers: list[bool]):
        calls: dict[str, list[str]] = {"prompts": [], "warnings": []}

        def prompt_yes_no(question: str, default: bool = False) -> bool:
            calls["prompts"].append(question)
            return answers.pop(0)

        def print_warning(msg: str) -> None:
            calls["warnings"].append(msg)

        return prompt_yes_no, print_warning, calls

    def test_missing_env_passes_without_prompts(self, tmp_path: Path) -> None:
        # Fresh file: save_env_value hardens on create, nothing to gate.
        prompt_yes_no, print_warning, calls = self._hooks([])
        ok = adapter_mod._gate_env_secret_write(
            tmp_path / ".env", prompt_yes_no=prompt_yes_no, print_warning=print_warning
        )
        assert ok is True
        assert calls["prompts"] == []
        assert calls["warnings"] == []

    def test_owner_only_env_passes_silently(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("EXISTING=1\n")
        env.chmod(0o600)
        prompt_yes_no, print_warning, calls = self._hooks([])
        ok = adapter_mod._gate_env_secret_write(
            env, prompt_yes_no=prompt_yes_no, print_warning=print_warning
        )
        assert ok is True
        assert calls["prompts"] == []

    def test_permissive_env_hardened_with_consent(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("EXISTING=1\n")
        env.chmod(0o644)
        prompt_yes_no, print_warning, calls = self._hooks([True])
        ok = adapter_mod._gate_env_secret_write(
            env, prompt_yes_no=prompt_yes_no, print_warning=print_warning
        )
        assert ok is True
        assert (env.stat().st_mode & 0o777) == 0o600
        # The warning names the current mode.
        assert any("644" in w for w in calls["warnings"])

    def test_permissive_env_refused_when_both_declined(self, tmp_path: Path) -> None:
        # The regression the issue pins: a pre-existing permissive .env
        # cannot SILENTLY receive the secret — declining the repair and
        # the explicit override refuses the write and leaves the file be.
        env = tmp_path / ".env"
        env.write_text("EXISTING=1\n")
        env.chmod(0o644)
        prompt_yes_no, print_warning, calls = self._hooks([False, False])
        ok = adapter_mod._gate_env_secret_write(
            env, prompt_yes_no=prompt_yes_no, print_warning=print_warning
        )
        assert ok is False
        assert (env.stat().st_mode & 0o777) == 0o644
        assert len(calls["prompts"]) == 2

    def test_permissive_env_explicit_override_names_mode(
        self, tmp_path: Path
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("EXISTING=1\n")
        env.chmod(0o644)
        prompt_yes_no, print_warning, calls = self._hooks([False, True])
        ok = adapter_mod._gate_env_secret_write(
            env, prompt_yes_no=prompt_yes_no, print_warning=print_warning
        )
        assert ok is True
        # Override consent question names the mode and the risk.
        assert "644" in calls["prompts"][1]
        assert "ANYWAY" in calls["prompts"][1]

    def test_group_readable_640_is_gated_too(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("EXISTING=1\n")
        env.chmod(0o640)
        prompt_yes_no, print_warning, _calls = self._hooks([True])
        ok = adapter_mod._gate_env_secret_write(
            env, prompt_yes_no=prompt_yes_no, print_warning=print_warning
        )
        assert ok is True
        assert (env.stat().st_mode & 0o777) == 0o600

    def test_chmod_failure_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("EXISTING=1\n")
        env.chmod(0o644)

        def boom(path: object, mode: int) -> None:
            raise OSError("simulated chmod failure")

        prompt_yes_no, print_warning, calls = self._hooks([True])
        monkeypatch.setattr(adapter_mod.os, "chmod", boom)
        ok = adapter_mod._gate_env_secret_write(
            env, prompt_yes_no=prompt_yes_no, print_warning=print_warning
        )
        assert ok is False
        assert any("chmod failed" in w for w in calls["warnings"])
