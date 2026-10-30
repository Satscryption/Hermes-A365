#!/usr/bin/env python3
"""Fetch the pinned Hermes Agent revision used by gateway integration tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PIN = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
REPO = "https://github.com/NousResearch/hermes-agent.git"


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    vendor = root / ".vendor" / "hermes-agent"
    vendor.parent.mkdir(parents=True, exist_ok=True)

    if (vendor / ".git").is_dir():
        _run(
            ["git", "-C", str(vendor), "fetch", "--depth", "1", "origin", PIN],
            check=False,
        )
        _run(["git", "-C", str(vendor), "checkout", "--detach", PIN])
    else:
        _run(["git", "clone", "--depth", "1", REPO, str(vendor)])
        _run(["git", "-C", str(vendor), "checkout", "--detach", PIN])

    rev = _run(["git", "-C", str(vendor), "rev-parse", "HEAD"]).stdout.strip()
    if rev != PIN:
        print(
            f"Hermes vendor revision mismatch: found {rev}, expected {PIN}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
