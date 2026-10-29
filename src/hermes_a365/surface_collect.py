"""Live public-surface extraction used by the renderer and snapshot tests."""

from __future__ import annotations

import ast
import importlib
import re
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

_SRC_ROOT = Path(__file__).resolve().parent

_ENV_LITERAL_RE = re.compile(r"^([A-Z][A-Z0-9_]+)$")


def scan_env_literals() -> set[str]:
    names: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        if path.name == "surface_collect.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if value.startswith(("A365_", "HERMES_")) and _ENV_LITERAL_RE.match(value):
                names.add(value)
    return names


@dataclass
class _StubSendResult:
    success: bool
    message_id: Any = None
    error: str | None = None


class _StubMessageType(Enum):
    TEXT = "text"


@dataclass
class _StubMessageEvent:
    text: str


@dataclass
class _StubPlatformConfig:
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


class _StubPlatform:
    def __init__(self, value: str) -> None:
        self.value = value


class _StubBasePlatformAdapter:
    def __init__(self, config: Any, platform: Any) -> None:
        self.config = config
        self.platform = platform


@dataclass
class _StubSessionSource:
    platform: Any
    chat_id: str
    chat_type: str = "dm"


def install_gateway_stubs() -> None:
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

    def _session_key(source: Any, **_kwargs: Any) -> str:
        return f"agent:main:agent365:{source.chat_type}:{source.chat_id}"

    session.build_session_key = _session_key

    sys.modules["gateway"] = gateway
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.base"] = base
    sys.modules["gateway.config"] = config
    sys.modules["gateway.session"] = session


def _serialize_action(action: Any) -> dict[str, Any]:
    option_strings = list(action.option_strings)
    if not option_strings and action.dest not in ("help", "a365_command", "instance_command"):
        option_strings = [action.dest]
    payload: dict[str, Any] = {
        "dest": action.dest,
        "option_strings": sorted(option_strings),
        "required": bool(action.required),
    }
    if action.choices is not None:
        payload["choices"] = sorted(str(c) for c in action.choices)
    return payload


def _serialize_parser(parser: Any) -> dict[str, Any]:
    import argparse

    actions = [_serialize_action(a) for a in parser._actions if a.dest not in ("help",)]
    actions.sort(key=lambda x: (x["dest"], tuple(x["option_strings"])))
    node: dict[str, Any] = {"prog": parser.prog, "actions": actions, "subparsers": {}}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in sorted(action.choices.items()):
            node["subparsers"][name] = _serialize_parser(sub)
    return node


def collect_cli_trees() -> dict[str, Any]:
    import argparse

    from hermes_a365.cli import _build_parser
    from hermes_a365.plugin.cli import register_cli

    standalone = _serialize_parser(_build_parser())
    plugin_root = argparse.ArgumentParser(prog="hermes a365")
    register_cli(plugin_root)
    plugin = _serialize_parser(plugin_root)
    return {"standalone": standalone, "plugin": plugin}


def collect_adapter_public_members() -> set[str]:
    import inspect

    install_gateway_stubs()
    adapter_mod = importlib.import_module("hermes_a365.plugin.adapter")
    cls = adapter_mod.Agent365Adapter
    live: set[str] = set()
    for name, member in cls.__dict__.items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(member) or isinstance(member, property):
            live.add(name)
    return live


_FASTAPI_DOCUMENTATION_PATHS = frozenset(
    {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)


def _filter_product_routes(routes: list[dict[str, str]]) -> list[dict[str, str]]:
    filtered = [r for r in routes if r["path"] not in _FASTAPI_DOCUMENTATION_PATHS]
    filtered.sort(key=lambda r: (r["path"], r["method"]))
    return filtered


def collect_product_http_routes() -> list[dict[str, str]]:
    from unittest.mock import MagicMock

    from hermes_a365.activity_bridge import BridgeConfig, make_app

    install_gateway_stubs()
    adapter_mod = importlib.import_module("hermes_a365.plugin.adapter")

    cfg = BridgeConfig(
        slug="surface-test",
        tenant_id="contoso.onmicrosoft.com",
        blueprint_client_id="00000000-0000-0000-0000-000000000001",
        blueprint_client_secret="secret",
        webhook_url="https://127.0.0.1:9999/respond",
        log_path=Path("/tmp/hermes-surface-bridge.log"),
        pid_path=Path("/tmp/hermes-surface-bridge.pid"),
        allow_all_users=True,
    )
    serve_routes = sorted(
        (frozenset(r.methods - {"HEAD", "OPTIONS"}), r.path)
        for r in make_app(cfg).routes
        if hasattr(r, "methods")
    )
    adapter = adapter_mod.Agent365Adapter(MagicMock(extra={}))
    gateway_routes = sorted(
        (frozenset(r.methods - {"HEAD", "OPTIONS"}), r.path)
        for r in adapter.build_app().routes
        if hasattr(r, "methods")
    )
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for methods, path in serve_routes + gateway_routes:
        for method in sorted(methods):
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            out.append({"method": method, "path": path})
    out.sort(key=lambda r: (r["path"], r["method"]))
    return _filter_product_routes(out)
