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
_SKIP_ENV_SCAN = frozenset({"surface_collect.py", "surface.py"})

_ENV_LITERAL_RE = re.compile(r"^([A-Z][A-Z0-9_]+)$")


def scan_env_literals() -> set[str]:
    names: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        if path.name in _SKIP_ENV_SCAN:
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


def _serialize_action(action: Any) -> dict[str, Any] | None:
    import argparse

    if isinstance(action, argparse._SubParsersAction):
        return None
    if action.dest == "help":
        return None
    option_strings = list(action.option_strings)
    payload: dict[str, Any] = {
        "dest": action.dest,
        "option_strings": sorted(option_strings),
        "required": bool(action.required),
    }
    if action.nargs is not None:
        nargs = action.nargs
        payload["nargs"] = nargs if isinstance(nargs, int) else str(nargs)
    if action.choices is not None:
        payload["choices"] = sorted(str(c) for c in action.choices)
    return payload


def _serialize_parser(parser: Any) -> dict[str, Any]:
    import argparse

    actions = [item for a in parser._actions if (item := _serialize_action(a)) is not None]
    actions.sort(key=lambda x: (x["dest"], tuple(x["option_strings"])))
    node: dict[str, Any] = {"actions": actions, "subparsers": {}}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        node["subparsers_required"] = bool(action.required)
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


def _module_has_main_guard(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], ast.Eq):
            continue
        if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
            continue
        if len(test.comparators) != 1:
            continue
        comparator = test.comparators[0]
        if isinstance(comparator, ast.Constant) and comparator.value == "__main__":
            return True
    return False


def collect_main_guard_modules() -> list[str]:
    modules: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _module_has_main_guard(tree):
            continue
        rel = path.relative_to(_SRC_ROOT.parent)
        modules.append(str(rel.with_suffix("")).replace("/", "."))
    return modules


def collect_python_module_surface() -> dict[str, list[str]]:
    main_modules = collect_main_guard_modules()
    public_cli = ["hermes_a365.keychain"]
    reference_only = ["hermes_a365.hermes_responder"]
    internal = sorted(m for m in main_modules if m not in public_cli and m not in reference_only)
    return {
        "public_cli": public_cli,
        "reference_only": reference_only,
        "internal": internal,
    }


def collect_plugin_requires_env() -> tuple[str, ...]:
    import yaml

    plugin_yaml = _SRC_ROOT / "plugin" / "plugin.yaml"
    data = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))
    requires = data.get("requires_env") or []
    return tuple(str(x) for x in requires)


def collect_keychain_cli_surface() -> dict[str, Any]:
    from hermes_a365 import surface as surface_mod
    from hermes_a365.keychain import _build_parser

    parser = _build_parser()
    tree = _serialize_parser(parser)
    subcommand_set = set(tree["subparsers"])
    expected = set(surface_mod.KEYCHAIN_CLI_SUBCOMMANDS)
    if subcommand_set != expected:
        msg = f"keychain subcommands {sorted(subcommand_set)!r} != {sorted(expected)!r}"
        raise ValueError(msg)
    subcommands = [name for name in surface_mod.KEYCHAIN_CLI_SUBCOMMANDS if name in subcommand_set]
    options: set[str] = set()
    for sub_name in subcommands:
        for action in tree["subparsers"][sub_name]["actions"]:
            options.update(action["option_strings"])
    return {
        "module": "hermes_a365.keychain",
        "subcommands": subcommands,
        "options": sorted(options),
    }


def _adapter_extra_key_present(text: str, key: str) -> bool:
    return (
        f'extra.get("{key}"' in text
        or f'extra, "{key}"' in text
        or f'setdefault("{key}"' in text
        or f'extra["{key}"]' in text
    )


def collect_gateway_extra_keys_from_adapter(keys: tuple[str, ...]) -> tuple[str, ...]:
    text = (_SRC_ROOT / "plugin" / "adapter.py").read_text(encoding="utf-8")
    missing = [key for key in keys if not _adapter_extra_key_present(text, key)]
    if missing:
        msg = f"gateway extra keys not referenced in adapter.py: {missing}"
        raise ValueError(msg)
    return keys


def collect_sidecar_inventory_from_code() -> tuple[dict[str, Any], ...]:
    from hermes_a365 import bot_service as bs
    from hermes_a365 import publish as pub
    from hermes_a365.plugin import conversations as conv

    return (
        {
            "path": "a365.bot-service.config.json",
            "location": "cwd",
            "schema_version_write": bs.SIDECAR_SCHEMA_VERSION,
            "schema_version_read": tuple(sorted(bs._SUPPORTED_SIDECAR_SCHEMA_VERSIONS)),
        },
        {
            "path": "~/.hermes/agents/<slug>/copilot-chat-publish.json",
            "schema_version_write": pub._COPILOT_CHAT_PUBLISH_STATE_SCHEMA,
            "schema_version_read": (
                pub._COPILOT_CHAT_PUBLISH_STATE_SCHEMA_V1,
                pub._COPILOT_CHAT_PUBLISH_STATE_SCHEMA,
            ),
        },
        {
            "path": "~/.hermes/agents/<slug>/conversations.json",
            "schema_version_write": conv.ConversationRegistry.SCHEMA_VERSION,
            "schema_version_read": (conv.ConversationRegistry.SCHEMA_VERSION,),
            "missing_version_means": conv.ConversationRegistry.SCHEMA_VERSION,
            "on_disk_version_field": "schema",
        },
    )


async def collect_task_fetch_card_from_invoke() -> dict[str, Any]:
    from hermes_a365.invoke import InvokeContext, _handle_task_fetch

    ctx = InvokeContext(
        name="task/fetch",
        activity={},
        value={},
        user_oid="",
        user_upn="",
        tenant_id="",
        service_url="",
        conv={},
        chat_type="dm",
        path_tag="A",
    )
    resp = await _handle_task_fetch(ctx)
    assert resp.body is not None
    task_value = resp.body["task"]["value"]
    card = task_value["card"]["content"]
    return {
        "adaptive_card_version": card["version"],
        "title": task_value.get("title", ""),
        "body_text": card["body"][0]["text"],
    }


def collect_invoke_adapter_handler_names() -> tuple[str, ...]:
    from unittest.mock import MagicMock

    from hermes_a365 import invoke as invoke_mod

    install_gateway_stubs()
    adapter_mod = importlib.import_module("hermes_a365.plugin.adapter")
    adapter = adapter_mod.Agent365Adapter(MagicMock(extra={}))
    names = sorted(k for k in adapter._invoke_registry if k not in invoke_mod.INVOKE_REGISTRY)
    return tuple(names)
