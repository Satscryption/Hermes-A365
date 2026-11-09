"""Public-surface freeze gate (#193 slice 1)."""

from __future__ import annotations

import ast
import asyncio
import json
import re
import subprocess
import textwrap
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_a365 import surface as surface_mod
from hermes_a365.instance_create import _DEPRECATED_OTLP_ENV_KEY, _MANAGED_AGENT_ENV_KEYS
from hermes_a365.plugin.conversations import ConversationRegistry
from hermes_a365.register import REGISTER_AITEAMMATE_REMOVED
from test_register import EXPECTED_REGISTER_AITEAMMATE_REMOVED
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


def _action_has_aiteammate(action: dict) -> bool:
    return "--aiteammate" in action.get("option_strings", [])


def test_register_aiteammate_refusal_text(snapshot: dict) -> None:
    assert REGISTER_AITEAMMATE_REMOVED == EXPECTED_REGISTER_AITEAMMATE_REMOVED
    assert snapshot["register_aiteammate_refusal"] == EXPECTED_REGISTER_AITEAMMATE_REMOVED
    assert snapshot["exit_codes"]["register_aiteammate"] == 2
    for tree_key in ("plugin", "standalone"):
        register_actions = snapshot["cli"][tree_key]["subparsers"]["register"]["actions"]
        publish_actions = snapshot["cli"][tree_key]["subparsers"]["publish"]["actions"]
        assert not any(_action_has_aiteammate(a) for a in register_actions)
        assert any(_action_has_aiteammate(a) for a in publish_actions)


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


_PAIRING = re.compile(
    r"(?i)register.{0,80}--aiteammate|--aiteammate.{0,80}register"
)


def _tracked_text_files() -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths: list[Path] = []
    for line in proc.stdout.splitlines():
        path = REPO_ROOT / line
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        paths.append(path)
    return paths


def _source_spans(text: str) -> list[tuple[str, int, int]]:
    physical = text.splitlines()
    spans: list[tuple[str, int, int]] = []
    i = 0
    while i < len(physical):
        start = i + 1
        parts = [physical[i].rstrip("\\").rstrip()]
        while physical[i].rstrip().endswith("\\") and i + 1 < len(physical):
            i += 1
            parts.append(physical[i].rstrip("\\").rstrip())
        end = i + 1
        spans.append((" ".join(parts), start, end))
        i += 1
    return spans


def _stray_aiteammate_lines(text: str, const_range: tuple[int, int]) -> list[int]:
    start_const, end_const = const_range
    stray: list[int] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if "--aiteammate" not in line:
            continue
        if start_const <= line_no <= end_const:
            continue
        if (
            '== "--aiteammate"' in line
            or 'startswith("--aiteammate=")' in line
        ) and "register" not in line.lower():
            continue
        stray.append(line_no)
    return stray


def _register_constant_line_range(register_py: Path) -> tuple[int, int]:
    tree = ast.parse(register_py.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "REGISTER_AITEAMMATE_REMOVED":
                    assert hasattr(node, "lineno") and hasattr(node, "end_lineno")
                    return (node.lineno, node.end_lineno or node.lineno)
    raise AssertionError("REGISTER_AITEAMMATE_REMOVED assignment missing")


def test_no_current_register_aiteammate_reference() -> None:
    register_py = REPO_ROOT / "src" / "hermes_a365" / "register.py"
    const_range = _register_constant_line_range(register_py)
    allowlist_ranges = {register_py: {const_range}}
    json_path = REPO_ROOT / "docs" / "surface" / "public-surface.json"
    json_payload = json.loads(json_path.read_text(encoding="utf-8"))
    refusal = json_payload["register_aiteammate_refusal"]

    for path in _tracked_text_files():
        if path == REPO_ROOT / "CHANGELOG.md":
            continue
        if path in (
            REPO_ROOT / "tests" / "test_register.py",
            REPO_ROOT / "tests" / "test_public_surface.py",
        ):
            continue
        text = path.read_text(encoding="utf-8")
        if path == json_path:
            assert refusal.count("register") and refusal.count("--aiteammate")
        for span_text, span_start, span_end in _source_spans(text):
            if not _PAIRING.search(span_text):
                continue
            if path == json_path and "register_aiteammate_refusal" in span_text:
                continue
            allowed = allowlist_ranges.get(path, set())
            if any(
                start <= span_start and span_end <= end for start, end in allowed
            ):
                continue
            raise AssertionError(
                f"pairing hit at {path.relative_to(REPO_ROOT)}:"
                f"{span_start}-{span_end}: {span_text!r}"
            )

    text = register_py.read_text(encoding="utf-8")
    predicate_lines = [
        line
        for line in text.splitlines()
        if "--aiteammate" in line
        and "register" not in line.lower()
        and (
            '== "--aiteammate"' in line
            or 'startswith("--aiteammate=")' in line
        )
    ]
    assert len(predicate_lines) <= 2
    assert 'add_argument("--aiteammate"' not in text
    assert _stray_aiteammate_lines(text, const_range) == []


def test_changelog_records_register_aiteammate_removal() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased_start = changelog.index("## [Unreleased]")
    next_section = changelog.index("## [0.9.6]", unreleased_start)
    unreleased = changelog[unreleased_start:next_section]
    assert "### Removed" in unreleased
    removed_start = unreleased.index("### Removed")
    removed_body = unreleased[removed_start:]
    next_heading = removed_body.find("\n### ", len("### Removed"))
    removed_section = removed_body if next_heading < 0 else removed_body[:next_heading]
    bullets = [line for line in removed_section.splitlines() if line.strip().startswith("- ")]
    assert len(bullets) == 1
    bullet = bullets[0]
    for needle in (
        "#85",
        "`register --aiteammate`",
        "exits 2",
        "--help",
        "does not build or apply a plan",
        "`publish --aiteammate` is unchanged",
    ):
        assert needle in bullet
    tail_after_removed = unreleased[removed_start + len(removed_section) :]
    assert not any(
        line.startswith("#") for line in tail_after_removed.splitlines() if line.strip()
    )
    start_073 = changelog.index("## [0.7.3]")
    rest_073 = changelog[start_073:]
    end_073 = rest_073.find("\n## [", len("## [0.7.3]"))
    section_073 = rest_073 if end_073 < 0 else rest_073[:end_073]
    assert "unsupported/deprecated" in section_073
    assert "`hermes a365 register --aiteammate`" in section_073
