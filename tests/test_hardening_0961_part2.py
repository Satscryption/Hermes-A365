"""0.9.6.1 hardening part 2 — sidecar UTF-8, create guard, backup errors, parse hygiene."""

from __future__ import annotations

import gc
import json
import os
import traceback
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from test_bot_service import SUBSCRIPTION_ID, TENANT_ID, FakeRunner

from hermes_a365._common import SidecarBackupError, write_owner_only_bytes_exclusive
from hermes_a365.bot_service import (
    BotServiceConfig,
    BotServiceCreateInputs,
    BotServiceError,
    _read_bound_sidecar,
    apply_create_plan,
    build_create_plan,
)
from hermes_a365.bot_service_diagnostics import probe_bot_service_config
from hermes_a365.plugin.conversations import ConversationRegistry
from hermes_a365.sidecar_schema import SidecarSchemaError, validate_bot_service_sidecar_schema


def _sidecar_v1_bytes(*, agent_name: str | None = None) -> bytes:
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "subscriptionId": SUBSCRIPTION_ID,
        "resourceGroup": "rg",
        "botName": "bot",
        "armResourceId": (
            "/subscriptions/x/resourceGroups/rg/providers/Microsoft.BotService/botServices/bot"
        ),
        "msaAppId": "00000000-0000-0000-0000-000000000001",
        "tenantId": TENANT_ID,
        "messagingEndpoint": "https://example.test/api/messages",
        "channelsEnabled": ["msteams"],
        "createdAt": "2020-01-01T00:00:00Z",
        "resourceGroupManaged": False,
    }
    if agent_name is not None:
        payload["agentName"] = agent_name
    return json.dumps(payload).encode()


def _sidecar_v2_bytes(agent_name: str = "agent") -> bytes:
    payload = json.loads(_sidecar_v1_bytes().decode())
    payload["schemaVersion"] = 2
    payload["agentName"] = agent_name
    return json.dumps(payload).encode()


def _iter_exception_chain(exc: BaseException | None) -> Iterable[BaseException]:
    seen: set[int] = set()
    pending: list[BaseException] = []
    if exc is not None:
        pending.append(exc)
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def _scan_value_for_marker(value: Any, marker: str, *, _seen: set[int]) -> None:
    if value is None:
        return
    oid = id(value)
    if oid in _seen:
        return
    if isinstance(value, (str, bytes, bytearray)):
        text = value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else value
        assert marker not in text, repr(value)[:200]
        return
    _seen.add(oid)
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_value_for_marker(key, marker, _seen=_seen)
            _scan_value_for_marker(item, marker, _seen=_seen)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _scan_value_for_marker(item, marker, _seen=_seen)
        return


def _assert_failure_artifacts_clean(exc: BaseException, marker: str) -> None:
    gc.collect()
    assert exc.__cause__ is None
    assert exc.__context__ is None
    for link in _iter_exception_chain(exc):
        assert marker not in str(link)
        assert marker not in repr(link)
        assert all(marker not in str(arg) for arg in link.args)
        for key, val in vars(link).items():
            _scan_value_for_marker(key, marker, _seen=set())
            _scan_value_for_marker(val, marker, _seen=set())
    for link in _iter_exception_chain(exc):
        tb = link.__traceback__
        while tb is not None:
            filename = tb.tb_frame.f_code.co_filename
            in_pkg = f"{os.sep}hermes_a365{os.sep}" in filename
            in_tests = f"{os.sep}tests{os.sep}" in filename
            if in_pkg and not in_tests:
                for _name, value in tb.tb_frame.f_locals.items():
                    _scan_value_for_marker(value, marker, _seen=set())
            tb = tb.tb_next
    assert marker not in "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _assert_marker_absent_from_bot_service_failure(exc: BaseException, marker: str) -> None:
    _assert_failure_artifacts_clean(exc, marker)


class TestC1ConversationsStrictUtf8:
    def test_utf16_registry_roundtrip_via_save(self, tmp_path: Path) -> None:
        path = tmp_path / "conversations.json"
        original = '{"schemaVersion": 1, "conversations": {}}'.encode("utf-16-le")
        path.write_bytes(original)
        reg = ConversationRegistry.load(path)
        ConversationRegistry.write_payload(path, reg.to_payload())
        quarantine = next(p for p in tmp_path.iterdir() if ".corrupt-" in p.name)
        assert quarantine.read_bytes() == original
        rewritten = path.read_bytes()
        assert rewritten.decode("utf-8").startswith("{")
        assert json.loads(rewritten.decode("utf-8"))["schemaVersion"] == 1

    def test_utf32_bom_registry_roundtrip_via_save(self, tmp_path: Path) -> None:
        path = tmp_path / "conversations.json"
        body = '{"schemaVersion": 1, "conversations": {}}'.encode("utf-32")
        path.write_bytes(body)
        reg = ConversationRegistry.load(path)
        ConversationRegistry.write_payload(path, reg.to_payload())
        assert path.read_bytes().decode("utf-8").startswith("{")


class TestC2CreateRefusesExistingSidecar:
    def test_valid_v1_sidecar_bytes_unchanged_no_azure(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "a365.bot-service.config.json"
        original = _sidecar_v1_bytes()
        sidecar.write_bytes(original)
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError, match="already exists"):
            apply_create_plan(plan, runner=runner)
        assert sidecar.read_bytes() == original
        assert runner.calls == []

    def test_valid_v2_same_agent_bytes_unchanged_no_azure(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "a365.bot-service.config.json"
        original = _sidecar_v2_bytes("agent")
        sidecar.write_bytes(original)
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError, match="already exists"):
            apply_create_plan(plan, runner=runner, operator_env=env)
        assert sidecar.read_bytes() == original
        assert runner.calls == []


class TestC3AgentNameTypeValidation:
    def test_dict_agent_name_error_names_type_not_value(self) -> None:
        sentinel = "SENTINEL-NOT-A-SECRET-C3"
        payload = {
            "schemaVersion": 2,
            "agentName": {sentinel: 1},
            "subscriptionId": SUBSCRIPTION_ID,
            "resourceGroup": "rg",
            "botName": "bot",
            "armResourceId": (
                "/subscriptions/x/resourceGroups/rg/providers/Microsoft.BotService/botServices/bot"
            ),
            "msaAppId": "00000000-0000-0000-0000-000000000001",
            "tenantId": TENANT_ID,
            "messagingEndpoint": "https://example.test/api/messages",
            "channelsEnabled": ["msteams"],
            "createdAt": "2020-01-01T00:00:00Z",
            "resourceGroupManaged": False,
        }
        with pytest.raises(BotServiceError) as exc:
            BotServiceConfig.from_json_text(json.dumps(payload), source="cfg")
        _assert_failure_artifacts_clean(exc.value, sentinel)
        assert sentinel not in str(exc.value)
        assert "dict" in str(exc.value).lower() or "unsupported type" in str(exc.value).lower()

    def test_list_agent_name_error_names_type_not_value(self) -> None:
        sentinel = "SENTINEL-NOT-A-SECRET-C3B"
        payload = {
            "schemaVersion": 2,
            "agentName": [sentinel],
            "subscriptionId": SUBSCRIPTION_ID,
            "resourceGroup": "rg",
            "botName": "bot",
            "armResourceId": (
                "/subscriptions/x/resourceGroups/rg/providers/Microsoft.BotService/botServices/bot"
            ),
            "msaAppId": "00000000-0000-0000-0000-000000000001",
            "tenantId": TENANT_ID,
            "messagingEndpoint": "https://example.test/api/messages",
            "channelsEnabled": ["msteams"],
            "createdAt": "2020-01-01T00:00:00Z",
            "resourceGroupManaged": False,
        }
        with pytest.raises(BotServiceError) as exc:
            BotServiceConfig.from_json_text(json.dumps(payload), source="cfg")
        _assert_failure_artifacts_clean(exc.value, sentinel)
        assert sentinel not in str(exc.value)
        assert "list" in str(exc.value).lower() or "unsupported type" in str(exc.value).lower()


class TestC4SchemaVersionOutOfRangeMessage:
    def test_int_99_is_unsupported_version_not_type(self) -> None:
        with pytest.raises(SidecarSchemaError, match="unsupported version"):
            validate_bot_service_sidecar_schema({"schemaVersion": 99}, source="cfg")


class TestC5BackupOSErrorNormalized:
    @pytest.mark.parametrize(
        "patch_target,failure",
        [
            ("mkdir", "SidecarBackupError"),
            ("open", "SidecarBackupError"),
            ("write", "SidecarBackupError"),
            ("link", "SidecarBackupError"),
            ("dir_fsync", "SidecarBackupError"),
        ],
    )
    def test_fault_injection_becomes_sidecar_backup_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_target: str,
        failure: str,
    ) -> None:
        target = tmp_path / "nested" / "sidecar.json.backup-test"
        real_write = os.write
        real_fsync = os.fsync

        if patch_target == "mkdir":

            def fail_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
                raise OSError("injected mkdir failure")

            monkeypatch.setattr(Path, "mkdir", fail_mkdir)
        elif patch_target == "open":

            def fail_open(*args: Any, **kwargs: Any) -> int:
                raise OSError("injected open failure")

            monkeypatch.setattr(os, "open", fail_open)
        elif patch_target == "write":
            write_calls = 0

            def fail_write(fd: int, data: bytes | memoryview) -> int:
                nonlocal write_calls
                write_calls += 1
                if write_calls == 1:
                    raise OSError("injected write failure")
                return real_write(fd, data)

            monkeypatch.setattr(os, "write", fail_write)
        elif patch_target == "link":

            def fail_link(src: str | os.PathLike, dst: str | os.PathLike) -> None:
                raise OSError("injected link failure")

            monkeypatch.setattr(os, "link", fail_link)
        elif patch_target == "dir_fsync":
            dir_fsync_calls = 0

            def fail_fsync(fd: int) -> None:
                nonlocal dir_fsync_calls
                dir_fsync_calls += 1
                if dir_fsync_calls >= 2:
                    raise OSError("injected directory fsync failure")
                real_fsync(fd)

            monkeypatch.setattr(os, "fsync", fail_fsync)
        else:
            raise AssertionError(patch_target)

        with pytest.raises(SidecarBackupError):
            write_owner_only_bytes_exclusive(target, b"payload\n")
        assert failure == "SidecarBackupError"
        assert list(tmp_path.rglob("*.part-*")) == []
        if patch_target != "dir_fsync":
            assert not target.exists()

    def test_close_failure_still_cleans_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "sidecar.json.backup-test"

        def fail_close(fd: int) -> None:
            raise OSError("injected close failure")

        monkeypatch.setattr(os, "close", fail_close)
        with pytest.raises(SidecarBackupError):
            write_owner_only_bytes_exclusive(target, b"x")
        assert not target.exists()
        assert list(tmp_path.glob(".sidecar.json.backup-test.part-*")) == []


class TestParseExceptionLeak:
    def test_create_invalid_json_does_not_retain_sentinel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        marker = "SENTINEL-PARSE-LEAK-271-2"
        sidecar = tmp_path / "a365.bot-service.config.json"
        sidecar.write_bytes(f'{{"schemaVersion": 1, "secret": "{marker}"'.encode())
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError) as excinfo:
            apply_create_plan(plan, runner=runner, operator_env=env)
        _assert_marker_absent_from_bot_service_failure(excinfo.value, marker)
        assert marker not in capsys.readouterr().err
        assert isinstance(excinfo.value.__cause__, type(None)) or excinfo.value.__cause__ is None

    def test_from_json_text_invalid_json_does_not_chain_decode_doc(self) -> None:
        marker = "SENTINEL-PARSE-LEAK-FROM-JSON"
        broken = json.dumps({"schemaVersion": 1, "x": marker})[:-2]
        with pytest.raises(BotServiceError) as excinfo:
            BotServiceConfig.from_json_text(broken, source="cfg")
        _assert_marker_absent_from_bot_service_failure(excinfo.value, marker)
        assert excinfo.value.__cause__ is None


class TestPr274FixRound:
    def test_read_bound_sidecar_invalid_utf8_scrubs_sentinel(self, tmp_path: Path) -> None:
        marker = "SENTINEL-274-READ-BOUND"
        sidecar = tmp_path / "a365.bot-service.config.json"
        sidecar.write_bytes(b"\xff\xfe" + marker.encode())
        with pytest.raises(BotServiceError, match="not valid UTF-8") as excinfo:
            _read_bound_sidecar(sidecar)
        _assert_failure_artifacts_clean(excinfo.value, marker)

    def test_from_file_invalid_utf8_scrubs_sentinel(self, tmp_path: Path) -> None:
        marker = "SENTINEL-274-FROM-FILE"
        sidecar = tmp_path / "a365.bot-service.config.json"
        sidecar.write_bytes(b"\xff\xfe" + marker.encode())
        with pytest.raises(BotServiceError) as excinfo:
            BotServiceConfig.from_file(sidecar)
        _assert_failure_artifacts_clean(excinfo.value, marker)

    def test_probe_bot_service_config_invalid_utf8_safe_message(self, tmp_path: Path) -> None:
        marker = "SENTINEL-274-PROBE"
        sidecar = tmp_path / "a365.bot-service.config.json"
        sidecar.write_bytes(b"\xff\xfe" + marker.encode())
        result, config = probe_bot_service_config(sidecar)
        assert config is None
        assert result.state == "error"
        assert marker not in result.detail
        assert marker not in repr(result.data)

    def test_create_refuses_directory_before_azure(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "a365.bot-service.config.json"
        sidecar.mkdir()
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError, match="not a regular sidecar file"):
            apply_create_plan(plan, runner=runner, operator_env=env)
        assert runner.calls == []

    def test_write_payload_recheck_utf8_quarantines_without_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "conversations.json"
        valid = (json.dumps({"schemaVersion": 1, "conversations": {}}) + "\n").encode()
        path.write_bytes(valid)
        reads = {"n": 0}
        real_read_bytes = Path.read_bytes
        second_read = bytes([0xFF])

        def read_bytes(self: Path) -> bytes:
            reads["n"] += 1
            if reads["n"] == 1:
                return real_read_bytes(self)
            return second_read

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        payload = {"schemaVersion": 1, "conversations": {}}
        ConversationRegistry.write_payload(path, payload)
        quarantine = next(p for p in tmp_path.iterdir() if ".corrupt-" in p.name)
        assert real_read_bytes(quarantine) == second_read
        json.loads(path.read_text())

    def test_from_json_text_invalid_json_reports_line_column(self) -> None:
        broken = '{"x": "unclosed'
        with pytest.raises(BotServiceError) as excinfo:
            BotServiceConfig.from_json_text(broken, source="cfg")
        assert str(excinfo.value) == (
            "cfg is not valid JSON (Unterminated string starting at: line 1 column 7 (char 6))"
        )
        assert "at at line" not in str(excinfo.value)
        assert excinfo.value.__context__ is None

    def test_from_json_text_schema_version_sentinel_scrubs_locals(self) -> None:
        marker = "SENTINEL-274-R4-SCHEMA-VERSION"
        payload = json.loads(_sidecar_v1_bytes().decode())
        payload["schemaVersion"] = marker
        with pytest.raises(BotServiceError) as excinfo:
            BotServiceConfig.from_json_text(json.dumps(payload), source="cfg")
        _assert_failure_artifacts_clean(excinfo.value, marker)
        assert marker not in str(excinfo.value)

    def test_from_json_text_channels_sentinel_scrubs_locals(self) -> None:
        marker = "SENTINEL-274-R4-CHANNELS"
        payload = json.loads(_sidecar_v1_bytes().decode())
        payload["channelsEnabled"] = marker
        with pytest.raises(BotServiceError) as excinfo:
            BotServiceConfig.from_json_text(json.dumps(payload), source="cfg")
        _assert_failure_artifacts_clean(excinfo.value, marker)
        assert marker not in str(excinfo.value)

    def test_create_schema_validation_sentinel_without_cause_chain(self, tmp_path: Path) -> None:
        marker = "SENTINEL-274-R4-CREATE-SCHEMA"
        sidecar = tmp_path / "a365.bot-service.config.json"
        payload = json.loads(_sidecar_v1_bytes().decode())
        payload["schemaVersion"] = marker
        sidecar.write_bytes(json.dumps(payload).encode())
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError) as excinfo:
            apply_create_plan(plan, runner=runner, operator_env=env)
        _assert_failure_artifacts_clean(excinfo.value, marker)
        assert excinfo.value.__cause__ is None
        assert runner.calls == []

    def test_create_agent_name_sentinel_scrubs_bound_local(self, tmp_path: Path) -> None:
        marker = "SENTINEL-274-R4-CREATE-BOUND"
        sidecar = tmp_path / "a365.bot-service.config.json"
        payload = json.loads(_sidecar_v2_bytes("agent").decode())
        payload["agentName"] = {marker: 1}
        sidecar.write_bytes(json.dumps(payload).encode())
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError) as excinfo:
            apply_create_plan(plan, runner=runner, operator_env=env)
        _assert_failure_artifacts_clean(excinfo.value, marker)
        assert marker not in str(excinfo.value)
        assert runner.calls == []

    def test_create_refuses_dangling_symlink_before_azure(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "a365.bot-service.config.json"
        sidecar.symlink_to(tmp_path / "missing.json")
        inputs = BotServiceCreateInputs(
            agent_name="agent",
            resource_group="rg",
            endpoint="https://example.test/api/messages",
            sidecar_path=sidecar,
            app_id="00000000-0000-0000-0000-000000000001",
        )
        env = {"A365_BF_APP_ID": "00000000-0000-0000-0000-000000000001"}
        plan = build_create_plan(inputs, operator_env=env)
        runner = FakeRunner(group_exists=False)
        with pytest.raises(BotServiceError, match="not a regular sidecar file"):
            apply_create_plan(plan, runner=runner, operator_env=env)
        assert runner.calls == []

    def test_backup_temp_permission_error_is_sidecar_backup_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "sidecar.json.backup-test"
        part = target.with_name(f".{target.name}.part-{os.getpid()}")
        part.mkdir()
        real_is_dir = Path.is_dir

        def guarded_is_dir(self: Path) -> bool:
            if self == part:
                raise PermissionError("injected eacces")
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
        with pytest.raises(SidecarBackupError, match=r"prepare|directory|backup"):
            write_owner_only_bytes_exclusive(target, b"payload\n")
        assert not target.exists()

    def test_recheck_quarantine_backup_failure_scrubs_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365 import _common
        from hermes_a365.plugin import conversations as conversations_mod

        marker = b"\xff" + b"SENTINEL-274-QUARANTINE-BACKUP"
        path = tmp_path / "conversations.json"
        valid = (json.dumps({"schemaVersion": 1, "conversations": {}}) + "\n").encode()
        path.write_bytes(valid)
        reads = {"n": 0}
        real_read_bytes = Path.read_bytes
        real_write = _common.write_owner_only_bytes_exclusive

        def read_bytes(self: Path) -> bytes:
            reads["n"] += 1
            if reads["n"] == 1:
                return real_read_bytes(self)
            return marker

        def fail_backup(path_arg: Path, data: bytes, **kwargs: Any) -> Path:
            if marker in data:
                raise SidecarBackupError("injected quarantine backup failure")
            return real_write(path_arg, data, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        monkeypatch.setattr(_common, "write_owner_only_bytes_exclusive", fail_backup)
        monkeypatch.setattr(conversations_mod, "write_owner_only_bytes_exclusive", fail_backup)

        with pytest.raises(SidecarBackupError) as excinfo:
            ConversationRegistry.write_payload(path, {"schemaVersion": 1, "conversations": {}})
        _assert_failure_artifacts_clean(excinfo.value, "SENTINEL-274-QUARANTINE-BACKUP")

    def test_backup_temp_directory_raises_sidecar_backup_error(self, tmp_path: Path) -> None:
        target = tmp_path / "sidecar.json.backup-test"
        part = target.with_name(f".{target.name}.part-{os.getpid()}")
        part.mkdir()
        with pytest.raises(SidecarBackupError, match="directory"):
            write_owner_only_bytes_exclusive(target, b"payload\n")
        assert not target.exists()
