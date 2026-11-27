"""Local harness for two walk cells that need an induced fault.

Stream-retry exhaustion and delivery-cache reclaim do not occur on a healthy
live stand-up. These tests drive the real adapter and bridge code with fake
transports so each cell can be graded without tenant changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import test_agent365_plugin as plugin_tests
from test_agent365_plugin import (
    _make_adapter,
    _make_inbound,
    _StubMessageEvent,
    _StubSessionSource,
    adapter_mod,
)
from test_delivery_retry import Delivery, response_is

from hermes_a365 import activity_bridge as bridge_mod

# Import the helper class by attribute so pytest does not collect it here.
_W = plugin_tests.TestEditMessage


@pytest.fixture
async def bridge_delivery() -> Any:
    async with AsyncExitStack() as stack:

        def make(**kwargs: Any) -> Delivery:
            item = Delivery(**kwargs)
            stack.push_async_callback(item.outbound.aclose)
            stack.push_async_callback(item.client.aclose)
            return item

        yield make


def _final_503() -> MagicMock:
    return MagicMock(status_code=503, text="", headers={"Retry-After": "0"}, json=lambda: {})


def _copilot_turn(monkeypatch: pytest.MonkeyPatch, chat: str, post: Any) -> tuple[Any, Any, Any]:
    a = _make_adapter(monkeypatch)
    inbound = _make_inbound(conv_id=chat, activity_id="act-1")
    inbound["conversation"]["conversationType"] = "groupChat"
    _W._wire_adapter(a, inbound=inbound)
    _W._patch_token_mint(monkeypatch)
    _W._no_sleep(monkeypatch)
    a._http_client.post = post
    bridge = adapter_mod._import_bridge()
    send_reply = AsyncMock(return_value=None)
    monkeypatch.setattr(bridge, "send_reply", send_reply)
    event = _StubMessageEvent(
        text="hi",
        source=_StubSessionSource(platform=None, chat_id=chat),
        message_id="act-1",
    )
    return a, event, send_reply


class TestStreamRetryExhaustionHarness:
    @pytest.mark.asyncio
    async def test_copilot_final_retry_exhaustion_posts_one_plain_answer(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        chat = "conv-359-exhaust"
        ledger: list[str] = []

        async def post(*_args: Any, **kwargs: Any) -> MagicMock:
            entity = kwargs["json"]["entities"][0]
            if entity.get("streamType") == "final":
                ledger.append("final")
                return _final_503()
            ledger.append("informative")
            return _W._http_result(201, "ka-359")

        a, event, send_reply = _copilot_turn(monkeypatch, chat, post)

        async def plain(**_kwargs: Any) -> None:
            ledger.append("plain")

        send_reply.side_effect = plain
        with caplog.at_level(logging.INFO, logger=adapter_mod.logger.name):
            await a.on_processing_start(event)
            a._turn_buffers[chat]["keepalive_first_interval_elapsed"] = True
            await a.send(chat_id=chat, content="Exhausted answer", reply_to=None)
            await a.on_processing_complete(event, outcome="success")

        assert ledger.count("final") == adapter_mod._ANSWER_FINAL_MAX_ATTEMPTS
        assert ledger.count("plain") == 1
        # Nothing reaches the stream after the plain answer.
        assert ledger[-1] == "plain"
        assert send_reply.await_args.kwargs["reply"]["text"] == "Exhausted answer"
        fallbacks = [r for r in caplog.records if "answer plain fallback" in r.message]
        assert len(fallbacks) == 1
        assert fallbacks[0].levelno == logging.WARNING
        assert "status=503" in fallbacks[0].message
        assert "reason=retry_exhausted" in fallbacks[0].message
        assert not [r for r in caplog.records if "delivery uncertain" in r.message]
        assert chat not in a._turn_buffers

    @pytest.mark.asyncio
    async def test_copilot_final_retry_exhaustion_after_ambiguous_post_stays_silent(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        chat = "conv-359-exhaust-ambiguous"
        finals = {"n": 0}

        async def post(*_args: Any, **kwargs: Any) -> MagicMock:
            entity = kwargs["json"]["entities"][0]
            if entity.get("streamType") != "final":
                return _W._http_result(201, "ka-359-amb")
            finals["n"] += 1
            if finals["n"] == 1:
                raise httpx.RemoteProtocolError("connection dropped")
            return _final_503()

        a, event, send_reply = _copilot_turn(monkeypatch, chat, post)
        with caplog.at_level(logging.INFO, logger=adapter_mod.logger.name):
            await a.on_processing_start(event)
            a._turn_buffers[chat]["keepalive"] = {
                "stream_id": "ka-359-amb",
                "sequence": 1,
                "opened_mono": time.monotonic(),
            }
            await a.send(chat_id=chat, content="Maybe landed", reply_to=None)
            await a.on_processing_complete(event, outcome="success")

        assert finals["n"] >= adapter_mod._ANSWER_FINAL_MAX_ATTEMPTS
        assert send_reply.await_count == 0
        uncertain = [r for r in caplog.records if "delivery uncertain" in r.message]
        assert len(uncertain) == 1
        assert uncertain[0].levelno == logging.WARNING
        assert "context=stream retry exhausted" in uncertain[0].message
        assert not [r for r in caplog.records if "answer plain fallback" in r.message]
        assert chat not in a._turn_buffers

    @pytest.mark.asyncio
    async def test_personal_final_retry_exhaustion_keeps_one_bubble(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-359-personal")
        opened = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-359"})
        failures = [_final_503() for _ in range(adapter_mod._STREAM_EDIT_MAX_ATTEMPTS)]
        post_mock = _W._wire_adapter(a, inbound=inbound, post_responses=[opened, *failures])
        _W._patch_token_mint(monkeypatch)
        _W._no_sleep(monkeypatch)
        caplog.set_level(logging.INFO)

        await a.edit_message("conv-359-personal", "m1", "Partial")
        result = await a.edit_message("conv-359-personal", "m1", "Final", finalize=True)

        assert result.success is False
        assert post_mock.await_count == 1 + adapter_mod._STREAM_EDIT_MAX_ATTEMPTS
        finals = [c.kwargs["json"] for c in post_mock.await_args_list[1:]]
        assert all(act["entities"][0].get("streamId") == "bf-359" for act in finals)
        assert all(act["type"] == "message" for act in finals)
        assert ("conv-359-personal", "m1") in a._streams
        retries = [r for r in caplog.records if "call_site=adapter.edit_message" in r.message]
        assert [f"attempt={n}/3" in r.message for n, r in enumerate(retries, 1)] == [True, True]
        assert any(
            r.message.endswith("http_status=503 attempt=3 bf_error_code=other")
            for r in caplog.records
        )


class TestDeliveryCacheReclaimHarness:
    async def test_bridge_redelivery_reclaims_expired_inflight_reply(
        self, bridge_delivery: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = bridge_mod._IdempotencyCache(ttl_seconds=60)
        old = bridge_delivery(cache=cache)
        new = bridge_delivery(cache=cache)
        old.block_at = "reply"
        caplog.set_level(logging.INFO, logger=bridge_mod.logger.name)

        first = asyncio.create_task(old.post())
        try:
            await old.entered.wait()
            stuck = cache.record(old.key)
            assert stuck.reply == "inflight"
            # Before expiry the connector retry is told to back off.
            response_is(await new.post(), 503, "delivery_in_progress")
            assert new.webhook_calls == 0
            assert cache.record(new.key) is stuck

            stuck.inserted_at -= cache.ttl_seconds
            response_is(await new.post(), 200, "replied")
            fresh = cache.record(new.key)
            assert fresh is not stuck
            assert fresh.reply == "sent"
            assert stuck.result is None
            assert new.webhook_calls == 1
            assert len(new.posts) == 1
        finally:
            old.resume.set()
            await first
        # The stale request settles without touching the replacement.
        response_is(first.result(), 200, "reply_unconfirmed")
        assert cache.record(new.key) is fresh
        assert fresh.reply == "sent"
        response_is(await new.post(), 200, "duplicate")
        assert cache.result_bytes == 0
        events = [r.message.split(" ", 1)[0] for r in caplog.records]
        assert "delivery_inflight" in events
        assert events.index("delivery_inflight") < len(events) - 1 - events[::-1].index(
            "delivery_claim"
        )

    @pytest.mark.parametrize("sibling_first", [False, True])
    def test_gateway_redelivery_after_ttl_dispatches_once_more(
        self, monkeypatch: pytest.MonkeyPatch, sibling_first: bool
    ) -> None:
        from fastapi.testclient import TestClient

        a = _make_adapter(monkeypatch)
        bridge = adapter_mod._import_bridge()
        monkeypatch.setattr(
            bridge,
            "validate_inbound_jwt",
            AsyncMock(return_value={"aud": "x", "iss": "y", "azp": "z"}),
        )
        a._http_client = MagicMock()
        client = TestClient(a.build_app())
        headers = {"Authorization": "Bearer a.b.c"}
        sibling = _make_inbound(activity_id="act-sibling")
        body = _make_inbound(activity_id="act-target")
        if sibling_first:
            # A younger head entry stops the sweep; only the per-key check expires.
            assert (
                client.post("/api/messages", json=sibling, headers=headers).json()["status"]
                == "dispatched"
            )
        assert (
            client.post("/api/messages", json=body, headers=headers).json()["status"]
            == "dispatched"
        )
        assert (
            client.post("/api/messages", json=body, headers=headers).json()["status"] == "duplicate"
        )
        cache = a._idempotency_cache
        target = list(cache.seen.values())[-1]
        target.inserted_at -= cache.ttl_seconds + (10.0 if sibling_first else 0.0)
        before = len(a._handled_events)
        assert (
            client.post("/api/messages", json=body, headers=headers).json()["status"]
            == "dispatched"
        )
        assert len(a._handled_events) == before + 1
        assert (
            client.post("/api/messages", json=body, headers=headers).json()["status"] == "duplicate"
        )
        assert len(a._handled_events) == before + 1
