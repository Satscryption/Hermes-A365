"""Bootstrap a shallow Hermes Agent checkout for gateway integration tests.

The plugin normally runs against stubs when Hermes is not installed locally.
Tests that exercise pending-message merge, approval wait/settle, and the turn
runner contract import the real ``gateway`` and ``tools`` packages from this
vendor tree when present.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

_VENDOR_ROOT = Path(__file__).resolve().parent.parent / ".vendor" / "hermes-agent"
_HERMES_GIT = "https://github.com/NousResearch/hermes-agent.git"
_HERMES_REF = "v0.22.5"
_bootstrapped = False


def hermes_vendor_root() -> Path:
    return _VENDOR_ROOT


def ensure_hermes_vendor() -> Path | None:
    global _bootstrapped
    if _bootstrapped:
        return _VENDOR_ROOT if _VENDOR_ROOT.is_dir() else None
    _bootstrapped = True
    if not _VENDOR_ROOT.is_dir():
        _VENDOR_ROOT.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                _HERMES_REF,
                _HERMES_GIT,
                str(_VENDOR_ROOT),
            ],
            check=True,
            capture_output=True,
        )
    vendor_str = str(_VENDOR_ROOT)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)
    _ensure_hermes_runtime_deps()
    return _VENDOR_ROOT


def _ensure_hermes_runtime_deps() -> None:
    import shutil
    import subprocess

    if shutil.which("pip") is None:
        return
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "ruamel.yaml>=0.18",
        ],
        check=False,
        capture_output=True,
    )


def try_import_real_gateway() -> bool:
    """Load selected Hermes modules from the vendor tree without replacing the stub adapter."""
    root = ensure_hermes_vendor()
    if root is None:
        return False
    try:
        wire_real_hermes_modules()
    except Exception:
        return False
    return True


def wire_real_hermes_modules() -> None:
    import importlib

    importlib.import_module("tools.approval")
    merge = _load_vendor_merge_pending()
    base = sys.modules.get("gateway.platforms.base")
    if base is not None and merge is not None:
        base.merge_pending_message_event = merge
    try:
        importlib.import_module("gateway.platforms.base_exec_approval")
        importlib.import_module("gateway.run_turn_runner_approval_settle")
    except Exception:
        pass


def load_renders_exec_approval_buttons() -> Any:
    """Return ``gateway.run_turn_runner._renders_exec_approval_buttons`` without a full import."""
    import ast

    root = ensure_hermes_vendor()
    if root is None:
        raise RuntimeError("hermes vendor missing")
    runner_path = root / "gateway" / "run_turn_runner.py"
    source = runner_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(runner_path))
    func_src = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_renders_exec_approval_buttons":
            func_src = ast.get_source_segment(source, node)
            break
    if not func_src:
        raise RuntimeError("hermes runner probe unavailable")
    base = sys.modules.get("gateway.platforms.base")
    if base is None:
        raise RuntimeError("gateway.platforms.base stub missing")
    namespace: dict[str, Any] = {
        "BasePlatformAdapter": base.BasePlatformAdapter,
        "type": type,
    }
    exec(compile(func_src, str(runner_path), "exec"), namespace)
    probe = namespace.get("_renders_exec_approval_buttons")
    if not callable(probe):
        raise RuntimeError("hermes runner probe unavailable")
    return probe


def _load_vendor_merge_pending() -> Any | None:
    import importlib.util

    base_path = _VENDOR_ROOT / "gateway" / "platforms" / "base.py"
    if not base_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("hermes_gateway_base_vendor", base_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    merge = getattr(module, "merge_pending_message_event", None)
    return merge if callable(merge) else None
