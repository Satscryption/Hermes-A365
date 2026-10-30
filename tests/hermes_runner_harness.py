"""Load Agent365 against the vendored Hermes ``BasePlatformAdapter`` (no stub base)."""

from __future__ import annotations

import enum
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

_BOOTSTRAP_PATH = Path(__file__).resolve().parent / "_hermes_bootstrap.py"
_spec = importlib.util.spec_from_file_location("hermes_a365_hermes_bootstrap", _BOOTSTRAP_PATH)
assert _spec and _spec.loader
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
ensure_hermes_vendor = _bootstrap.ensure_hermes_vendor
expected_hermes_revision = _bootstrap.expected_hermes_revision


def _extend_platform_enum() -> None:

    import gateway.config as cfg

    platform = cfg.Platform
    if not isinstance(platform, enum.EnumMeta):
        return
    if any(member.value == "agent365" for member in platform):
        return
    members = {member.name: member.value for member in platform}
    members["AGENT365"] = "agent365"
    cfg.Platform = enum.Enum("Platform", members)  # type: ignore[misc]


def _install_vendor_gateway_modules() -> None:

    import gateway.config as vendor_cfg
    import gateway.platforms.base as vendor_base
    import gateway.session as vendor_session

    sys.modules["gateway.config"] = vendor_cfg
    sys.modules["gateway.platforms.base"] = vendor_base
    sys.modules["gateway.session"] = vendor_session


def load_agent365_adapter_module() -> Any:
    ensure_hermes_vendor()
    _install_vendor_gateway_modules()
    _extend_platform_enum()
    adapter_name = "hermes_a365.plugin.adapter"
    if adapter_name in sys.modules:
        del sys.modules[adapter_name]
    return importlib.import_module(adapter_name)


def install_bridge_stubs(monkeypatch: Any, adapter_mod: Any) -> None:
    import hermes_a365.activity_bridge as bridge_mod

    monkeypatch.setattr(adapter_mod, "_import_bridge", lambda: bridge_mod)
    monkeypatch.setattr(
        bridge_mod,
        "validate_inbound_jwt",
        AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
    )
    monkeypatch.setattr(bridge_mod, "send_reply", AsyncMock(return_value=None))
    monkeypatch.setattr(bridge_mod, "shutdown_jwks_cache_refresh", AsyncMock(return_value=None))
