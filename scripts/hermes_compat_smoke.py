#!/usr/bin/env python3
"""Verify pinned Hermes git refs against the v1 plugin/multiplex floors (#126).

This is source-level integration evidence (not a live gateway walk). It clones
each ref shallowly, imports ``gateway.platforms.base``, and checks adapter
contract files exist.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_a365.compatibility import (  # noqa: E402
    HERMES_INTEGRATION_SMOKE_REFS,
    HERMES_MULTIPLEX_MIN_CALENDAR,
    HERMES_PLUGIN_MIN_CALENDAR,
    format_calendar_tag,
)


def _calendar_from_ref(ref: str) -> tuple[int, int, int] | None:
    text = ref.lstrip("v")
    parts = text.split(".")
    if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) >= 2020:
        return int(parts[0]), int(parts[1]), int(parts[2])
    return None


def _clone(ref: str, dest: Path) -> None:
    if dest.exists():
        subprocess.run(["rm", "-rf", str(dest)], check=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            ref,
            "https://github.com/NousResearch/hermes-agent.git",
            str(dest),
        ],
        check=True,
    )


def _check_ref(ref: str, work: Path) -> list[str]:
    errors: list[str] = []
    checkout = work / ref.replace("/", "_")
    _clone(ref, checkout)
    base_py = checkout / "gateway" / "platforms" / "base.py"
    if not base_py.is_file():
        errors.append(f"{ref}: missing gateway/platforms/base.py")
    else:
        text = base_py.read_text(encoding="utf-8", errors="replace")
        if "BasePlatformAdapter" not in text:
            errors.append(f"{ref}: BasePlatformAdapter not found in base.py")
        import_check = subprocess.run(
            [
                sys.executable,
                "-c",
                "from gateway.platforms.base import BasePlatformAdapter",
            ],
            cwd=checkout,
            env={**os.environ, "PYTHONPATH": str(checkout)},
            capture_output=True,
            text=True,
        )
        if import_check.returncode != 0:
            detail = (import_check.stderr or import_check.stdout or "").strip()
            errors.append(f"{ref}: gateway.platforms.base import failed ({detail})")

    calendar = _calendar_from_ref(ref)
    if calendar is not None and calendar < HERMES_PLUGIN_MIN_CALENDAR:
        errors.append(
            f"{ref}: calendar tag {format_calendar_tag(calendar)} is below plugin floor "
            f"{format_calendar_tag(HERMES_PLUGIN_MIN_CALENDAR)}"
        )

    multiplex_tests = list(checkout.glob("tests/gateway/test_multiplex*.py"))
    if (
        calendar is not None
        and calendar >= HERMES_MULTIPLEX_MIN_CALENDAR
        and not multiplex_tests
    ):
        errors.append(f"{ref}: expected multiplex gateway tests at multiplex floor")
    if ref.startswith("v0.") and not multiplex_tests:
        errors.append(f"{ref}: expected multiplex gateway tests on semver line")

    plugin_entry = checkout / "hermes_cli" / "plugins.py"
    if not plugin_entry.is_file():
        errors.append(f"{ref}: missing hermes_cli/plugins.py (entry-point loader)")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref",
        action="append",
        dest="refs",
        help="Hermes git tag (default: compatibility.HERMES_INTEGRATION_SMOKE_REFS)",
    )
    args = parser.parse_args(argv)
    refs = tuple(args.refs or HERMES_INTEGRATION_SMOKE_REFS)
    work = Path(tempfile.mkdtemp(prefix="hermes-compat-"))
    failures: list[str] = []
    for ref in refs:
        failures.extend(_check_ref(ref, work))
    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        return 1
    print(f"hermes compat smoke ok: {', '.join(refs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
