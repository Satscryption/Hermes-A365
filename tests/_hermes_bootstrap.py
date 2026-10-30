"""Bootstrap a pinned Hermes Agent checkout for gateway integration tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

_VENDOR_ROOT = Path(__file__).resolve().parent.parent / ".vendor" / "hermes-agent"
_HERMES_PIN = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
_bootstrapped = False


def hermes_vendor_root() -> Path:
    return _VENDOR_ROOT


def expected_hermes_revision() -> str:
    return _HERMES_PIN


def ensure_hermes_vendor() -> Path:
    if not _VENDOR_ROOT.is_dir():
        raise RuntimeError(
            "Hermes vendor tree is missing. Run scripts/vendor-hermes-agent.sh "
            "from the repository root, then re-run the tests."
        )
    if not (_VENDOR_ROOT / ".git").exists():
        raise RuntimeError(
            "Hermes vendor checkout has no .git metadata; run scripts/vendor-hermes-agent.sh "
            "to refresh the checkout."
        )
    rev_proc = subprocess.run(
        ["git", "-C", str(_VENDOR_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    rev = rev_proc.stdout.strip()
    if rev_proc.returncode != 0 or rev != _HERMES_PIN:
        detail = rev or rev_proc.stderr.strip() or "unknown revision"
        raise RuntimeError(
            f"Hermes vendor revision mismatch: found {detail}, expected {_HERMES_PIN}. "
            "Run scripts/vendor-hermes-agent.sh to refresh the checkout."
        )
    vendor_str = str(_VENDOR_ROOT)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)
    return _VENDOR_ROOT


def try_import_real_gateway() -> bool:
    global _bootstrapped
    if _bootstrapped:
        return _VENDOR_ROOT.is_dir()
    _bootstrapped = True
    if __import__("os").environ.get("HERMES_A365_REQUIRE_VENDOR") != "1":
        try:
            ensure_hermes_vendor()
        except Exception:
            return False
    else:
        ensure_hermes_vendor()
    try:
        wire_real_hermes_modules()
    except Exception:
        return False
    return True


def wire_real_hermes_modules() -> None:
    import importlib

    ensure_hermes_vendor()
    importlib.import_module("tools.approval")
    try:
        importlib.import_module("gateway.platforms.base_exec_approval")
        importlib.import_module("gateway.run_turn_runner_approval_settle")
    except Exception:
        pass


def load_renders_exec_approval_buttons() -> Any:
    """Return ``gateway.run_turn_runner._renders_exec_approval_buttons`` without a full import."""
    import ast

    root = ensure_hermes_vendor()
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
    import gateway.platforms.base as base_mod

    namespace: dict[str, Any] = {
        "BasePlatformAdapter": base_mod.BasePlatformAdapter,
        "type": type,
    }
    exec(compile(func_src, str(runner_path), "exec"), namespace)
    probe = namespace.get("_renders_exec_approval_buttons")
    if not callable(probe):
        raise RuntimeError("hermes runner probe unavailable")
    return probe
