"""Public-surface freeze gate (#193 slice 1)."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_a365 import surface as surface_mod
from hermes_a365.instance_create import _MANAGED_AGENT_ENV_KEYS
from hermes_a365.register import AITEAMMATE_REGISTER_UNSUPPORTED, RegisterInputs
from hermes_a365.surface_collect import (
    collect_adapter_public_members,
    collect_cli_trees,
    collect_gateway_extra_keys_from_adapter,
    collect_invoke_adapter_handler_names,
    collect_keychain_cli_surface,
    collect_plugin_requires_env,
    collect_product_http_routes,
    collect_python_module_surface,
    collect_sidecar_inventory_from_code,
    collect_task_fetch_card_from_invoke,
    scan_env_literals,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = REPO_ROOT / "docs" / "surface" / "public-surface.json"


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_snapshot_matches_renderer() -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "render_surface.py"), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_owner_decisions_accepted(snapshot: dict) -> None:
    od = snapshot["owner_decisions"]
    assert od["status"] == surface_mod.OWNER_DECISIONS_STATUS == "accepted"
    assert od["accepted"] == surface_mod.ACCEPTED_OWNER_DECISIONS


def test_cli_standalone_matches_plugin() -> None:
    trees = collect_cli_trees()
    assert trees["standalone"] == trees["plugin"]


def test_cli_parser_parity(snapshot: dict) -> None:
    live = collect_cli_trees()
    assert live["standalone"] == snapshot["cli"]["standalone"]
    assert live["plugin"] == snapshot["cli"]["plugin"]


def test_env_literal_classification_closure(snapshot: dict) -> None:
    scanned = scan_env_literals()
    assert scanned == set(snapshot["env_literals_scanned"])
    classified = set(surface_mod.ENV_CLASSIFICATION)
    assert scanned == classified
    for name, kind in surface_mod.ENV_CLASSIFICATION.items():
        assert kind in ("public", "internal")
        assert snapshot["env_classification"][name] == kind


def test_stale_env_classification_name_would_break_closure() -> None:
    scanned = scan_env_literals()
    with_stale = {**surface_mod.ENV_CLASSIFICATION, "HERMES_STALE_SURFACE_STUB": "internal"}
    assert set(with_stale) != scanned


def test_instance_env_keys_match_instance_create() -> None:
    assert tuple(sorted(surface_mod.INSTANCE_ENV_PUBLIC_KEYS)) == tuple(
        sorted(_MANAGED_AGENT_ENV_KEYS)
    )
    assert "HERMES_BRIDGE_PORT" not in surface_mod.INSTANCE_ENV_PUBLIC_KEYS


def test_adapter_public_members(snapshot: dict) -> None:
    live = collect_adapter_public_members()
    frozen = set(snapshot["adapter_public_members"])
    assert live == frozen
    assert frozen == surface_mod.ADAPTER_PUBLIC_MEMBERS


def test_product_http_routes(snapshot: dict) -> None:
    live = collect_product_http_routes()
    frozen = snapshot["product_http_routes_frozen"]
    assert live == frozen
    assert {tuple(r.items()) for r in live} == {
        tuple(r.items()) for r in snapshot["http_routes_live"]
    }


def test_register_aiteammate_refusal_text(snapshot: dict) -> None:
    assert snapshot["register_aiteammate_refusal"] == AITEAMMATE_REGISTER_UNSUPPORTED
    with pytest.raises(ValueError, match="register --aiteammate"):
        RegisterInputs(agent_name="demo", aiteammate=True)


def test_publish_default_scopes_match_d2() -> None:
    from hermes_a365.publish import _COPILOT_CHAT_DEFAULT_SCOPES

    assert tuple(_COPILOT_CHAT_DEFAULT_SCOPES) == surface_mod.D2_CEA_SCOPES


def test_invoke_registry_and_task_fetch_card(snapshot: dict) -> None:
    from hermes_a365 import invoke as invoke_mod

    assert sorted(invoke_mod.INVOKE_REGISTRY) == snapshot["invoke"]["registry"]
    live_card = asyncio.run(collect_task_fetch_card_from_invoke())
    assert live_card == dict(surface_mod.TASK_FETCH_CARD)
    assert live_card == snapshot["invoke"]["task_fetch_card"]
    assert surface_mod.D3_TASK_FETCH_CARD_FROZEN is True


def test_invoke_adapter_handlers_match_code(snapshot: dict) -> None:
    live = collect_invoke_adapter_handler_names()
    assert live == surface_mod.INVOKE_ADAPTER_PUBLIC
    assert list(live) == snapshot["invoke"]["adapter_handlers"]


def test_gateway_extra_keys_match_adapter() -> None:
    assert (
        collect_gateway_extra_keys_from_adapter(surface_mod.GATEWAY_EXTRA_KEYS)
        == surface_mod.GATEWAY_EXTRA_KEYS
    )


def test_plugin_requires_env_matches_yaml() -> None:
    assert collect_plugin_requires_env() == surface_mod.PLUGIN_REQUIRES_ENV


def test_keychain_cli_matches_parser(snapshot: dict) -> None:
    live = collect_keychain_cli_surface()
    assert live["subcommands"] == list(surface_mod.KEYCHAIN_CLI_SUBCOMMANDS)
    assert live["options"] == list(surface_mod.KEYCHAIN_CLI_OPTIONS)
    assert live == snapshot["keychain_cli"]


def test_python_modules_internal_from_main_guards(snapshot: dict) -> None:
    live = collect_python_module_surface()
    assert live == snapshot["python_modules"]
    assert "hermes_a365.keychain" in live["public_cli"]
    assert "hermes_a365.cli" in live["internal"]


def test_sidecar_inventory_matches_code(snapshot: dict) -> None:
    live = tuple(collect_sidecar_inventory_from_code())
    assert live == tuple(surface_mod.HERMES_OWNED_SIDECARS)


def test_bot_service_verify_exit_codes_and_run_verify(tmp_path: Path) -> None:
    from hermes_a365.bot_service import _run_verify

    assert surface_mod.EXIT_CODES["bot_service_verify"] == (0, 1, 2)
    args = Namespace(
        sidecar=tmp_path / "missing-sidecar.json",
        directline_probe=False,
        generated_config=None,
    )
    assert _run_verify(args) == 2


def test_security_md_documents_d4_window() -> None:
    text = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert surface_mod.SECURITY_SUPPORT_AFTER_V1 in text
    assert "reviewed at v1.0" not in text
