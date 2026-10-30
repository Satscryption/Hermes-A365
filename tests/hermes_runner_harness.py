"""Load Agent365 against the vendored Hermes ``BasePlatformAdapter`` (no stub base)."""

from __future__ import annotations

import enum
import importlib
import importlib.util
import inspect
import subprocess
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
    for name in (
        "gateway.platforms.base",
        "gateway.config",
        "gateway.session",
        "gateway.platforms",
        "gateway",
    ):
        sys.modules.pop(name, None)

    import gateway.config as vendor_cfg
    import gateway.platforms.base as vendor_base
    import gateway.session as vendor_session

    sys.modules["gateway.config"] = vendor_cfg
    sys.modules["gateway.platforms.base"] = vendor_base
    sys.modules["gateway.session"] = vendor_session


def load_agent365_adapter_module(*, in_subprocess: bool = False) -> Any:
    if in_subprocess:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--load-check"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "harness child failed")
        return None
    ensure_hermes_vendor()
    _install_vendor_gateway_modules()
    _extend_platform_enum()
    adapter_name = "hermes_a365.plugin.adapter"
    if adapter_name in sys.modules:
        del sys.modules[adapter_name]
    return importlib.import_module(adapter_name)


def _child_load_check() -> int:
    ensure_hermes_vendor()
    import gateway.platforms.base as vendor_base

    vendor_root = ensure_hermes_vendor().resolve()
    base_file = Path(inspect.getfile(vendor_base.BasePlatformAdapter)).resolve()
    merge_file = Path(inspect.getfile(vendor_base.merge_pending_message_event)).resolve()
    if not str(base_file).startswith(str(vendor_root)):
        print(f"BasePlatformAdapter not under vendor tree: {base_file}", file=sys.stderr)
        return 1
    if not str(merge_file).startswith(str(vendor_root)):
        print(f"merge_pending_message_event not under vendor tree: {merge_file}", file=sys.stderr)
        return 1
    if hasattr(vendor_base, "_StubBasePlatformAdapter"):
        print("vendor base defines _StubBasePlatformAdapter", file=sys.stderr)
        return 1
    load_agent365_adapter_module()
    return 0


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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--load-check":
        raise SystemExit(_child_load_check())
