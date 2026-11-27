"""Offline tests for deploy/test-vm/scripts with stub az and terraform on PATH."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "test-vm" / "scripts"
APP = "11111111-2222-3333-4444-555555555555"
TENANT = "66666666-7777-8888-9999-000000000000"

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


TF_OUTPUTS = [
    ["output -raw resource_group_name", "example-walk-rg", 0],
    ["output -raw bot_resource_group_name", "example-walk-bot-rg", 0],
    ["output -raw messages_endpoint", "https://example.invalid/api/messages", 0],
]


def mutating(calls):
    verbs = {"create", "delete", "update", "put", "patch"}
    return [c for c in calls if c[0] == "az" and verbs & {a.lower() for a in c}]


def test_up_creates_bot_group_bot_and_teams_channel(tmp_path):
    rules = [
        *TF_OUTPUTS,
        ["group exists -n example-walk-bot-rg", "false", 0],
        ["group show -n example-walk-rg --query location", "centralindia", 0],
        [
            "group show -n example-walk-rg --query tags",
            '{"purpose": "example-walk", "owner": "example-team"}',
            0,
        ],
        ["bot show -g example-walk-bot-rg -n example-walk-bot --query properties.msaAppId", "", 3],
        ["account show", "00000000-0000-0000-0000-000000000000", 0],
        ["rest --method get", "", 3],
    ]
    proc, calls = run(
        tmp_path, "bot-channel.sh", ["up"], rules, {"BOT_APP_ID": APP, "BOT_TENANT_ID": TENANT}
    )
    assert proc.returncode == 0, proc.stderr
    group_creates = [c for c in calls if c[1:3] == ["group", "create"]]
    assert len(group_creates) == 1
    assert "example-walk-bot-rg" in group_creates[0]
    assert "example-walk-rg" not in group_creates[0]
    assert {"purpose=example-walk", "owner=example-team"} <= set(group_creates[0])
    bot_create = next(c for c in calls if c[1:3] == ["bot", "create"])
    assert {"SingleTenant", "F0", APP, TENANT, "https://example.invalid/api/messages"} <= set(
        bot_create
    )
    put = next(c for c in calls if c[1:4] == ["rest", "--method", "put"])
    assert "/channels/MsTeamsChannel?api-version=2022-09-15" in " ".join(put)
    body = json.loads(put[put.index("--body") + 1])
    assert body["properties"]["properties"]["deploymentEnvironment"] == "CommercialDeployment"
    assert not any(c[1:3] == ["bot", "msteams"] for c in calls)
    assert APP not in proc.stdout and TENANT not in proc.stdout


def test_up_is_repeatable_and_never_recreates_existing_group(tmp_path):
    rules = [
        *TF_OUTPUTS,
        ["group exists -n example-walk-bot-rg", "true", 0],
        ["bot show -g example-walk-bot-rg -n example-walk-bot --query properties.msaAppId", APP, 0],
        ["account show", "00000000-0000-0000-0000-000000000000", 0],
        ["rest --method get", "true", 0],
    ]
    proc, calls = run(
        tmp_path, "bot-channel.sh", ["up"], rules, {"BOT_APP_ID": APP, "BOT_TENANT_ID": TENANT}
    )
    assert proc.returncode == 0, proc.stderr
    assert not any(c[1:3] == ["group", "create"] for c in calls)
    assert not any(c[1:3] == ["bot", "create"] for c in calls)
    assert not any(c[1:4] == ["rest", "--method", "put"] for c in calls)


def test_up_refuses_other_app_id(tmp_path):
    rules = [
        *TF_OUTPUTS,
        ["group exists -n example-walk-bot-rg", "true", 0],
        [
            "bot show -g example-walk-bot-rg -n example-walk-bot --query properties.msaAppId",
            TENANT,
            0,
        ],
    ]
    proc, calls = run(
        tmp_path, "bot-channel.sh", ["up"], rules, {"BOT_APP_ID": APP, "BOT_TENANT_ID": TENANT}
    )
    assert proc.returncode != 0
    assert mutating(calls) == []


@pytest.mark.parametrize("env", [{}, {"BOT_APP_ID": "not-a-guid", "BOT_TENANT_ID": TENANT}])
def test_up_requires_ids_before_any_change(tmp_path, env):
    proc, calls = run(tmp_path, "bot-channel.sh", ["up"], TF_OUTPUTS, env)
    assert proc.returncode != 0
    assert mutating(calls) == []


def test_down_deletes_only_a_bot_only_group(tmp_path):
    rules = [
        *TF_OUTPUTS,
        ["resource list -g example-walk-bot-rg", "0", 0],
        ["group exists -n example-walk-bot-rg", "true", 0],
    ]
    # First exists check says present; after delete the stub still says true, so
    # the readback must fail loudly.
    proc, calls = run(tmp_path, "bot-channel.sh", ["down"], rules)
    deletes = [c for c in calls if c[1:3] == ["group", "delete"]]
    assert len(deletes) == 1 and "example-walk-bot-rg" in deletes[0]
    assert "example-walk-rg" not in deletes[0]
    assert proc.returncode != 0


def test_down_refuses_group_with_other_resources(tmp_path):
    rules = [
        *TF_OUTPUTS,
        ["group exists -n example-walk-bot-rg", "true", 0],
        ["resource list -g example-walk-bot-rg", "2", 0],
    ]
    proc, calls = run(tmp_path, "bot-channel.sh", ["down"], rules)
    assert proc.returncode != 0
    assert mutating(calls) == []


def test_status_is_read_only(tmp_path):
    rules = [*TF_OUTPUTS, ["group exists", "true", 0], ["bot show", "{}", 0]]
    proc, calls = run(tmp_path, "bot-channel.sh", ["status"], rules)
    assert proc.returncode == 0, proc.stderr
    assert mutating(calls) == []


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
