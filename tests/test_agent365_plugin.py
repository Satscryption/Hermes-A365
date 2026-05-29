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
    async def test_refuses_non_personal_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BF streaming-ux: "Streaming bot message is available only
        # for one-on-one chats." Group/channel must hard-fail so
        # Hermes falls back to send().
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-G")
        inbound["conversation"]["conversationType"] = "groupChat"
        self._wire_adapter(a, inbound=inbound)
        self._patch_token_mint(monkeypatch)

        r = await a.edit_message("conv-G", "msg-1", "hi", finalize=False)
        assert r.success is False
        assert "personal chat" in (r.error or "").lower()
        # No POST should have been issued.
        assert a._http_client.post.await_count == 0

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
