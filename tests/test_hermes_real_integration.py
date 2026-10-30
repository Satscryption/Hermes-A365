"""Integration tests against the vendored Hermes gateway packages."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import test_agent365_plugin as _tap

_HERMES_BOOTSTRAP_MODULE = "hermes_a365_hermes_bootstrap"


def _import_hermes_bootstrap() -> Any:
    cached = sys.modules.get(_HERMES_BOOTSTRAP_MODULE)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent / "_hermes_bootstrap.py"
    spec = importlib.util.spec_from_file_location(_HERMES_BOOTSTRAP_MODULE, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("hermes bootstrap unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_HERMES_BOOTSTRAP_MODULE] = module
    spec.loader.exec_module(module)
    return module


_hermes_bootstrap = _import_hermes_bootstrap()

adapter_mod = _tap.adapter_mod
_make_adapter = _tap._make_adapter
_make_inbound = _tap._make_inbound

pytestmark = pytest.mark.skipif(
    not _hermes_bootstrap.try_import_real_gateway(),
    reason="Hermes vendor checkout unavailable",
)


def _connect_cards(adapter: Any) -> None:
    _tap.TestInteractiveCards()._connect(adapter)


def _enqueue_exec(session_key: str, request_id: str) -> None:
    from tools import approval as approval_mod
    from tools.approval_gateway_wait import _ApprovalEntry

    entry = _ApprovalEntry({"request_id": request_id, "session_key": session_key})
    with approval_mod._lock:
        approval_mod._gateway_queues.setdefault(session_key, []).append(entry)


def test_run_turn_runner_does_not_select_agent365_buttons() -> None:
    renders_buttons = _hermes_bootstrap.load_renders_exec_approval_buttons()

    assert adapter_mod.Agent365Adapter.supports_exec_approval_buttons() is False
    assert renders_buttons(adapter_mod.Agent365Adapter) is False


@pytest.mark.asyncio
async def test_missing_request_id_send_exec_approval_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    _connect_cards(adapter)
    bridge = adapter_mod._import_bridge()
    send_reply = AsyncMock(return_value=None)
    monkeypatch.setattr(bridge, "send_reply", send_reply)
    result = await adapter.send_exec_approval(
        "conv-1",
        "danger",
        "sess-1",
        description="x",
    )
    assert result.success is False
    assert adapter._card_capabilities == {}
    send_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_smart_denied_exec_card_offers_once_and_deny_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    _connect_cards(adapter)
    bridge = adapter_mod._import_bridge()
    send_reply = AsyncMock(return_value=None)
    monkeypatch.setattr(bridge, "send_reply", send_reply)
    inbound = _make_inbound()
    inbound["conversation"]["conversationType"] = "personal"
    adapter._conversations.upsert(adapter_mod.ConversationRef.from_activity(inbound))
    _enqueue_exec("sess-1", "req-smart")
    result = await adapter.send_exec_approval(
        "conv-1",
        "danger",
        "sess-1",
        request_id="req-smart",
        smart_denied=True,
        allow_session=True,
        allow_permanent=True,
    )
    assert result.success is True
    capability = next(iter(adapter._card_capabilities.values()))
    assert list(capability["choices"].values()) == ["once", "deny"]


def test_cross_origin_pending_merge_uses_overflow_not_metadata_theft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    adapter = _make_adapter(monkeypatch, group_sessions_per_user=False)
    session_key = "agent:main:agent365:group:19:group-chat@thread.v2"

    def _event(user_aad: str, text: str, activity_id: str) -> Any:
        inbound = _tap.TestCardCapabilityTurnOrigin()._group_inbound(
            activity_id=activity_id,
            text=text,
            user_aad=user_aad,
            user_id=user_aad,
        )
        source = SessionSource(
            platform=Platform("agent365"),
            chat_id="19:group-chat@thread.v2",
            chat_type="group",
            user_id=user_aad,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=activity_id,
        )
        origin = adapter._turn_origin_from_activity(inbound, session_key)
        adapter._stamp_turn_origin_on_event(event, origin)
        return event

    event_b = _event(_tap.TestCardCapabilityTurnOrigin._USER_B, "harmless", "act-b")
    event_a = _event(_tap.TestCardCapabilityTurnOrigin._USER_A, "approve rm -rf /", "act-a")
    adapter._agent365_merge_pending_message_event(session_key, event_b, merge_text=True)
    adapter._agent365_merge_pending_message_event(session_key, event_a, merge_text=True)
    assert adapter._pending_messages[session_key] is event_b
    assert list(adapter._pending_turn_fifo.get(session_key, [])) == [event_a]
    assert (
        adapter._event_turn_origin(adapter._pending_messages[session_key])["user_id"]
        == _tap.TestCardCapabilityTurnOrigin._USER_B
    )


@pytest.mark.asyncio
async def test_merged_pending_cross_origin_cards_bind_to_correct_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    adapter = _make_adapter(monkeypatch, group_sessions_per_user=False)
    tc = _tap.TestCardCapabilityTurnOrigin()
    tc._wire_adapter(adapter, monkeypatch)
    group = tc._GROUP_CONV
    a_inbound = tc._group_inbound(
        activity_id="act-a1",
        text="start-a",
        user_aad=tc._USER_A,
        user_id="user-a",
    )
    b_inbound = tc._group_inbound(
        activity_id="act-b",
        text="from-b",
        user_aad=tc._USER_B,
        user_id="user-b",
    )
    a2_inbound = tc._group_inbound(
        activity_id="act-a2",
        text="need-approval",
        user_aad=tc._USER_A,
        user_id="user-a",
    )
    a_ready = asyncio.Event()
    b_posted = asyncio.Event()
    release_a = asyncio.Event()
    cards_by_user: dict[str, dict[str, Any]] = {}
    drain_done = asyncio.Event()
    observed_session_key: dict[str, str] = {}

    async def gateway_message_handler(event: Any) -> None:
        message_id = str(getattr(event, "message_id", "") or "")
        session_key = adapter._session_key_for(event) or f"agent365:{group}"
        observed_session_key["key"] = session_key
        origin_user = adapter._event_turn_origin(event)["user_id"]
        if message_id == "act-a1":
            a_ready.set()
            await b_posted.wait()
            await release_a.wait()
            return
        if message_id == "act-b":
            cards_by_user.setdefault(origin_user, {})
            _enqueue_exec(session_key, "cfm-b")
            cards_by_user[origin_user]["slash"] = await adapter.send_slash_confirm(
                group,
                "Confirm",
                "Run?",
                session_key,
                "cfm-b",
            )
            cards_by_user[origin_user]["clarify"] = await adapter.send_clarify(
                group,
                "Pick",
                ["Alpha"],
                "clr-b",
                session_key,
            )
            return
        if message_id != "act-a2":
            return
        cards_by_user.setdefault(origin_user, {})
        _enqueue_exec(session_key, "req-a2")
        cards_by_user[origin_user]["exec"] = await adapter.send_exec_approval(
            group,
            "danger",
            session_key,
            request_id="req-a2",
        )
        drain_done.set()

    adapter.set_message_handler(gateway_message_handler)
    transport = httpx.ASGITransport(app=adapter.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:

        async def post_a1() -> None:
            await client.post(
                "/api/messages",
                json=a_inbound,
                headers={"Authorization": "Bearer a.b.c"},
            )

        a_task = asyncio.create_task(post_a1())
        await asyncio.wait_for(a_ready.wait(), timeout=3.0)
        await client.post(
            "/api/messages",
            json=b_inbound,
            headers={"Authorization": "Bearer a.b.c"},
        )
        b_posted.set()
        await client.post(
            "/api/messages",
            json=a2_inbound,
            headers={"Authorization": "Bearer a.b.c"},
        )
        release_a.set()
        await a_task
        await asyncio.wait_for(drain_done.wait(), timeout=5.0)
        assert observed_session_key.get("key")

    assert cards_by_user[tc._USER_B]["slash"].success
    assert cards_by_user[tc._USER_B]["clarify"].success
    assert cards_by_user[tc._USER_A]["exec"].success
    a_submit = tc._group_inbound(
        activity_id="act-a-submit",
        text="",
        user_aad=tc._USER_A,
        user_id="user-a",
    )
    b_submit = tc._group_inbound(
        activity_id="act-b-submit",
        text="",
        user_aad=tc._USER_B,
        user_id="user-b",
    )
    for nonce, capability in list(adapter._card_capabilities.items()):
        value = {
            "hermes_kind": capability["kind"],
            "capability": nonce,
            "choice_id": next(iter(capability["choices"])),
        }
        owner = a_submit if capability["user_id"] == tc._USER_A else b_submit
        other = b_submit if owner is a_submit else a_submit
        assert (await adapter._handle_card_action(owner, value)).status_code == 200
        assert (await adapter._handle_card_action(other, value)).status_code == 403


@pytest.mark.asyncio
async def test_exec_settlement_observers_revoke_stale_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.approval import list_gateway_approvals

    adapter = _make_adapter(monkeypatch, group_sessions_per_user=False)
    _connect_cards(adapter)
    bridge = adapter_mod._import_bridge()
    send_reply = AsyncMock(return_value=None)
    monkeypatch.setattr(bridge, "send_reply", send_reply)
    session_key = "sess-1"
    _enqueue_exec(session_key, "req-a")
    result = await adapter.send_exec_approval(
        "conv-1",
        "danger",
        session_key,
        request_id="req-a",
    )
    assert result.success is True
    card_a = send_reply.await_args.kwargs["reply"]["attachments"][0]["content"]["actions"][0][
        "data"
    ]
    nonce = card_a["capability"]
    assert nonce in adapter._card_capabilities

    from gateway.run_turn_runner_approval_settle import register_timeout_notice

    ctx = SimpleNamespace(
        session_key=session_key,
        _status_chat_id="conv-1",
        _status_thread_metadata={},
        _run_still_current=lambda: True,
    )

    class _Runner:
        _ctx = ctx

        def _schedule(self, _coro: Any, _label: str) -> None:
            return None

    register_timeout_notice(
        _Runner(),
        {"request_id": "req-a", "command": "danger", "timeout_s": 1},
        command="danger",
        card_message_id=result.message_id,
    )
    assert adapter._gw_resolve_approval(session_key, "once", request_id="req-a") == 1
    assert nonce not in adapter._card_capabilities
    _enqueue_exec(session_key, "req-b")
    token = adapter_mod._AGENT_TURN_ORIGIN.set(
        adapter._turn_origin_from_activity(_make_inbound(), session_key)
    )
    try:
        await adapter.send_exec_approval(
            "conv-1",
            "danger-b",
            session_key,
            request_id="req-b",
        )
    finally:
        adapter_mod._AGENT_TURN_ORIGIN.reset(token)
    assert [x["request_id"] for x in list_gateway_approvals(session_key)] == ["req-b"]
    rejected = await adapter._handle_card_action(_make_inbound(), card_a)
    assert rejected.status_code == 403


@pytest.mark.asyncio
async def test_exec_card_revoked_after_timeout_settle_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    _connect_cards(adapter)
    bridge = adapter_mod._import_bridge()
    monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
    session_key = "sess-timeout"
    _enqueue_exec(session_key, "req-timeout")
    result = await adapter.send_exec_approval(
        "conv-1",
        "danger",
        session_key,
        request_id="req-timeout",
    )
    assert result.success is True
    card_data = bridge.send_reply.await_args.kwargs["reply"]["attachments"][0]["content"][
        "actions"
    ][0]["data"]
    nonce = card_data["capability"]
    from gateway.run_turn_runner_approval_settle import register_timeout_notice

    ctx = SimpleNamespace(
        session_key=session_key,
        _status_chat_id="conv-1",
        _status_thread_metadata={},
        _run_still_current=lambda: True,
    )

    class _Runner:
        _ctx = ctx

        def _schedule(self, _coro: Any, _label: str) -> None:
            return None

    register_timeout_notice(
        _Runner(),
        {"request_id": "req-timeout", "command": "danger"},
        command="danger",
        card_message_id=result.message_id,
    )
    from tools import approval as approval_mod

    with approval_mod._lock:
        entry = next(
            item
            for item in approval_mod._gateway_queues.get(session_key, [])
            if item.data.get("request_id") == "req-timeout"
        )
        settle_hook = entry.settle
    assert callable(settle_hook)
    settle_hook("timeout")
    assert nonce not in adapter._card_capabilities


@pytest.mark.asyncio
async def test_exec_card_revoked_after_typed_slash_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    _connect_cards(adapter)
    bridge = adapter_mod._import_bridge()
    monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
    session_key = "sess-slash"
    _enqueue_exec(session_key, "req-slash")
    await adapter.send_exec_approval(
        "conv-1",
        "danger",
        session_key,
        request_id="req-slash",
    )
    card_data = bridge.send_reply.await_args.kwargs["reply"]["attachments"][0]["content"][
        "actions"
    ][0]["data"]
    nonce = card_data["capability"]
    assert adapter._gw_resolve_approval(session_key, "once", request_id="req-slash") == 1
    assert nonce not in adapter._card_capabilities


@pytest.mark.asyncio
async def test_exec_card_not_minted_when_settle_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    _connect_cards(adapter)
    bridge = adapter_mod._import_bridge()
    monkeypatch.setattr(bridge, "send_reply", AsyncMock(return_value=None))
    session_key = "sess-late"
    result = await adapter.send_exec_approval(
        "conv-1",
        "danger",
        session_key,
        request_id="req-late",
    )
    assert result.success is True
    assert adapter._card_capabilities == {}
    card_data = bridge.send_reply.await_args.kwargs["reply"]["attachments"][0]["content"][
        "actions"
    ][0]["data"]
    rejected = await adapter._handle_card_action(_make_inbound(), card_data)
    assert rejected.status_code == 403
