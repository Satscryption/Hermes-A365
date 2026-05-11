"""Tests for hermes_a365.hermes_responder — slice 19c (Tier-1 responder).

Covers each mode (echo / greeting / canned), `invoke` activity
handling, the conversation store cap, the optional history endpoint,
log-line emission, CLI startup-validation, and the FastAPI app via
`TestClient`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hermes_a365.hermes_responder import (
    DEFAULT_HISTORY_MAX,
    ConversationStore,
    ResponderConfig,
    ResponderConfigError,
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
        assert store.for_conv("conv-1") == [
            {"in": {"text": "a"}, "out": {"text": "A"}}
        ]

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

    def test_no_path_writes_to_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_event(None, {"type": "message", "text_in": "hi"})
        out = capsys.readouterr().out
        assert json.loads(out.strip())["text_in"] == "hi"


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
            r = client.post(
                "/respond", json=_envelope(activity_type="conversationUpdate")
            )
        assert r.status_code == 200
        assert r.json() == {"text": ""}

    def test_history_endpoint_off_by_default(self) -> None:
        app = make_app(ResponderConfig(mode="echo", debug_endpoints=False))
        with TestClient(app) as client:
            r = client.get("/history/conv-1")
        assert r.status_code == 404

    def test_history_endpoint_on_with_flag(self) -> None:
        app = make_app(ResponderConfig(mode="echo", debug_endpoints=True))
        with TestClient(app) as client:
            client.post("/respond", json=_envelope(text="one", conv_id="conv-1"))
            client.post("/respond", json=_envelope(text="two", conv_id="conv-1"))
            r = client.get("/history/conv-1")
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


# ---------------------------------------------------------------------------
# CLI argparse / startup
# ---------------------------------------------------------------------------


class TestCli:
    def test_canned_without_file_exits_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["serve", "--mode", "canned"])
        assert rc == 2
        assert "canned-response-file is required" in capsys.readouterr().err

    def test_invalid_mode_rejected_by_argparse(self) -> None:
        with pytest.raises(SystemExit):
            main(["serve", "--mode", "bogus"])
