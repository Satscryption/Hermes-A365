"""Tests for hermes_a365.hermes_responder — slice 19c (Tier-1 responder).

Covers each mode (echo / greeting / canned), `invoke` activity
handling, the conversation store cap, the optional history endpoint,
log-line emission, CLI startup-validation, and the FastAPI app via
`TestClient`.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hermes_a365.hermes_responder import (
    DEFAULT_HISTORY_MAX,
    ConversationStore,
    ResponderConfig,
    ResponderConfigError,
    _resolve_history_token,
    log_event,
    main,
    make_app,
    render_canned_reply,
    render_echo_reply,
    render_greeting_reply,
    render_invoke_response,
    resolve_log_path,
)

# ---------------------------------------------------------------------------
# Pure render helpers
# ---------------------------------------------------------------------------


class TestRenderEcho:
    def test_basic(self) -> None:
        assert render_echo_reply({"text": "hi"}) == {"text": "You said: hi"}

    def test_empty_text(self) -> None:
        assert render_echo_reply({}) == {"text": "You said:"}

    def test_strips_trailing_blank(self) -> None:
        # "You said: <empty>" → strip trims to "You said:"
        assert render_echo_reply({"text": ""}) == {"text": "You said:"}


class TestRenderGreeting:
    def test_first_in_conv_returns_card(self) -> None:
        reply = render_greeting_reply(first_in_conv=True, activity={"text": "hi"})
        assert "card" in reply
        assert reply["card"]["type"] == "AdaptiveCard"
        assert reply["card"]["version"] == "1.6"
        assert reply["text"]

    def test_subsequent_falls_back_to_echo(self) -> None:
        reply = render_greeting_reply(first_in_conv=False, activity={"text": "round 2"})
        assert reply == {"text": "You said: round 2"}


class TestRenderCanned:
    def test_reads_file(self, tmp_path: Path) -> None:
        path = tmp_path / "canned.json"
        path.write_text(json.dumps({"text": "canned reply"}))
        assert render_canned_reply(path) == {"text": "canned reply"}

    def test_hot_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "canned.json"
        path.write_text(json.dumps({"text": "v1"}))
        assert render_canned_reply(path)["text"] == "v1"
        path.write_text(json.dumps({"text": "v2"}))
        # Same path read twice should reflect the new content.
        assert render_canned_reply(path)["text"] == "v2"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResponderConfigError, match="missing"):
            render_canned_reply(tmp_path / "nonexistent.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not json {{")
        with pytest.raises(ResponderConfigError, match="not valid JSON"):
            render_canned_reply(path)


class TestRenderInvoke:
    def test_returns_invoke_response_envelope(self) -> None:
        out = render_invoke_response({"name": "adaptiveCard/action"})
        assert out["invokeResponse"]["status"] == 200
        assert "adaptiveCard/action" in out["invokeResponse"]["body"]["text"]


# ---------------------------------------------------------------------------
# ConversationStore
# ---------------------------------------------------------------------------


class TestConversationStore:
    def test_append_and_retrieve(self) -> None:
        store = ConversationStore()
        store.append("conv-1", {"in": {"text": "a"}, "out": {"text": "A"}})
        assert store.for_conv("conv-1") == [{"in": {"text": "a"}, "out": {"text": "A"}}]

    def test_history_capped(self) -> None:
        store = ConversationStore(history_max=3)
        for i in range(10):
            store.append("conv-1", {"i": i})
        out = store.for_conv("conv-1")
        # deque(maxlen=3) keeps the last three.
        assert len(out) == 3
        assert [e["i"] for e in out] == [7, 8, 9]

    def test_empty_conv_id_ignored(self) -> None:
        # Don't fill the store with `""` if the bridge ever forwards an
        # activity without a conversation id.
        store = ConversationStore()
        store.append("", {"in": {"text": "?"}})
        assert store.conversation_count == 0

    def test_default_history_max_pinned(self) -> None:
        # The cap doubles as a memory-safety bound; pin the default.
        assert DEFAULT_HISTORY_MAX == 50


# ---------------------------------------------------------------------------
# resolve_log_path
# ---------------------------------------------------------------------------


class TestResolveLogPath:
    def test_no_slug_means_no_file_log(self) -> None:
        assert resolve_log_path(None) is None

    def test_slug_resolves_under_hermes_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = resolve_log_path("inbox-helper")
        assert path == tmp_path / "agents" / "inbox-helper" / "responder.log"

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ".", "a\\b"])
    def test_traversal_shaped_slug_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: str
    ) -> None:
        # #103 / M9: --slug feeds log_event's mkdir/append primitives;
        # a traversal-shaped value must fail before the path join.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError):
            resolve_log_path(bad)


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------


class TestLogEvent:
    def test_appends_jsonl_line_to_file(self, tmp_path: Path) -> None:
        log_path = tmp_path / "agents" / "x" / "responder.log"
        log_event(log_path, {"conversation_id": "c1", "type": "message"})
        log_event(log_path, {"conversation_id": "c1", "type": "message"})
        lines = log_path.read_text().splitlines()
        assert len(lines) == 2
        for line in lines:
            payload = json.loads(line)
            assert payload["conversation_id"] == "c1"
            assert "ts" in payload

    def test_no_path_writes_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        log_event(None, {"type": "message", "text_in": "hi"})
        out = capsys.readouterr().out
        assert json.loads(out.strip())["text_in"] == "hi"

    def test_fresh_dir_and_file_are_owner_only(self, tmp_path: Path) -> None:
        # #115 (CS-007): even under a permissive umask the slug dir and
        # log file must be created owner-only (M365 message text lands here).
        log_path = tmp_path / "agents" / "x" / "responder.log"
        old = os.umask(0o022)
        try:
            log_event(log_path, {"conversation_id": "c1", "type": "message"})
        finally:
            os.umask(old)
        assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

    def test_repairs_pre_existing_loose_modes(self, tmp_path: Path) -> None:
        # A dir/file left world-readable by an older build must be tightened
        # on the next write, and the existing content preserved (append).
        slug_dir = tmp_path / "agents" / "x"
        slug_dir.mkdir(parents=True)
        log_path = slug_dir / "responder.log"
        log_path.write_text(json.dumps({"conversation_id": "old"}) + "\n")
        slug_dir.chmod(0o755)
        log_path.chmod(0o644)
        old = os.umask(0o022)
        try:
            log_event(log_path, {"conversation_id": "new", "type": "message"})
        finally:
            os.umask(old)
        assert stat.S_IMODE(slug_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
        lines = log_path.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["conversation_id"] == "old"
        assert json.loads(lines[1])["conversation_id"] == "new"

    def test_fchmod_failure_does_not_leak_fd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If fchmod raises (EPERM on a foreign-owned but writable log), the
        # descriptor from os.open must still be closed — else the long-lived
        # responder leaks one fd per inbound message until exhaustion.
        slug_dir = tmp_path / "agents" / "x"
        slug_dir.mkdir(parents=True)
        log_path = slug_dir / "responder.log"

        def boom(fd: int, mode: int) -> None:
            raise PermissionError("simulated EPERM")

        monkeypatch.setattr(os, "fchmod", boom)
        before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
        for _ in range(20):
            with pytest.raises(PermissionError):
                log_event(log_path, {"conversation_id": "x", "type": "message"})
        if before is not None:
            after = len(os.listdir("/dev/fd"))
            # Allow small jitter from the listdir fd itself; a real leak would
            # be ~20.
            assert after - before <= 2

    def test_symlinked_log_path_is_refused(self, tmp_path: Path) -> None:
        # O_NOFOLLOW: a pre-planted symlink at the log path must not be
        # opened (and then fchmod'd) — that would let an attacker retarget
        # the 0600 chmod at an arbitrary owner-owned file.
        slug_dir = tmp_path / "agents" / "x"
        slug_dir.mkdir(parents=True)
        victim = tmp_path / "victim.txt"
        victim.write_text("untouched")
        log_path = slug_dir / "responder.log"
        log_path.symlink_to(victim)
        with pytest.raises(OSError):
            log_event(log_path, {"conversation_id": "x", "type": "message"})
        assert victim.read_text() == "untouched"


# ---------------------------------------------------------------------------
# FastAPI app — TestClient
# ---------------------------------------------------------------------------


def _envelope(
    *,
    text: str = "hi",
    conv_id: str = "conv-1",
    activity_type: str = "message",
    name: str = "",
) -> dict[str, Any]:
    return {
        "version": "1",
        "agent": {
            "slug": "inbox-helper",
            "tenant_id": "tenant-id",
            "bot_app_id": "bot-app",
        },
        "activity": {
            "type": activity_type,
            "id": "act-1",
            "text": text,
            "name": name,
            "channelId": "msteams",
            "conversation": {"id": conv_id},
            "from": {"id": "user", "name": "User"},
            "recipient": {"id": "bot", "name": "Bot"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        },
    }


class TestServeApp:
    def test_healthz(self) -> None:
        app = make_app(ResponderConfig(mode="echo"))
        with TestClient(app) as client:
            r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "echo"
        assert body["conversations"] == 0

    def test_echo_mode(self) -> None:
        app = make_app(ResponderConfig(mode="echo"))
        with TestClient(app) as client:
            r = client.post("/respond", json=_envelope(text="ping"))
        assert r.status_code == 200
        assert r.json() == {"text": "You said: ping"}

    def test_greeting_first_then_echo(self) -> None:
        app = make_app(ResponderConfig(mode="greeting"))
        with TestClient(app) as client:
            r1 = client.post("/respond", json=_envelope(text="hi"))
            r2 = client.post("/respond", json=_envelope(text="round 2"))
        # First turn → card. Second turn → echo (no card).
        assert "card" in r1.json()
        assert r2.json() == {"text": "You said: round 2"}

    def test_greeting_card_per_conversation(self) -> None:
        # Two distinct conversations both get a card on their first turn.
        app = make_app(ResponderConfig(mode="greeting"))
        with TestClient(app) as client:
            a = client.post("/respond", json=_envelope(text="hi", conv_id="conv-A"))
            b = client.post("/respond", json=_envelope(text="hi", conv_id="conv-B"))
        assert "card" in a.json()
        assert "card" in b.json()

    def test_canned_mode(self, tmp_path: Path) -> None:
        canned = tmp_path / "responses.json"
        canned.write_text(json.dumps({"text": "canned reply"}))
        app = make_app(ResponderConfig(mode="canned", canned_response_file=canned))
        with TestClient(app) as client:
            r = client.post("/respond", json=_envelope())
        assert r.json() == {"text": "canned reply"}

    def test_canned_mode_rejects_without_file(self) -> None:
        with pytest.raises(ResponderConfigError, match="canned-response-file is required"):
            make_app(ResponderConfig(mode="canned"))

    def test_invoke_returns_invoke_response(self) -> None:
        app = make_app(ResponderConfig(mode="echo"))
        with TestClient(app) as client:
            r = client.post(
                "/respond",
                json=_envelope(activity_type="invoke", name="adaptiveCard/action"),
            )
        body = r.json()
        assert body["invokeResponse"]["status"] == 200
        assert "adaptiveCard/action" in body["invokeResponse"]["body"]["text"]

    def test_conversation_update_acked_with_empty_text(self) -> None:
        # Bridge already filters these, but be defensive.
        app = make_app(ResponderConfig(mode="echo"))
        with TestClient(app) as client:
            r = client.post("/respond", json=_envelope(activity_type="conversationUpdate"))
        assert r.status_code == 200
        assert r.json() == {"text": ""}

    def test_history_endpoint_off_by_default(self) -> None:
        app = make_app(ResponderConfig(mode="echo", debug_endpoints=False))
        with TestClient(app) as client:
            r = client.get("/history/conv-1")
        assert r.status_code == 404

    def test_history_endpoint_fail_closed_without_token(self) -> None:
        # #114 (CS-006): flag on but no token → route not registered.
        app = make_app(ResponderConfig(mode="echo", debug_endpoints=True))
        with TestClient(app) as client:
            r = client.get("/history/conv-1")
        assert r.status_code == 404

    def test_history_endpoint_missing_header_401(self) -> None:
        app = make_app(ResponderConfig(mode="echo", debug_endpoints=True, history_token="tok"))
        with TestClient(app) as client:
            r = client.get("/history/conv-1")
        assert r.status_code == 401

    def test_history_endpoint_wrong_token_403(self) -> None:
        app = make_app(ResponderConfig(mode="echo", debug_endpoints=True, history_token="tok"))
        with TestClient(app) as client:
            r = client.get("/history/conv-1", headers={"X-Hermes-History-Token": "nope"})
        assert r.status_code == 403

    def test_history_endpoint_on_with_token(self) -> None:
        app = make_app(ResponderConfig(mode="echo", debug_endpoints=True, history_token="tok"))
        headers = {"X-Hermes-History-Token": "tok"}
        with TestClient(app) as client:
            client.post("/respond", json=_envelope(text="one", conv_id="conv-1"))
            client.post("/respond", json=_envelope(text="two", conv_id="conv-1"))
            r = client.get("/history/conv-1", headers=headers)
        assert r.status_code == 200
        history = r.json()["activities"]
        assert len(history) == 2
        assert history[0]["in"]["text"] == "one"
        assert history[1]["out"]["text"] == "You said: two"

    def test_log_file_appended_when_slug_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        log_path = resolve_log_path("inbox-helper")
        assert log_path is not None
        cfg = ResponderConfig(mode="echo", slug="inbox-helper", log_path=log_path)
        app = make_app(cfg)
        with TestClient(app) as client:
            client.post("/respond", json=_envelope(text="hi"))
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["text_in"] == "hi"
        assert payload["channel"] == "msteams"
        # #115 (CS-007): the log holding M365 text is owner-only.
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# CLI argparse / startup
# ---------------------------------------------------------------------------


class TestCli:
    def test_canned_without_file_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["serve", "--mode", "canned"])
        assert rc == 2
        assert "canned-response-file is required" in capsys.readouterr().err

    def test_invalid_mode_rejected_by_argparse(self) -> None:
        with pytest.raises(SystemExit):
            main(["serve", "--mode", "bogus"])


# ---------------------------------------------------------------------------
# _resolve_history_token — #114 (CS-006)
# ---------------------------------------------------------------------------


class TestResolveHistoryToken:
    def test_disabled_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_RESPONDER_HISTORY_TOKEN", "from-env")
        assert _resolve_history_token(False) is None

    def test_uses_env_var_when_set(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HERMES_RESPONDER_HISTORY_TOKEN", "from-env")
        assert _resolve_history_token(True) == "from-env"
        # An operator-supplied token is not echoed to stderr.
        assert "from-env" not in capsys.readouterr().err

    def test_empty_env_var_falls_back_to_generation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_RESPONDER_HISTORY_TOKEN", "")
        token = _resolve_history_token(True)
        assert token
        assert len(token) >= 32

    def test_generates_and_prints_once(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("HERMES_RESPONDER_HISTORY_TOKEN", raising=False)
        token = _resolve_history_token(True)
        assert token is not None
        assert len(token) >= 32
        err = capsys.readouterr().err
        assert "X-Hermes-History-Token" in err
        assert token in err

    def test_generated_tokens_are_unique(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HERMES_RESPONDER_HISTORY_TOKEN", raising=False)
        assert _resolve_history_token(True) != _resolve_history_token(True)
