"""PR #269 fix-round: schema guards, durability ordering, publish lock, packaging."""

from __future__ import annotations

import errno
import json
import logging
import multiprocessing
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from hermes_a365._common import (
    _fsync_directory,
    exclusive_sidecar_lock,
    sidecar_backup_path,
    write_owner_only_bytes_exclusive,
    write_owner_only_text_atomic,
)
from hermes_a365.plugin.conversations import ConversationRegistry, ConversationSidecarError
from hermes_a365.publish import (
    PublishError,
    _copilot_chat_publish_state_path,
    _write_copilot_chat_publish_state,
)
from hermes_a365.sidecar_schema import validate_conversations_sidecar_schema

_SECRET = "SENTINEL_DO_NOT_LEAK_269"


@pytest.mark.parametrize(
    ("payload",),
    [
        ({"schema": 99},),
        ({"schema": 99, "schemaVersion": 1},),
        ({"schema": None},),
        ({"schemaVersion": None},),
        ({"schema": True},),
        ({"schemaVersion": False},),
        ({"schema": 0},),
        ({"schemaVersion": 0},),
        ({"schema": 2, "schemaVersion": 2},),
    ],
)
def test_conversations_invalid_schema_rejected_on_load_and_save(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    path = tmp_path / "conversations.json"
    body = {"conversations": [], **payload}
    original = (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(original)

    with pytest.raises(ConversationSidecarError):
        ConversationRegistry.load(path)
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.backup-*")) == []

    reg = ConversationRegistry()
    with pytest.raises(ConversationSidecarError):
        ConversationRegistry.write_payload(path, reg.to_payload())
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.backup-*")) == []


def test_conversations_schema_error_never_echoes_raw_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {"schema": _SECRET, "conversations": []}
    with pytest.raises(ConversationSidecarError) as exc:
        validate_conversations_sidecar_schema(payload)
    assert _SECRET not in str(exc.value)
    assert "schema" in str(exc.value).lower()

    nested = {"schema": {"nested": _SECRET}, "conversations": []}
    with pytest.raises(ConversationSidecarError) as exc2:
        validate_conversations_sidecar_schema(nested)
    assert _SECRET not in str(exc2.value)


def test_conversations_load_and_write_errors_omit_secret_sentinels(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps({"schema": _SECRET, "conversations": []}) + "\n")
    with pytest.raises(ConversationSidecarError) as load_exc:
        ConversationRegistry.load(path)
    assert _SECRET not in str(load_exc.value)

    caplog.set_level(logging.WARNING, logger="hermes_a365.plugin.adapter")
    path.write_text(json.dumps({"schema": 1, "schemaVersion": 99, "conversations": []}) + "\n")
    with pytest.raises(ConversationSidecarError) as save_exc:
        ConversationRegistry.write_payload(path, ConversationRegistry().to_payload())
    assert _SECRET not in str(save_exc.value)
    assert _SECRET not in caplog.text


def test_atomic_write_fsync_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def track_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def track_replace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", track_fsync)
    monkeypatch.setattr(os, "replace", track_replace)

    path = tmp_path / "conversations.json"
    path.write_bytes(b'{"schema":1,"conversations":[]}\n')
    ConversationRegistry.write_payload(path, ConversationRegistry.load(path).to_payload())

    assert "replace" in events
    assert events.index("replace") > events.index("fsync")
    assert events.count("fsync") >= 3


@pytest.mark.parametrize(
    "fail_after",
    ["backup_fsync", "replace"],
)
def test_conversations_write_failure_preserves_original_or_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_after: str
) -> None:
    path = tmp_path / "conversations.json"
    original = b'{"schema":1,"conversations":[]}\n'
    path.write_bytes(original)
    payload = ConversationRegistry.load(path).to_payload()
    fsync_count = 0
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(fd: int) -> None:
        nonlocal fsync_count
        fsync_count += 1
        if fail_after == "backup_fsync" and fsync_count == 2:
            raise OSError("injected after backup fsync")
        real_fsync(fd)

    def replace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
        if fail_after == "replace":
            raise OSError("injected at replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(OSError):
        ConversationRegistry.write_payload(path, payload)

    if fail_after == "replace":
        assert path.read_bytes() == original
        backups = list(tmp_path.glob("conversations.backup-*.json"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == original
    else:
        assert path.read_bytes() == original


def _hold_lock(path_str: str, ready: multiprocessing.Queue, release: multiprocessing.Queue) -> None:
    with exclusive_sidecar_lock(Path(path_str)):
        ready.put("ok")
        release.get()


def test_publish_write_with_lock_held_and_absent_file_creates_nothing(
    tmp_path: Path,
) -> None:
    state_path = _copilot_chat_publish_state_path(tmp_path, "agent")
    state_path.parent.mkdir(parents=True)
    assert not state_path.exists()
    ctx = multiprocessing.get_context("spawn")
    ready: multiprocessing.Queue = ctx.Queue()
    release: multiprocessing.Queue = ctx.Queue()
    proc = ctx.Process(target=_hold_lock, args=(str(state_path), ready, release))
    proc.start()
    assert ready.get(timeout=5) == "ok"
    try:
        with pytest.raises(PublishError, match="another publish operation"):
            _write_copilot_chat_publish_state(
                state_path,
                manifest_id="44444444-4444-4444-4444-444444444444",
                app_version="1.1.5",
                hermes_home=tmp_path,
                tenant_id="22222222-2222-2222-2222-222222222222",
                profile="default",
                cea_catalog_id_minted=False,
            )
        assert not state_path.exists()
        assert not list(state_path.parent.glob("*.backup-*"))
    finally:
        release.put("go")
        proc.join(timeout=10)


def test_publish_race_newer_schema_on_disk_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _copilot_chat_publish_state_path(tmp_path, "agent")
    state_path.parent.mkdir(parents=True)
    v1 = (
        json.dumps({"schemaVersion": 1, "manifestId": "x", "lastAppVersion": "1.0.0"}) + "\n"
    ).encode()
    v99 = (
        json.dumps({"schemaVersion": 99, "manifestId": "x", "lastAppVersion": "1.0.0"}) + "\n"
    ).encode()
    state_path.write_bytes(v1)
    calls = 0
    real_read_bytes = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        nonlocal calls
        if self == state_path:
            calls += 1
            return v1 if calls == 1 else v99
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
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
    assert state_path.read_bytes() == v99
    assert not list(state_path.parent.glob("*.backup-*"))


@pytest.mark.parametrize(
    "bad_version",
    [None, 0, True, 99.0, "99", 99, 3],
)
def test_publish_save_rejects_bad_schema_version_before_write(
    tmp_path: Path, bad_version: Any
) -> None:
    state_path = _copilot_chat_publish_state_path(tmp_path, "agent")
    state_path.parent.mkdir(parents=True)
    if bad_version is None:
        original_obj: dict[str, Any] = {"manifestId": "x", "lastAppVersion": "1.0.0"}
    else:
        original_obj = {
            "schemaVersion": bad_version,
            "manifestId": "x",
            "lastAppVersion": "1.0.0",
        }
    original = (json.dumps(original_obj) + "\n").encode()
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


def test_publish_schema_less_file_unchanged(tmp_path: Path) -> None:
    state_path = _copilot_chat_publish_state_path(tmp_path, "agent")
    state_path.parent.mkdir(parents=True)
    original = (json.dumps({"manifestId": "x", "lastAppVersion": "1.0.0"}) + "\n").encode()
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


def test_backup_bytes_exclusive_fsyncs_before_return(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[str] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    backup = tmp_path / "x.backup-20260101-000000.json"
    write_owner_only_bytes_exclusive(backup, b"{}\n")
    assert order.count("fsync") >= 2


def test_text_atomic_fsync_before_replace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def replace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
        order.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    target = tmp_path / "t.json"
    write_owner_only_text_atomic(target, "{}\n")
    assert order.index("replace") > 0
    assert order[order.index("replace") - 1] == "fsync"


def test_fsync_directory_skips_before_open_on_nt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: list[str] = []

    def open_stub(path: str | os.PathLike, flags: int, *args: object, **kwargs: object) -> int:
        opened.append(str(path))
        raise AssertionError("directory open should not run on nt")

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "open", open_stub)
    _fsync_directory(tmp_path)
    assert opened == []


def test_fsync_directory_unsupported_errno_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    path = tmp_path / "conversations.json"
    write_owner_only_text_atomic(path, '{"schema":1,"conversations":[]}\n')
    assert path.is_file()


def test_file_fsync_failure_prevents_atomic_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "conversations.json"
    path.write_bytes(b'{"schema":1,"conversations":[]}\n')
    real_fsync = os.fsync
    real_replace = os.replace
    fsync_calls = 0

    def fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError(5, "EIO")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", real_replace)
    with pytest.raises(OSError):
        write_owner_only_text_atomic(path, '{"schema":2,"conversations":[]}\n')
    assert path.read_bytes() == b'{"schema":1,"conversations":[]}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_backup_partial_write_leaves_no_final_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = tmp_path / "conversations.json"
    original = b'{"schema":1,"conversations":[]}\n'
    sidecar.write_bytes(original)
    backup = sidecar_backup_path(sidecar)
    real_write = os.write
    backup_writes = 0

    def write(fd: int, data: bytes | memoryview) -> int:
        nonlocal backup_writes
        backup_writes += 1
        if backup_writes == 1 and len(data) > 1:
            real_write(fd, data[:1])
            raise OSError("injected partial backup write")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write)
    with pytest.raises(OSError):
        write_owner_only_bytes_exclusive(backup, original)
    assert sidecar.read_bytes() == original
    assert not backup.exists()
    assert list(tmp_path.glob("conversations.backup-*.json")) == []
    assert list(tmp_path.glob(".conversations.backup-*.part-*")) == []

    write_owner_only_bytes_exclusive(backup, original)
    assert backup.read_bytes() == original
    assert sidecar.read_bytes() == original
