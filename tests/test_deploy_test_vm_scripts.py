"""Offline tests for deploy/test-vm/scripts with stub az and terraform on PATH."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "test-vm" / "scripts"

STUB = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0])] + args) + "\n")
rules = json.loads(os.environ.get("STUB_RULES", "[]"))
line = " ".join([os.path.basename(sys.argv[0])] + args)
for needle, out, rc in rules:
    if needle in line:
        sys.stdout.write(out + ("\n" if out else ""))
        sys.exit(rc)
sys.exit(0)
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def run(tmp_path, script, args, rules, env=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("az", "terraform"):
        stub = bin_dir / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    log.write_text("")
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "STUB_RULES": json.dumps(rules),
        **(env or {}),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    return proc, calls


def mutating(calls):
    verbs = {"create", "delete", "update", "put", "patch"}
    return [c for c in calls if c[0] == "az" and verbs & {a.lower() for a in c}]


CLEAN = [
    ["group exists", "false", 0],
    ["resource list --query", "0", 0],
    ["resource list --resource-type Microsoft.Compute/disks", "0", 0],
]


def test_leftovers_clean(tmp_path):
    proc, calls = run(
        tmp_path, "check-leftovers.sh", ["example-walk", "example-walk-vm_disk1_0123abcd"], CLEAN
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert mutating(calls) == []
    disk_q = next(c for c in calls if "Microsoft.Compute/disks" in c)
    q = disk_q[disk_q.index("--query") + 1]
    assert "starts_with(name, 'example-walk-vm_')" in q
    assert "name == 'example-walk-vm_disk1_0123abcd'" in q
    groups = [c[c.index("-n") + 1] for c in calls if c[1:3] == ["group", "exists"]]
    assert groups == ["example-walk-rg", "example-walk-bot-rg"]


def test_leftover_disk_fails(tmp_path):
    rules = [["resource list --resource-type Microsoft.Compute/disks", "1", 0], *CLEAN]
    proc, _ = run(tmp_path, "check-leftovers.sh", ["example-walk"], rules)
    assert proc.returncode == 1
    assert "LEFTOVER OS disks: 1" in proc.stdout


def test_leftover_group_and_tag(tmp_path):
    rules = [
        ["group exists -n example-walk-bot-rg", "true", 0],
        ["resource list --tag walk=1", "0", 0],
        ["group list --tag walk=1", "1", 0],
        *CLEAN,
    ]
    proc, _ = run(
        tmp_path, "check-leftovers.sh", ["example-walk"], rules, {"LEFTOVER_TAG": "walk=1"}
    )
    assert proc.returncode == 1
    assert "LEFTOVER resource group example-walk-bot-rg" in proc.stdout
    assert "LEFTOVER resource groups tagged walk=1: 1" in proc.stdout


@pytest.mark.parametrize("args", [["AB"], ["example-walk", "bad'name"], []])
def test_leftovers_rejects_bad_input(tmp_path, args):
    proc, calls = run(tmp_path, "check-leftovers.sh", args, CLEAN)
    assert proc.returncode == 2
    assert calls == []


def test_no_fixed_os_disk_word():
    text = (SCRIPTS / "check-leftovers.sh").read_text()
    assert "OsDisk" not in text and "osdisk" not in text.lower().replace("os_disk", "")
