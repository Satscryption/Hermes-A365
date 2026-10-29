"""Acceptance tests for #193 slice 2 — sidecar schemaVersion migration."""

from __future__ import annotations

import json
import multiprocessing
import stat
from pathlib import Path

import pytest
import test_bot_service as bot_tests
import test_publish as publish_tests

from hermes_a365._common import SidecarLockError, exclusive_sidecar_lock
from hermes_a365.bot_service import (
    SIDECAR_SCHEMA_VERSION,
    BotServiceConfig,
    BotServiceError,
    apply_update_endpoint_plan,
    build_update_endpoint_plan,
)
from hermes_a365.plugin.conversations import (
    ConversationRegistry,
    ConversationSidecarError,
)
from hermes_a365.publish import (
    PublishError,
    PublishInputs,
    _copilot_chat_publish_state_path,
    _write_copilot_chat_publish_state,
    build_publish_plan,
)

BotServiceUpdateEndpointInputs = bot_tests.BotServiceUpdateEndpointInputs
FakeRunner = bot_tests.FakeRunner
_write_sidecar = bot_tests._write_sidecar
apply_publish_plan = publish_tests.apply_publish_plan


def _hold_sidecar_lock_worker(
    path_str: str,
    ready: multiprocessing.Queue,
    release: multiprocessing.Queue,
) -> None:
    sidecar = Path(path_str)
    with exclusive_sidecar_lock(sidecar):
        ready.put("held")
        release.get()


def _legacy_conversations_bytes() -> bytes:
    payload = {
        "schema": 1,
        "conversations": [
            {
                "conversation_id": "conv-legacy",
                "service_url": "https://smba.trafficmanager.net/teams/",
                "raw": {},
            }
        ],
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def test_conversations_legacy_file_migrates_once_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "conversations.json"
    original = _legacy_conversations_bytes()
    path.write_bytes(original)
    reg = ConversationRegistry.load(path)
    assert reg.get("conv-legacy") is not None

    payload = reg.to_payload()
    ConversationRegistry.write_payload(path, payload)
    updated_bytes = path.read_bytes()
    assert b'"schemaVersion": 1' in updated_bytes
    backups = sorted(tmp_path.glob("conversations.backup-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert (backups[0].stat().st_mode & 0o777) == 0o600

    ConversationRegistry.write_payload(path, payload)
    assert sorted(tmp_path.glob("conversations.backup-*.json")) == backups
    assert path.read_bytes() == updated_bytes


def test_conversations_future_schema_version_refused_on_load_and_save(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversations.json"
    original = (
        json.dumps(
            {
                "schema": 1,
                "schemaVersion": ConversationRegistry.SCHEMA_VERSION + 1,
                "conversations": [{"conversation_id": "keep-me", "service_url": "https://x/"}],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    path.write_bytes(original)

    with pytest.raises(ConversationSidecarError, match="newer than this build"):
        ConversationRegistry.load(path)

    reg = ConversationRegistry()
    with pytest.raises(ConversationSidecarError, match="refusing to rewrite"):
        ConversationRegistry.write_payload(path, reg.to_payload())
    assert path.read_bytes() == original


def test_conversations_backup_restore_parses_as_pre_migration(tmp_path: Path) -> None:
    path = tmp_path / "conversations.json"
    original = _legacy_conversations_bytes()
    path.write_bytes(original)
    reg = ConversationRegistry.load(path)
    ConversationRegistry.write_payload(path, reg.to_payload())
    backup = next(tmp_path.glob("conversations.backup-*.json"))
    backup.replace(path)
    restored = ConversationRegistry.load(path)
    assert "schemaVersion" not in json.loads(path.read_text())
    assert restored.get("conv-legacy") is not None


def test_conversations_save_refuses_when_lock_held_by_other_process(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversations.json"
    path.write_bytes(_legacy_conversations_bytes())
    ctx = multiprocessing.get_context("spawn")
    ready: multiprocessing.Queue = ctx.Queue()
    release: multiprocessing.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_hold_sidecar_lock_worker,
        args=(str(path), ready, release),
    )
    proc.start()
    assert ready.get(timeout=5) == "held"
    try:
        with pytest.raises(SidecarLockError):
            ConversationRegistry.write_payload(
                path,
                ConversationRegistry.load(path).to_payload(),
            )
        assert path.read_bytes() == _legacy_conversations_bytes()
    finally:
        release.put("go")
        proc.join(timeout=10)
        assert proc.exitcode == 0


def test_bot_service_legacy_binding_still_requires_confirmation(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = 1
    raw.pop("agentName", None)
    sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://new.example/api/messages",
            sidecar_path=sidecar,
        )
    )
    with pytest.raises(BotServiceError, match="--confirm-legacy-binding"):
        apply_update_endpoint_plan(plan, runner=runner)
    assert json.loads(sidecar.read_text())["schemaVersion"] == 1
    assert not list(tmp_path.glob("a365.bot-service.config.backup-*.json"))


def test_publish_v1_still_requires_app_version(tmp_path: Path) -> None:
    manifest_id = "44444444-4444-4444-4444-444444444444"
    state_path = _copilot_chat_publish_state_path(tmp_path, "x")
    state_path.parent.mkdir(parents=True)
    original = (
        json.dumps(
            {"schemaVersion": 1, "manifestId": manifest_id, "lastAppVersion": "1.1.4"},
            indent=2,
        )
        + "\n"
    )
    state_path.write_text(original, encoding="utf-8")
    suite = publish_tests.TestApplyPublishPlanCopilotChat()
    zp = suite._seed_manifest_zip(
        tmp_path,
        {
            "id": manifest_id,
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        },
    )
    plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
    with pytest.raises(PublishError, match="legacy schema v1"):
        apply_publish_plan(plan, mutator=suite._scripted_zip_emit(zp), hermes_home=tmp_path)
    assert state_path.read_text(encoding="utf-8") == original


def test_publish_v1_to_v2_backup_first(tmp_path: Path) -> None:
    manifest_id = "44444444-4444-4444-4444-444444444444"
    state_path = _copilot_chat_publish_state_path(tmp_path, "agent")
    state_path.parent.mkdir(parents=True)
    original = (
        json.dumps(
            {
                "schemaVersion": 1,
                "manifestId": manifest_id,
                "lastAppVersion": "1.1.4",
            },
            indent=2,
        )
        + "\n"
    ).encode()
    state_path.write_bytes(original)
    _write_copilot_chat_publish_state(
        state_path,
        manifest_id=manifest_id,
        app_version="1.1.5",
        hermes_home=tmp_path,
        tenant_id="22222222-2222-2222-2222-222222222222",
        profile="default",
        cea_catalog_id_minted=False,
    )
    backups = sorted(state_path.parent.glob("copilot-chat-publish.backup-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert (backups[0].stat().st_mode & 0o777) == 0o600
    updated = json.loads(state_path.read_text())
    assert updated["schemaVersion"] == 2
    assert updated["tenantId"]

    before = state_path.read_bytes()
    _write_copilot_chat_publish_state(
        state_path,
        manifest_id=manifest_id,
        app_version="1.1.6",
        hermes_home=tmp_path,
        tenant_id="22222222-2222-2222-2222-222222222222",
        profile="default",
        cea_catalog_id_minted=False,
    )
    assert sorted(state_path.parent.glob("copilot-chat-publish.backup-*.json")) == backups
    assert json.loads(state_path.read_text())["lastAppVersion"] == "1.1.6"
    assert state_path.read_bytes() != before


def test_publish_future_schema_version_untouched(tmp_path: Path) -> None:
    state_path = _copilot_chat_publish_state_path(tmp_path, "agent")
    state_path.parent.mkdir(parents=True)
    original = (
        json.dumps({"schemaVersion": 99, "manifestId": "x", "lastAppVersion": "1.0.0"}) + "\n"
    ).encode()
    state_path.write_bytes(original)
    with pytest.raises(PublishError, match="unsupported"):
        _write_copilot_chat_publish_state(
            state_path,
            manifest_id="44444444-4444-4444-4444-444444444444",
            app_version="1.1.5",
            hermes_home=tmp_path,
            tenant_id="22222222-2222-2222-2222-222222222222",
            profile="default",
            cea_catalog_id_minted=False,
        )
    assert state_path.read_bytes() == original


def test_bot_service_future_schema_version_untouched(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = SIDECAR_SCHEMA_VERSION + 1
    sidecar.write_text(json.dumps(raw))
    with pytest.raises(BotServiceError, match="schemaVersion"):
        BotServiceConfig.from_file(sidecar)


def test_bot_service_v1_migrate_once_leaves_backup(tmp_path: Path) -> None:
    sidecar = _write_sidecar(tmp_path)
    snapshot = sidecar.read_bytes()
    raw = json.loads(sidecar.read_text())
    raw["schemaVersion"] = 1
    raw.pop("agentName", None)
    sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    snapshot = sidecar.read_bytes()
    runner = FakeRunner(bot=FakeRunner._bot(), teams=FakeRunner._teams())
    plan = build_update_endpoint_plan(
        BotServiceUpdateEndpointInputs(
            agent_name="Hermes Inbox Helper",
            url="https://new-tunnel.example/api/messages",
            sidecar_path=sidecar,
            legacy_binding_confirmation="Hermes Inbox Helper",
        )
    )
    apply_update_endpoint_plan(plan, runner=runner)
    backups = sorted(tmp_path.glob("a365.bot-service.config.backup-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == snapshot
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    updated = BotServiceConfig.from_file(sidecar)
    assert updated.schemaVersion == SIDECAR_SCHEMA_VERSION
