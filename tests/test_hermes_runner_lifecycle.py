"""Gateway runner lifecycle tests on the vendored Hermes base adapter."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import importlib.util
from pathlib import Path
from typing import Any

import pytest

_BOOTSTRAP_PATH = Path(__file__).resolve().parent / "_hermes_bootstrap.py"
_spec = importlib.util.spec_from_file_location("hermes_a365_hermes_bootstrap", _BOOTSTRAP_PATH)
assert _spec and _spec.loader
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)

pytestmark = pytest.mark.skipif(
    not _bootstrap.try_import_real_gateway(),
    reason="Hermes vendor checkout unavailable",
)

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
GROUP_CONV = "19:group-chat@thread.v2"


@pytest.fixture(autouse=True)
def _isolate_vendor_adapter_module():  # type: ignore[no-untyped-def]
    import importlib
    import sys

    saved_base = sys.modules.get("gateway.platforms.base")
    saved_config = sys.modules.get("gateway.config")
    saved_session = sys.modules.get("gateway.session")
    adapter_name = "hermes_a365.plugin.adapter"
    saved_adapter = sys.modules.get(adapter_name)
    had_adapter = adapter_name in sys.modules
    yield
    if saved_config is not None:
        sys.modules["gateway.config"] = saved_config
    if saved_session is not None:
        sys.modules["gateway.session"] = saved_session
    if saved_base is not None:
        sys.modules["gateway.platforms.base"] = saved_base
    if had_adapter:
        if saved_adapter is not None:
            sys.modules[adapter_name] = saved_adapter
        else:
            sys.modules.pop(adapter_name, None)
            importlib.import_module(adapter_name)
    loaded = sys.modules.get(adapter_name)
    if loaded is not None:
        loaded._PENDING_MERGE_REGISTRY.clear()
    import test_agent365_plugin as _tap

    _tap._install_gateway_stubs(force=True)


async def _cancel_stray_vendor_session_tasks() -> None:
    current = asyncio.current_task()
    pending: list[asyncio.Task[Any]] = []
    for task in asyncio.all_tasks():
        if task is current or task.done():
            continue
        coro = task.get_coro()
        qual = getattr(coro, "__qualname__", "") or ""
        if "_process_message_background" in qual or "_drain_after" in qual:
            task.cancel()
            pending.append(task)
    if pending:
        with contextlib.suppress(Exception):
            await asyncio.wait(pending, timeout=2.0)


@pytest.fixture(autouse=True)
async def _isolate_vendor_session_tasks_between_lifecycle_tests():  # type: ignore[no-untyped-def]
    await _cancel_stray_vendor_session_tasks()
    yield
    await _cancel_stray_vendor_session_tasks()


def _load_adapter_mod() -> Any:
    from hermes_runner_harness import load_agent365_adapter_module

    return load_agent365_adapter_module()


def _make_adapter(monkeypatch: pytest.MonkeyPatch, adapter_mod: Any, **extra: Any) -> Any:
    from test_agent365_plugin import _StubPlatformConfig, _write_matching_allow_all_profile

    monkeypatch.setenv("A365_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("A365_APP_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("A365_BLUEPRINT_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("A365_ALLOW_ALL_USERS", "1")
    _write_matching_allow_all_profile()
    cfg_extra = {"slug": "test-agent", "port": 0, "group_sessions_per_user": False}
    cfg_extra.update(extra)
    return adapter_mod.Agent365Adapter(_StubPlatformConfig(extra=cfg_extra))


def _group_inbound(
    *,
    activity_id: str,
    text: str,
    user_aad: str,
    user_id: str,
) -> dict[str, Any]:
    return {
        "type": "message",
        "id": activity_id,
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/amer/x/",
        "conversation": {
            "id": GROUP_CONV,
            "conversationType": "group",
            "tenantId": "11111111-1111-1111-1111-111111111111",
        },
        "from": {"id": user_id, "aadObjectId": user_aad, "name": user_id},
        "recipient": {"id": "agent-1"},
        "text": text,
    }


def _wire_adapter(adapter: Any, adapter_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from hermes_runner_harness import install_bridge_stubs

    install_bridge_stubs(monkeypatch, adapter_mod)
    adapter._http_client = MagicMock()
    adapter._bridge_cfg = MagicMock()
    adapter._fmi_cache = MagicMock()
    adapter._user_cache = MagicMock()
    adapter._bf_token_cache = MagicMock()
    adapter._running = True


async def _shutdown_adapter(adapter: Any) -> None:
    for session_key in list(getattr(adapter, "_session_tasks", {}).keys()):
        with contextlib.suppress(Exception):
            await adapter.cancel_session_processing(session_key)
    for task in list(getattr(adapter, "_background_tasks", set())):
        task.cancel()
    pending = [task for task in getattr(adapter, "_background_tasks", set()) if not task.done()]
    if pending:
        with contextlib.suppress(Exception):
            await asyncio.wait(pending, timeout=2.0)
    adapter._pending_messages.clear()
    adapter._pending_turn_fifo.clear()


def test_vendor_revision_pin_is_verified() -> None:
    root = _bootstrap.ensure_hermes_vendor()
    assert root.is_dir()
    assert _bootstrap.expected_hermes_revision() == "bf0203a794a07aac3ff764972ab9087baa4e4f4e"


def test_vendor_revision_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_root = tmp_path / "hermes-agent"
    bad_root.mkdir()
    (bad_root / ".git").mkdir()
    monkeypatch.setattr(_bootstrap, "_VENDOR_ROOT", bad_root)
    monkeypatch.setattr(_bootstrap, "_HERMES_PIN", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setattr(_bootstrap, "_bootstrapped", False)
    with pytest.raises(RuntimeError, match="revision mismatch"):
        _bootstrap.ensure_hermes_vendor()


@pytest.mark.asyncio
async def test_fifo_preserves_arrival_order_for_mixed_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(monkeypatch, adapter_mod)
    session_key = f"agent:main:agent365:group:{GROUP_CONV}"
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    def _event(user: str, text: str, mid: str) -> Any:
        inbound = _group_inbound(activity_id=mid, text=text, user_aad=user, user_id=user)
        source = SessionSource(
            platform=adapter_mod.Platform("agent365"),
            chat_id=GROUP_CONV,
            chat_type="group",
            user_id=user,
        )
        event = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source, message_id=mid
        )
        origin = adapter._turn_origin_from_activity(inbound, session_key)
        adapter._stamp_turn_origin_on_event(event, origin)
        return event

    b1 = _event(USER_B, "b1", "b1")
    a2 = _event(USER_A, "a2", "a2")
    b2 = _event(USER_B, "b2", "b2")
    adapter._agent365_admit_pending_turn_event(session_key, b1, merge_text=True)
    adapter._agent365_admit_pending_turn_event(session_key, a2, merge_text=True)
    adapter._agent365_admit_pending_turn_event(session_key, b2, merge_text=True)
    fifo = list(adapter._pending_turn_fifo[session_key])
    assert [e.message_id for e in fifo] == ["a2", "b2"]
    assert adapter._pending_messages[session_key].message_id == "b1"


def _enqueue_gateway_exec_request(session_key: str, request_id: str) -> None:
    from tools import approval as approval_mod
    from tools.approval_gateway_wait import _ApprovalEntry

    entry = _ApprovalEntry({"request_id": request_id, "session_key": session_key})
    with approval_mod._lock:
        approval_mod._gateway_queues.setdefault(session_key, []).append(entry)


@pytest.mark.asyncio
async def test_merge_registry_drops_adapter_after_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.platforms.base as base_mod

    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(monkeypatch, adapter_mod)
    pending_id = id(adapter._pending_messages)
    assert adapter_mod._PENDING_MERGE_REGISTRY[pending_id]() is adapter
    adapter._unregister_pending_merge_guard()
    assert pending_id not in adapter_mod._PENDING_MERGE_REGISTRY
    del adapter
    gc.collect()
    assert adapter_mod._PENDING_MERGE_REGISTRY.get(pending_id) is None
    assert base_mod.merge_pending_message_event is not None


@pytest.mark.asyncio
async def test_aba_http_cards_bind_to_turn_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _cancel_stray_vendor_session_tasks()
    import httpx

    aba_conv = "19:aba-card-origin@thread.v2"
    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(monkeypatch, adapter_mod)
    _wire_adapter(adapter, adapter_mod, monkeypatch)

    def _aba_inbound(**kwargs: Any) -> dict[str, Any]:
        activity = _group_inbound(**kwargs)
        activity["conversation"] = {
            "id": aba_conv,
            "conversationType": "group",
            "tenantId": "11111111-1111-1111-1111-111111111111",
        }
        return activity

    handler_counts: dict[str, int] = {}
    cards_by_user: dict[str, dict[str, Any]] = {}
    a_ready = asyncio.Event()
    b_posted = asyncio.Event()
    release_a = asyncio.Event()
    done = asyncio.Event()

    async def gateway_message_handler(event: Any) -> None:
        mid = str(getattr(event, "message_id", "") or "")
        handler_counts[mid] = handler_counts.get(mid, 0) + 1
        session_key = adapter._session_key_for(event) or f"agent365:{aba_conv}"
        origin_user = adapter._event_turn_origin(event)["user_id"]
        if mid == "act-a1":
            a_ready.set()
            await b_posted.wait()
            await release_a.wait()
            return
        if mid == "act-b":
            cards_by_user.setdefault(origin_user, {})
            cards_by_user[origin_user]["slash"] = await adapter.send_slash_confirm(
                aba_conv, "Confirm", "Run?", session_key, "cfm-b"
            )
            cards_by_user[origin_user]["clarify"] = await adapter.send_clarify(
                aba_conv, "Pick", ["Alpha"], "clr-b", session_key
            )
            return
        if mid != "act-a2":
            return
        _enqueue_gateway_exec_request(session_key, "req-a2")
        cards_by_user.setdefault(origin_user, {})
        cards_by_user[origin_user]["exec"] = await adapter.send_exec_approval(
            aba_conv, "danger", session_key, request_id="req-a2"
        )
        done.set()

    adapter.set_message_handler(gateway_message_handler)
    try:
        transport = httpx.ASGITransport(app=adapter.build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            a_task = asyncio.create_task(
                client.post(
                    "/api/messages",
                    json=_aba_inbound(
                        activity_id="act-a1",
                        text="start-a",
                        user_aad=USER_A,
                        user_id="user-a",
                    ),
                    headers={"Authorization": "Bearer a.b.c"},
                )
            )
            await asyncio.wait_for(a_ready.wait(), timeout=15.0)
            await client.post(
                "/api/messages",
                json=_aba_inbound(
                    activity_id="act-b", text="from-b", user_aad=USER_B, user_id="user-b"
                ),
                headers={"Authorization": "Bearer a.b.c"},
            )
            b_posted.set()
            await client.post(
                "/api/messages",
                json=_aba_inbound(
                    activity_id="act-a2",
                    text="need-approval",
                    user_aad=USER_A,
                    user_id="user-a",
                ),
                headers={"Authorization": "Bearer a.b.c"},
            )
            release_a.set()
            await a_task
            for _ in range(150):
                if handler_counts.get("act-a2", 0) == 1:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("expected act-a2 handler to run once")
            await asyncio.wait_for(done.wait(), timeout=15.0)

        assert handler_counts.get("act-a1", 0) == 1
        assert handler_counts.get("act-b", 0) == 1
        assert handler_counts.get("act-a2", 0) == 1
        assert len(cards_by_user[USER_B]) == 2
        assert len(cards_by_user[USER_A]) == 1
        assert cards_by_user[USER_B]["slash"].success
        assert cards_by_user[USER_B]["clarify"].success
        assert cards_by_user[USER_A]["exec"].success
        for nonce, capability in list(adapter._card_capabilities.items()):
            value = {
                "hermes_kind": capability["kind"],
                "capability": nonce,
                "choice_id": next(iter(capability["choices"])),
            }
            conv_id = str(capability.get("conversation_id") or aba_conv)
            owner = _aba_inbound(
                activity_id=f"submit-owner-{nonce}",
                text="",
                user_aad=capability["user_id"],
                user_id="owner",
            )
            owner["conversation"]["id"] = conv_id
            other = _aba_inbound(
                activity_id=f"submit-other-{nonce}",
                text="",
                user_aad=USER_B if capability["user_id"] == USER_A else USER_A,
                user_id="other",
            )
            other["conversation"]["id"] = conv_id
            if capability.get("kind") == adapter_mod._CARD_KIND_EXEC_APPROVAL:
                resolver = capability.get("resolver") or {}
                _enqueue_gateway_exec_request(
                    str(resolver.get("session_key") or ""),
                    str(resolver.get("request_id") or ""),
                )
            assert (await adapter._handle_card_action(other, value)).status_code == 403
            assert (await adapter._handle_card_action(owner, value)).status_code == 200
    finally:
        await _shutdown_adapter(adapter)


@pytest.mark.asyncio
async def test_stop_clears_pending_turn_queue_before_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(monkeypatch, adapter_mod)
    _wire_adapter(adapter, adapter_mod, monkeypatch)
    seen: list[str] = []

    async def gateway_message_handler(event: Any) -> None:
        seen.append(str(getattr(event, "message_id", "")))
        if getattr(event, "message_id", "") == "act-a1":
            await asyncio.sleep(0.2)

    adapter.set_message_handler(gateway_message_handler)
    transport = httpx.ASGITransport(app=adapter.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        a_task = asyncio.create_task(
            client.post(
                "/api/messages",
                json=_group_inbound(
                    activity_id="act-a1",
                    text="hold",
                    user_aad=USER_A,
                    user_id="user-a",
                ),
                headers={"Authorization": "Bearer a.b.c"},
            )
        )
        await asyncio.sleep(0.05)
        await client.post(
            "/api/messages",
            json=_group_inbound(
                activity_id="act-b", text="queued", user_aad=USER_B, user_id="user-b"
            ),
            headers={"Authorization": "Bearer a.b.c"},
        )
        await adapter._retire_chat_agent_turns(GROUP_CONV)
        await client.post(
            "/api/messages",
            json=_group_inbound(
                activity_id="act-new",
                text="fresh",
                user_aad=USER_A,
                user_id="user-a",
            ),
            headers={"Authorization": "Bearer a.b.c"},
        )
        await a_task
        await asyncio.sleep(0.3)

    assert "act-b" not in seen
    assert "act-new" in seen


async def _run_teardown_clears_pending_scenario(
    monkeypatch: pytest.MonkeyPatch,
    *,
    teardown: Any,
) -> None:
    import httpx

    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(monkeypatch, adapter_mod)
    _wire_adapter(adapter, adapter_mod, monkeypatch)
    seen: list[str] = []
    a_release = asyncio.Event()

    async def gateway_message_handler(event: Any) -> None:
        mid = str(getattr(event, "message_id", "") or "")
        seen.append(mid)
        if mid == "act-a1":
            await a_release.wait()

    adapter.set_message_handler(gateway_message_handler)
    transport = httpx.ASGITransport(app=adapter.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        a_task = asyncio.create_task(
            client.post(
                "/api/messages",
                json=_group_inbound(
                    activity_id="act-a1",
                    text="hold",
                    user_aad=USER_A,
                    user_id="user-a",
                ),
                headers={"Authorization": "Bearer a.b.c"},
            )
        )
        await asyncio.sleep(0.05)
        await client.post(
            "/api/messages",
            json=_group_inbound(
                activity_id="act-b", text="queued", user_aad=USER_B, user_id="user-b"
            ),
            headers={"Authorization": "Bearer a.b.c"},
        )
        await client.post(
            "/api/messages",
            json=_group_inbound(
                activity_id="act-a2",
                text="also-queued",
                user_aad=USER_A,
                user_id="user-a",
            ),
            headers={"Authorization": "Bearer a.b.c"},
        )
        await teardown(adapter)
        await client.post(
            "/api/messages",
            json=_group_inbound(
                activity_id="act-new",
                text="fresh",
                user_aad=USER_A,
                user_id="user-a",
            ),
            headers={"Authorization": "Bearer a.b.c"},
        )
        a_release.set()
        await a_task
        await asyncio.sleep(0.3)

    assert "act-b" not in seen
    assert "act-a2" not in seen
    assert "act-new" in seen


@pytest.mark.asyncio
async def test_chat_teardown_clears_pending_turn_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def teardown(adapter: Any) -> None:
        await adapter._teardown_chat_state_impl(GROUP_CONV)

    await _run_teardown_clears_pending_scenario(monkeypatch, teardown=teardown)


@pytest.mark.asyncio
async def test_disconnect_clears_pending_turn_state_before_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections import deque

    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(monkeypatch, adapter_mod)
    session_key = f"agent:main:agent365:group:{GROUP_CONV}"
    adapter._pending_messages[session_key] = object()
    adapter._pending_turn_fifo[session_key] = deque([object()])
    adapter._http_client = object()

    async def _fast_disconnect(_http_client: Any) -> None:
        return None

    monkeypatch.setattr(adapter, "_disconnect", _fast_disconnect)
    await adapter.disconnect()
    assert not adapter._pending_messages
    assert not adapter._pending_turn_fifo


@pytest.mark.asyncio
async def test_uninstall_teardown_clears_pending_turn_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def teardown(adapter: Any) -> None:
        await adapter._teardown_chat_state_impl(GROUP_CONV)

    await _run_teardown_clears_pending_scenario(monkeypatch, teardown=teardown)


@pytest.mark.asyncio
async def test_pending_turn_fifo_rejects_when_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(
        monkeypatch, adapter_mod, pending_turn_fifo_max=1, group_sessions_per_user=False
    )
    _wire_adapter(adapter, adapter_mod, monkeypatch)
    session_key = f"agent:main:agent365:group:{GROUP_CONV}"
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    replies: list[str] = []

    async def capture_send(chat_id: str, text: str, **_kwargs: Any) -> Any:
        replies.append(text)
        return adapter_mod.SendResult(success=True)

    monkeypatch.setattr(adapter, "send", capture_send)

    def _event(mid: str, user: str) -> Any:
        inbound = _group_inbound(activity_id=mid, text=mid, user_aad=user, user_id=user)
        source = SessionSource(
            platform=adapter_mod.Platform("agent365"),
            chat_id=GROUP_CONV,
            chat_type="group",
            user_id=user,
        )
        event = MessageEvent(text=mid, message_type=MessageType.TEXT, source=source, message_id=mid)
        origin = adapter._turn_origin_from_activity(inbound, session_key)
        adapter._stamp_turn_origin_on_event(event, origin)
        return event

    adapter._pending_messages[session_key] = _event("slot", USER_A)
    assert adapter._agent365_admit_pending_turn_event(session_key, _event("q1", USER_B))
    adapter._agent365_merge_pending_message_event(session_key, _event("q2", USER_A))
    await asyncio.sleep(0.05)
    assert replies
    assert "busy" in replies[0].lower()


@pytest.mark.asyncio
async def test_pending_turn_fifo_isolated_per_session_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(monkeypatch, adapter_mod, group_sessions_per_user=True)
    session_a = f"agent:main:agent365:group:{GROUP_CONV}:user:{USER_A}"
    session_b = f"agent:main:agent365:group:{GROUP_CONV}:user:{USER_B}"
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    def _event(session_key: str, mid: str, user: str) -> Any:
        inbound = _group_inbound(activity_id=mid, text=mid, user_aad=user, user_id=user)
        source = SessionSource(
            platform=adapter_mod.Platform("agent365"),
            chat_id=GROUP_CONV,
            chat_type="group",
            user_id=user,
        )
        event = MessageEvent(text=mid, message_type=MessageType.TEXT, source=source, message_id=mid)
        origin = adapter._turn_origin_from_activity(inbound, session_key)
        adapter._stamp_turn_origin_on_event(event, origin)
        return event

    adapter._agent365_admit_pending_turn_event(session_a, _event(session_a, "a1", USER_A))
    adapter._agent365_admit_pending_turn_event(session_b, _event(session_b, "b1", USER_B))
    assert adapter._pending_messages[session_a].message_id == "a1"
    assert adapter._pending_messages[session_b].message_id == "b1"


@pytest.mark.asyncio
async def test_sustained_pending_arrivals_all_run_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    sustained_conv = "19:sustained-pending@thread.v2"
    adapter_mod = _load_adapter_mod()
    adapter = _make_adapter(monkeypatch, adapter_mod)
    _wire_adapter(adapter, adapter_mod, monkeypatch)
    handler_counts: dict[str, int] = {}
    release = asyncio.Event()
    expected = ["msg-0", "msg-1"]

    async def gateway_message_handler(event: Any) -> None:
        mid = str(getattr(event, "message_id", "") or "")
        handler_counts[mid] = handler_counts.get(mid, 0) + 1
        if mid == "msg-0":
            await release.wait()

    def _inbound(mid: str, user: str) -> dict[str, Any]:
        return _group_inbound(
            activity_id=mid,
            text=mid,
            user_aad=user,
            user_id="user-x",
        ) | {
            "conversation": {
                "id": sustained_conv,
                "conversationType": "group",
                "tenantId": "11111111-1111-1111-1111-111111111111",
            }
        }

    adapter.set_message_handler(gateway_message_handler)
    try:
        transport = httpx.ASGITransport(app=adapter.build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(
                "/api/messages",
                json=_inbound("msg-0", USER_A),
                headers={"Authorization": "Bearer a.b.c"},
            )
            await asyncio.sleep(0.05)
            for mid in expected[1:]:
                await client.post(
                    "/api/messages",
                    json=_inbound(mid, USER_B),
                    headers={"Authorization": "Bearer a.b.c"},
                )
                release.set()
                for _ in range(300):
                    if all(handler_counts.get(mid, 0) == 1 for mid in expected):
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError(
                        f"expected one handler call per message, got {handler_counts!r}"
                    )
    finally:
        await _shutdown_adapter(adapter)
