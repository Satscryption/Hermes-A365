"""Public-surface freeze gate (#193 slice 1)."""

from __future__ import annotations

import ast
import asyncio
import json
import textwrap
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_a365 import surface as surface_mod
from hermes_a365.instance_create import _DEPRECATED_OTLP_ENV_KEY, _MANAGED_AGENT_ENV_KEYS
from hermes_a365.plugin.conversations import ConversationRegistry
from hermes_a365.register import AITEAMMATE_REGISTER_UNSUPPORTED, RegisterInputs
from hermes_a365.surface_collect import (
    _module_has_main_guard,
    collect_adapter_public_members,
    collect_cli_trees,
    collect_gateway_extra_keys_from_adapter,
    collect_invoke_adapter_handler_names,
    collect_keychain_cli_surface,
    collect_main_guard_modules,
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
    assert "267" in od["accepted"]["D5"]


def test_cli_standalone_matches_plugin() -> None:
    trees = collect_cli_trees()
    assert trees["standalone"] == trees["plugin"]


def test_cli_parser_parity(snapshot: dict) -> None:
    live = collect_cli_trees()
    assert live["standalone"] == snapshot["cli"]["standalone"]
    assert live["plugin"] == snapshot["cli"]["plugin"]


def test_cli_subparsers_required_flags() -> None:
    tree = collect_cli_trees()["standalone"]
    assert tree["subparsers"]["activity-bridge"]["subparsers_required"] is True
    assert tree["subparsers"]["bot-service"]["subparsers_required"] is False


def test_env_literal_classification_closure(snapshot: dict) -> None:
    scanned = scan_env_literals()
    assert scanned == set(snapshot["env_literals_scanned"])
    classified = set(surface_mod.ENV_CLASSIFICATION)
    assert scanned == classified
    for name, kind in surface_mod.ENV_CLASSIFICATION.items():
        assert kind in ("public", "internal")
        assert snapshot["env_classification"][name] == kind
    assert surface_mod.ENV_CLASSIFICATION["HERMES_OTLP_ENDPOINT"] == "internal"


def test_stale_env_classification_name_would_break_closure() -> None:
    scanned = scan_env_literals()
    with_stale = {**surface_mod.ENV_CLASSIFICATION, "HERMES_STALE_SURFACE_STUB": "internal"}
    assert set(with_stale) != scanned


def test_main_guard_requires_ast_not_string_literal(tmp_path: Path) -> None:
    fake = tmp_path / "fake_main_string.py"
    fake.write_text(
        textwrap.dedent(
            '''
            """Doc."""
            NEEDLE = 'if __name__ == "__main__"'
            '''
        ),
        encoding="utf-8",
    )
    tree = ast.parse(fake.read_text(encoding="utf-8"))
    assert _module_has_main_guard(tree) is False
    assert "hermes_a365.surface_collect" not in collect_main_guard_modules()


def test_instance_env_keys_match_instance_create() -> None:
    expected = sorted(_MANAGED_AGENT_ENV_KEYS - {_DEPRECATED_OTLP_ENV_KEY})
    assert list(surface_mod.INSTANCE_ENV_PUBLIC_KEYS) == expected
    assert "HERMES_OTLP_ENDPOINT" not in surface_mod.INSTANCE_ENV_PUBLIC_KEYS


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
    assert set(live["subcommands"]) == set(surface_mod.KEYCHAIN_CLI_SUBCOMMANDS)
    assert live["subcommands"] == list(surface_mod.KEYCHAIN_CLI_SUBCOMMANDS)
    assert live["options"] == list(surface_mod.KEYCHAIN_CLI_OPTIONS)
    assert live == snapshot["keychain_cli"]


def test_keychain_collect_rejects_subcommand_set_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surface_mod, "KEYCHAIN_CLI_SUBCOMMANDS", ("store", "get"))
    with pytest.raises(ValueError, match="keychain subcommands"):
        collect_keychain_cli_surface()


def test_python_modules_internal_from_main_guards(snapshot: dict) -> None:
    live = collect_python_module_surface()
    assert live == snapshot["python_modules"]
    assert "hermes_a365.keychain" in live["public_cli"]
    assert "hermes_a365.cli" in live["internal"]
    assert "hermes_a365.surface_collect" not in live["internal"]


def test_sidecar_inventory_matches_code(snapshot: dict) -> None:
    live = tuple(collect_sidecar_inventory_from_code())
    assert live == tuple(surface_mod.HERMES_OWNED_SIDECARS)


def test_exit_codes_match_doctor_status_and_bridge_verify() -> None:
    from hermes_a365 import activity_bridge as activity_bridge_mod
    from hermes_a365 import doctor as doctor_mod
    from hermes_a365 import status as status_mod

    doctor_map = surface_mod.EXIT_CODES["doctor"]
    for state in ("ok", "warn", "error"):
        assert doctor_map[state] == doctor_mod.overall_to_exit_code(state)

    status_map = surface_mod.EXIT_CODES["status"]
    for state in ("ok", "partial", "broken", "uninitialized"):
        assert status_map[state] == status_mod.overall_to_exit_code(state)

    bridge_map = surface_mod.EXIT_CODES["activity_bridge_verify"]
    for state in ("ok", "warn", "error"):
        assert bridge_map[state] == activity_bridge_mod._overall_to_exit_code(state)


def test_bot_service_verify_exit_codes_and_run_verify(tmp_path: Path) -> None:
    from hermes_a365.bot_service import BotServiceVerifyReport, ProbeResult, _run_verify

    codes = surface_mod.EXIT_CODES["bot_service_verify"]
    assert codes == (0, 1, 2)
    ok_report = BotServiceVerifyReport(
        sidecar_path=tmp_path / "x.json",
        results=[ProbeResult("probe", "OK", "fine")],
    )
    bad_report = BotServiceVerifyReport(
        sidecar_path=tmp_path / "x.json",
        results=[ProbeResult("probe", "ERROR", "broken")],
    )
    assert codes[0] == (0 if ok_report.ok else 1)
    assert codes[1] == (0 if bad_report.ok else 1)
    args = Namespace(
        sidecar=tmp_path / "missing-sidecar.json",
        directline_probe=False,
        generated_config=None,
    )
    assert _run_verify(args) == codes[2]


def test_sidecar_migration_today_matches_conversations_payload(snapshot: dict) -> None:
    payload = ConversationRegistry().to_payload()
    assert "schema" in payload
    assert payload["schemaVersion"] == payload["schema"]
    assert snapshot["sidecar_migration_today"] == surface_mod.SIDECAR_MIGRATION_TODAY
    assert snapshot["sidecar_migration_policy"] == surface_mod.SIDECAR_MIGRATION_POLICY


def test_security_md_documents_d4_window() -> None:
    text = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert surface_mod.SECURITY_SUPPORT_AFTER_V1 in text
    assert "reviewed at v1.0" not in text
