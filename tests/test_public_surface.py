"""Public-surface freeze gate (#193 slice 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_a365 import surface as surface_mod
from hermes_a365.register import AITEAMMATE_REGISTER_UNSUPPORTED, RegisterInputs
from hermes_a365.surface_collect import (
    collect_adapter_public_members,
    collect_cli_trees,
    collect_product_http_routes,
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
    card = snapshot["invoke"]["task_fetch_card"]
    assert surface_mod.D3_TASK_FETCH_CARD_FROZEN is True
    assert card["body_text"] == "Request received."
    assert card["title"] == "Hermes"
    assert card["adaptive_card_version"] == "1.5"


def test_security_md_documents_d4_window() -> None:
    text = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert surface_mod.SECURITY_SUPPORT_AFTER_V1 in text
    assert "reviewed at v1.0" not in text
