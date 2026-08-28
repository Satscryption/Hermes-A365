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

    async def cancel_session_processing(self, session_key: str, **_kwargs: Any) -> None:
        task = self._session_tasks.pop(session_key, None)
        if task is not None:
            task.cancel()
        self._active_sessions.pop(session_key, None)

    async def cancel_background_tasks(self) -> None:
        for task in self._session_tasks.values():
            task.cancel()
        self._session_tasks.clear()
        self._active_sessions.clear()

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

    def _stub_build_session_key(
        source, *, group_sessions_per_user=True, thread_sessions_per_user=False
    ):
        # Deterministic, non-crashing stand-in for the real build_session_key
        # (#105/M11): enough for _session_key_for to compute a key. Tests that
        # exercise the in-flight guard inject _active_sessions +
        # _session_key_to_conv directly rather than relying on this shape.
        ct = getattr(source, "chat_type", "dm")
        cid = getattr(source, "chat_id", "")
        return f"agent:main:agent365:{ct}:{cid}"

    session.build_session_key = _stub_build_session_key

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
    monkeypatch.setenv("A365_ALLOW_ALL_USERS", "1")
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


def _seed_card_capability(
    adapter: Any,
    activity: dict[str, Any],
    *,
    kind: str,
    choice: str,
    resolver: dict[str, str],
) -> dict[str, str]:
    nonce = "test-capability"
    choice_id = "test-choice"
    adapter._card_capabilities[nonce] = {
        "kind": kind,
        "conversation_id": (activity.get("conversation") or {}).get("id", ""),
        "user_id": adapter_mod._import_bridge()._canonical_activity_user(activity),
        "tenant_id": adapter.tenant_id.lower(),
        "lifecycle_generation": adapter._lifecycle_generation,
        "chat_generation": adapter._chat_generation(
            (activity.get("conversation") or {}).get("id", "")
        ),
        "created_at": adapter_mod.time.monotonic(),
        "resolver": resolver,
        "choices": {choice_id: choice},
    }
    return {"hermes_kind": kind, "capability": nonce, "choice_id": choice_id}


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

    @pytest.mark.asyncio
    async def test_failed_connect_does_not_claim_persistence_owner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        conv_path = tmp_path / "failed-connect-owner.json"
        active = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        replacement = _make_adapter(
            monkeypatch, conversations_path=str(conv_path)
        )
        await active._activate_persist_owner()

        def fail_config() -> Any:
            raise RuntimeError("invalid replacement config")

        monkeypatch.setattr(replacement, "_make_bridge_config", fail_config)
        assert await replacement.connect() is False
        assert replacement._persist_owner_sequence is None

        active._conversations.upsert(
            ConversationRef(conversation_id="still-active", service_url="https://x/")
        )
        await active._persist_conversations()
        assert "still-active" in type(active._conversations).load(conv_path)

    @pytest.mark.asyncio
    async def test_failed_connect_cleanup_releases_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeServer:
            def __init__(self) -> None:
                self.should_exit = False

            async def serve(self) -> None:
                while not self.should_exit:
                    await asyncio.sleep(0)

        a = _make_adapter(monkeypatch)
        server = FakeServer()
        server_task = asyncio.create_task(server.serve())
        client = MagicMock()
        client.aclose = AsyncMock()
        a._uvicorn_server = server
        a._uvicorn_task = server_task
        a._http_client = client

        assert await a._run_failed_connect_cleanup() is False
        assert a._uvicorn_server is None
        assert a._uvicorn_task is None
        assert a._http_client is None
        client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_cancellation_cleans_runtime_then_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import types

        class FakeServer:
            def __init__(self, _config: Any) -> None:
                self.started = True
                self.should_exit = False

            async def serve(self) -> None:
                while not self.should_exit:
                    await asyncio.sleep(0)

        monkeypatch.setitem(
            sys.modules,
            "uvicorn",
            types.SimpleNamespace(
                Config=lambda *_args, **_kwargs: object(), Server=FakeServer
            ),
        )
        a = _make_adapter(monkeypatch)
        monkeypatch.setattr(a, "_make_bridge_config", MagicMock(return_value=MagicMock()))
        first_client = MagicMock()
        first_client.aclose = AsyncMock()
        a._http_client = first_client
        monkeypatch.setattr(
            a,
            "_activate_persist_owner",
            AsyncMock(side_effect=asyncio.CancelledError),
        )

        with pytest.raises(asyncio.CancelledError):
            await a.connect()
        assert a._uvicorn_server is None
        assert a._uvicorn_task is None
        assert a._http_client is None
        first_client.aclose.assert_awaited_once()

        second_client = MagicMock()
        second_client.aclose = AsyncMock()
        a._http_client = second_client
        monkeypatch.setattr(a, "_activate_persist_owner", AsyncMock(return_value=1))
        assert await a.connect() is True
        await a.disconnect()
        second_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_timeout_cleans_runtime_then_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import types

        start_now = {"value": False}

        class FakeServer:
            def __init__(self, _config: Any) -> None:
                self.should_exit = False

            @property
            def started(self) -> bool:
                return start_now["value"]

            async def serve(self) -> None:
                while not self.should_exit:
                    await asyncio.sleep(0)

        monkeypatch.setattr(adapter_mod, "_UVICORN_STARTUP_TIMEOUT_SEC", 0.01)
        monkeypatch.setitem(
            sys.modules,
            "uvicorn",
            types.SimpleNamespace(
                Config=lambda *_args, **_kwargs: object(), Server=FakeServer
            ),
        )
        a = _make_adapter(monkeypatch)
        monkeypatch.setattr(a, "_make_bridge_config", MagicMock(return_value=MagicMock()))
        first_client = MagicMock()
        first_client.aclose = AsyncMock()
        a._http_client = first_client

        assert await a.connect() is False
        assert a._uvicorn_server is None
        assert a._uvicorn_task is None
        assert a._http_client is None
        first_client.aclose.assert_awaited_once()

        start_now["value"] = True
        second_client = MagicMock()
        second_client.aclose = AsyncMock()
        a._http_client = second_client
        assert await a.connect() is True
        await a.disconnect()

    @pytest.mark.asyncio
    async def test_connect_early_server_death_cleans_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import types

        class DeadServer:
            started = False
            should_exit = False

            def __init__(self, _config: Any) -> None:
                pass

            async def serve(self) -> None:
                raise RuntimeError("bind failed")

        monkeypatch.setitem(
            sys.modules,
            "uvicorn",
            types.SimpleNamespace(
                Config=lambda *_args, **_kwargs: object(), Server=DeadServer
            ),
        )
        a = _make_adapter(monkeypatch)
        monkeypatch.setattr(a, "_make_bridge_config", MagicMock(return_value=MagicMock()))
        client = MagicMock()
        client.aclose = AsyncMock()
        a._http_client = client

        assert await a.connect() is False
        assert a._uvicorn_server is None
        assert a._uvicorn_task is None
        assert a._http_client is None
        client.aclose.assert_awaited_once()

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
        cfg_path.chmod(0o600)
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
        assert a.blueprint_client_secret == ""

    def test_profile_env_must_be_owner_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("A365_TENANT_ID=exposed-tenant\n")
        env_path.chmod(0o644)
        hermes_constants = types.ModuleType("hermes_constants")
        hermes_constants.get_hermes_home = lambda: tmp_path
        monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)

        with pytest.raises(
            RuntimeError, match=r"profile \.env is group/world-readable"
        ):
            adapter_mod.Agent365Adapter(_StubPlatformConfig())

    def test_preflight_rejects_loose_profile_env_before_scoped_reads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(
            "A365_TENANT_ID=exposed-tenant\nA365_APP_ID=exposed-app\n"
        )
        env_path.chmod(0o644)
        hermes_constants = types.ModuleType("hermes_constants")
        hermes_constants.get_hermes_home = lambda: tmp_path
        monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)

        assert adapter_mod.validate_config(_StubPlatformConfig()) is False

    def test_profile_secret_scope_beats_default_process_credentials(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class UnscopedSecretError(RuntimeError):
            pass

        scoped = {
            "AGENT_IDENTITY": "profile-b",
            "HERMES_BRIDGE_PORT": "4100",
            "A365_TENANT_ID": "profile-b-tenant",
            "A365_APP_ID": "profile-b-app",
            "A365_BLUEPRINT_CLIENT_SECRET": "profile-b-secret",
            "A365_ALLOWED_USERS": "profile-b-user",
            "A365_CONVERSATIONS_PATH": str(tmp_path / "profile-b.json"),
        }
        secret_scope = types.ModuleType("agent.secret_scope")
        secret_scope.UnscopedSecretError = UnscopedSecretError
        secret_scope.get_secret = lambda name, default=None: scoped.get(name, default)
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []  # type: ignore[attr-defined]
        agent_pkg.secret_scope = secret_scope
        monkeypatch.setitem(sys.modules, "agent", agent_pkg)
        monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)

        monkeypatch.setenv("A365_TENANT_ID", "default-tenant")
        monkeypatch.setenv("A365_APP_ID", "default-app")
        monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", "default-secret")
        monkeypatch.setenv("A365_BF_APP_ID", "default-bf-app")
        monkeypatch.setenv("A365_BF_CLIENT_SECRET", "default-bf-secret")

        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig())

        assert adapter.slug == "profile-b"
        assert adapter.port == 4100
        assert adapter.tenant_id == "profile-b-tenant"
        assert adapter.blueprint_app_id == "profile-b-app"
        assert adapter.blueprint_client_secret == "profile-b-secret"
        assert adapter.bf_app_id == ""
        assert adapter.bf_client_secret == ""
        assert adapter._allowed_users == ("profile-b-user",)
        assert adapter_mod.validate_config(_StubPlatformConfig()) is True

        scoped.clear()
        # A scoped miss is authoritative: validation must not borrow the
        # default profile's process-global tenant/app values.
        assert adapter_mod.validate_config(_StubPlatformConfig()) is False

    def test_profile_hermes_home_owns_runtime_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        hermes_constants = types.ModuleType("hermes_constants")
        hermes_constants.get_hermes_home = lambda: tmp_path
        monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
        monkeypatch.delenv("A365_CONVERSATIONS_PATH", raising=False)

        adapter = adapter_mod.Agent365Adapter(
            _StubPlatformConfig(
                extra={
                    "slug": "profile-b",
                    "tenant_id": "tenant",
                    "app_id": "app",
                    "blueprint_client_secret": "secret",
                }
            )
        )
        bridge_cfg = adapter._make_bridge_config()

        assert adapter._conversations_path == (
            tmp_path / "agents" / "profile-b" / "conversations.json"
        )
        assert bridge_cfg.log_path == tmp_path / "agents" / "profile-b" / "bridge.log"

    def test_unscoped_multiplex_default_rebuilds_profile_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class UnscopedSecretError(RuntimeError):
            pass

        scoped = {
            "AGENT_IDENTITY": "active-default",
            "A365_TENANT_ID": "scoped-tenant",
            "A365_APP_ID": "scoped-app",
            "A365_BLUEPRINT_CLIENT_SECRET": "scoped-secret",
            "A365_CONVERSATIONS_PATH": str(tmp_path / "scoped.json"),
        }
        secret_scope = types.ModuleType("agent.secret_scope")
        secret_scope.UnscopedSecretError = UnscopedSecretError
        secret_scope.get_secret = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            UnscopedSecretError()
        )
        secret_scope.is_multiplex_active = lambda: True
        secret_scope.build_profile_secret_scope = lambda home: (
            scoped if Path(home) == tmp_path else {}
        )
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []  # type: ignore[attr-defined]
        agent_pkg.secret_scope = secret_scope
        hermes_constants = types.ModuleType("hermes_constants")
        hermes_constants.get_hermes_home = lambda: tmp_path
        monkeypatch.setitem(sys.modules, "agent", agent_pkg)
        monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
        monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)

        monkeypatch.setenv("A365_TENANT_ID", "process-tenant")
        monkeypatch.setenv("A365_APP_ID", "process-app")
        monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", "process-secret")

        adapter = adapter_mod.Agent365Adapter(_StubPlatformConfig())

        assert adapter.tenant_id == "scoped-tenant"
        assert adapter.blueprint_app_id == "scoped-app"
        assert adapter.blueprint_client_secret == "scoped-secret"
        assert adapter._generated_config_path == tmp_path / "a365.generated.config.json"

    def test_unscoped_secret_api_skew_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class UnscopedSecretError(RuntimeError):
            pass

        secret_scope = types.ModuleType("agent.secret_scope")
        secret_scope.UnscopedSecretError = UnscopedSecretError
        secret_scope.get_secret = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            UnscopedSecretError()
        )
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []  # type: ignore[attr-defined]
        agent_pkg.secret_scope = secret_scope
        monkeypatch.setitem(sys.modules, "agent", agent_pkg)
        monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
        monkeypatch.setenv("A365_TENANT_ID", "another-profile-tenant")

        with pytest.raises(UnscopedSecretError):
            adapter_mod._profile_env("A365_TENANT_ID")

    def test_env_secret_does_not_bypass_generated_config_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "a365.generated.config.json"
        cfg_path.write_text(
            '{"agentBlueprintClientSecret": "stale-exposed-secret"}'
        )
        cfg_path.chmod(0o644)
        monkeypatch.setenv("A365_TENANT_ID", "tenant")
        monkeypatch.setenv("A365_APP_ID", "app")
        monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", "environment-secret")

        adapter = adapter_mod.Agent365Adapter(
            _StubPlatformConfig(
                extra={"generated_config_path": str(cfg_path)}
            )
        )

        with pytest.raises(RuntimeError, match="group/world-readable"):
            adapter._make_bridge_config()

    def test_make_bridge_config_raises_without_secret(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("A365_TENANT_ID", "t")
        monkeypatch.setenv("A365_APP_ID", "a")
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        # Generated config exists but has no secret.
        cfg_path = tmp_path / "a365.generated.config.json"
        cfg_path.write_text("{}")
        cfg_path.chmod(0o600)
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
            headers={"Authorization": "Bearer a.b.c"},
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

    def test_oversized_identity_is_acked_without_dispatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unroutable identity must stop before media work or Hermes."""
        from fastapi.testclient import TestClient

        a = _make_adapter(
            monkeypatch,
            conversations_path=str(tmp_path / "conversations.json"),
        )
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        extract_media = AsyncMock()
        monkeypatch.setattr(a, "_extract_inbound_media", extract_media)

        body = _make_inbound(
            conv_id="conv-unroutable-new",
            activity_id="x" * 40_000,
        )
        r = TestClient(a.build_app()).post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert r.status_code == 200
        assert r.json() == {"status": "acked", "reason": "unroutable"}
        assert a._handled_events == []
        assert a._conversations.get("conv-unroutable-new") is None
        assert "conv-unroutable-new" not in a._seen_inbounds_this_lifetime
        extract_media.assert_not_awaited()

    def test_unroutable_turn_does_not_reuse_cached_activity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A rejected turn must not dispatch against an older reply target."""
        from fastapi.testclient import TestClient

        a = _make_adapter(
            monkeypatch,
            conversations_path=str(tmp_path / "conversations.json"),
        )
        cached = _make_inbound(
            conv_id="conv-unroutable-cached",
            activity_id="activity-before-reject",
        )
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(cached))
        a._seen_inbounds_this_lifetime.add("conv-unroutable-cached")

        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        body = _make_inbound(
            conv_id="conv-unroutable-cached",
            activity_id="x" * 40_000,
        )
        r = TestClient(a.build_app()).post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert r.status_code == 200
        assert r.json() == {"status": "acked", "reason": "unroutable"}
        assert a._handled_events == []
        ref = a._conversations.get("conv-unroutable-cached")
        assert ref is not None
        assert ref.last_inbound_activity_id == "activity-before-reject"
        assert ref.raw["id"] == "activity-before-reject"

    @pytest.mark.parametrize("bad_conversation", [{}, {"id": 123}])
    def test_invalid_conversation_id_is_acked_without_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        bad_conversation: dict[str, Any],
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(
            monkeypatch,
            conversations_path=str(tmp_path / "conversations.json"),
        )
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        extract_media = AsyncMock()
        monkeypatch.setattr(a, "_extract_inbound_media", extract_media)
        body = _make_inbound()
        body["conversation"] = bad_conversation

        r = TestClient(a.build_app()).post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert r.status_code == 200
        assert r.json() == {"status": "acked", "reason": "unroutable"}
        assert a._handled_events == []
        assert len(a._conversations) == 0
        extract_media.assert_not_awaited()

    @pytest.mark.parametrize("bad_activity_id", [None, 123])
    def test_invalid_activity_id_is_acked_without_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        bad_activity_id: Any,
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(
            monkeypatch,
            conversations_path=str(tmp_path / "conversations.json"),
        )
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        extract_media = AsyncMock()
        monkeypatch.setattr(a, "_extract_inbound_media", extract_media)
        body = _make_inbound(conv_id="conv-invalid-activity-id")
        if bad_activity_id is None:
            body.pop("id")
        else:
            body["id"] = bad_activity_id

        r = TestClient(a.build_app()).post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert r.status_code == 200
        assert r.json() == {"status": "acked", "reason": "unroutable"}
        assert a._handled_events == []
        assert a._conversations.get("conv-invalid-activity-id") is None
        extract_media.assert_not_awaited()

    def test_repeated_unroutable_delivery_never_dispatches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(
            monkeypatch,
            conversations_path=str(tmp_path / "conversations.json"),
        )
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        extract_media = AsyncMock()
        monkeypatch.setattr(a, "_extract_inbound_media", extract_media)
        body = _make_inbound(
            conv_id="conv-unroutable-repeat",
            activity_id="x" * 40_000,
        )
        client = TestClient(a.build_app())
        headers = {"Authorization": "Bearer a.b.c"}

        first = client.post("/api/messages", json=body, headers=headers)
        second = client.post("/api/messages", json=body, headers=headers)

        assert first.status_code == second.status_code == 200
        assert first.json() == {"status": "acked", "reason": "unroutable"}
        assert second.json() == {"status": "duplicate"}
        assert a._handled_events == []
        assert a._conversations.get("conv-unroutable-repeat") is None
        extract_media.assert_not_awaited()

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
        headers = {"Authorization": "Bearer a.b.c"}
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
            headers={"Authorization": "Bearer a.b.c"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "acked"
        assert a._handled_events == []


class TestMessagesRoutePathBDispatch:
    """#34 — route handler peeks the unverified ``iss`` claim and
    dispatches to ``validate_inbound_jwt_bf`` for Path B (classic Bot
    Framework) tokens, or ``validate_inbound_jwt`` for Path A (A365 /
    AAD-v2) tokens. The peek is a routing hint only — both validators
    still do real signature checks, so a malformed ``Bearer a.b.c``
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

    def test_unparseable_token_is_rejected_before_validator_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed compact tokens never reach issuer parsing or validators."""
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
            headers={"Authorization": "Bearer malformed"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid bearer token"
        a365_validator.assert_not_awaited()
        bf_validator.assert_not_awaited()

    def test_bf_validator_failure_returns_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BF validator diagnostics stay server-side on a fixed public 403."""
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
        assert r.json()["detail"] == "invalid bearer token"
        assert "BF signature/aud/iss" not in r.text
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

    def test_disconnect_gate_rejects_authenticated_ingress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        a._disconnecting = True

        response = client.post(
            "/api/messages",
            json=_make_inbound(conv_id="conv-disconnecting"),
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert response.status_code == 503
        assert response.json()["reason"] == "disconnecting"
        assert "conv-disconnecting" not in a._conversations
        assert a._handled_events == []

    @pytest.mark.asyncio
    async def test_pre_auth_admission_rejects_excess_jwt_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        a = _make_adapter(monkeypatch)
        a._pre_auth_semaphore = asyncio.Semaphore(1)
        bridge = adapter_mod._import_bridge()
        jwt_started = asyncio.Event()
        release_jwt = asyncio.Event()

        async def blocked_jwt(**_kwargs: Any) -> dict[str, str]:
            jwt_started.set()
            await release_jwt.wait()
            return {"aud": "x", "iss": "y", "azp": "z"}

        monkeypatch.setattr(bridge, "validate_inbound_jwt", blocked_jwt)
        a._http_client = MagicMock()
        transport = httpx.ASGITransport(app=a.build_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            admitted = asyncio.create_task(
                client.post(
                    "/api/messages",
                    json=_make_inbound(conv_id="conv-pre-auth-one"),
                    headers={"Authorization": "Bearer a.b.c"},
                )
            )
            await jwt_started.wait()
            rejected = await client.post(
                "/api/messages/",
                json=_make_inbound(conv_id="conv-pre-auth-two"),
                headers={"Authorization": "Bearer a.b.c"},
            )
            release_jwt.set()
            accepted = await admitted

        assert rejected.status_code == 503
        assert rejected.json()["reason"] == "pre_auth_backlog_full"
        assert accepted.status_code == 200

    @pytest.mark.asyncio
    async def test_disconnect_tracks_cancellation_resistant_pre_auth_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        jwt_started = asyncio.Event()
        release_jwt = asyncio.Event()

        async def resistant_jwt(**_kwargs: Any) -> dict[str, str]:
            jwt_started.set()
            while not release_jwt.is_set():
                try:
                    await release_jwt.wait()
                except asyncio.CancelledError:
                    continue
            return {"aud": "x", "iss": "y", "azp": "z"}

        monkeypatch.setattr(bridge, "validate_inbound_jwt", resistant_jwt)
        a._http_client = MagicMock()
        transport = httpx.ASGITransport(app=a.build_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/api/messages",
                    json=_make_inbound(conv_id="conv-resistant-pre-auth"),
                    headers={"Authorization": "Bearer a.b.c"},
                )
            )
            await jwt_started.wait()
            await asyncio.wait_for(a.disconnect(), timeout=0.5)

            assert a._disconnecting is True
            assert a._lifecycle_owner_survivors

            release_jwt.set()
            response = await asyncio.wait_for(request, timeout=0.5)

        assert response.status_code == 503
        await asyncio.sleep(0)
        assert a._lifecycle_owner_survivors == {}
        assert a._disconnecting is False

    @pytest.mark.asyncio
    async def test_failed_connect_drains_post_start_ingress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        validator = AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"})
        monkeypatch.setattr(bridge, "validate_inbound_jwt", validator)
        monkeypatch.setattr(a, "_activate_persist_owner", AsyncMock(return_value=1))
        client_owner = MagicMock()
        client_owner.aclose = AsyncMock()
        a._http_client = client_owner
        a._connect_starting = True
        a._connect_failed = False
        a._connect_ready.clear()
        transport = httpx.ASGITransport(app=a.build_app())

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/api/messages",
                    json=_make_inbound(conv_id="conv-failed-connect-race"),
                    headers={"Authorization": "Bearer a.b.c"},
                )
            )
            while not a._pre_auth_tasks:
                await asyncio.sleep(0)
            assert not request.done()
            await a._cleanup_failed_connect_runtime()
            try:
                response = await request
            except asyncio.CancelledError:
                response = None

        if response is not None:
            assert response.status_code == 503
            assert response.json()["reason"] in {"connect_failed", "disconnecting"}

        validator.assert_not_awaited()
        assert "conv-failed-connect-race" not in a._conversations
        assert a._handled_events == []
        assert a._pre_auth_tasks == set()
        assert a._disconnecting is False

    @pytest.mark.asyncio
    async def test_pre_auth_admission_times_out_incomplete_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_PRE_AUTH_BODY_TIMEOUT_SEC", 0.01)
        a = _make_adapter(monkeypatch)
        semaphore = asyncio.Semaphore(1)

        async def body_reader(_scope: Any, receive: Any, _send: Any) -> None:
            await receive()

        async def stalled_receive() -> Any:
            await asyncio.Event().wait()

        sent: list[dict[str, Any]] = []

        async def capture(message: dict[str, Any]) -> None:
            sent.append(message)

        middleware = adapter_mod._PreAuthAdmissionMiddleware(
            body_reader,
            semaphore=semaphore,
            owner=a,
        )
        await middleware(
            {"type": "http", "method": "POST"},
            stalled_receive,
            capture,
        )

        assert sent[0]["status"] == 408
        assert b"request_body_timeout" in sent[1]["body"]
        assert semaphore._value == 1
        assert a._pre_auth_tasks == set()

    def test_active_turn_admission_rejects_before_registry_growth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_ACTIVE_AGENT_TURNS", 1)
        a = _make_adapter(monkeypatch)
        a._active_sessions["occupied-session"] = asyncio.Event()
        client = self._client(a, monkeypatch)

        response = client.post(
            "/api/messages",
            json=_make_inbound(conv_id="conv-over-active-cap"),
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert response.status_code == 503
        assert response.json()["reason"] == "active_turns_full"
        assert "conv-over-active-cap" not in a._conversations
        assert a._handled_events == []

    def test_retiring_chat_acks_without_upsert_or_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        a._registry_evicting_chats.add("conv-retiring-ingress")

        response = client.post(
            "/api/messages",
            json=_make_inbound(conv_id="conv-retiring-ingress"),
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert response.status_code == 200
        assert response.json()["reason"] == "conversation_retiring"
        assert "conv-retiring-ingress" not in a._conversations
        assert a._handled_events == []

    def test_deferred_uninstall_retry_is_not_deduplicated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_CHAT_LIFECYCLE_GENERATIONS", 1)
        a = _make_adapter(monkeypatch)
        body = self._lifecycle_body(
            conv_id="conv-uninstall-retry",
            type="installationUpdate",
            action="remove",
        )
        ref = adapter_mod.ConversationRef.from_activity(body)
        assert ref is not None
        a._conversations.upsert(ref)
        a._chat_lifecycle_generation["conv-live-epoch"] = 1
        a._chat_lifecycle_sequence = 1
        a._active_sessions["session-live-epoch"] = asyncio.Event()
        a._session_key_to_conv["session-live-epoch"] = "conv-live-epoch"
        client = self._client(a, monkeypatch)

        deferred = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )
        a._active_sessions.pop("session-live-epoch")
        retried = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert deferred.status_code == 503
        assert deferred.json()["reason"] == "eviction_backlog_full"
        assert retried.status_code == 200
        assert retried.json()["lifecycle"] == "evict"
        assert "conv-uninstall-retry" not in a._conversations

    def test_successful_uninstall_retry_cannot_cross_reinstall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        removed_body = self._lifecycle_body(
            conv_id="conv-lifecycle-dedupe",
            id="remove-delivery",
            type="installationUpdate",
            action="remove",
        )
        installed_body = self._lifecycle_body(
            conv_id="conv-lifecycle-dedupe",
            id="install-delivery",
            type="installationUpdate",
            action="add",
        )

        removed = client.post(
            "/api/messages",
            json=removed_body,
            headers={"Authorization": "Bearer a.b.c"},
        )
        installed = client.post(
            "/api/messages",
            json=installed_body,
            headers={"Authorization": "Bearer a.b.c"},
        )
        stale_retry = client.post(
            "/api/messages",
            json=removed_body,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert removed.status_code == 200
        assert installed.status_code == 200
        assert stale_retry.json()["status"] == "duplicate"
        assert "conv-lifecycle-dedupe" in a._conversations

    @pytest.mark.asyncio
    async def test_uninstall_cancels_inflight_message_before_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        a._http_client = MagicMock()
        persist_started = asyncio.Event()
        persist_calls = 0

        async def delayed_first_persist(_reservation: Any = None) -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                persist_started.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(a, "_persist_conversations", delayed_first_persist)
        transport = httpx.ASGITransport(app=a.build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            message = asyncio.create_task(
                client.post(
                    "/api/messages",
                    json=_make_inbound(conv_id="conv-race-uninstall"),
                    headers={"Authorization": "Bearer a.b.c"},
                )
            )
            await persist_started.wait()
            uninstall = await client.post(
                "/api/messages",
                json=self._lifecycle_body(
                    conv_id="conv-race-uninstall",
                    type="installationUpdate",
                    action="remove",
                ),
                headers={"Authorization": "Bearer a.b.c"},
            )

        assert uninstall.status_code == 200
        with pytest.raises(asyncio.CancelledError):
            await message
        assert "conv-race-uninstall" not in a._conversations
        assert a._handled_events == []

    @pytest.mark.asyncio
    async def test_old_agent_turn_cannot_send_after_reinstall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        turn_started = asyncio.Event()
        release_turn = asyncio.Event()
        turn_tasks: list[asyncio.Task[Any]] = []
        turn_results: list[Any] = []

        async def resistant_turn() -> None:
            turn_started.set()
            while not release_turn.is_set():
                try:
                    await release_turn.wait()
                except asyncio.CancelledError:
                    continue
            turn_results.append(await a.send("conv-turn-reinstall", "stale"))

        async def spawn_turn(event: Any) -> None:
            session_key = a._session_key_for(event)
            assert session_key is not None
            task = asyncio.create_task(resistant_turn())
            turn_tasks.append(task)
            a._active_sessions[session_key] = asyncio.Event()
            a._session_tasks[session_key] = task

        monkeypatch.setattr(a, "handle_message", spawn_turn)
        transport = httpx.ASGITransport(app=a.build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            dispatched = await asyncio.create_task(
                client.post(
                    "/api/messages",
                    json=_make_inbound(conv_id="conv-turn-reinstall"),
                    headers={"Authorization": "Bearer a.b.c"},
                )
            )
            await turn_started.wait()
            removed = await client.post(
                "/api/messages",
                json=self._lifecycle_body(
                    conv_id="conv-turn-reinstall",
                    type="installationUpdate",
                    action="remove",
                ),
                headers={"Authorization": "Bearer a.b.c"},
            )
            installed = await client.post(
                "/api/messages",
                json=self._lifecycle_body(
                    conv_id="conv-turn-reinstall",
                    type="installationUpdate",
                    action="add",
                ),
                headers={"Authorization": "Bearer a.b.c"},
            )

        assert dispatched.status_code == 200
        assert removed.status_code == 200
        assert installed.status_code == 200
        release_turn.set()
        await asyncio.wait_for(turn_tasks[0], timeout=0.5)
        assert turn_results[0].success is False
        assert "agent turn lifecycle changed" in str(turn_results[0].error)
        assert a._http_client.post.await_count == 0

    @pytest.mark.asyncio
    async def test_request_blocked_in_jwt_cannot_cross_uninstall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        monkeypatch.setattr(adapter_mod, "_MAX_CHAT_LIFECYCLE_GENERATIONS", 1)
        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        jwt_started = asyncio.Event()
        release_jwt = asyncio.Event()
        validation_calls = 0

        async def delayed_first_jwt(**_kwargs: Any) -> dict[str, str]:
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 1:
                jwt_started.set()
                while not release_jwt.is_set():
                    try:
                        await release_jwt.wait()
                    except asyncio.CancelledError:
                        continue
            return {"aud": "x", "iss": "y", "azp": "z"}

        monkeypatch.setattr(bridge, "validate_inbound_jwt", delayed_first_jwt)
        a._http_client = MagicMock()
        transport = httpx.ASGITransport(app=a.build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            delayed = asyncio.create_task(
                client.post(
                    "/api/messages",
                    json=_make_inbound(conv_id="conv-jwt-race"),
                    headers={"Authorization": "Bearer a.b.c"},
                )
            )
            await jwt_started.wait()
            removed = await client.post(
                "/api/messages",
                json=self._lifecycle_body(
                    conv_id="conv-jwt-race",
                    type="installationUpdate",
                    action="remove",
                ),
                headers={"Authorization": "Bearer a.b.c"},
            )
            churn_deferred = await a._teardown_chat_state("conv-jwt-churn")
            release_jwt.set()
            stale = await delayed

        assert removed.status_code == 200
        assert churn_deferred is False
        assert "conv-jwt-race" in a._chat_lifecycle_generation
        assert stale.status_code == 503
        assert stale.json()["reason"] == "lifecycle_changed"
        assert "conv-jwt-race" not in a._conversations
        assert a._handled_events == []

    @pytest.mark.asyncio
    async def test_disconnect_bounds_stalled_durable_eviction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01)
        a = _make_adapter(monkeypatch)
        ref = adapter_mod.ConversationRef.from_activity(
            _make_inbound(conv_id="conv-stalled-eviction")
        )
        assert ref is not None
        a._conversations.upsert(ref)
        a._http_client = MagicMock()
        a._http_client.aclose = AsyncMock()
        persist_started = asyncio.Event()
        release_persist = asyncio.Event()
        release_reply_survivor = asyncio.Event()

        reply_survivor = asyncio.create_task(release_reply_survivor.wait())
        a._track_coalesced_reply_survivor(
            reply_survivor, "conv-existing-survivor"
        )

        async def stalled_persist(_reservation: Any = None) -> None:
            persist_started.set()
            await release_persist.wait()

        monkeypatch.setattr(a, "_persist_conversations", stalled_persist)
        eviction = asyncio.create_task(a._evict_conversation("conv-stalled-eviction"))
        await persist_started.wait()

        await asyncio.wait_for(a.disconnect(), timeout=0.5)
        assert a._disconnecting is True
        assert a._coalesced_reply_survivors
        assert a._lifecycle_owner_survivors

        release_reply_survivor.set()
        await asyncio.wait_for(reply_survivor, timeout=0.5)
        await asyncio.sleep(0)
        assert a._coalesced_reply_survivors == {}
        assert a._disconnecting is True

        release_persist.set()
        assert await asyncio.wait_for(eviction, timeout=0.5) is True
        await asyncio.sleep(0)
        assert a._lifecycle_owner_survivors == {}
        assert a._disconnecting is False

    @pytest.mark.asyncio
    async def test_disconnect_tracks_outer_eviction_across_nested_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_INBOUND_TASKS", 1)
        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        a = _make_adapter(monkeypatch)
        release_teardown = asyncio.Event()
        persistence_started = asyncio.Event()
        release_persistence = asyncio.Event()

        async def nested_teardown() -> None:
            await release_teardown.wait()

        teardown = asyncio.create_task(nested_teardown())

        async def durable_owner() -> None:
            await teardown
            persistence_started.set()
            await release_persistence.wait()

        owner = asyncio.create_task(durable_owner())
        a._chat_teardown_tasks["conv-nested-owner"] = teardown
        a._registry_eviction_tasks["conv-nested-owner"] = owner

        await asyncio.wait_for(a.disconnect(), timeout=0.5)
        assert a._lifecycle_owner_survivors == {
            owner: "conv-nested-owner"
        }

        release_teardown.set()
        await persistence_started.wait()
        await asyncio.sleep(0)
        assert a._disconnecting is True

        release_persistence.set()
        await asyncio.wait_for(owner, timeout=0.5)
        await asyncio.sleep(0)
        assert a._lifecycle_owner_survivors == {}
        assert a._disconnecting is False

    @pytest.mark.asyncio
    async def test_cancelled_uninstall_still_persists_eviction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        ref = adapter_mod.ConversationRef.from_activity(
            _make_inbound(conv_id="conv-cancelled-eviction")
        )
        assert ref is not None
        a._conversations.upsert(ref)
        persist_started = asyncio.Event()
        release_persist = asyncio.Event()
        persisted = asyncio.Event()

        async def delayed_persist(_reservation: Any = None) -> None:
            persist_started.set()
            await release_persist.wait()
            persisted.set()

        monkeypatch.setattr(a, "_persist_conversations", delayed_persist)
        eviction = asyncio.create_task(a._evict_conversation("conv-cancelled-eviction"))
        await persist_started.wait()
        eviction.cancel()
        await asyncio.sleep(0)
        assert eviction.done() is False
        release_persist.set()
        with pytest.raises(asyncio.CancelledError):
            await eviction

        assert persisted.is_set()
        assert "conv-cancelled-eviction" not in a._conversations
        assert a._registry_eviction_tasks == {}
        assert a._registry_evicting_chats == set()

    @pytest.mark.asyncio
    async def test_distinct_evictions_are_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_INBOUND_TASKS", 1)
        a = _make_adapter(monkeypatch)
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def blocked_eviction(
            chat_id: str,
            reservation: tuple[dict[str, Any], int, int],
        ) -> bool:
            assert chat_id == "conv-eviction-one"
            first_started.set()
            await release_first.wait()
            adapter_mod._complete_persist_mutation(
                reservation[0], reservation[1]
            )
            return True

        monkeypatch.setattr(a, "_evict_conversation_impl", blocked_eviction)
        first = asyncio.create_task(a._evict_conversation("conv-eviction-one"))
        await first_started.wait()

        assert await a._evict_conversation("conv-eviction-two") is False
        release_first.set()
        assert await first is True

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
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
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
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
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
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
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
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
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
            "/api/messages", json=msg, headers={"Authorization": "Bearer a.b.c"}
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
            "/api/messages", json=lc, headers={"Authorization": "Bearer a.b.c"}
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
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
        )
        assert r.json() == {"status": "acked", "lifecycle": "evict"}
        assert a._handled_events == []
        assert "conv-rm" not in a._conversations
        assert "conv-rm" not in a._seen_inbounds_this_lifetime

    def test_installation_remove_evicts_despite_oversized_activity_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        seed = _make_inbound(path="B", conv_id="conv-rm-oversized")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(seed))
        a._seen_inbounds_this_lifetime.add("conv-rm-oversized")
        teardown = MagicMock(wraps=a._teardown_chat_state)
        monkeypatch.setattr(a, "_teardown_chat_state", teardown)
        client = self._client(a, monkeypatch)
        body = self._lifecycle_body(
            conv_id="conv-rm-oversized",
            id="x" * 40_000,
            type="installationUpdate",
            action="remove",
        )

        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
        )

        assert r.json() == {"status": "acked", "lifecycle": "evict"}
        assert a._handled_events == []
        teardown.assert_called_once_with("conv-rm-oversized")
        assert "conv-rm-oversized" not in a._conversations
        assert "conv-rm-oversized" not in a._seen_inbounds_this_lifetime

    def test_plugin_honors_non_default_idempotency_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch, idempotency_max_entries=2)
        a.build_app()
        assert a._idempotency_cache.max_entries == 2
        assert a._make_bridge_config().idempotency_max_entries == 2

    def test_plugin_bool_idempotency_cap_falls_back_not_one(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # #105 review: platform `extra` is the ONLY place this cap is
        # operator-supplied, and `idempotency_max_entries: true` is a plausible
        # mis-entry. The adapter must NOT pre-coerce it — `int(True)` is 1,
        # which would silently near-destroy dedupe (any interleaved delivery
        # evicts the previous entry) and bypass the cache's own normalization.
        from hermes_a365.activity_bridge import DEFAULT_IDEMPOTENCY_MAX_ENTRIES

        caplog.set_level("WARNING")
        a = _make_adapter(monkeypatch, idempotency_max_entries=True)
        a.build_app()
        assert a._idempotency_cache.max_entries == DEFAULT_IDEMPOTENCY_MAX_ENTRIES
        assert "not an integer" in caplog.text

    def test_plugin_float_idempotency_cap_falls_back_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same class: 3.7 must not silently become a cap of 3.
        from hermes_a365.activity_bridge import DEFAULT_IDEMPOTENCY_MAX_ENTRIES

        a = _make_adapter(monkeypatch, idempotency_max_entries=3.7)
        a.build_app()
        assert a._idempotency_cache.max_entries == DEFAULT_IDEMPOTENCY_MAX_ENTRIES

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
        a._streams[("conv-rm", "sid-1")] = {"bf_stream_id": "sid-1"}
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
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
        )
        assert r.json() == {"status": "acked", "lifecycle": "evict"}

        assert "conv-rm" not in a._active_stream_by_chat
        assert ("conv-rm", "sid-1") not in a._streams
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
                headers={"Authorization": "Bearer a.b.c"},
            )
            assert r.status_code == 200, r.text
        assert len(a._seen_inbounds_this_lifetime) <= 3

    def test_seen_inbounds_bound_never_evicts_the_current_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # L2 (#105) routing invariant: the bound must never evict the chat
        # just received — send()'s reply-vs-proactive gate keys off its
        # membership in _seen_inbounds_this_lifetime, so evicting it would
        # misroute THIS turn's response to sendToConversation.
        monkeypatch.setattr(adapter_mod, "_MAX_SEEN_INBOUNDS", 4)
        a = _make_adapter(monkeypatch)
        # Fill the seen-set to the cap with unrelated chats.
        for i in range(4):
            a._seen_inbounds_this_lifetime.add(f"other-{i}")
        assert len(a._seen_inbounds_this_lifetime) == 4
        client = self._client(a, monkeypatch)
        body = _make_inbound(
            path="B", conv_id="fresh-chat", activity_id="act-fresh"
        )
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
        )
        assert r.status_code == 200, r.text
        # The just-received chat is retained (so send() takes the reply path,
        # not proactive) and the cap still holds.
        assert "fresh-chat" in a._seen_inbounds_this_lifetime
        assert len(a._seen_inbounds_this_lifetime) <= 4

    def test_registry_cap_enforced_on_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # M11 (#105): the durable registry is bounded on the dispatch hot
        # path. With a low cap, posting more distinct chats than the cap keeps
        # the registry bounded (enforce_cap is wired in after upsert).
        monkeypatch.setattr(adapter_mod, "_MAX_REGISTRY_ENTRIES", 3)
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        for i in range(8):
            body = _make_inbound(
                path="B", conv_id=f"reg-{i}", activity_id=f"act-{i}"
            )
            r = client.post(
                "/api/messages",
                json=body,
                headers={"Authorization": "Bearer a.b.c"},
            )
            assert r.status_code == 200, r.text
        assert len(a._conversations) <= 3
        # The most-recent chat survives (LRU keeps newest).
        assert "reg-7" in a._conversations

    def test_registry_cap_skips_in_flight_conversation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # M11 (#105): enforce_cap must not evict a conversation whose turn is
        # in flight — bridged from the base's whole-turn _active_sessions
        # (session-key space) to the registry's conversation-id space via
        # _session_key_to_conv — even when it's the stale/LRU entry. This is
        # the fix for the session-key-vs-conversation-id mismatch that made the
        # active guard a no-op (and covers a turn suspended on a human, whose
        # _active_sessions guard is still held).
        import asyncio as _asyncio

        monkeypatch.setattr(adapter_mod, "_MAX_REGISTRY_ENTRIES", 2)
        a = _make_adapter(monkeypatch)
        for cid in ("stale-inflight", "stale-idle"):
            a._conversations.upsert(
                adapter_mod.ConversationRef(
                    conversation_id=cid, service_url="https://x/"
                ),
                now=1000.0,  # both ancient / LRU
            )
        # A turn is in flight for "stale-inflight": the base holds its session
        # guard, and we have the session_key → conversation_id mapping.
        a._active_sessions["sk:stale-inflight"] = _asyncio.Event()
        a._session_key_to_conv["sk:stale-inflight"] = "stale-inflight"

        client = self._client(a, monkeypatch)
        # A fresh inbound pushes the registry over cap (2) → enforce_cap runs.
        body = _make_inbound(path="B", conv_id="fresh", activity_id="act-f")
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
        )
        assert r.status_code == 200, r.text
        # The in-flight stale entry survived; the idle stale one was evicted.
        assert "stale-inflight" in a._conversations
        assert "stale-idle" not in a._conversations
        assert len(a._conversations) <= 2

    def test_registry_cap_never_evicts_current_turn_when_saturated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # M11 (#105): if pinned/in-flight entries already fill the cap, the
        # just-upserted CURRENT-turn reply target must not be evicted — better
        # to sit over cap than lose the in-progress reply. (Recency alone
        # doesn't protect it when it's the sole non-pinned/non-active
        # candidate; the current id is added to the protected set explicitly.)
        monkeypatch.setattr(adapter_mod, "_MAX_REGISTRY_ENTRIES", 2)
        a = _make_adapter(monkeypatch)
        for cid in ("pin-0", "pin-1"):
            a._conversations.upsert(
                adapter_mod.ConversationRef(
                    conversation_id=cid, service_url="https://x/"
                ),
                now=1000.0,
            )
            a._conversations.pin(cid)

        client = self._client(a, monkeypatch)
        body = _make_inbound(path="B", conv_id="current-turn", activity_id="act-c")
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
        )
        assert r.status_code == 200, r.text
        # Current-turn entry survives (its reply target); pinned entries too.
        assert "current-turn" in a._conversations

    def test_registry_cap_enforced_on_lifecycle_capture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # M11 (#105) review: the cap is wired after the LIFECYCLE upsert too,
        # not just the message-dispatch upsert. A gateway spammed with
        # installation/bot-added lifecycle events (which never reach the agent
        # loop) must not grow the durable registry without bound. Fill past a
        # small cap via lifecycle activities and confirm the newest lifecycle
        # target survives while the registry stays bounded.
        monkeypatch.setattr(adapter_mod, "_MAX_REGISTRY_ENTRIES", 3)
        a = _make_adapter(monkeypatch)
        client = self._client(a, monkeypatch)
        for i in range(8):
            body = self._lifecycle_body(
                conv_id=f"lc-{i}",
                id=f"cu-{i}",
                type="conversationUpdate",
                membersAdded=[{"id": "agent-1"}],
            )
            r = client.post(
                "/api/messages",
                json=body,
                headers={"Authorization": "Bearer a.b.c"},
            )
            assert r.json() == {"status": "acked", "lifecycle": "upsert"}
        assert len(a._conversations) <= 3
        # The most-recent lifecycle target survives (LRU keeps newest).
        assert "lc-7" in a._conversations


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
            headers={"Authorization": "Bearer a.b.c"},
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
            headers={"Authorization": "Bearer a.b.c"},
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
            headers={"Authorization": "Bearer a.b.c"},
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
            headers={"Authorization": "Bearer a.b.c"},
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
            headers={"Authorization": "Bearer a.b.c"},
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

    def test_from_dict_reprojects_deep_raw_preserving_routing(
        self, tmp_path: Path
    ) -> None:
        # M11 (#105) review: M10 guards a NON-dict raw, but a PRE-M11 / corrupted
        # file can carry a *dict* raw nested arbitrarily deep. The READ side
        # re-projects it through the same size-bounded allowlist: the deep bloat
        # is dropped (so to_payload()/asdict() can't RecursionError on the next
        # save) while the identity/routing subkeys are PRESERVED (a pinned
        # proactive target keeps working across an upgrade).
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        deep: Any = "leaf"
        for _ in range(500):
            deep = {"n": deep}
        ref = ConversationRef.from_dict(
            {
                "conversation_id": "conv-deep",
                "service_url": "https://x/",
                "raw": {
                    "conversation": {"id": "conv-deep", "bloat": deep},
                    "serviceUrl": "https://x/",
                },
            }
        )
        # Routing preserved, deep bloat gone, whole raw bounded.
        assert ref.raw["conversation"]["id"] == "conv-deep"
        assert "bloat" not in ref.raw["conversation"]
        assert len(json.dumps(ref.raw, default=str)) < 16_384
        # And a full registry round-trip (save → load) doesn't crash.
        reg = ConversationRegistry()
        reg.upsert(ref)
        path = tmp_path / "c.json"
        reg.save(path)  # asdict() over the flattened raw — no recursion
        assert ConversationRegistry.load(path).get("conv-deep") is not None

    def test_from_dict_coerces_unroutable_oversized_raw_to_empty(self) -> None:
        # M11 (#105) review: when even the minimal identity projection can't
        # fit — any retained identity field, here the display name
        # conversation.name (the projection is all-or-nothing) — the read side
        # coerces raw to {} rather than admit an unbounded entry.
        from hermes_a365.plugin.conversations import ConversationRef

        ref = ConversationRef.from_dict(
            {
                "conversation_id": "conv-big",
                "service_url": "https://x/",
                "raw": {"conversation": {"id": "conv-big", "name": "x" * 40_000}},
            }
        )
        assert ref.raw == {}

    def test_load_survives_recursionerror_on_parse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # M11 (#105) review: a file nested past the interpreter recursion limit
        # makes json.loads itself raise RecursionError (only JSONDecodeError was
        # caught) — that must yield an empty registry, not crash adapter
        # construction.
        from hermes_a365.plugin import conversations as _conv_mod
        from hermes_a365.plugin.conversations import ConversationRegistry

        path = tmp_path / "c.json"
        path.write_text('{"schema": 1, "conversations": []}')

        def _boom(*_a: object, **_k: object) -> object:
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(_conv_mod.json, "loads", _boom)
        reg = ConversationRegistry.load(path)
        assert len(reg) == 0

    def test_from_activity_trims_large_keys_from_cached_raw(self) -> None:
        # M11 (#105): the cached raw drops large inbound-only keys
        # (attachments/channelData/…) but keeps whole the identity + routing
        # dicts the outbound/mint paths echo/read.
        import copy as _copy

        activity = _make_inbound()
        activity["attachments"] = [{"contentType": "image/png", "contentUrl": "x"}]
        activity["channelData"] = {"big": "x" * 1000}
        activity["entities"] = [{"type": "mention"}]
        activity["text"] = "hello there"
        activity["value"] = {"cardAction": "y"}
        original = _copy.deepcopy(activity)

        ref = adapter_mod.ConversationRef.from_activity(activity)
        assert ref is not None
        # Large inbound-only keys stripped from the cache…
        for k in ("attachments", "channelData", "entities", "text", "value"):
            assert k not in ref.raw
        # …identity / routing keys kept WHOLE (echoed into outbound).
        assert ref.raw["conversation"] == activity["conversation"]
        assert ref.raw["recipient"] == activity["recipient"]
        assert ref.raw["from"] == activity["from"]
        assert ref.raw["serviceUrl"] == activity["serviceUrl"]
        assert ref.raw["id"] == activity["id"]
        # Constraint: the PASSED-IN activity is not mutated — the capturing
        # turn still reads attachments/text for media/mention extraction.
        assert activity == original

    def test_from_activity_drops_unknown_oversized_top_level_field(self) -> None:
        # M11 (#105) review: the cache is an ALLOWLIST, not a denylist — an
        # unknown top-level key (any size, any future BF field) is never
        # cached, so a large unforeseen payload can't bloat the registry.
        activity = _make_inbound()
        activity["extensionData"] = {"blob": "x" * 40_000}

        ref = adapter_mod.ConversationRef.from_activity(activity)
        assert ref is not None
        assert "extensionData" not in ref.raw
        # Identity/routing still cached whole (kept set stays small).
        assert ref.raw["conversation"] == activity["conversation"]
        assert ref.raw["from"] == activity["from"]

    def test_from_activity_bounds_oversized_nested_field(self) -> None:
        # M11 (#105) review: even a RETAINED top-level object (conversation)
        # can't smuggle unbounded bytes in — when the kept projection exceeds
        # the byte ceiling the cache falls back to a minimal per-key subset,
        # dropping the bloat while keeping the routing subkeys.
        activity = _make_inbound()
        activity["conversation"]["description"] = "x" * 40_000

        ref = adapter_mod.ConversationRef.from_activity(activity)
        assert ref is not None
        conv = ref.raw["conversation"]
        # Bloat dropped; the id/tenantId routing subkeys survive.
        assert conv.get("description") is None
        assert conv["id"] == activity["conversation"]["id"]
        assert conv.get("tenantId") == activity["conversation"].get("tenantId")
        # Whole cached raw is bounded well under the huge inbound.
        assert len(json.dumps(ref.raw, default=str)) < 16_384

    def test_from_activity_rejects_oversized_required_scalar(self) -> None:
        # M11 (#105) re-review: a routing-critical *scalar* that is itself
        # oversized (here the activity `id`) can't be shrunk by any projection.
        # We must NOT truncate it (that would silently retarget the reply), so
        # from_activity rejects the whole activity as unroutable → no unbounded
        # durable entry, and no oversized top-level ConversationRef fields.
        activity = _make_inbound(activity_id="x" * 40_000)
        assert adapter_mod.ConversationRef.from_activity(activity) is None

    def test_from_activity_rejects_oversized_nested_routing_value(self) -> None:
        # M11 (#105) re-review: an oversized value under a RETAINED routing
        # subkey (conversation.id — which is also the top-level
        # conversation_id) survives the minimal projection, so the projection
        # still can't fit the ceiling → reject rather than truncate the id.
        activity = _make_inbound()
        activity["conversation"]["id"] = "c" * 40_000
        assert adapter_mod.ConversationRef.from_activity(activity) is None

    def test_from_activity_rejects_oversized_service_url(self) -> None:
        # M11 (#105) re-review: same shape for serviceUrl — an oversized URL is
        # rejected, never truncated (truncation would misroute the outbound
        # POST / token audience).
        activity = _make_inbound(service_url="https://x/" + "y" * 40_000)
        assert adapter_mod.ConversationRef.from_activity(activity) is None

    def test_from_activity_bounds_deeply_nested_kept_object(self) -> None:
        # M11 (#105) re-review: a deeply-nested value under a RETAINED object is
        # cheap COMPACT but balloons under indent=2 on disk (indentation cost
        # grows with depth). The size gate measures the persisted (indented)
        # form, so the whole-conversation fast path is rejected and the flat
        # minimal projection (which drops the nested value) is used instead.
        activity = _make_inbound()
        deep: Any = "leaf"
        for _ in range(300):
            deep = [deep]  # 300-deep nested list: tiny compact, huge indented
        activity["conversation"]["topic"] = deep

        ref = adapter_mod.ConversationRef.from_activity(activity)
        assert ref is not None
        # The deep value was dropped by the minimal projection…
        assert "topic" not in ref.raw["conversation"]
        assert ref.raw["conversation"]["id"] == activity["conversation"]["id"]
        # …and the persisted (indented) form is within the ceiling.
        indented = json.dumps(ref.raw, indent=2, sort_keys=True, default=str)
        assert len(indented) <= 16_384

    def test_accepted_cached_raw_always_within_ceiling(self) -> None:
        # M11 (#105) re-review invariant: EVERY accepted ref's raw, serialized
        # THE WAY IT IS PERSISTED (indent=2, sort_keys — matching write_payload),
        # is <= _RAW_MAX_BYTES — the hard per-entry ON-DISK storage bound. Spans
        # small/normal, dropped-nested-bloat, deep-nesting, and boundary cases;
        # oversized routing fields are rejected (None) rather than accepted.
        from hermes_a365.plugin.conversations import ConversationRef

        ceiling = ConversationRef._RAW_MAX_BYTES
        cases = [
            _make_inbound(),  # normal
            _make_inbound(path="B"),  # classic BF shape
            _make_inbound(text="hi", conv_id="c2", activity_id="a2"),
        ]
        # Oversized non-routing nested bloat → accepted but bounded (dropped).
        bloat = _make_inbound(conv_id="c3", activity_id="a3")
        bloat["conversation"]["description"] = "z" * 40_000
        bloat["channelData"] = {"blob": "z" * 40_000}
        cases.append(bloat)
        # Deeply-nested kept object → accepted, nested value dropped.
        deep_case = _make_inbound(conv_id="c4", activity_id="a4")
        _deep: Any = "leaf"
        for _ in range(250):
            _deep = [_deep]
        deep_case["conversation"]["topic"] = _deep
        cases.append(deep_case)
        # Oversized routing scalar → rejected (None), never accepted oversized.
        oversized = _make_inbound(activity_id="q" * 40_000)

        for act in cases:
            ref = ConversationRef.from_activity(act)
            assert ref is not None
            # Bound the PERSISTED (indented) form — the actual on-disk artifact.
            on_disk = json.dumps(ref.raw, indent=2, sort_keys=True, default=str)
            assert len(on_disk) <= ceiling
        assert ConversationRef.from_activity(oversized) is None


class TestRegistryEnforceCap:
    """#105/M11 — ConversationRegistry.enforce_cap bounds the registry with
    pin/active-aware LRU eviction."""

    @staticmethod
    def _reg(n: int):
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        reg = ConversationRegistry()
        for i in range(n):
            # last_used_at ascending: conv-0 is oldest, conv-(n-1) newest.
            reg.upsert(
                ConversationRef(conversation_id=f"conv-{i}", service_url="https://x/"),
                now=1000.0 + i,
            )
        return reg

    def test_no_cap_when_within_limit(self) -> None:
        reg = self._reg(5)
        assert reg.enforce_cap(10) == 0
        assert len(reg) == 5

    def test_none_max_disables_cap(self) -> None:
        reg = self._reg(5)
        assert reg.enforce_cap(None) == 0
        assert len(reg) == 5

    def test_evicts_least_recently_used_down_to_cap(self) -> None:
        reg = self._reg(10)
        dropped = reg.enforce_cap(4)
        assert dropped == 6
        assert len(reg) == 4
        # The 4 most-recently-used survive; the oldest are gone.
        assert "conv-0" not in reg
        assert "conv-5" not in reg
        assert "conv-9" in reg and "conv-6" in reg

    def test_never_evicts_pinned_even_if_oldest(self) -> None:
        reg = self._reg(6)
        reg.pin("conv-0")  # oldest, but pinned
        reg.enforce_cap(3)
        assert "conv-0" in reg  # pinned survives
        assert len(reg) == 3

    def test_never_evicts_active_conversation(self) -> None:
        reg = self._reg(6)
        # conv-1 is old but has a Hermes turn in flight (by conversation id).
        reg.enforce_cap(3, active_conversation_ids={"conv-1"})
        assert "conv-1" in reg
        assert len(reg) == 3

    def test_pinned_over_cap_are_all_kept(self) -> None:
        # If everything droppable is pinned, the registry may stay over cap
        # rather than evict a pinned entry.
        reg = self._reg(5)
        for i in range(5):
            reg.pin(f"conv-{i}")
        assert reg.enforce_cap(2) == 0
        assert len(reg) == 5


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
            headers={"Authorization": "Bearer a.b.c"},
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
        ref = adapter_mod.ConversationRef.from_activity(inbound)
        assert ref is not None
        recipient = inbound.get("recipient") or {}
        ref.validated_path = "A" if recipient.get("agenticAppId") else "B"
        a._capture_coalesced_turn_target(ref)
        a._conversations.upsert(ref)
        a._active_stream_by_chat["conv-1"] = "m1"
        a._streams[("conv-1", "m1")] = {
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
        ref = adapter_mod.ConversationRef.from_activity(inbound)
        assert ref is not None
        recipient = inbound.get("recipient") or {}
        ref.validated_path = "A" if recipient.get("agenticAppId") else "B"
        a._capture_coalesced_turn_target(ref)
        a._conversations.upsert(ref)
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
    async def test_personal_stream_duplicate_finalize_remains_a_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-S-double-final")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-1"})
        final = MagicMock(status_code=202, text="", json=lambda: {})
        post_mock = self._wire_adapter(
            a, inbound=inbound, post_responses=[first, final]
        )
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)

        await a.edit_message("conv-S-double-final", "m1", "Hi", finalize=False)
        await a.edit_message("conv-S-double-final", "m1", "Done", finalize=True)
        duplicate = await a.edit_message(
            "conv-S-double-final", "m1", "Done", finalize=True
        )

        assert duplicate.success is True
        assert duplicate.message_id == ""
        assert post_mock.await_count == 2

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
        assert str(first.message_id).startswith("coalesced:")
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

        cross_chat = await a.edit_message(
            "conv-other",
            str(first.message_id),
            "Hello world!",
            finalize=True,
        )
        assert cross_chat.success is False
        assert "unknown coalesced message" in str(cross_chat.error)

    def test_recently_finalized_cache_is_ttl_and_count_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_RECENTLY_FINALIZED", 3)
        a = _make_adapter(monkeypatch)
        for index in range(4):
            a._record_recently_finalized(
                f"coalesced:{index}", "conv-G", now=float(index)
            )
        assert list(a._recently_finalized) == [
            ("conv-G", "coalesced:1"),
            ("conv-G", "coalesced:2"),
            ("conv-G", "coalesced:3"),
        ]

        a._record_recently_finalized(
            "coalesced:fresh",
            "conv-G",
            now=a._recently_finalized_ttl_sec + 10.0,
        )
        assert a._recently_finalized == {
            ("conv-G", "coalesced:fresh"): (
                a._recently_finalized_ttl_sec + 10.0
            )
        }

    @pytest.mark.asyncio
    async def test_non_personal_same_turn_delivers_each_coalesced_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-segments")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)

        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)

        first = await a.send(
            chat_id="conv-G-segments",
            content="First segment ▉",
            reply_to="act-1",
        )
        await a.edit_message(
            "conv-G-segments",
            str(first.message_id),
            "First segment",
            finalize=True,
        )

        second = await a.send(
            chat_id="conv-G-segments",
            content="Second segment ▉",
            reply_to="act-1",
        )
        assert second.message_id != first.message_id
        assert second.message_id in a._coalesced_replies

        await a.edit_message(
            "conv-G-segments",
            str(second.message_id),
            "Second segment updated ▉",
            finalize=False,
        )
        await a.edit_message(
            "conv-G-segments",
            str(second.message_id),
            "Second segment updated",
            finalize=True,
        )

        assert [
            call.kwargs["reply"]["text"]
            for call in send_reply_mock.await_args_list
        ] == ["First segment", "Second segment updated"]
        assert ("conv-G-segments", first.message_id) in a._recently_finalized
        assert ("conv-G-segments", second.message_id) in a._recently_finalized
        assert a._coalesced_replies == {}
        assert a._active_coalesced_reply_by_chat == {}

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
        assert ("conv-G-stale", message_id) in a._recently_finalized

        late_final = await a.edit_message(
            "conv-G-stale",
            message_id,
            "Recovered reply",
            finalize=True,
        )
        assert late_final.success is True
        assert send_reply_mock.await_count == 1

        next_segment = await a.send(
            chat_id="conv-G-stale",
            content="After recovery ▉",
            reply_to="act-1",
        )
        assert next_segment.message_id != message_id
        await a.edit_message(
            "conv-G-stale",
            str(next_segment.message_id),
            "After recovery",
            finalize=True,
        )
        assert send_reply_mock.await_count == 2
        assert send_reply_mock.await_args.kwargs["reply"]["text"] == "After recovery"

    @pytest.mark.asyncio
    async def test_coalesced_finalize_detaches_before_network_await(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-overlap")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()
        release = asyncio.Event()
        sent: list[str] = []

        async def blocking_send_reply(**kwargs: Any) -> None:
            sent.append(kwargs["reply"]["text"])
            if len(sent) == 1:
                started.set()
                await release.wait()

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=blocking_send_reply)
        )

        first = await a.send(
            chat_id="conv-G-overlap", content="First", reply_to="act-1"
        )
        first_finalize = asyncio.create_task(
            a.edit_message(
                "conv-G-overlap", str(first.message_id), "First", finalize=True
            )
        )
        await started.wait()

        second = await a.send(
            chat_id="conv-G-overlap", content="Second", reply_to="act-1"
        )
        assert second.message_id != first.message_id
        assert a._active_coalesced_reply_by_chat["conv-G-overlap"] == second.message_id

        release.set()
        assert (await first_finalize).success is True
        assert second.message_id in a._coalesced_replies
        await a.edit_message(
            "conv-G-overlap", str(second.message_id), "Second", finalize=True
        )
        assert sent == ["First", "Second"]

    @pytest.mark.asyncio
    async def test_coalesced_watchdog_and_explicit_finalize_share_one_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-finalize-race")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_send_reply(**_kwargs: Any) -> None:
            started.set()
            await release.wait()

        send_reply_mock = AsyncMock(side_effect=blocking_send_reply)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        first = await a.send(
            chat_id="conv-G-finalize-race", content="Only once", reply_to="act-1"
        )

        explicit = asyncio.create_task(
            a.edit_message(
                "conv-G-finalize-race",
                str(first.message_id),
                "Only once",
                finalize=True,
            )
        )
        await started.wait()
        watchdog = asyncio.create_task(
            a._flush_stale_coalesced_reply(str(first.message_id))
        )
        await asyncio.sleep(0)
        release.set()

        explicit_result, watchdog_result = await asyncio.gather(explicit, watchdog)
        assert explicit_result.success is True
        assert watchdog_result is True
        assert send_reply_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_coalesced_finalizes_share_one_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-double-final")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_send_reply(**_kwargs: Any) -> None:
            started.set()
            await release.wait()

        send_reply_mock = AsyncMock(side_effect=blocking_send_reply)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        first = await a.send(
            chat_id="conv-G-double-final", content="Only once", reply_to="act-1"
        )
        args = ("conv-G-double-final", str(first.message_id), "Only once")
        finalize_one = asyncio.create_task(a.edit_message(*args, finalize=True))
        await started.wait()
        finalize_two = asyncio.create_task(a.edit_message(*args, finalize=True))
        await asyncio.sleep(0)
        release.set()

        results = await asyncio.gather(finalize_one, finalize_two)
        assert all(result.success for result in results)
        assert send_reply_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_finalize_does_not_erase_overlapping_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-failed-overlap")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()
        release = asyncio.Event()

        async def failing_send_reply(**_kwargs: Any) -> None:
            started.set()
            await release.wait()
            raise RuntimeError("connector down")

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=failing_send_reply)
        )
        first = await a.send(
            chat_id="conv-G-failed-overlap", content="First", reply_to="act-1"
        )
        first_finalize = asyncio.create_task(
            a.edit_message(
                "conv-G-failed-overlap",
                str(first.message_id),
                "First",
                finalize=True,
            )
        )
        await started.wait()
        second = await a.send(
            chat_id="conv-G-failed-overlap", content="Second", reply_to="act-1"
        )
        release.set()

        assert (await first_finalize).success is False
        assert second.message_id in a._coalesced_replies
        assert (
            a._active_coalesced_reply_by_chat["conv-G-failed-overlap"]
            == second.message_id
        )

        successful_send = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", successful_send)
        second_finalize = asyncio.create_task(
            a.edit_message(
                "conv-G-failed-overlap",
                str(second.message_id),
                "Second",
                finalize=True,
            )
        )
        await asyncio.sleep(0)
        assert second_finalize.done() is False

        retry = await a.edit_message(
            "conv-G-failed-overlap",
            str(first.message_id),
            "First",
            finalize=True,
        )
        result = await second_finalize
        assert retry.success is True
        assert result.success is True
        assert [
            call.kwargs["reply"]["text"]
            for call in successful_send.await_args_list
        ] == ["First", "Second"]

    @pytest.mark.asyncio
    async def test_coalesced_generation_backlog_is_bounded_per_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_mod, "_MAX_COALESCED_REPLY_GENERATIONS_PER_CHAT", 2
        )
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-backlog")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_send_reply(**_kwargs: Any) -> None:
            if not started.is_set():
                started.set()
                await release.wait()

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=blocking_send_reply)
        )
        first = await a.send(
            chat_id="conv-G-backlog", content="First", reply_to="act-1"
        )
        first_finalize = asyncio.create_task(
            a.edit_message(
                "conv-G-backlog", str(first.message_id), "First", finalize=True
            )
        )
        await started.wait()
        second = await a.send(
            chat_id="conv-G-backlog", content="Second", reply_to="act-1"
        )
        second_finalize = asyncio.create_task(
            a.edit_message(
                "conv-G-backlog",
                str(second.message_id),
                "Second",
                finalize=True,
            )
        )
        await asyncio.sleep(0)
        assert second_finalize.done() is False

        states_before = set(a._coalesced_replies)
        tasks_before = set(a._coalesced_reply_tasks)
        rejected = await a.send(
            chat_id="conv-G-backlog", content="Third", reply_to="act-1"
        )
        assert rejected.success is False
        assert "backlog full" in str(rejected.error)
        assert set(a._coalesced_replies) == states_before
        assert set(a._coalesced_reply_tasks) == tasks_before

        rejected_edit = await a.edit_message(
            "conv-G-backlog", "unknown-edit", "Third", finalize=False
        )
        rejected_final = await a.edit_message(
            "conv-G-backlog", "unknown-final", "Third", finalize=True
        )
        assert rejected_edit.success is False
        assert rejected_final.success is False
        assert "unknown coalesced message id" in str(rejected_edit.error)
        assert "unknown coalesced message id" in str(rejected_final.error)
        assert set(a._coalesced_replies) == states_before
        assert set(a._coalesced_reply_tasks) == tasks_before
        assert "unknown-edit" not in a._coalesced_replies
        assert "unknown-final" not in a._coalesced_replies

        release.set()
        assert (await first_finalize).success is True
        assert (await second_finalize).success is True

    @pytest.mark.asyncio
    async def test_coalesced_message_id_is_bound_to_its_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound_a = _make_inbound(conv_id="conv-A", activity_id="act-A")
        inbound_a["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound_a)
        inbound_b = _make_inbound(conv_id="conv-B", activity_id="act-B")
        inbound_b["conversation"]["conversationType"] = "groupChat"
        ref_b = adapter_mod.ConversationRef.from_activity(inbound_b)
        assert ref_b is not None
        ref_b.validated_path = "A"
        a._capture_coalesced_turn_target(ref_b)
        a._conversations.upsert(ref_b)
        a._seen_inbounds_this_lifetime.add("conv-B")

        first_a = await a.send(chat_id="conv-A", content="A", reply_to="act-A")
        first_b = await a.send(chat_id="conv-B", content="B", reply_to="act-B")
        result = await a.edit_message(
            "conv-A", str(first_b.message_id), "poison", finalize=True
        )

        assert result.success is False
        assert "another chat" in str(result.error)
        assert a._coalesced_replies[first_a.message_id]["content"] == "A"
        assert a._coalesced_replies[first_b.message_id]["content"] == "B"
        assert a._active_coalesced_reply_by_chat == {
            "conv-A": first_a.message_id,
            "conv-B": first_b.message_id,
        }
        a._drop_coalesced_reply_state(str(first_a.message_id))
        a._drop_coalesced_reply_state(str(first_b.message_id))

    @pytest.mark.asyncio
    async def test_teardown_cancels_detached_finalize_and_preserves_other_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-evict-finalizing")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        other = _make_inbound(conv_id="conv-evict-other", activity_id="act-2")
        other["conversation"]["conversationType"] = "groupChat"
        other_ref = adapter_mod.ConversationRef.from_activity(other)
        assert other_ref is not None
        other_ref.validated_path = "A"
        a._capture_coalesced_turn_target(other_ref)
        a._conversations.upsert(other_ref)
        a._seen_inbounds_this_lifetime.add("conv-evict-other")
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_send_reply(**_kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=blocked_send_reply)
        )
        first = await a.send(
            chat_id="conv-evict-finalizing", content="First", reply_to="act-1"
        )
        other_reply = await a.send(
            chat_id="conv-evict-other", content="Other", reply_to="act-2"
        )
        finalize = asyncio.create_task(
            a.edit_message(
                "conv-evict-finalizing",
                str(first.message_id),
                "First",
                finalize=True,
            )
        )
        await started.wait()

        await a._teardown_chat_state("conv-evict-finalizing")
        await cancelled.wait()
        with pytest.raises(asyncio.CancelledError):
            await finalize

        assert first.message_id not in a._coalesced_replies
        assert "conv-evict-finalizing" not in a._active_coalesced_reply_by_chat
        assert other_reply.message_id in a._coalesced_replies
        assert (
            a._active_coalesced_reply_by_chat["conv-evict-other"]
            == other_reply.message_id
        )
        a._drop_coalesced_reply_state(str(other_reply.message_id))

    @pytest.mark.asyncio
    async def test_disconnect_cancels_detached_finalize_before_client_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-disconnect-finalizing")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_send_reply(**_kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=blocked_send_reply)
        )
        first = await a.send(
            chat_id="conv-disconnect-finalizing", content="First", reply_to="act-1"
        )
        finalize = asyncio.create_task(
            a.edit_message(
                "conv-disconnect-finalizing",
                str(first.message_id),
                "First",
                finalize=True,
            )
        )
        await started.wait()

        await a.disconnect()
        await cancelled.wait()
        with pytest.raises(asyncio.CancelledError):
            await finalize
        assert a._coalesced_replies == {}
        assert a._coalesced_reply_tasks == {}
        assert a._coalesced_delivery_tail_by_turn == {}

    @pytest.mark.asyncio
    async def test_disconnect_releases_chained_generation_waiters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-disconnect-chain")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()

        async def blocked_send_reply(**_kwargs: Any) -> None:
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=blocked_send_reply)
        )
        first = await a.send(
            chat_id="conv-disconnect-chain", content="First", reply_to="act-1"
        )
        first_finalize = asyncio.create_task(
            a.edit_message(
                "conv-disconnect-chain",
                str(first.message_id),
                "First",
                finalize=True,
            )
        )
        await started.wait()

        second = await a.send(
            chat_id="conv-disconnect-chain", content="Second", reply_to="act-1"
        )
        first_complete = a._coalesced_replies[first.message_id][
            "delivery_complete"
        ]
        second_complete = a._coalesced_replies[second.message_id][
            "delivery_complete"
        ]
        second_finalize = asyncio.create_task(
            a.edit_message(
                "conv-disconnect-chain",
                str(second.message_id),
                "Second",
                finalize=True,
            )
        )
        await asyncio.sleep(0)

        await a.disconnect()

        with pytest.raises(asyncio.CancelledError):
            await first_finalize
        with pytest.raises(asyncio.CancelledError):
            await second_finalize
        assert first_complete.is_set()
        assert second_complete.is_set()
        assert a._coalesced_replies == {}
        assert a._coalesced_reply_tasks == {}
        assert a._coalesced_delivery_tail_by_turn == {}

    @pytest.mark.asyncio
    async def test_disconnect_barrier_precedes_queued_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-disconnect-barrier")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        client = a._http_client
        client.aclose = AsyncMock()

        disconnect = asyncio.create_task(a.disconnect())
        late_send = asyncio.create_task(
            a.send(
                chat_id="conv-disconnect-barrier",
                content="Too late",
                reply_to="act-1",
            )
        )

        rejected = await late_send
        await disconnect

        assert rejected.success is False
        assert "disconnecting" in str(rejected.error)
        assert a._coalesced_replies == {}
        assert a._coalesced_reply_tasks == {}
        assert a._coalesced_delivery_tail_by_turn == {}
        assert a._coalesced_turn_targets == {}
        assert a._disconnecting is False
        client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_bounds_cancellation_resistant_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.05
        )
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-disconnect-resistant")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()

        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def cancellation_resistant_send_reply(**_kwargs: Any) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()

        monkeypatch.setattr(
            bridge,
            "send_reply",
            AsyncMock(side_effect=cancellation_resistant_send_reply),
        )
        client = a._http_client
        client.aclose = AsyncMock()
        first = await a.send(
            chat_id="conv-disconnect-resistant",
            content="First",
            reply_to="act-1",
        )
        finalize = asyncio.create_task(
            a.edit_message(
                "conv-disconnect-resistant",
                str(first.message_id),
                "First",
                finalize=True,
            )
        )
        await started.wait()
        delivery = a._coalesced_replies[first.message_id]["finalize_task"]

        disconnect = asyncio.create_task(a.disconnect())
        await asyncio.wait_for(cancelled.wait(), timeout=0.5)
        rejected = await a.send(
            chat_id="conv-disconnect-resistant",
            content="Too late",
            reply_to="act-1",
        )
        disconnect.cancel()
        await asyncio.sleep(0)
        disconnect.cancel()

        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(disconnect, timeout=0.5)

            assert rejected.success is False
            assert "disconnecting" in str(rejected.error)
            assert cancelled.is_set()
            assert a._coalesced_replies == {}
            assert a._coalesced_reply_tasks == {}
            assert a._coalesced_delivery_tail_by_turn == {}
            assert a._http_client is None
            assert a._disconnecting is True
            client.aclose.assert_awaited_once()
            with pytest.raises(asyncio.CancelledError):
                await finalize
            assert delivery.done() is False

            a._http_client = MagicMock()
            monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
            after_reconnect = await a.send(
                chat_id="conv-disconnect-resistant",
                content="After reconnect",
            )
            assert after_reconnect.success is False
            assert "disconnecting" in str(after_reconnect.error)
        finally:
            release.set()

        result = await asyncio.wait_for(delivery, timeout=0.5)
        assert result.success is False
        assert "cancelled" in str(result.error)
        assert a._recently_finalized == {}
        assert a._disconnecting is False
        a._http_client = MagicMock()
        after_retirement = await a.send(
            chat_id="conv-disconnect-resistant",
            content="After retirement",
        )
        assert after_retirement.success is True

    @pytest.mark.asyncio
    async def test_chat_teardown_tracks_resistant_delivery_until_completion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        monkeypatch.setattr(adapter_mod, "_MAX_COALESCED_REPLY_SURVIVORS", 1)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-evict-resistant")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def resistant_send_reply(**_kwargs: Any) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    continue

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=resistant_send_reply)
        )
        first = await a.send(
            chat_id="conv-evict-resistant", content="First", reply_to="act-1"
        )
        finalize = asyncio.create_task(
            a.edit_message(
                "conv-evict-resistant",
                str(first.message_id),
                "First",
                finalize=True,
            )
        )
        await started.wait()
        delivery = a._coalesced_replies[first.message_id]["finalize_task"]

        teardown = asyncio.create_task(
            a._teardown_chat_state("conv-evict-resistant")
        )
        await cancelled.wait()
        joined_teardown = asyncio.create_task(
            a._teardown_chat_state("conv-evict-resistant")
        )
        teardown.cancel()
        await asyncio.sleep(0)
        teardown.cancel()
        rejected = await a.send(
            chat_id="conv-evict-resistant",
            content="Too late",
            reply_to="act-1",
        )
        await joined_teardown

        with pytest.raises(asyncio.CancelledError):
            await teardown
        with pytest.raises(asyncio.CancelledError):
            await finalize
        assert rejected.success is False
        assert "disconnecting" in str(rejected.error)
        assert delivery in a._coalesced_reply_survivors
        assert a._coalesced_replies == {}
        assert a._chat_generation("conv-evict-resistant") == 1

        release.set()
        result = await asyncio.wait_for(delivery, timeout=0.5)
        await asyncio.sleep(0)
        assert result.success is False
        assert a._coalesced_reply_survivors == {}

    @pytest.mark.asyncio
    async def test_one_shot_reply_cannot_report_success_after_chat_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-oneshot-evict")
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        delivered = asyncio.Event()

        async def delayed_send_reply(**_kwargs: Any) -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            delivered.set()

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=delayed_send_reply)
        )
        send = asyncio.create_task(
            a.send(chat_id="conv-oneshot-evict", content="One shot")
        )
        await started.wait()
        await a._teardown_chat_state("conv-oneshot-evict")
        await cancelled.wait()
        release.set()
        result = await asyncio.wait_for(send, timeout=0.5)

        assert result.success is False
        assert "cancelled" in str(result.error)
        assert delivered.is_set() is False

    @pytest.mark.asyncio
    async def test_survivor_counts_against_generation_admission_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        monkeypatch.setattr(adapter_mod, "_MAX_COALESCED_REPLY_GENERATIONS", 1)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-survivor-budget")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        release = asyncio.Event()

        async def resistant_send_reply(**_kwargs: Any) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=resistant_send_reply)
        )
        first = await a.send(
            chat_id="conv-survivor-budget", content="First", reply_to="act-1"
        )
        finalize = asyncio.create_task(
            a.edit_message(
                "conv-survivor-budget",
                str(first.message_id),
                "First",
                finalize=True,
            )
        )
        await started.wait()
        delivery = a._coalesced_replies[first.message_id]["finalize_task"]
        await a.disconnect()

        a._http_client = MagicMock()
        rejected = await a.send(
            chat_id="conv-survivor-budget", content="Second", reply_to="act-1"
        )
        assert rejected.success is False
        assert "disconnecting" in str(rejected.error)

        release.set()
        await delivery
        await asyncio.sleep(0)
        assert a._coalesced_reply_survivors == {}
        accepted = await a.send(
            chat_id="conv-survivor-budget", content="Second", reply_to="act-1"
        )
        assert accepted.success is True
        a._drop_coalesced_reply_state(str(accepted.message_id))
        with pytest.raises(asyncio.CancelledError):
            await finalize

    @pytest.mark.asyncio
    async def test_delayed_personal_stream_start_cannot_cross_disconnect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-personal-late")
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("token", "A")),
        )
        monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
        posted = asyncio.Event()
        release = asyncio.Event()

        async def delayed_post(*_args: Any, **_kwargs: Any) -> Any:
            posted.set()
            await release.wait()
            return MagicMock(
                status_code=201,
                text="",
                json=lambda: {"id": "bf-late"},
            )

        client = a._http_client
        client.post = AsyncMock(side_effect=delayed_post)
        client.aclose = AsyncMock()
        send = asyncio.create_task(
            a.send(
                chat_id="conv-personal-late",
                content="Hello",
                reply_to="act-1",
            )
        )
        await posted.wait()
        await a.disconnect()
        a._http_client = MagicMock()

        release.set()
        result = await asyncio.wait_for(send, timeout=0.5)
        assert result.success is False
        assert a._streams == {}
        assert a._active_stream_by_chat == {}
        assert bridge.send_reply.await_count == 0

    @pytest.mark.asyncio
    async def test_disconnect_preserves_personal_finalize_tombstone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-personal-tombstone")
        self._wire_adapter(a, inbound=inbound)
        a._active_stream_by_chat["conv-personal-tombstone"] = "stream-old"
        a._streams[("conv-personal-tombstone", "stream-old")] = {
            "bf_stream_id": "stream-old",
            "chat_id": "conv-personal-tombstone",
            "sequence": 1,
            "last_emit_ts": 0.0,
        }
        client = a._http_client
        client.aclose = AsyncMock()

        await a.disconnect()

        assert (
            "conv-personal-tombstone",
            "stream-old",
        ) in a._recently_finalized
        a._http_client = MagicMock()
        a._http_client.post = AsyncMock()
        result = await a.edit_message(
            "conv-personal-tombstone",
            "stream-old",
            "Done",
            finalize=True,
        )
        assert result.success is True
        assert a._http_client.post.await_count == 0

    @pytest.mark.asyncio
    async def test_disconnect_tombstones_authoritative_stream_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._streams[("conv-authoritative", "stream-keyed")] = {
            "bf_stream_id": "stream-keyed",
            "chat_id": "conv-stale-metadata",
            "sequence": 1,
            "last_emit_ts": 0.0,
        }

        await a.disconnect()

        assert (
            "conv-authoritative",
            "stream-keyed",
        ) in a._recently_finalized
        assert (
            "conv-stale-metadata",
            "stream-keyed",
        ) not in a._recently_finalized

    @pytest.mark.asyncio
    async def test_disconnect_drains_teardown_started_during_server_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        teardown_started = asyncio.Event()
        release_teardown = asyncio.Event()

        async def late_teardown() -> None:
            teardown_started.set()
            await release_teardown.wait()

        async def stop_with_late_teardown() -> None:
            a._chat_teardown_tasks["conv-late-teardown"] = asyncio.create_task(late_teardown())

        monkeypatch.setattr(a, "_stop_uvicorn", stop_with_late_teardown)
        disconnect = asyncio.create_task(a._disconnect(None))
        await teardown_started.wait()
        await asyncio.sleep(0)

        assert disconnect.done() is False
        release_teardown.set()
        await asyncio.wait_for(disconnect, timeout=0.5)
        assert a._chat_teardown_tasks == {}

    @pytest.mark.asyncio
    async def test_inflight_first_final_is_tombstoned_across_disconnect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-first-final")
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("token", "A")),
        )
        post_started = asyncio.Event()
        release_post = asyncio.Event()

        async def delayed_post(*_args: Any, **_kwargs: Any) -> Any:
            post_started.set()
            await release_post.wait()
            return MagicMock(
                status_code=201,
                text="",
                json=lambda: {"id": "bf-first-final"},
            )

        old_client = a._http_client
        old_client.post = AsyncMock(side_effect=delayed_post)
        old_client.aclose = AsyncMock()
        first_final = asyncio.create_task(
            a.edit_message(
                "conv-first-final",
                "local-first-final",
                "Done",
                finalize=True,
            )
        )
        await post_started.wait()
        await a.disconnect()
        assert (
            "conv-first-final",
            "local-first-final",
        ) in a._recently_finalized

        replacement = MagicMock()
        replacement.post = AsyncMock()
        a._http_client = replacement
        release_post.set()
        old_result = await asyncio.wait_for(first_final, timeout=0.5)
        retry = await a.edit_message(
            "conv-first-final",
            "local-first-final",
            "Done",
            finalize=True,
        )

        assert old_result.success is False
        assert retry.success is True
        assert replacement.post.await_count == 0

    @pytest.mark.asyncio
    async def test_personal_tombstones_are_chat_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-tombstone-B")
        self._wire_adapter(a, inbound=inbound)
        a._record_recently_finalized("shared-stream-id", "conv-tombstone-A")
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("token", "A")),
        )
        a._http_client.post = AsyncMock(
            return_value=MagicMock(status_code=202, text="", json=lambda: {})
        )
        a._streams[("conv-tombstone-B", "shared-stream-id")] = {
            "bf_stream_id": "shared-stream-id",
            "chat_id": "conv-tombstone-B",
            "sequence": 1,
            "last_emit_ts": 0.0,
            "opened_ts": asyncio.get_event_loop().time(),
            "finalize_failures": 0,
            "lifecycle_generation": a._lifecycle_generation,
            "chat_generation": a._chat_generation("conv-tombstone-B"),
        }

        result = await a.edit_message(
            "conv-tombstone-B",
            "shared-stream-id",
            "Still live",
            finalize=False,
        )

        assert result.success is True
        assert a._http_client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_live_personal_stream_rejects_cross_chat_replay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-live-B")
        self._wire_adapter(a, inbound=inbound)
        a._streams[("conv-live-A", "shared-live-id")] = {
            "bf_stream_id": "shared-live-id",
            "chat_id": "conv-live-A",
            "sequence": 1,
            "last_emit_ts": 0.0,
            "opened_ts": asyncio.get_event_loop().time(),
            "finalize_failures": 0,
            "lifecycle_generation": a._lifecycle_generation,
            "chat_generation": a._chat_generation("conv-live-A"),
        }
        a._http_client.post = AsyncMock()

        result = await a.edit_message(
            "conv-live-B",
            "shared-live-id",
            "Wrong chat",
            finalize=False,
        )

        assert result.success is False
        assert "another chat" in str(result.error)
        assert a._streams[("conv-live-A", "shared-live-id")]["sequence"] == 1
        assert a._http_client.post.await_count == 0

    @pytest.mark.asyncio
    async def test_equal_bf_stream_ids_remain_isolated_between_chats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound_a = _make_inbound(conv_id="conv-equal-A", activity_id="act-A")
        inbound_b = _make_inbound(conv_id="conv-equal-B", activity_id="act-B")
        self._wire_adapter(a, inbound=inbound_a)
        ref_b = adapter_mod.ConversationRef.from_activity(inbound_b)
        assert ref_b is not None
        ref_b.validated_path = "A"
        a._conversations.upsert(ref_b)
        a._seen_inbounds_this_lifetime.add("conv-equal-B")
        self._patch_token_mint(monkeypatch)
        self._no_sleep(monkeypatch)
        a._http_client.post = AsyncMock(
            side_effect=[
                MagicMock(
                    status_code=201,
                    text="",
                    json=lambda: {"id": "bf-shared"},
                ),
                MagicMock(
                    status_code=201,
                    text="",
                    json=lambda: {"id": "bf-shared"},
                ),
                MagicMock(status_code=202, text="", json=lambda: {}),
                MagicMock(status_code=202, text="", json=lambda: {}),
                MagicMock(status_code=202, text="", json=lambda: {}),
                MagicMock(status_code=202, text="", json=lambda: {}),
            ]
        )

        start_a = await a._send_stream_start(
            chat_id="conv-equal-A", content="A", inbound=inbound_a
        )
        start_b = await a._send_stream_start(
            chat_id="conv-equal-B", content="B", inbound=inbound_b
        )
        assert start_a is not None and start_a.success
        assert start_b is not None and start_b.success
        assert ("conv-equal-A", "bf-shared") in a._streams
        assert ("conv-equal-B", "bf-shared") in a._streams

        edit_a = await a.edit_message(
            "conv-equal-A", "bf-shared", "A2", finalize=False
        )
        edit_b = await a.edit_message(
            "conv-equal-B", "bf-shared", "B2", finalize=False
        )
        assert edit_a.success and edit_b.success
        assert a._streams[("conv-equal-A", "bf-shared")]["sequence"] == 2
        assert a._streams[("conv-equal-B", "bf-shared")]["sequence"] == 2

        final_a = await a.edit_message(
            "conv-equal-A", "bf-shared", "A done", finalize=True
        )
        final_b = await a.edit_message(
            "conv-equal-B", "bf-shared", "B done", finalize=True
        )
        assert final_a.success and final_b.success
        assert ("conv-equal-A", "bf-shared") not in a._streams
        assert ("conv-equal-B", "bf-shared") not in a._streams

    @pytest.mark.asyncio
    async def test_proactive_send_cannot_use_replacement_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-proactive-old")
        ref = adapter_mod.ConversationRef.from_activity(inbound)
        assert ref is not None
        ref.validated_path = "A"
        a._conversations.upsert(ref)
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        bridge = adapter_mod._import_bridge()
        token_started = asyncio.Event()
        release_token = asyncio.Event()

        async def delayed_token(**_kwargs: Any) -> tuple[str, str]:
            token_started.set()
            await release_token.wait()
            return "token", "A"

        monkeypatch.setattr(
            bridge, "acquire_reply_token", AsyncMock(side_effect=delayed_token)
        )
        old_client = a._http_client
        old_client.post = AsyncMock()
        old_client.aclose = AsyncMock()
        send = asyncio.create_task(a.send("conv-proactive-old", "Hello"))
        await token_started.wait()
        await a.disconnect()
        replacement = MagicMock()
        replacement.post = AsyncMock()
        a._http_client = replacement

        release_token.set()
        result = await asyncio.wait_for(send, timeout=0.5)

        assert result.success is False
        assert old_client.post.await_count == 0
        assert replacement.post.await_count == 0

    @pytest.mark.asyncio
    async def test_typing_cannot_use_replacement_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-typing-old")
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()
        token_started = asyncio.Event()
        release_token = asyncio.Event()

        async def delayed_token(**_kwargs: Any) -> tuple[str, str]:
            token_started.set()
            await release_token.wait()
            return "token", "A"

        monkeypatch.setattr(
            bridge, "acquire_reply_token", AsyncMock(side_effect=delayed_token)
        )
        old_client = a._http_client
        old_client.post = AsyncMock()
        old_client.aclose = AsyncMock()
        typing = asyncio.create_task(a.send_typing("conv-typing-old"))
        await token_started.wait()
        await a.disconnect()
        replacement = MagicMock()
        replacement.post = AsyncMock()
        a._http_client = replacement

        release_token.set()
        await asyncio.wait_for(typing, timeout=0.5)

        assert old_client.post.await_count == 0
        assert replacement.post.await_count == 0

    @pytest.mark.asyncio
    async def test_stale_stream_finalize_cannot_resume_on_replacement_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-stale-old")
        self._wire_adapter(a, inbound=inbound)
        a._active_stream_by_chat["conv-stale-old"] = "stream-stale"
        a._streams[("conv-stale-old", "stream-stale")] = {
            "bf_stream_id": "stream-stale",
            "chat_id": "conv-stale-old",
            "sequence": 2,
            "last_emit_ts": 0.0,
            "opened_ts": asyncio.get_event_loop().time(),
            "last_content": "Old",
            "finalize_failures": 0,
            "lifecycle_generation": a._lifecycle_generation,
            "chat_generation": a._chat_generation("conv-stale-old"),
        }
        bridge = adapter_mod._import_bridge()
        token_started = asyncio.Event()
        release_token = asyncio.Event()

        async def delayed_token(**_kwargs: Any) -> tuple[str, str]:
            token_started.set()
            await release_token.wait()
            return "token", "A"

        monkeypatch.setattr(
            bridge, "acquire_reply_token", AsyncMock(side_effect=delayed_token)
        )
        old_client = a._http_client
        old_client.post = AsyncMock()
        old_client.aclose = AsyncMock()
        send = asyncio.create_task(
            a.send("conv-stale-old", "New", reply_to="act-1")
        )
        await token_started.wait()
        await a.disconnect()
        replacement = MagicMock()
        replacement.post = AsyncMock()
        a._http_client = replacement

        release_token.set()
        result = await asyncio.wait_for(send, timeout=0.5)

        assert result.success is False
        assert old_client.post.await_count == 0
        assert replacement.post.await_count == 0

    @pytest.mark.asyncio
    async def test_agent_turn_context_cannot_cross_adapter_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = _make_adapter(monkeypatch)
        replacement = _make_adapter(monkeypatch)
        guard = asyncio.Event()
        token = adapter_mod._AGENT_TURN_LIFECYCLE.set(
            (id(old), "conv-old-adapter", 0, 0, guard)
        )
        try:
            result = await replacement.send("conv-old-adapter", "stale")
        finally:
            adapter_mod._AGENT_TURN_LIFECYCLE.reset(token)

        assert result.success is False
        assert "agent turn lifecycle changed" in str(result.error)

    @pytest.mark.asyncio
    async def test_foreign_stream_id_is_rejected_before_active_substitution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-stream-B")
        self._wire_adapter(a, inbound=inbound)
        a._active_stream_by_chat["conv-stream-B"] = "stream-B"
        a._streams[("conv-stream-A", "stream-A")] = {
            "chat_id": "conv-stream-A"
        }
        a._streams[("conv-stream-B", "stream-B")] = {
            "chat_id": "conv-stream-B",
            "bf_stream_id": "bf-B",
            "sequence": 1,
            "last_emit_ts": 0.0,
        }

        result = await a.edit_message(
            "conv-stream-B", "stream-A", "content from A", finalize=True
        )

        assert result.success is False
        assert "another chat" in str(result.error)
        assert a._http_client.post.await_count == 0

    @pytest.mark.asyncio
    async def test_chat_lifecycle_generation_lru_preserves_unrelated_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_CHAT_LIFECYCLE_GENERATIONS", 2)
        a = _make_adapter(monkeypatch)

        await a._teardown_chat_state("conv-churn-1")
        await a._teardown_chat_state("conv-churn-2")
        a._active_stream_by_chat["conv-rollover-live"] = "stream-rollover"
        a._streams[("conv-rollover-live", "stream-rollover")] = {
            "bf_stream_id": "stream-rollover",
            "chat_id": "conv-rollover-live",
        }
        a._coalesced_status["status-rollover"] = {
            "chat_id": "conv-rollover-live",
            "lines": ["old"],
        }
        a._pending_file_uploads["pending-rollover"] = {
            "conversation_id": "conv-rollover-live"
        }
        a._card_capabilities["card-rollover"] = {
            "conversation_id": "conv-rollover-live"
        }
        await a._teardown_chat_state("conv-churn-3")

        assert len(a._chat_lifecycle_generation) <= 2
        assert a._lifecycle_generation == 0
        assert a._retiring_chats == set()
        assert ("conv-rollover-live", "stream-rollover") in a._streams
        assert a._active_stream_by_chat == {"conv-rollover-live": "stream-rollover"}
        assert "status-rollover" in a._coalesced_status
        assert "pending-rollover" in a._pending_file_uploads
        assert "card-rollover" in a._card_capabilities
        assert (
            "conv-rollover-live",
            "stream-rollover",
        ) not in a._recently_finalized

    def test_chat_lifecycle_generation_cap_defers_when_all_epochs_are_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_CHAT_LIFECYCLE_GENERATIONS", 1)
        a = _make_adapter(monkeypatch)
        a._chat_lifecycle_generation["conv-live"] = 1
        a._session_key_to_conv["session-live"] = "conv-live"
        a._active_sessions["session-live"] = asyncio.Event()

        deferred = a._advance_chat_generation("conv-new")

        assert deferred is None
        assert a._chat_lifecycle_generation == {"conv-live": 1}

        a._active_sessions.pop("session-live")
        admitted = a._advance_chat_generation("conv-new")

        assert admitted is not None
        assert len(a._chat_lifecycle_generation) == 1
        assert a._chat_lifecycle_generation == {"conv-new": admitted}

    @pytest.mark.asyncio
    async def test_lifecycle_generation_lru_preserves_unrelated_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_CHAT_LIFECYCLE_GENERATIONS", 1)
        monkeypatch.setattr(adapter_mod, "_MAX_COALESCED_REPLY_GENERATIONS", 4)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-rollover-resistant")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        release = asyncio.Event()

        async def resistant_send_reply(**_kwargs: Any) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=resistant_send_reply)
        )
        first = await a.send(
            chat_id="conv-rollover-resistant",
            content="First",
            reply_to="act-1",
        )
        finalize = asyncio.create_task(
            a.edit_message(
                "conv-rollover-resistant",
                str(first.message_id),
                "First",
                finalize=True,
            )
        )
        await started.wait()
        delivery = a._coalesced_replies[first.message_id]["finalize_task"]

        await a._teardown_chat_state("conv-churn-before-rollover")
        await a._teardown_chat_state("conv-churn-causes-rollover")
        assert delivery not in a._coalesced_reply_survivors
        assert delivery.done() is False
        assert first.message_id in a._coalesced_replies
        assert len(a._chat_lifecycle_generation) <= 1

        replacement = _make_inbound(
            conv_id="conv-rollover-replacement", activity_id="act-2"
        )
        replacement["conversation"]["conversationType"] = "groupChat"
        replacement_ref = adapter_mod.ConversationRef.from_activity(replacement)
        assert replacement_ref is not None
        replacement_ref.validated_path = "B"
        a._capture_coalesced_turn_target(replacement_ref)
        a._conversations.upsert(replacement_ref)
        a._seen_inbounds_this_lifetime.add("conv-rollover-replacement")
        accepted = await a.send(
            chat_id="conv-rollover-replacement",
            content="Second",
            reply_to="act-2",
        )
        assert accepted.success is True
        assert a._disconnecting is False
        a._drop_coalesced_reply_state(str(accepted.message_id))

        release.set()
        result = await asyncio.wait_for(delivery, timeout=0.5)
        finalized = await finalize
        await asyncio.sleep(0)
        assert result.success is True
        assert finalized.success is True
        assert a._coalesced_reply_survivors == {}
        assert a._disconnecting is False

    @pytest.mark.asyncio
    async def test_teardown_drops_detached_personal_streams_for_only_its_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-detached-A")
        self._wire_adapter(a, inbound=inbound)
        self._patch_token_mint(monkeypatch)
        post_started = asyncio.Event()
        release_post = asyncio.Event()

        async def delayed_post(*_args: Any, **_kwargs: Any) -> Any:
            post_started.set()
            await release_post.wait()
            return MagicMock(
                status_code=201,
                text="",
                json=lambda: {"id": "bf-shared-final"},
            )

        a._http_client.post = AsyncMock(side_effect=delayed_post)
        finalize = asyncio.create_task(
            a.edit_message(
                "conv-detached-A",
                "local-final",
                "Done",
                finalize=True,
            )
        )
        await post_started.wait()
        assert ("conv-detached-A", "local-final") in a._streams
        a._streams[("conv-detached-A", "extra-detached")] = {
            "chat_id": "conv-detached-A"
        }
        a._streams[("conv-detached-B", "local-final")] = {
            "chat_id": "conv-detached-B"
        }

        await a._teardown_chat_state("conv-detached-A")
        assert ("conv-detached-A", "local-final") not in a._streams
        assert ("conv-detached-A", "extra-detached") not in a._streams
        assert ("conv-detached-B", "local-final") in a._streams

        release_post.set()
        result = await asyncio.wait_for(finalize, timeout=0.5)
        assert result.success is False
        assert ("conv-detached-A", "local-final") not in a._streams

    def test_coalesced_reply_ids_are_opaque_and_fixed_size(self) -> None:
        message_id = adapter_mod.Agent365Adapter._coalesced_reply_message_id()
        prefix, opaque_id = message_id.split(":", 1)
        assert prefix == "coalesced"
        assert len(opaque_id) == 32
        assert int(opaque_id, 16) >= 0

    @pytest.mark.asyncio
    async def test_concurrent_group_turns_retain_immutable_reply_ownership(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound_a = _make_inbound(
            conv_id="conv-G-users", activity_id="act-user-A"
        )
        inbound_a["conversation"]["conversationType"] = "groupChat"
        inbound_a["from"] = {"id": "user-A", "name": "A"}
        self._wire_adapter(a, inbound=inbound_a)
        bridge = adapter_mod._import_bridge()
        send_reply_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply_mock)
        ref_a = adapter_mod.ConversationRef.from_activity(inbound_a)
        assert ref_a is not None
        ref_a.validated_path = "A"
        a._capture_coalesced_turn_target(ref_a)

        inbound_b = _make_inbound(
            conv_id="conv-G-users", activity_id="act-user-B", path="B"
        )
        inbound_b["conversation"]["conversationType"] = "groupChat"
        inbound_b["from"] = {"id": "user-B", "name": "B"}
        ref_b = adapter_mod.ConversationRef.from_activity(inbound_b)
        assert ref_b is not None
        ref_b.validated_path = "B"
        a._capture_coalesced_turn_target(ref_b)
        a._conversations.upsert(ref_b)

        # Turn B is now the latest durable registry entry before delayed turn A
        # asks to send. Exact turn-target capture must still route A to A.
        first = await a.send(
            chat_id="conv-G-users", content="Reply A", reply_to="act-user-A"
        )
        second = await a.send(
            chat_id="conv-G-users", content="Reply B", reply_to="act-user-B"
        )

        assert first.message_id != second.message_id
        assert a._coalesced_replies[first.message_id]["inbound"]["id"] == "act-user-A"
        assert a._coalesced_replies[second.message_id]["inbound"]["id"] == "act-user-B"

        await a.send(
            chat_id="conv-G-users", content="Reply A updated", reply_to="act-user-A"
        )
        assert a._coalesced_replies[first.message_id]["content"] == "Reply A updated"
        assert a._coalesced_replies[first.message_id]["inbound"]["from"]["id"] == "user-A"

        second_finalize = asyncio.create_task(
            a.edit_message(
                "conv-G-users",
                str(second.message_id),
                "Reply B",
                finalize=True,
            )
        )
        assert (await asyncio.wait_for(second_finalize, timeout=0.1)).success is True
        assert (
            await a.edit_message(
                "conv-G-users",
                str(first.message_id),
                "Reply A updated",
                finalize=True,
            )
        ).success is True

        assert [
            call.kwargs["inbound"]["id"]
            for call in send_reply_mock.await_args_list
        ] == ["act-user-B", "act-user-A"]
        assert [
            call.kwargs["reply"]["replyToId"]
            for call in send_reply_mock.await_args_list
        ] == ["act-user-B", "act-user-A"]
        assert [
            call.kwargs["validated_path"]
            for call in send_reply_mock.await_args_list
        ] == ["B", "A"]

    @pytest.mark.asyncio
    async def test_global_coalesced_generation_budget_bounds_many_chats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_COALESCED_REPLY_GENERATIONS", 2)
        a = _make_adapter(monkeypatch)
        first_inbound = _make_inbound(conv_id="conv-global-1")
        first_inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=first_inbound)

        accepted: list[tuple[str, Any]] = []
        for index in (1, 2):
            chat_id = f"conv-global-{index}"
            inbound = _make_inbound(conv_id=chat_id, activity_id=f"act-{index}")
            inbound["conversation"]["conversationType"] = "groupChat"
            ref = adapter_mod.ConversationRef.from_activity(inbound)
            assert ref is not None
            ref.validated_path = "A"
            a._capture_coalesced_turn_target(ref)
            a._conversations.upsert(ref)
            a._seen_inbounds_this_lifetime.add(chat_id)
            result = await a.send(
                chat_id=chat_id, content=f"Reply {index}", reply_to=f"act-{index}"
            )
            assert result.success is True
            accepted.append((chat_id, result.message_id))

        third_inbound = _make_inbound(
            conv_id="conv-global-3", activity_id="act-3"
        )
        third_inbound["conversation"]["conversationType"] = "groupChat"
        third_ref = adapter_mod.ConversationRef.from_activity(third_inbound)
        assert third_ref is not None
        third_ref.validated_path = "A"
        a._capture_coalesced_turn_target(third_ref)
        a._conversations.upsert(third_ref)
        a._seen_inbounds_this_lifetime.add("conv-global-3")
        rejected = await a.send(
            chat_id="conv-global-3", content="Reply 3", reply_to="act-3"
        )

        assert rejected.success is False
        assert "global" in str(rejected.error)
        assert len(a._coalesced_replies) == 2
        assert len(a._coalesced_reply_tasks) == 2
        assert a._coalesced_generation_count_by_chat == {
            "conv-global-1": 1,
            "conv-global-2": 1,
        }
        for _chat_id, message_id in accepted:
            a._drop_coalesced_reply_state(str(message_id))
        assert a._coalesced_generation_count_by_chat == {}

    @pytest.mark.asyncio
    async def test_coalesced_content_bound_rejects_without_mutation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-content-bound")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        oversized = "x" * (a.MAX_MESSAGE_LENGTH + 1)

        rejected = await a.send(
            chat_id="conv-G-content-bound",
            content=oversized,
            reply_to="act-1",
        )
        assert rejected.success is False
        assert a._coalesced_replies == {}
        assert a._coalesced_reply_tasks == {}

        accepted = await a.send(
            chat_id="conv-G-content-bound", content="keep", reply_to="act-1"
        )
        state = a._coalesced_replies[accepted.message_id]
        captured_inbound = state["inbound"]
        rejected_update = await a.edit_message(
            "conv-G-content-bound",
            str(accepted.message_id),
            oversized,
            finalize=False,
        )
        assert rejected_update.success is False
        assert state["content"] == "keep"
        assert state["inbound"] is captured_inbound
        a._drop_coalesced_reply_state(str(accepted.message_id))

    @pytest.mark.asyncio
    async def test_duplicate_drop_cannot_undercount_generation_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G-idempotent-drop")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        first = await a.send(
            chat_id="conv-G-idempotent-drop", content="First", reply_to="act-1"
        )

        assert a._coalesced_generation_count_by_chat == {
            "conv-G-idempotent-drop": 1
        }
        a._drop_coalesced_reply_state(str(first.message_id))
        a._drop_coalesced_reply_state(str(first.message_id))
        assert a._coalesced_generation_count_by_chat == {}
        assert a._coalesced_generation_count_by_turn == {}

    @pytest.mark.asyncio
    async def test_evicted_turn_target_fails_closed_without_retargeting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_COALESCED_TURN_TARGETS_PER_CHAT", 2)
        a = _make_adapter(monkeypatch)
        first_inbound = _make_inbound(
            conv_id="conv-G-target-cap", activity_id="act-1"
        )
        first_inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=first_inbound)

        for activity_id in ("act-2", "act-3"):
            inbound = _make_inbound(
                conv_id="conv-G-target-cap", activity_id=activity_id
            )
            inbound["conversation"]["conversationType"] = "groupChat"
            ref = adapter_mod.ConversationRef.from_activity(inbound)
            assert ref is not None
            ref.validated_path = "A"
            a._capture_coalesced_turn_target(ref)
            a._conversations.upsert(ref)

        assert (
            "conv-G-target-cap",
            "act-1",
        ) not in a._coalesced_turn_targets
        rejected = await a.send(
            chat_id="conv-G-target-cap", content="Late reply", reply_to="act-1"
        )
        assert rejected.success is False
        assert "no cached inbound for exact turn" in str(rejected.error)
        assert a._coalesced_replies == {}
        assert a._coalesced_turn_target_count_by_chat == {
            "conv-G-target-cap": 2
        }

        await a._teardown_chat_state("conv-G-target-cap")
        assert a._coalesced_turn_targets == {}
        assert a._coalesced_turn_target_count_by_chat == {}

    @pytest.mark.asyncio
    async def test_exact_turn_precedes_lifetime_proactive_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(
            conv_id="conv-G-lifetime-evicted", activity_id="act-exact"
        )
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        proactive = AsyncMock()
        monkeypatch.setattr(a, "_send_proactive", proactive)
        a._seen_inbounds_this_lifetime.discard("conv-G-lifetime-evicted")

        result = await a.send(
            chat_id="conv-G-lifetime-evicted",
            content="Exact reply",
            reply_to="act-exact",
        )
        assert result.success is True
        assert result.message_id in a._coalesced_replies
        assert proactive.await_count == 0
        a._drop_coalesced_reply_state(str(result.message_id))

    @pytest.mark.asyncio
    async def test_persisted_exact_id_without_lifetime_capture_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(
            conv_id="conv-G-restarted", activity_id="act-before-restart"
        )
        inbound["conversation"]["conversationType"] = "groupChat"
        ref = adapter_mod.ConversationRef.from_activity(inbound)
        assert ref is not None
        ref.validated_path = "A"
        a._conversations.upsert(ref)

        result = await a.send(
            chat_id="conv-G-restarted",
            content="Must not use stale replyToActivity",
            reply_to="act-before-restart",
        )
        assert result.success is False
        assert "no cached inbound for exact turn" in str(result.error)
        assert a._coalesced_replies == {}

    @pytest.mark.asyncio
    async def test_exact_group_turn_precedes_latest_personal_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        group = _make_inbound(
            conv_id="conv-mixed-latest", activity_id="act-group"
        )
        group["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=group)

        personal = _make_inbound(
            conv_id="conv-mixed-latest", activity_id="act-personal"
        )
        personal_ref = adapter_mod.ConversationRef.from_activity(personal)
        assert personal_ref is not None
        personal_ref.validated_path = "A"
        a._capture_coalesced_turn_target(personal_ref)
        a._conversations.upsert(personal_ref)
        stream_start = AsyncMock()
        monkeypatch.setattr(a, "_send_stream_start", stream_start)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        result = await a.send(
            chat_id="conv-mixed-latest",
            content="Group reply",
            reply_to="act-group",
        )
        assert result.success is True
        assert result.message_id in a._coalesced_replies
        assert stream_start.await_count == 0
        assert (
            a._coalesced_replies[result.message_id]["inbound"]["id"]
            == "act-group"
        )
        finalized = await a.edit_message(
            "conv-mixed-latest",
            str(result.message_id),
            "Group reply",
            finalize=True,
        )
        assert finalized.success is True
        assert send_reply.await_args.kwargs["inbound"]["id"] == "act-group"
        assert send_reply.await_args.kwargs["validated_path"] == "A"

    @pytest.mark.asyncio
    async def test_teardown_does_not_evict_another_chats_delayed_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_COALESCED_TURN_TARGETS", 1)
        a = _make_adapter(monkeypatch)
        inbound_a = _make_inbound(conv_id="conv-target-A", activity_id="act-A")
        inbound_a["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound_a)
        generation_a = await a.send(
            chat_id="conv-target-A", content="A", reply_to="act-A"
        )

        inbound_b = _make_inbound(conv_id="conv-target-B", activity_id="act-B")
        inbound_b["conversation"]["conversationType"] = "groupChat"
        ref_b = adapter_mod.ConversationRef.from_activity(inbound_b)
        assert ref_b is not None
        ref_b.validated_path = "A"
        a._capture_coalesced_turn_target(ref_b)
        a._conversations.upsert(ref_b)
        a._seen_inbounds_this_lifetime.add("conv-target-B")

        await a._teardown_chat_state("conv-target-A")
        assert generation_a.message_id not in a._coalesced_replies
        assert ("conv-target-B", "act-B") in a._coalesced_turn_targets

        result_b = await a.send(
            chat_id="conv-target-B", content="B", reply_to="act-B"
        )
        assert result_b.success is True
        assert result_b.message_id in a._coalesced_replies
        a._drop_coalesced_reply_state(str(result_b.message_id))

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
        assert ("conv-G-watch", message_id) in a._recently_finalized

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
        assert ("conv-G-fail", message_id) not in a._recently_finalized
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
        assert a._streams[("conv-S", "hermes-msg-1")]["bf_stream_id"] == (
            "bf-stream-abc"
        )
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
        assert ("conv-F", "m1") not in a._streams
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
        assert ("conv-CC", "m2") not in a._streams
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
        assert ("conv-X", "m1") not in a._streams

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
        assert ("conv-N", "m1") not in a._streams

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
        ref = adapter_mod.ConversationRef.from_activity(inbound)
        assert ref is not None
        recipient = inbound.get("recipient") or {}
        ref.validated_path = "A" if recipient.get("agenticAppId") else "B"
        a._capture_coalesced_turn_target(ref)
        a._conversations.upsert(ref)
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
    async def test_status_cannot_buffer_while_old_client_closes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-status-disconnect")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        close_started = asyncio.Event()
        release_close = asyncio.Event()

        async def blocked_close() -> None:
            close_started.set()
            await release_close.wait()

        old_client = a._http_client
        old_client.aclose = AsyncMock(side_effect=blocked_close)
        disconnect = asyncio.create_task(a.disconnect())
        await close_started.wait()

        rejected = await a.send_or_update_status(
            "conv-status-disconnect", "lifecycle", "Too late"
        )
        assert rejected.success is False
        assert a._coalesced_status == {}

        release_close.set()
        await disconnect
        a._http_client = MagicMock()
        await asyncio.sleep(0.02)
        assert send_reply.await_count == 0
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

    @pytest.mark.asyncio
    async def test_teardown_tracks_resistant_status_flush_and_preserves_other_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        monkeypatch.setattr(
            adapter_mod, "_MAX_COALESCED_REPLY_GENERATIONS_PER_CHAT", 1
        )
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-status-retire")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        release = asyncio.Event()

        async def resistant_send_reply(**_kwargs: Any) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=resistant_send_reply)
        )
        result = await a.send_or_update_status(
            "conv-status-retire", "lifecycle", "Working"
        )
        key = str(result.message_id)
        original_watchdog = a._coalesced_status_tasks[key]
        original_watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await original_watchdog
        flush = asyncio.create_task(a._flush_coalesced_status(key))
        a._coalesced_status_tasks[key] = flush

        other_key = a._coalesced_status_key("conv-status-other", "lifecycle")
        other_release = asyncio.Event()
        other_task = asyncio.create_task(other_release.wait())
        a._coalesced_status[other_key] = {
            "chat_id": "conv-status-other",
            "lines": ["Keep"],
        }
        a._coalesced_status_tasks[other_key] = other_task
        await started.wait()

        await a._teardown_chat_state("conv-status-retire")
        assert flush in a._coalesced_reply_survivors
        assert key not in a._coalesced_status
        assert a._coalesced_status_tasks[other_key] is other_task
        assert other_task.done() is False
        rejected = a._buffer_coalesced_status(
            chat_id="conv-status-retire",
            status_key="replacement",
            content="Too soon",
            inbound=inbound,
        )
        assert rejected.success is False
        assert "disconnecting" in str(rejected.error)

        release.set()
        assert await asyncio.wait_for(flush, timeout=0.5) is False
        await asyncio.sleep(0)
        assert a._coalesced_reply_survivors == {}
        accepted = a._buffer_coalesced_status(
            chat_id="conv-status-retire",
            status_key="replacement",
            content="Now safe",
            inbound=inbound,
        )
        assert accepted.success is True
        a._drop_coalesced_status_state(str(accepted.message_id))
        other_release.set()
        await other_task
        a._drop_coalesced_status_state(other_key)

    @pytest.mark.asyncio
    async def test_disconnect_tracks_resistant_status_flush_until_completion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-status-disconnect-resistant")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        release = asyncio.Event()

        async def resistant_send_reply(**_kwargs: Any) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=resistant_send_reply)
        )
        result = await a.send_or_update_status(
            "conv-status-disconnect-resistant", "lifecycle", "Working"
        )
        key = str(result.message_id)
        original_watchdog = a._coalesced_status_tasks[key]
        original_watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await original_watchdog
        flush = asyncio.create_task(a._flush_coalesced_status(key))
        a._coalesced_status_tasks[key] = flush
        a._http_client.aclose = AsyncMock()
        await started.wait()

        await a.disconnect()
        assert flush in a._coalesced_reply_survivors
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

        release.set()
        assert await asyncio.wait_for(flush, timeout=0.5) is False
        await asyncio.sleep(0)
        assert a._coalesced_reply_survivors == {}

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
    async def test_reply_and_status_share_the_same_admission_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_mod, "_MAX_COALESCED_REPLY_GENERATIONS_PER_CHAT", 1
        )
        monkeypatch.setattr(adapter_mod, "_MAX_COALESCED_REPLY_GENERATIONS", 1)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-shared-budget")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)

        reply = await a.send(
            chat_id="conv-shared-budget", content="Reply", reply_to="act-1"
        )
        assert reply.success is True
        rejected_status = a._buffer_coalesced_status(
            chat_id="conv-shared-budget",
            status_key="lifecycle",
            content="Status",
            inbound=inbound,
        )
        assert rejected_status.success is False
        assert "backlog full" in str(rejected_status.error)
        a._drop_coalesced_reply_state(str(reply.message_id))

        status = a._buffer_coalesced_status(
            chat_id="conv-shared-budget",
            status_key="lifecycle",
            content="Status",
            inbound=inbound,
        )
        assert status.success is True
        rejected_reply = await a.send(
            chat_id="conv-shared-budget", content="Reply", reply_to="act-1"
        )
        assert rejected_reply.success is False
        assert "backlog full" in str(rejected_reply.error)
        a._drop_coalesced_status_state(str(status.message_id))

    @pytest.mark.asyncio
    async def test_status_buffer_caps_line_count_and_aggregate_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-status-bounds")
        inbound["conversation"]["conversationType"] = "groupChat"

        first = a._buffer_coalesced_status(
            chat_id="conv-status-bounds",
            status_key="lifecycle",
            content="x" * (adapter_mod._MAX_STATUS_LINE_CHARS * 2),
            inbound=inbound,
        )
        assert first.success is True
        key = str(first.message_id)
        assert len(a._coalesced_status[key]["lines"][0]) == (
            adapter_mod._MAX_STATUS_LINE_CHARS
        )

        results = [
            a._buffer_coalesced_status(
                chat_id="conv-status-bounds",
                status_key="lifecycle",
                content=f"{index}:" + "y" * adapter_mod._MAX_STATUS_LINE_CHARS,
                inbound=inbound,
            )
            for index in range(adapter_mod._MAX_STATUS_LINES * 2)
        ]
        lines = a._coalesced_status[key]["lines"]
        assert any(result.success is False for result in results)
        assert len(lines) <= adapter_mod._MAX_STATUS_LINES
        assert sum(len(line) for line in lines) + len(lines) - 1 <= (
            a.MAX_MESSAGE_LENGTH
        )
        a._drop_coalesced_status_state(key)
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_continuous_status_updates_flush_at_hard_lifetime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_STATUS_COALESCE_FLUSH_AFTER_SEC", 10.0)
        monkeypatch.setattr(adapter_mod, "_MAX_STATUS_BUFFER_AGE_SEC", 0.02)
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-status-lifetime")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire(a, inbound)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)

        await a.send_or_update_status(
            "conv-status-lifetime", "lifecycle", "line-0"
        )
        for index in range(1, 4):
            await asyncio.sleep(0.004)
            await a.send_or_update_status(
                "conv-status-lifetime", "lifecycle", f"line-{index}"
            )
        await asyncio.sleep(0.04)

        assert send_reply.await_count == 1
        assert a._coalesced_status == {}
        assert a._coalesced_status_tasks == {}

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
        reply = await a.send(
            chat_id="conv-G-buf", content="partial ▉", reply_to="act-1"
        )
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
            str(reply.message_id),
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
        inbound = _make_inbound(
            conv_id="conv-S1", activity_id="inbound-id-1"
        )  # personal by default
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
        assert ("conv-S1", "bf-stream-from-send") in a._streams
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
        inbound = _make_inbound(conv_id="conv-S2", activity_id="inbound-id-1")
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
        assert ("conv-S2", "bf-S2") not in a._streams
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
        a._streams[("conv-X", "stale-stream")] = {
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
        assert ("conv-X", "stale-stream") in a._streams
        assert a._active_stream_by_chat["conv-X"] == "stale-stream"

    @pytest.mark.asyncio
    async def test_new_stream_first_chunk_finalizes_prior_stream_before_starting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A new streaming first chunk may replace a stale stream, but only
        # after the adapter sends streamType=final for the previous one.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X2", activity_id="inbound-id-1")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        a._active_stream_by_chat["conv-X2"] = "stale-stream"
        a._streams[("conv-X2", "stale-stream")] = {
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
        assert ("conv-X2", "stale-stream") not in a._streams
        assert a._active_stream_by_chat["conv-X2"] == "bf-new"
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_new_stream_first_chunk_blocked_when_prior_finalize_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X3", activity_id="inbound-id-1")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        a._active_stream_by_chat["conv-X3"] = "stale-stream"
        a._streams[("conv-X3", "stale-stream")] = {
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
        assert ("conv-X3", "stale-stream") in a._streams
        assert a._active_stream_by_chat["conv-X3"] == "stale-stream"
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_repeated_stale_finalize_failure_force_drops_and_starts_new_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Liveness guard for #54 review feedback: a permanently dead BF
        # stream id must not wedge the chat forever.
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X4", activity_id="inbound-id-1")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        a._active_stream_by_chat["conv-X4"] = "stale-stream"
        a._streams[("conv-X4", "stale-stream")] = {
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
        assert ("conv-X4", "stale-stream") not in a._streams
        assert ("conv-X4", "stale-stream") in a._recently_finalized
        assert a._active_stream_by_chat["conv-X4"] == "bf-new"
        assert send_reply_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_expired_stale_stream_force_drops_on_first_finalize_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-X5", activity_id="inbound-id-1")
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        a._seen_inbounds_this_lifetime.add(inbound["conversation"]["id"])  # 19x-e
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        loop_now = asyncio.get_event_loop().time()
        a._active_stream_by_chat["conv-X5"] = "stale-stream"
        a._streams[("conv-X5", "stale-stream")] = {
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
        assert ("conv-X5", "stale-stream") not in a._streams
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
        a._streams[("c1", "m1")] = {
            "bf_stream_id": "m1",
            "sequence": 3,
            "last_emit_ts": 0.0,
        }
        a._active_stream_by_chat["c1"] = "m1"
        a._drop_stream_state("c1", "m1")
        assert ("c1", "m1") not in a._streams
        assert "c1" not in a._active_stream_by_chat

    def test_drop_stream_state_only_clears_chat_slot_when_id_matches(
        self, monkeypatch
    ) -> None:
        # Defensive: if a different stream is active in the chat slot,
        # don't clobber it.
        a = _make_adapter(monkeypatch)
        a._streams[("c1", "m1")] = {
            "bf_stream_id": "m1",
            "sequence": 3,
            "last_emit_ts": 0.0,
        }
        a._active_stream_by_chat["c1"] = "different-stream"
        a._drop_stream_state("c1", "m1")
        assert ("c1", "m1") not in a._streams
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
        # #105: prune protects in-flight turns in the registry's
        # conversation-id space — bridged from the base's whole-turn
        # _active_sessions (prefixed session-key space, which never matched the
        # registry's bare ids) via _session_key_to_conv.
        import asyncio as _asyncio

        a._active_sessions["sk:active-chat"] = _asyncio.Event()
        a._session_key_to_conv["sk:active-chat"] = "active-chat"

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
        await a._persist_conversations()

        dropped = await a.prune_conversations()
        assert dropped == 1
        # Round-trip from disk: the dropped entry isn't there.
        reloaded = ConversationRegistry.load(conv_path)
        assert "stale" not in reloaded

    @pytest.mark.asyncio
    async def test_concurrent_persists_serialize_freshest_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # M11 (#105): off-loop saves are serialized by _persist_lock, so a
        # later (fresher) snapshot's write always lands AFTER an earlier one's
        # — an older snapshot can't os.replace over a newer one and silently
        # stale/drop entries on disk. Force the first write to be slow so that
        # WITHOUT the lock the older {A} snapshot would land last.
        import asyncio as _asyncio
        import time as _time

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "c.json"
        a = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        real_write = ConversationRegistry.write_payload
        calls = {"n": 0}

        def slow_write(path: Path, payload: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                _time.sleep(0.25)  # the first (older) snapshot's write is slow
            real_write(path, payload)

        monkeypatch.setattr(
            ConversationRegistry, "write_payload", staticmethod(slow_write)
        )

        a._conversations.upsert(
            ConversationRef(conversation_id="A", service_url="https://x/")
        )
        t1 = _asyncio.create_task(a._persist_conversations())
        await _asyncio.sleep(0.02)  # let t1 take the lock + start its slow write
        a._conversations.upsert(
            ConversationRef(conversation_id="B", service_url="https://x/")
        )
        t2 = _asyncio.create_task(a._persist_conversations())
        await _asyncio.gather(t1, t2)

        # Freshest state ({A,B}) is what's on disk — not the stale {A}.
        reloaded = ConversationRegistry.load(conv_path)
        assert "A" in reloaded and "B" in reloaded

    @pytest.mark.asyncio
    async def test_cancelled_older_write_still_orders_before_newer_save(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # M11 (#105) review: _persist_conversations shields the locked write, so
        # cancelling an in-flight older save (e.g. on shutdown) must NOT let its
        # executor thread os.replace AFTER a newer save landed. The shield keeps
        # the cancelled older write holding _persist_lock until it completes, so
        # a newer save serializes strictly after it and wins on disk. Without the
        # shield the cancel would release the lock, the newer save would replace
        # first, then the still-running older thread would clobber it with stale
        # {A}.
        import asyncio as _asyncio
        import time as _time

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "c.json"
        a = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        real_write = ConversationRegistry.write_payload
        calls = {"n": 0}

        def slow_write(path: Path, payload: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                _time.sleep(0.25)  # the first (older) snapshot's write is slow
            real_write(path, payload)

        monkeypatch.setattr(
            ConversationRegistry, "write_payload", staticmethod(slow_write)
        )

        a._conversations.upsert(
            ConversationRef(conversation_id="A", service_url="https://x/")
        )
        t1 = _asyncio.create_task(a._persist_conversations())
        await _asyncio.sleep(0.02)  # let t1 take the lock + start its slow write
        a._conversations.upsert(
            ConversationRef(conversation_id="B", service_url="https://x/")
        )
        t2 = _asyncio.create_task(a._persist_conversations())
        # Cancel the OLDER save's awaiter mid-write; the shielded inner keeps
        # running to completion, still holding the lock.
        t1.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await t1
        await t2
        # Let the shielded (detached) older write finish its os.replace.
        while calls["n"] < 2:
            await _asyncio.sleep(0.01)
        await _asyncio.sleep(0.05)

        # Newer snapshot ({A,B}) is what survives on disk — not stale {A}.
        reloaded = ConversationRegistry.load(conv_path)
        assert "A" in reloaded and "B" in reloaded

    @pytest.mark.asyncio
    async def test_replacement_adapter_write_orders_after_old_shielded_save(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import asyncio as _asyncio
        import threading as _threading

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        conv_path = tmp_path / "shared.json"
        old = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        replacement = _make_adapter(
            monkeypatch, conversations_path=str(conv_path)
        )
        real_write = ConversationRegistry.write_payload
        old_started = _threading.Event()
        release_old = _threading.Event()

        def blocked_old_write(path: Path, payload: Any) -> None:
            ids = {
                str(item.get("conversation_id") or "")
                for item in payload.get("conversations", [])
            }
            if ids == {"old"}:
                old_started.set()
                assert release_old.wait(timeout=1.0)
            real_write(path, payload)

        monkeypatch.setattr(
            ConversationRegistry,
            "write_payload",
            staticmethod(blocked_old_write),
        )
        old._conversations.upsert(
            ConversationRef(conversation_id="old", service_url="https://x/")
        )
        old_save = _asyncio.create_task(old._persist_conversations())
        assert await _asyncio.to_thread(old_started.wait, 1.0)
        old._conversations.upsert(
            ConversationRef(
                conversation_id="queued-stale", service_url="https://x/"
            )
        )
        queued_stale_save = _asyncio.create_task(old._persist_conversations())
        await _asyncio.sleep(0.02)
        old_save.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await old_save
        queued_stale_save.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await queued_stale_save

        await _asyncio.wait_for(old.disconnect(), timeout=0.5)
        assert old._disconnecting is True
        replacement_activation = _asyncio.create_task(
            replacement._activate_persist_owner()
        )
        await _asyncio.sleep(0.02)
        release_old.set()
        await _asyncio.wait_for(replacement_activation, timeout=0.5)
        replacement._conversations.upsert(
            ConversationRef(
                conversation_id="replacement", service_url="https://x/"
            )
        )
        replacement_save = _asyncio.create_task(
            replacement._persist_conversations()
        )
        await _asyncio.wait_for(replacement_save, timeout=0.5)
        await _asyncio.sleep(0)

        reloaded = ConversationRegistry.load(conv_path)
        assert "replacement" in reloaded

    @pytest.mark.asyncio
    async def test_replacement_write_beats_older_save_waiting_for_admission(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import asyncio as _asyncio
        import threading as _threading

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "shared-admission.json"
        old = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        replacement = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        old._persist_semaphore = _asyncio.Semaphore(1)
        real_write = ConversationRegistry.write_payload
        first_started = _threading.Event()
        release_first = _threading.Event()

        def blocked_first_write(path: Path, payload: Any) -> None:
            ids = {
                str(item.get("conversation_id") or "")
                for item in payload.get("conversations", [])
            }
            if ids == {"old-first"}:
                first_started.set()
                assert release_first.wait(timeout=1.0)
            real_write(path, payload)

        monkeypatch.setattr(
            ConversationRegistry,
            "write_payload",
            staticmethod(blocked_first_write),
        )
        old._conversations.upsert(
            ConversationRef(conversation_id="old-first", service_url="https://x/")
        )
        first_save = _asyncio.create_task(old._persist_conversations())
        assert await _asyncio.to_thread(first_started.wait, 1.0)

        old._conversations.upsert(
            ConversationRef(conversation_id="old-queued", service_url="https://x/")
        )
        queued_old_save = _asyncio.create_task(old._persist_conversations())
        await _asyncio.sleep(0)
        replacement_activation = _asyncio.create_task(
            replacement._activate_persist_owner()
        )
        await _asyncio.sleep(0.02)
        release_first.set()
        await _asyncio.wait_for(
            _asyncio.gather(first_save, queued_old_save, replacement_activation),
            timeout=1.0,
        )
        replacement._conversations.upsert(
            ConversationRef(conversation_id="replacement", service_url="https://x/")
        )
        replacement_save = _asyncio.create_task(replacement._persist_conversations())
        await _asyncio.wait_for(replacement_save, timeout=1.0)

        reloaded = ConversationRegistry.load(conv_path)
        assert "replacement" in reloaded
        assert "old-first" in reloaded
        assert "old-queued" in reloaded

    @pytest.mark.asyncio
    async def test_retiring_adapter_cannot_reserve_after_replacement_write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "retiring-owner.json"
        retiring = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        replacement = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        await retiring._activate_persist_owner()
        await replacement._activate_persist_owner()
        replacement._conversations.upsert(
            ConversationRef(conversation_id="replacement", service_url="https://x/")
        )
        await replacement._persist_conversations()

        retiring._conversations.upsert(
            ConversationRef(conversation_id="retiring", service_url="https://x/")
        )
        await retiring._persist_conversations()

        reloaded = ConversationRegistry.load(conv_path)
        assert "replacement" in reloaded
        assert "retiring" not in reloaded

    @pytest.mark.asyncio
    async def test_failed_replacement_releases_owner_and_ingress_cannot_reclaim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import httpx

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "failed-owner-rollback.json"
        active = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        replacement = _make_adapter(
            monkeypatch, conversations_path=str(conv_path)
        )
        active_owner = await active._activate_persist_owner()
        assert (
            await replacement._activate_persist_owner(tentative=True)
            > active_owner
        )
        replacement._http_client = MagicMock()
        replacement._http_client.aclose = AsyncMock()

        await replacement._cleanup_failed_connect_runtime()

        assert replacement._persist_owner_sequence is None
        transport = httpx.ASGITransport(app=replacement.build_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            rejected = await client.post(
                "/api/messages",
                json=_make_inbound(conv_id="conv-failed-owner"),
                headers={"Authorization": "Bearer a.b.c"},
            )
        assert rejected.status_code == 503
        assert rejected.json()["reason"] == "connect_failed"
        assert replacement._persist_owner_sequence is None

        active._conversations.upsert(
            ConversationRef(conversation_id="active-write", service_url="https://x/")
        )
        await active._persist_conversations()
        assert "active-write" in ConversationRegistry.load(conv_path)

    @pytest.mark.asyncio
    async def test_overlapping_failed_replacements_restore_live_owner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "overlapping-owner-rollback.json"
        active = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        failed_b = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        failed_c = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        active_owner = await active._activate_persist_owner()
        assert await failed_b._activate_persist_owner(tentative=True) > active_owner
        assert await failed_c._activate_persist_owner(tentative=True) > active_owner
        for adapter in (failed_b, failed_c):
            adapter._http_client = MagicMock()
            adapter._http_client.aclose = AsyncMock()

        await failed_b._cleanup_failed_connect_runtime()
        await failed_c._cleanup_failed_connect_runtime()

        active._conversations.upsert(
            ConversationRef(conversation_id="live-owner", service_url="https://x/")
        )
        await active._persist_conversations()
        assert "live-owner" in ConversationRegistry.load(conv_path)

    @pytest.mark.asyncio
    async def test_failed_owner_is_not_resurrected_by_inflight_successor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import asyncio as _asyncio
        import threading as _threading

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "inflight-successor-rollback.json"
        active = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        failed_b = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        failed_c = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        await active._activate_persist_owner()
        await failed_b._activate_persist_owner(tentative=True)

        real_write = ConversationRegistry.write_payload
        b_write_started = _threading.Event()
        release_b_write = _threading.Event()

        def blocked_b_write(path: Path, payload: Any) -> None:
            b_write_started.set()
            assert release_b_write.wait(timeout=1.0)
            real_write(path, payload)

        monkeypatch.setattr(
            ConversationRegistry,
            "write_payload",
            staticmethod(blocked_b_write),
        )
        failed_b._conversations.upsert(
            ConversationRef(conversation_id="from-b", service_url="https://x/")
        )
        state, sequence = adapter_mod._reserve_persist_sequence(
            conv_path, failed_b._persist_owner_sequence
        )
        assert sequence is not None
        b_save = _asyncio.create_task(
            failed_b._write_persist(
                ConversationRegistry,
                conv_path,
                failed_b._conversations.to_payload(),
                state,
                sequence,
                failed_b._persist_owner_sequence,
            )
        )
        assert await _asyncio.to_thread(b_write_started.wait, 1.0)
        c_activation = _asyncio.create_task(
            failed_c._activate_persist_owner(tentative=True)
        )
        await _asyncio.sleep(0.02)
        assert not c_activation.done()

        failed_b._release_failed_persist_owner()
        release_b_write.set()
        await _asyncio.wait_for(
            _asyncio.gather(b_save, c_activation), timeout=1.0
        )
        failed_c._release_failed_persist_owner()

        active._conversations.upsert(
            ConversationRef(conversation_id="live-after-c", service_url="https://x/")
        )
        await active._persist_conversations()
        assert "live-after-c" in ConversationRegistry.load(conv_path)

    @pytest.mark.asyncio
    async def test_committed_owner_survives_later_failed_reconnect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "committed-owner-reconnect.json"
        old = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        current = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        old_owner = await old._activate_persist_owner()
        current_owner = await current._activate_persist_owner(tentative=True)
        assert current_owner > old_owner
        current._commit_persist_owner()

        await current._cleanup_failed_connect_runtime()

        assert current._persist_owner_sequence == current_owner
        old._conversations.upsert(
            ConversationRef(conversation_id="stale", service_url="https://x/")
        )
        await old._persist_conversations()
        current._conversations.upsert(
            ConversationRef(conversation_id="current", service_url="https://x/")
        )
        await current._persist_conversations()
        reloaded = ConversationRegistry.load(conv_path)
        assert "current" in reloaded
        assert "stale" not in reloaded

    @pytest.mark.asyncio
    async def test_tentative_handoff_retains_live_owner_writes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import asyncio as _asyncio
        import threading as _threading

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "tentative-owner-writes.json"
        active = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        await active._activate_persist_owner()
        active._conversations.upsert(
            ConversationRef(conversation_id="seed", service_url="https://x/")
        )
        await active._persist_conversations()
        replacement = _make_adapter(monkeypatch, conversations_path=str(conv_path))

        real_write = ConversationRegistry.write_payload
        first_started = _threading.Event()
        release_first = _threading.Event()

        def blocked_first_write(path: Path, payload: Any) -> None:
            ids = {
                str(item.get("conversation_id") or "")
                for item in payload.get("conversations", [])
            }
            if ids == {"seed", "during-handoff"}:
                first_started.set()
                assert release_first.wait(timeout=1.0)
            real_write(path, payload)

        monkeypatch.setattr(
            ConversationRegistry,
            "write_payload",
            staticmethod(blocked_first_write),
        )
        active._conversations.upsert(
            ConversationRef(
                conversation_id="during-handoff", service_url="https://x/"
            )
        )
        first_save = _asyncio.create_task(active._persist_conversations())
        assert await _asyncio.to_thread(first_started.wait, 1.0)
        activation = _asyncio.create_task(
            replacement._activate_persist_owner(tentative=True)
        )
        await _asyncio.sleep(0.02)
        assert not activation.done()

        active._conversations.upsert(
            ConversationRef(
                conversation_id="late-handoff", service_url="https://x/"
            )
        )
        late_save = _asyncio.create_task(active._persist_conversations())
        await _asyncio.sleep(0.02)
        release_first.set()
        await _asyncio.wait_for(
            _asyncio.gather(first_save, late_save, activation), timeout=1.0
        )

        assert "during-handoff" in replacement._conversations
        assert "late-handoff" in replacement._conversations
        await replacement._cleanup_failed_connect_runtime()
        active._conversations.upsert(
            ConversationRef(conversation_id="after-rollback", service_url="https://x/")
        )
        await active._persist_conversations()
        reloaded = ConversationRegistry.load(conv_path)
        assert "late-handoff" in reloaded
        assert "after-rollback" in reloaded

    @pytest.mark.asyncio
    async def test_handoff_waits_for_inflight_uninstall_mutation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import asyncio as _asyncio

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "inflight-uninstall-handoff.json"
        active = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        await active._activate_persist_owner()
        active._conversations.upsert(
            ConversationRef(conversation_id="revoked", service_url="https://x/")
        )
        await active._persist_conversations()
        replacement = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        assert "revoked" in replacement._conversations

        teardown_started = _asyncio.Event()
        release_teardown = _asyncio.Event()

        async def blocked_teardown(_chat_id: str) -> bool:
            teardown_started.set()
            await release_teardown.wait()
            return True

        monkeypatch.setattr(active, "_teardown_chat_state", blocked_teardown)
        eviction = _asyncio.create_task(active._evict_conversation("revoked"))
        await teardown_started.wait()
        activation = _asyncio.create_task(
            replacement._activate_persist_owner(tentative=True)
        )
        await _asyncio.sleep(0.02)
        assert not activation.done()

        later_reservation = await active._reserve_registry_mutation()
        assert later_reservation is not None
        active._conversations.upsert(
            ConversationRef(conversation_id="unrelated", service_url="https://x/")
        )
        await active._persist_conversations(later_reservation)
        assert "revoked" in ConversationRegistry.load(conv_path)

        release_teardown.set()
        await _asyncio.wait_for(
            _asyncio.gather(eviction, activation), timeout=1.0
        )
        assert "revoked" not in replacement._conversations
        assert "unrelated" in replacement._conversations
        assert "revoked" not in ConversationRegistry.load(conv_path)

    @pytest.mark.asyncio
    async def test_cancelled_tentative_handoff_releases_claim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import asyncio as _asyncio

        conv_path = tmp_path / "cancelled-handoff.json"
        active = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        cancelled = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        successor = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        active_owner = await active._activate_persist_owner()
        state, sequence = adapter_mod._reserve_persist_sequence(
            conv_path, active_owner
        )
        assert sequence is not None

        activation = _asyncio.create_task(
            cancelled._activate_persist_owner(tentative=True)
        )
        await _asyncio.sleep(0.02)
        assert not activation.done()
        activation.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await _asyncio.wait_for(activation, timeout=0.5)
        assert cancelled._persist_owner_sequence is None

        adapter_mod._complete_persist_sequence(state, sequence)
        successor_owner = await _asyncio.wait_for(
            successor._activate_persist_owner(tentative=True), timeout=0.5
        )
        assert successor_owner > active_owner

    @pytest.mark.asyncio
    async def test_tentative_handoff_timeout_releases_claim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import asyncio as _asyncio

        monkeypatch.setattr(adapter_mod, "_PERSIST_HANDOFF_TIMEOUT_SEC", 0.02)
        conv_path = tmp_path / "timed-out-handoff.json"
        active = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        timed_out = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        successor = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        active_owner = await active._activate_persist_owner()
        state, sequence = adapter_mod._reserve_persist_sequence(
            conv_path, active_owner
        )
        assert sequence is not None

        with pytest.raises(TimeoutError, match="persistence handoff timed out"):
            await timed_out._activate_persist_owner(tentative=True)
        assert timed_out._persist_owner_sequence is None

        adapter_mod._complete_persist_sequence(state, sequence)
        successor_owner = await _asyncio.wait_for(
            successor._activate_persist_owner(tentative=True), timeout=0.5
        )
        assert successor_owner > active_owner

    @pytest.mark.asyncio
    async def test_replacement_activation_reloads_preclaim_uninstall(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "preclaim-uninstall.json"
        old = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        old.build_app()
        old._conversations.upsert(
            ConversationRef(conversation_id="revoked", service_url="https://x/")
        )
        await old._persist_conversations()

        replacement = _make_adapter(
            monkeypatch, conversations_path=str(conv_path)
        )
        assert "revoked" in replacement._conversations
        old._conversations.evict("revoked")
        await old._persist_conversations()

        await replacement._activate_persist_owner()
        assert "revoked" not in replacement._conversations
        replacement._conversations.upsert(
            ConversationRef(conversation_id="new", service_url="https://x/")
        )
        await replacement._persist_conversations()

        reloaded = ConversationRegistry.load(conv_path)
        assert "new" in reloaded
        assert "revoked" not in reloaded

    @pytest.mark.asyncio
    async def test_replacement_drains_queued_preclaim_uninstall(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import asyncio as _asyncio
        import threading as _threading

        from hermes_a365.plugin.conversations import (
            ConversationRef,
            ConversationRegistry,
        )

        conv_path = tmp_path / "queued-preclaim-uninstall.json"
        old = _make_adapter(monkeypatch, conversations_path=str(conv_path))
        old._conversations.upsert(
            ConversationRef(conversation_id="revoked", service_url="https://x/")
        )
        await old._persist_conversations()
        replacement = _make_adapter(
            monkeypatch, conversations_path=str(conv_path)
        )
        assert "revoked" in replacement._conversations

        real_write = ConversationRegistry.write_payload
        holding_started = _threading.Event()
        release_holding = _threading.Event()
        blocked_once = False

        def blocked_write(path: Path, payload: Any) -> None:
            nonlocal blocked_once
            ids = {
                str(item.get("conversation_id") or "")
                for item in payload.get("conversations", [])
            }
            if ids == {"revoked"} and not blocked_once:
                blocked_once = True
                holding_started.set()
                assert release_holding.wait(timeout=1.0)
            real_write(path, payload)

        monkeypatch.setattr(
            ConversationRegistry,
            "write_payload",
            staticmethod(blocked_write),
        )
        holding_save = _asyncio.create_task(old._persist_conversations())
        assert await _asyncio.to_thread(holding_started.wait, 1.0)
        old._conversations.evict("revoked")
        uninstall_save = _asyncio.create_task(old._persist_conversations())
        await _asyncio.sleep(0.02)

        activation = _asyncio.create_task(
            replacement._activate_persist_owner()
        )
        await _asyncio.sleep(0.02)
        assert not activation.done()
        activation.cancel()
        await _asyncio.sleep(0.02)
        assert not activation.done()
        release_holding.set()
        await _asyncio.wait_for(
            _asyncio.gather(holding_save, uninstall_save),
            timeout=1.0,
        )
        with pytest.raises(_asyncio.CancelledError):
            await activation
        await replacement._activate_persist_owner()

        assert "revoked" not in replacement._conversations
        reloaded = ConversationRegistry.load(conv_path)
        assert "revoked" not in reloaded

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
            headers={"Authorization": "Bearer a.b.c"},
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
            headers={"Authorization": "Bearer a.b.c"},
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
            headers={"Authorization": "Bearer a.b.c"},
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
            headers={"Authorization": "Bearer a.b.c"},
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
        headers = {"Authorization": "Bearer a.b.c"}
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
            headers={"Authorization": "Bearer a.b.c"},
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
            headers={"Authorization": "Bearer a.b.c"},
        )
        assert r.status_code == 200
        ctx = captured["ctx"]
        assert ctx.user_oid == "claim-oid"
        assert ctx.tenant_id == "claim-tid"
        assert ctx.user_upn == "u@x"

    @pytest.mark.parametrize(
        ("conversation_type", "expected_chat_type"),
        [("   ", "group"), ("PERSONAL", "dm"), ("CHANNEL", "channel")],
    )
    def test_conversation_type_normalization_matches_auth_and_invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        conversation_type: str,
        expected_chat_type: str,
    ) -> None:
        a, client = self._client(monkeypatch)
        authorized_chat_types: list[str] = []
        captured: dict[str, Any] = {}

        def authorize(_user_id: str, chat_type: str, _chat_id: str) -> bool:
            authorized_chat_types.append(chat_type)
            return chat_type == expected_chat_type

        async def capture(ctx: Any, *, registry: Any = None) -> Any:
            captured["ctx"] = ctx
            return adapter_mod.invoke.InvokeResponse(200, {"ok": True})

        a._is_sender_authorized = authorize
        monkeypatch.setattr(adapter_mod.invoke, "dispatch_invoke", capture)
        body = self._invoke_body()
        body["conversation"]["conversationType"] = conversation_type

        response = client.post(
            "/api/messages",
            json=body,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert response.status_code == 200
        assert authorized_chat_types == [expected_chat_type]
        assert captured["ctx"].chat_type == expected_chat_type


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
    async def test_concurrent_downloads_reserve_media_quota(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MEDIA_CACHE_QUOTA_BYTES", 10)
        a = self._adapter(monkeypatch, tmp_path)
        replacement = self._adapter(monkeypatch, tmp_path)
        stream_started = asyncio.Event()
        release_stream = asyncio.Event()
        resp = MagicMock(status_code=200, headers={})

        async def blocked_bytes() -> Any:
            stream_started.set()
            await release_stream.wait()
            yield b"123456"

        resp.aiter_bytes = blocked_bytes
        a._http_client.stream = MagicMock(return_value=_StreamCM(resp))
        replacement._http_client.stream = MagicMock()
        first = asyncio.create_task(
            a._download_inbound_media(
                "https://contoso.sharepoint.com/one",
                headers=None,
                extension=".bin",
                max_bytes=6,
                chat_id="conv-media-quota",
            )
        )
        await stream_started.wait()

        second = await replacement._download_inbound_media(
            "https://contoso.sharepoint.com/two",
            headers=None,
            extension=".bin",
            max_bytes=6,
            chat_id="conv-media-quota",
        )
        assert second is None
        assert a._http_client.stream.call_count == 1
        assert replacement._http_client.stream.call_count == 0

        release_stream.set()
        downloaded = await asyncio.wait_for(first, timeout=0.5)
        assert downloaded is not None
        state = adapter_mod._media_cache_state(a._media_cache_dir())
        assert state["reserved"] == 0

    @pytest.mark.asyncio
    async def test_cross_adapter_media_cache_enforces_file_count_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_MEDIA_CACHE_FILES", 1)
        first_adapter = self._adapter(monkeypatch, tmp_path)
        replacement = self._adapter(monkeypatch, tmp_path)
        self._stream(first_adapter, b"")
        self._stream(replacement, b"")

        first = await first_adapter._download_inbound_media(
            "https://contoso.sharepoint.com/one",
            headers=None,
            extension=".bin",
            max_bytes=1,
            chat_id="conv-media-files",
        )
        second = await replacement._download_inbound_media(
            "https://contoso.sharepoint.com/two",
            headers=None,
            extension=".bin",
            max_bytes=1,
            chat_id="conv-media-files",
        )

        assert first is not None
        assert second is not None
        assert not Path(first[0]).exists()
        assert Path(second[0]).exists()
        assert len(list(replacement._media_cache_dir().iterdir())) == 1

    @pytest.mark.asyncio
    async def test_active_turn_media_lease_blocks_cross_adapter_eviction(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_MEDIA_CACHE_FILES", 1)
        first_adapter = self._adapter(monkeypatch, tmp_path)
        replacement = self._adapter(monkeypatch, tmp_path)
        self._stream(first_adapter, b"first")
        self._stream(replacement, b"second")
        leases: set[Path] = set()

        first = await first_adapter._download_inbound_media(
            "https://contoso.sharepoint.com/one",
            headers=None,
            extension=".bin",
            max_bytes=8,
            chat_id="conv-media-lease",
            lease_paths=leases,
        )
        assert first is not None
        release_turn = asyncio.Event()

        async def active_turn() -> None:
            await release_turn.wait()

        owner = asyncio.create_task(active_turn())
        first_adapter._media_leases_by_session["session-media"] = leases
        first_adapter._watch_agent_turn_owner(
            "session-media", asyncio.Event(), owner
        )

        rejected = await replacement._download_inbound_media(
            "https://contoso.sharepoint.com/two",
            headers=None,
            extension=".bin",
            max_bytes=8,
            chat_id="conv-other",
        )
        assert rejected is None
        assert Path(first[0]).read_bytes() == b"first"

        release_turn.set()
        await owner
        await asyncio.sleep(0)
        admitted = await replacement._download_inbound_media(
            "https://contoso.sharepoint.com/two",
            headers=None,
            extension=".bin",
            max_bytes=8,
            chat_id="conv-other",
        )
        assert admitted is not None
        assert not Path(first[0]).exists()

    @pytest.mark.asyncio
    async def test_failed_same_session_request_releases_only_its_media(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(adapter_mod, "_MAX_MEDIA_CACHE_FILES", 2)
        active_adapter = self._adapter(monkeypatch, tmp_path)
        replacement = self._adapter(monkeypatch, tmp_path)
        self._stream(active_adapter, b"active")
        self._stream(replacement, b"pressure")
        active_leases: set[Path] = set()
        failed_leases: set[Path] = set()

        active = await active_adapter._download_inbound_media(
            "https://contoso.sharepoint.com/active",
            headers=None,
            extension=".bin",
            max_bytes=8,
            chat_id="conv-shared-session",
            lease_paths=active_leases,
        )
        failed = await active_adapter._download_inbound_media(
            "https://contoso.sharepoint.com/failed",
            headers=None,
            extension=".bin",
            max_bytes=8,
            chat_id="conv-shared-session",
            lease_paths=failed_leases,
        )
        assert active is not None and failed is not None
        active_adapter._media_leases_by_session["shared-session"] = (
            active_leases | failed_leases
        )

        active_adapter._release_request_media_leases(
            "shared-session", failed_leases
        )
        pressure = await replacement._download_inbound_media(
            "https://contoso.sharepoint.com/pressure",
            headers=None,
            extension=".bin",
            max_bytes=8,
            chat_id="conv-pressure",
        )

        assert pressure is not None
        assert Path(active[0]).read_bytes() == b"active"
        assert not Path(failed[0]).exists()
        active_adapter._release_session_media_leases("shared-session")

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
    async def test_inbound_media_token_mint_cannot_cross_disconnect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def delayed_token(**_kwargs: Any) -> tuple[str, str]:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "STALE", "A"

        monkeypatch.setattr(
            bridge, "acquire_reply_token", AsyncMock(side_effect=delayed_token)
        )
        old_client = a._http_client
        old_client.aclose = AsyncMock()
        activity = _make_inbound(conv_id="conv-media-token")
        activity["attachments"] = [
            {
                "contentType": "image/png",
                "contentUrl": "https://smba.trafficmanager.net/att/1",
            }
        ]
        extraction = asyncio.create_task(
            a._extract_inbound_media(activity, validated_path="A")
        )
        await started.wait()

        await a.disconnect()
        await cancelled.wait()
        replacement = MagicMock()
        replacement.stream = MagicMock()
        a._http_client = replacement
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(extraction, timeout=0.5)

        assert replacement.stream.call_count == 0

    @pytest.mark.asyncio
    async def test_inbound_media_stream_cannot_cross_disconnect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}

        async def delayed_bytes() -> Any:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield b"STALE"

        resp.aiter_bytes = delayed_bytes
        old_client = a._http_client
        old_client.stream = MagicMock(return_value=_StreamCM(resp))
        old_client.aclose = AsyncMock()
        activity = _make_inbound(conv_id="conv-media-stream")
        activity["attachments"] = [
            {
                "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                "content": {
                    "downloadUrl": "https://contoso.sharepoint.com/dl",
                    "fileType": "pdf",
                },
            }
        ]
        extraction = asyncio.create_task(
            a._extract_inbound_media(activity, validated_path="A")
        )
        await started.wait()

        await a.disconnect()
        await cancelled.wait()
        replacement = MagicMock()
        replacement.stream = MagicMock()
        a._http_client = replacement
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(extraction, timeout=0.5)

        assert replacement.stream.call_count == 0
        assert list(a._media_cache_dir().iterdir()) == []

    @pytest.mark.asyncio
    async def test_inbound_media_cancelled_during_stream_exit_removes_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._adapter(monkeypatch, tmp_path)
        exit_started = asyncio.Event()
        exit_cancelled = asyncio.Event()
        release_exit = asyncio.Event()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}

        async def one_chunk() -> Any:
            yield b"PARTIAL"

        resp.aiter_bytes = one_chunk

        class DelayedExitStream:
            async def __aenter__(self) -> Any:
                return resp

            async def __aexit__(self, *_exc: Any) -> bool:
                exit_started.set()
                try:
                    await release_exit.wait()
                except asyncio.CancelledError:
                    exit_cancelled.set()
                    raise
                return False

        a._http_client.stream = MagicMock(return_value=DelayedExitStream())
        a._http_client.aclose = AsyncMock()
        activity = _make_inbound(conv_id="conv-media-exit")
        activity["attachments"] = [
            {
                "contentType": adapter_mod._TEAMS_FILE_DOWNLOAD_INFO,
                "content": {
                    "downloadUrl": "https://contoso.sharepoint.com/dl",
                    "fileType": "pdf",
                },
            }
        ]
        extraction = asyncio.create_task(
            a._extract_inbound_media(activity, validated_path="A")
        )
        await exit_started.wait()

        await a.disconnect()
        await exit_cancelled.wait()
        release_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(extraction, timeout=0.5)

        assert list(a._media_cache_dir().iterdir()) == []

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
            "lifecycle_generation": a._lifecycle_generation,
            "chat_generation": a._chat_generation(conv_id),
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
    async def test_delayed_file_consent_offer_cannot_cross_disconnect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        a._conversations.upsert(
            adapter_mod.ConversationRef.from_activity(_make_inbound())
        )
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_send_reply(**_kwargs: Any) -> None:
            started.set()
            await release.wait()

        send_reply = AsyncMock(side_effect=delayed_send_reply)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        old_client = a._http_client
        old_client.aclose = AsyncMock()
        file_path = tmp_path / "late.pdf"
        file_path.write_bytes(b"late consent bytes")
        offer = asyncio.create_task(a.send_document("conv-1", str(file_path)))
        await started.wait()
        consent_id = send_reply.await_args.kwargs["reply"]["attachments"][0][
            "content"
        ]["acceptContext"]["consentId"]

        await a.disconnect()
        a._http_client = MagicMock()
        a._http_client.put = AsyncMock()
        release.set()
        result = await asyncio.wait_for(offer, timeout=0.5)

        assert result.success is False
        assert a._pending_file_uploads == {}
        await a._handle_file_consent(
            self._consent_activity(
                consent_id=consent_id,
                upload_info={"uploadUrl": self._GOOD_UPLOAD},
            ),
            validated_path="A",
        )
        assert a._http_client.put.await_count == 0

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
    async def test_file_acceptance_cannot_confirm_through_replacement_client(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        file_path = tmp_path / "cross-client.pdf"
        file_path.write_bytes(b"cross-client")
        self._seed_pending(a, file_path)
        bridge = adapter_mod._import_bridge()
        confirmations = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", confirmations)
        put_started = asyncio.Event()
        release_put = asyncio.Event()

        async def delayed_put(*_args: Any, **_kwargs: Any) -> Any:
            put_started.set()
            await release_put.wait()
            return MagicMock(status_code=201)

        old_client = a._http_client
        old_client.put = AsyncMock(side_effect=delayed_put)
        old_client.aclose = AsyncMock()
        acceptance = asyncio.create_task(
            a._handle_file_consent(
                self._consent_activity(
                    upload_info={"uploadUrl": self._GOOD_UPLOAD}
                ),
                validated_path="A",
            )
        )
        await put_started.wait()
        await a.disconnect()
        replacement = MagicMock()
        replacement.put = AsyncMock()
        a._http_client = replacement

        release_put.set()
        response = await asyncio.wait_for(acceptance, timeout=0.5)

        assert response.status == 200
        assert old_client.put.await_count == 1
        assert replacement.put.await_count == 0
        assert confirmations.await_count == 0

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
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
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
        assert all(
            set(act["data"]) == {"hermes_kind", "capability", "choice_id"}
            for act in actions
        )
        capability = a._card_capabilities[actions[0]["data"]["capability"]]
        assert list(capability["choices"].values()) == ["once", "session", "always", "deny"]
        assert capability["resolver"] == {"session_key": "sess-1"}
        assert all(act["data"]["hermes_kind"] == "exec_approval" for act in actions)
        assert all("session_key" not in act["data"] for act in actions)
        # A system approval card is NOT AI-generated content — no #73(a) entity.
        assert "entities" not in reply

    @pytest.mark.asyncio
    async def test_delayed_approval_card_cannot_survive_chat_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_send_reply(**_kwargs: Any) -> None:
            started.set()
            await release.wait()

        monkeypatch.setattr(
            bridge, "send_reply", AsyncMock(side_effect=delayed_send_reply)
        )
        send = asyncio.create_task(
            a.send_exec_approval("conv-1", "danger", "sess-1")
        )
        await started.wait()
        await a._teardown_chat_state("conv-1")
        release.set()
        result = await asyncio.wait_for(send, timeout=0.5)

        assert result.success is False
        assert a._card_capabilities == {}

    @pytest.mark.asyncio
    async def test_delivered_approval_is_rejected_after_chat_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        resolver = MagicMock(return_value=1)
        monkeypatch.setattr(a, "_gw_resolve_approval", resolver)
        result = await a.send_exec_approval("conv-1", "danger", "sess-1")
        assert result.success is True
        value = send_reply.await_args.kwargs["reply"]["attachments"][0]["content"][
            "actions"
        ][0]["data"]

        await a._teardown_chat_state("conv-1")
        response = await a._handle_card_action(_make_inbound(), value)

        assert response.status_code == 403
        resolver.assert_not_called()

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
        capability = a._card_capabilities[actions[0]["data"]["capability"]]
        assert list(capability["choices"].values()) == ["once", "always", "cancel"]
        assert capability["resolver"] == {
            "session_key": "sess-1",
            "confirm_id": "cfm-9",
        }
        assert all("confirm_id" not in act["data"] for act in actions)

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
        capability = a._card_capabilities[actions[0]["data"]["capability"]]
        assert list(capability["choices"].values()) == ["Alpha", "Beta", "__other__"]

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
        activity = _make_inbound(conv_id="conv-1")
        value = _seed_card_capability(
            a,
            activity,
            kind="exec_approval",
            choice="always",
            resolver={"session_key": "sess-1"},
        )
        resp = await a._handle_card_action(activity, value)
        assert resp.status_code == 200
        assert json.loads(resp.body)["kind"] == "exec_approval"
        resolver.assert_called_once_with("sess-1", "always")

    @pytest.mark.asyncio
    async def test_card_capability_is_user_bound_single_use_and_resists_dos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        resolver = MagicMock(return_value=1)
        monkeypatch.setattr(a, "_gw_resolve_approval", resolver)
        owner = _make_inbound(conv_id="conv-1")
        value = _seed_card_capability(
            a,
            owner,
            kind="exec_approval",
            choice="once",
            resolver={"session_key": "sess-1"},
        )
        attacker = _make_inbound(conv_id="conv-1")
        attacker["from"] = {"id": "user-2", "name": "Other User"}

        rejected = await a._handle_card_action(attacker, value)
        assert rejected.status_code == 403
        assert "test-capability" in a._card_capabilities
        resolver.assert_not_called()

        accepted = await a._handle_card_action(owner, value)
        assert accepted.status_code == 200
        resolver.assert_called_once_with("sess-1", "once")
        replayed = await a._handle_card_action(owner, value)
        assert replayed.status_code == 403
        resolver.assert_called_once()

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
        value = _seed_card_capability(
            a,
            activity,
            kind="slash_confirm",
            choice="once",
            resolver={"session_key": "sess-1", "confirm_id": "cfm-1"},
        )
        resp = await a._handle_card_action(activity, value)
        assert resp.status_code == 200
        # The resolver's follow-up text is posted as a reply.
        assert send_reply.await_args.kwargs["reply"]["text"] == "Command ran."

    @pytest.mark.asyncio
    async def test_inflight_card_action_cannot_post_after_chat_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_mod, "_COALESCED_REPLY_SHUTDOWN_TIMEOUT_SEC", 0.01
        )
        a = _make_adapter(monkeypatch)
        self._connect(a)
        bridge = adapter_mod._import_bridge()
        send_reply = AsyncMock(return_value=None)
        monkeypatch.setattr(bridge, "send_reply", send_reply)
        started = asyncio.Event()
        release = asyncio.Event()

        async def resistant_resolver(*_args: Any) -> str:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return "Must not be delivered"

        monkeypatch.setattr(
            a,
            "_gw_resolve_slash_confirm",
            AsyncMock(side_effect=resistant_resolver),
        )
        activity = _make_inbound(conv_id="conv-1")
        value = _seed_card_capability(
            a,
            activity,
            kind="slash_confirm",
            choice="once",
            resolver={"session_key": "sess-1", "confirm_id": "cfm-1"},
        )
        action = asyncio.create_task(a._handle_card_action(activity, value))
        await started.wait()

        await a._teardown_chat_state("conv-1")
        assert action in a._coalesced_reply_survivors
        release.set()
        response = await asyncio.wait_for(action, timeout=0.5)
        await asyncio.sleep(0)

        assert response.status_code == 200
        assert send_reply.await_count == 0
        assert a._coalesced_reply_survivors == {}

    @pytest.mark.asyncio
    async def test_handle_card_action_clarify_numeric(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        resolver = MagicMock(return_value=True)
        monkeypatch.setattr(a, "_gw_resolve_clarify", resolver)
        activity = _make_inbound(conv_id="conv-1")
        value = _seed_card_capability(
            a,
            activity,
            kind="clarify",
            choice="Beta",
            resolver={"clarify_id": "clr-1"},
        )
        resp = await a._handle_card_action(activity, value)
        assert resp.status_code == 200
        resolver.assert_called_once_with("clr-1", "Beta")

    @pytest.mark.asyncio
    async def test_handle_card_action_clarify_other_arms_intercept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_adapter(monkeypatch)
        mark = MagicMock()
        monkeypatch.setattr(a, "_gw_mark_awaiting_text", mark)
        activity = _make_inbound(conv_id="conv-1")
        value = _seed_card_capability(
            a,
            activity,
            kind="clarify",
            choice="__other__",
            resolver={"clarify_id": "clr-1"},
        )
        resp = await a._handle_card_action(activity, value)
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
        body["value"] = _seed_card_capability(
            a,
            body,
            kind="exec_approval",
            choice="deny",
            resolver={"session_key": "sess-1"},
        )
        client = TestClient(a.build_app())
        r = client.post(
            "/api/messages", json=body, headers={"Authorization": "Bearer a.b.c"}
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

    def test_append_handoff_link_never_exceeds_message_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_HANDOFF_LINK", "1")
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-cc-full")
        inbound["conversation"]["conversationType"] = "groupChat"
        a._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
        content = "x" * a.MAX_MESSAGE_LENGTH
        assert a._maybe_append_handoff_link("conv-cc-full", content) == content


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


# ---------------------------------------------------------------------------
# #19 — secrets-at-rest provider wiring on the gateway (plugin) runtime
# ---------------------------------------------------------------------------


class TestGatewaySecretsProviderWiring:
    """The adapter must CONSULT the provider for both identities.

    Drives the real adapter construction / `_ensure_secret` rather than the
    provider in isolation — a passing unit test proves nothing if the
    runtime seam never calls it.
    """

    _TENANT = "11111111-1111-1111-1111-111111111111"
    _BLUEPRINT_APP = "22222222-2222-2222-2222-222222222222"
    _BF_APP = "44444444-4444-4444-4444-444444444444"

    @pytest.fixture(autouse=True)
    def _restore_provider(self):
        from hermes_a365.secrets_provider import default_provider, set_default_provider

        original = default_provider()
        yield
        set_default_provider(original)

    @staticmethod
    def _provider(items: dict[tuple[str, str], str]):
        from hermes_a365.secrets_provider import set_default_provider

        class Fake:
            name = "fake-store"

            def __init__(self) -> None:
                self.asked: list[tuple[str, str]] = []

            def resolve(self, tenant: str, app_id: str) -> str | None:
                self.asked.append((tenant, app_id))
                return items.get((tenant, app_id))

        fake = Fake()
        set_default_provider(fake)
        return fake

    def test_bf_secret_filled_from_provider_when_env_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #19 walk §5b, Path B on the gateway: no A365_BF_CLIENT_SECRET in
        # env/config — the provider supplies it.
        monkeypatch.delenv("A365_BF_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("A365_BF_APP_ID", self._BF_APP)
        fake = self._provider(
            {(self._TENANT, self._BF_APP): "bf-from-keychain"}
        )

        a = _make_adapter(monkeypatch)

        # Construction runs on the gateway loop and must not perform sync I/O.
        assert fake.asked == []
        assert a._ensure_bf_secret() == "bf-from-keychain"
        assert fake.asked == [(self._TENANT, self._BF_APP)]

    def test_env_bf_secret_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A365_BF_APP_ID", self._BF_APP)
        monkeypatch.setenv("A365_BF_CLIENT_SECRET", "bf-from-env")
        fake = self._provider(
            {(self._TENANT, self._BF_APP): "bf-from-keychain"}
        )

        a = _make_adapter(monkeypatch)

        assert a._ensure_bf_secret() == "bf-from-env"
        assert fake.asked == []

    def test_blueprint_secret_filled_from_provider_on_ensure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _ensure_secret consults the provider only after env AND the
        # generated config have both missed.
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        self._provider({(self._TENANT, self._BLUEPRINT_APP): "bp-from-keychain"})
        a = _make_adapter(monkeypatch)
        a.blueprint_client_secret = ""  # env miss
        monkeypatch.setattr(a, "_load_secret_from_generated_config", lambda: "")

        assert a._ensure_secret() == "bp-from-keychain"

    def test_generated_config_blueprint_secret_still_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rotation safety on the gateway path.
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        fake = self._provider({(self._TENANT, self._BLUEPRINT_APP): "STALE"})
        a = _make_adapter(monkeypatch)
        a.blueprint_client_secret = ""
        monkeypatch.setattr(a, "_load_secret_from_generated_config", lambda: "FRESH")

        assert a._ensure_secret() == "FRESH"
        assert (self._TENANT, self._BLUEPRINT_APP) not in fake.asked

    def test_path_a_only_adapter_unaffected_by_empty_bf_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # bf_app_id="" — unusable provider key must degrade to a miss.
        monkeypatch.delenv("A365_BF_APP_ID", raising=False)
        monkeypatch.delenv("A365_BF_CLIENT_SECRET", raising=False)
        self._provider({})

        a = _make_adapter(monkeypatch)

        assert a.bf_app_id == ""
        assert a._ensure_bf_secret() == ""
        assert a.blueprint_client_secret == "fake-secret"

    def test_half_configured_path_b_fails_before_runtime_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A365_BF_APP_ID", self._BF_APP)
        monkeypatch.delenv("A365_BF_CLIENT_SECRET", raising=False)
        self._provider({})
        adapter = _make_adapter(monkeypatch)

        with pytest.raises(RuntimeError, match="A365_BF_CLIENT_SECRET"):
            adapter._make_bridge_config()

    def test_reconnect_clears_provider_hits_for_rotation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("A365_BF_APP_ID", self._BF_APP)
        monkeypatch.delenv("A365_BF_CLIENT_SECRET", raising=False)
        values = {
            (self._TENANT, self._BLUEPRINT_APP): "blueprint-v1",
            (self._TENANT, self._BF_APP): "bf-v1",
        }
        self._provider(values)
        adapter = _make_adapter(monkeypatch)
        adapter.blueprint_client_secret = ""
        monkeypatch.setattr(adapter, "_load_secret_from_generated_config", lambda: "")

        assert adapter._ensure_secret() == "blueprint-v1"
        assert adapter._ensure_bf_secret() == "bf-v1"
        values[(self._TENANT, self._BLUEPRINT_APP)] = "blueprint-v2"
        values[(self._TENANT, self._BF_APP)] = "bf-v2"

        adapter._clear_provider_secret_cache()

        assert adapter._ensure_secret() == "blueprint-v2"
        assert adapter._ensure_bf_secret() == "bf-v2"

    def test_connect_offloads_secret_resolution_from_the_gateway_loop(self) -> None:
        import inspect

        source = inspect.getsource(adapter_mod.Agent365Adapter.connect)
        source += inspect.getsource(adapter_mod.Agent365Adapter._connect)
        assert "await asyncio.to_thread(self._make_bridge_config)" in source
        assert "self._clear_provider_secret_cache()" in source

    def test_provider_secret_never_shadows_a_later_rotation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-1 finding: _ensure_secret used to write the provider result
        # back into `blueprint_client_secret`, so the NEXT call short-circuited
        # on the cached keychain value and a secret rotated into the generated
        # config afterwards could never win — falsifying the rotation-safety
        # property the precedence exists to provide.
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        self._provider({(self._TENANT, self._BLUEPRINT_APP): "from-keychain"})
        a = _make_adapter(monkeypatch)
        a.blueprint_client_secret = ""
        rotated: dict[str, str] = {"value": ""}
        monkeypatch.setattr(
            a, "_load_secret_from_generated_config", lambda: rotated["value"]
        )

        # First call: generated config empty -> provider fills the miss.
        assert a._ensure_secret() == "from-keychain"

        # `register --apply` now rotates a fresh secret into the config.
        rotated["value"] = "FRESH-ROTATED"

        # It must win on the next call, not lose to the cached provider value.
        assert a._ensure_secret() == "FRESH-ROTATED"

    def test_generated_config_rotation_is_seen_after_first_file_hit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)
        self._provider({})
        adapter = _make_adapter(monkeypatch)
        adapter.blueprint_client_secret = ""
        generated = {"value": "file-v1"}
        monkeypatch.setattr(
            adapter,
            "_load_secret_from_generated_config",
            lambda: generated["value"],
        )

        assert adapter._ensure_secret() == "file-v1"
        generated["value"] = "file-v2"

        assert adapter._ensure_secret() == "file-v2"

    def test_transient_provider_failure_is_not_pinned_for_the_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-3 finding: `resolve_secret` degrades a transient provider
        # failure (keychain locked, backend down, lookup timed out) to the
        # same "" a genuine miss returns. Caching that "" pinned the failure
        # for this object's life, so a second `connect()` on the same adapter
        # — an embedder retrying, rather than the gateway, which builds a
        # fresh adapter per attempt — could never recover.
        monkeypatch.delenv("A365_BLUEPRINT_CLIENT_SECRET", raising=False)

        calls: list[int] = []
        available = {"value": False}

        class Flaky:
            name = "flaky-store"

            def resolve(self, tenant: str, app_id: str) -> str | None:
                calls.append(1)
                if not available["value"]:
                    raise RuntimeError("keychain locked")
                return "recovered-secret"

        from hermes_a365.secrets_provider import set_default_provider

        set_default_provider(Flaky())
        a = _make_adapter(monkeypatch)
        a.blueprint_client_secret = ""
        monkeypatch.setattr(a, "_load_secret_from_generated_config", lambda: "")

        # First connect: the provider is down, so the read misses.
        assert a._ensure_secret() == ""
        assert len(calls) == 1

        # Operator unlocks the keychain and the gateway reconnects onto this
        # same adapter. The provider must be consulted again.
        available["value"] = True
        assert a._ensure_secret() == "recovered-secret"
        assert len(calls) == 2

        # A hit IS cached — no second subprocess per connect after success.
        assert a._ensure_secret() == "recovered-secret"
        assert len(calls) == 2

    def test_transient_bf_provider_failure_is_not_pinned_for_the_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Path B analog of the Path A lock above. `_ensure_bf_secret` caches a
        # HIT only — the CHANGELOG claims "Both Path A and Path B cache
        # provider hits only; a miss is re-consulted on the next connect", and
        # without this test a regression that pinned a Path B miss (e.g. a
        # None-init + `is not None` guard) would pass the whole suite green.
        monkeypatch.delenv("A365_BF_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("A365_BF_APP_ID", self._BF_APP)

        calls: list[int] = []
        available = {"value": False}

        class Flaky:
            name = "flaky-store"

            def resolve(self, tenant: str, app_id: str) -> str | None:
                calls.append(1)
                if not available["value"]:
                    raise RuntimeError("keychain locked")
                return "bf-recovered"

        from hermes_a365.secrets_provider import set_default_provider

        set_default_provider(Flaky())
        a = _make_adapter(monkeypatch)
        a.bf_client_secret = ""  # env miss

        # First connect: provider down -> miss, not pinned.
        assert a._ensure_bf_secret() == ""
        assert len(calls) == 1

        # Reconnect onto the same adapter after the keychain unlocks.
        available["value"] = True
        assert a._ensure_bf_secret() == "bf-recovered"
        assert len(calls) == 2

        # A hit IS cached — no re-ask after success.
        assert a._ensure_bf_secret() == "bf-recovered"
        assert len(calls) == 2

    # -- round-2 regressions: the setup wizard's Microsoft#408 branch --------

    def test_wizard_sees_a_secret_held_at_rest(self) -> None:
        # Round-2 finding: `agentBlueprintClientSecret: null` in the generated
        # config is the EXPECTED state once an operator moves the secret to
        # rest. The wizard read that null as Microsoft#408 and told them to run
        # `register --apply --auto-recover-secret`, which mints a NEW secret
        # and orphans the stored one — the documented flow undone by the tool
        # that documents it.
        self._provider({(self._TENANT, self._BLUEPRINT_APP): "held-at-rest"})

        found, reachable, label = adapter_mod._probe_secret_at_rest(
            self._TENANT, self._BLUEPRINT_APP
        )

        assert (found, reachable, label) == (True, True, "fake-store")

    def test_wizard_still_blames_408_when_nothing_holds_the_secret(self) -> None:
        # The fix must not suppress the #408 advice on a genuinely empty
        # store — that diagnosis is right when no tier has the credential.
        self._provider({})

        found, reachable, label = adapter_mod._probe_secret_at_rest(
            self._TENANT, self._BLUEPRINT_APP
        )

        # Empty but ANSWERING: #408 is the right call here.
        assert (found, reachable, label) == (False, True, "fake-store")

    def test_wizard_at_rest_check_never_returns_the_secret(self) -> None:
        # #5d: the wizard prints the provider LABEL on this branch. Keeping
        # the value out of the return type makes that structural rather than
        # a convention the next edit can quietly break.
        self._provider({(self._TENANT, self._BLUEPRINT_APP): "sup3r-s3cret"})

        result = adapter_mod._probe_secret_at_rest(
            self._TENANT, self._BLUEPRINT_APP
        )

        assert result == (True, True, "fake-store")
        assert "sup3r-s3cret" not in repr(result)

    def test_wizard_actually_calls_the_at_rest_check(self) -> None:
        # Round-3 mutation test: deleting the call site from
        # `interactive_setup` — restoring the unconditional #408 warning —
        # left the whole suite green, because the tests above exercise the
        # helper in isolation. The wizard body can't be driven under bare
        # pytest (it lazy-imports `hermes_cli`, which isn't installed), so
        # pin the wiring at source level instead.
        #
        # Round 5: assert on text unique to EACH of the three branches, not
        # just the helper name. An earlier version asserted a substring the
        # miss branch also contained, so deleting the success branch kept it
        # green.
        import inspect

        source = inspect.getsource(adapter_mod.interactive_setup)
        assert "_probe_secret_at_rest" in source
        # Each guard is a string that appears ONLY in its branch's print body,
        # never in a comment — deleting any one branch fails this test. (An
        # earlier version keyed the empty branch off "--auto-recover-secret",
        # which also appears in a comment, so that branch was not pinned.)
        assert "likely nothing to bootstrap" in source  # found
        assert "could not be consulted" in source  # unreachable
        assert "Manual secret replacement is disabled" in source
        assert "has nothing stored for this tenant/app" in source  # empty
        assert "--output-fd 3 3>/dev/null" in source

    def test_wizard_does_not_blame_408_when_the_provider_is_unreachable(
        self,
    ) -> None:
        # Round-5 finding: every provider failure collapsed to "nothing
        # stored", so a locked keychain sent the operator to
        # `register --apply --auto-recover-secret` — minting a secret that
        # then outranks the one they had stored all along. That is the exact
        # harm this branch was added to prevent, reintroduced by the fix.
        class Exploding:
            name = "exploding"

            def resolve(self, tenant: str, app_id: str) -> str | None:
                raise RuntimeError("keychain locked")

        found, reachable, label = adapter_mod._probe_secret_at_rest(
            self._TENANT, self._BLUEPRINT_APP, provider=Exploding()
        )

        # Not found, but NOT evidence of an empty store.
        assert (found, reachable, label) == (False, False, "exploding")


class TestSecurityAuthorizationOrder:
    def test_unauthorized_user_is_rejected_before_media_or_state_side_effects(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(
            monkeypatch,
            conversations_path=str(tmp_path / "conversations.json"),
        )
        a._allow_all_users = False
        a._allowed_users = ("user-allowed",)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        extract_media = AsyncMock()
        monkeypatch.setattr(a, "_extract_inbound_media", extract_media)

        response = TestClient(a.build_app()).post(
            "/api/messages",
            json=_make_inbound(conv_id="conv-unauthorized"),
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert response.status_code == 403
        extract_media.assert_not_awaited()
        assert a._handled_events == []
        assert not (tmp_path / "conversations.json").exists()

    @pytest.mark.parametrize(
        ("activity_type", "channel_id", "sender_id"),
        [
            ("installationUpdate", "msteams", "system"),
            ("conversationUpdate", "msteams", "system"),
            ("typing", "msteams", "system"),
            ("message", "agents", "no-reply@teams.mail.microsoft"),
        ],
    )
    def test_platform_control_traffic_bypasses_only_end_user_authorization(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        activity_type: str,
        channel_id: str,
        sender_id: str,
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(
            monkeypatch,
            conversations_path=str(tmp_path / "conversations.json"),
        )
        a._allow_all_users = False
        a._allowed_users = ("user-allowed",)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        activity = _make_inbound()
        activity["type"] = activity_type
        activity["channelId"] = channel_id
        activity["from"] = {"id": sender_id}
        if activity_type == "installationUpdate":
            activity["action"] = "add"

        response = TestClient(a.build_app()).post(
            "/api/messages",
            json=activity,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "acked"
        assert a._handled_events == []

    @pytest.mark.parametrize(
        ("authorized_id", "expected_seen"),
        [
            (
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                [
                    ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "group", "group-conv"),
                    ("opaque-channel-id", "group", "group-conv"),
                ],
            ),
            (
                "opaque-channel-id",
                [
                    ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "group", "group-conv"),
                    ("opaque-channel-id", "group", "group-conv"),
                ],
            ),
        ],
    )
    def test_gateway_auth_and_event_share_normalized_identity_and_chat_type(
        self,
        monkeypatch: pytest.MonkeyPatch,
        authorized_id: str,
        expected_seen: list[tuple[str, str, str]],
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        seen: list[tuple[str, str, str]] = []

        def authorize(user_id: str, chat_type: str, chat_id: str) -> bool:
            seen.append((user_id, chat_type, chat_id))
            return user_id == authorized_id

        a._is_sender_authorized = authorize
        activity = _make_inbound(conv_id="group-conv")
        activity["conversation"]["conversationType"] = "groupChat"
        activity["from"] = {
            "aadObjectId": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            "id": "opaque-channel-id",
            "name": "Sadiq",
        }

        response = TestClient(a.build_app()).post(
            "/api/messages",
            json=activity,
            headers={"Authorization": "Bearer a.b.c"},
        )

        assert response.status_code == 200
        assert seen == expected_seen
        event = a._handled_events[0]
        assert event.source.user_id == authorized_id
        assert event.source.chat_type == "group"
