"""Unit tests for shared outbound HTTP retry (#190)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import test_agent365_plugin as plugin_tests

from hermes_a365.outbound_retry import (
    OUTBOUND_BF_REPLY_TOTAL_BUDGET_SEC,
    OUTBOUND_DEFAULT_MAX_ATTEMPTS,
    OUTBOUND_HTTP_TIMEOUT_DEFAULT_SEC,
    OUTBOUND_ONEDRIVE_UPLOAD_ATTEMPT_TIMEOUT_SEC,
    OUTBOUND_RETRY_AFTER_CAP_SEC,
    OUTBOUND_TYPING_MAX_ATTEMPTS,
    OUTBOUND_WEBHOOK_ACK_DEADLINE_SEC,
    clamp_retry_delay_sec,
    compute_backoff_delay_sec,
    is_retryable_http_status,
    outbound_attempt_timeout_sec,
    parse_retry_after_seconds,
    request_with_outbound_retry,
)


def _resp(status: int, retry_after: str | None = None) -> MagicMock:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return MagicMock(status_code=status, headers=headers)


class TestParseRetryAfter:
    def test_delta_seconds(self) -> None:
        assert parse_retry_after_seconds(_resp(429, "2"), cap_sec=30.0) == 2.0

    def test_http_date(self) -> None:
        future = datetime.now(UTC) + timedelta(seconds=5)
        raw = format_datetime(future, usegmt=True)
        now = datetime.now(UTC).timestamp()
        delay = parse_retry_after_seconds(_resp(429, raw), cap_sec=30.0, now=now)
        assert 4.0 <= delay <= 6.0

    def test_cap_applied(self) -> None:
        assert parse_retry_after_seconds(_resp(429, "120"), cap_sec=30.0) == 30.0

    def test_keepalive_cap_at_sixty(self) -> None:
        assert parse_retry_after_seconds(_resp(429, "120"), cap_sec=60.0) == 60.0

    def test_missing_header_returns_zero(self) -> None:
        assert parse_retry_after_seconds(_resp(429), cap_sec=30.0) == 0.0

    def test_negative_delta_seconds(self) -> None:
        assert parse_retry_after_seconds(_resp(429, "-5"), cap_sec=30.0) == 0.0

    def test_past_http_date(self) -> None:
        past = datetime.now(UTC) - timedelta(seconds=30)
        raw = format_datetime(past, usegmt=True)
        assert parse_retry_after_seconds(_resp(429, raw), cap_sec=30.0) == 0.0

    def test_garbage_value(self) -> None:
        assert parse_retry_after_seconds(_resp(429, "not-a-date"), cap_sec=30.0) == 0.0


class TestIsRetryable:
    def test_429_always(self) -> None:
        assert is_retryable_http_status(429, allow_transient_5xx=False)

    def test_503_only_when_allowed(self) -> None:
        assert not is_retryable_http_status(503, allow_transient_5xx=False)
        assert is_retryable_http_status(503, allow_transient_5xx=True)

    def test_400_not_retryable(self) -> None:
        assert not is_retryable_http_status(400, allow_transient_5xx=True)


class TestClampRetryDelay:
    def test_clamps_to_remaining_budget(self) -> None:
        assert clamp_retry_delay_sec(10.0, 3.0) == 3.0

    def test_zero_budget_returns_zero(self) -> None:
        assert clamp_retry_delay_sec(2.0, 0.0) == 0.0


class TestOutboundAttemptTimeout:
    def test_onedrive_cap_exceeds_bf_default_when_budget_allows(self) -> None:
        import time

        deadline = time.monotonic() + 120.0
        bf_timeout = outbound_attempt_timeout_sec(
            deadline,
            attempt_cap_sec=OUTBOUND_HTTP_TIMEOUT_DEFAULT_SEC,
        )
        upload_timeout = outbound_attempt_timeout_sec(
            deadline,
            attempt_cap_sec=OUTBOUND_ONEDRIVE_UPLOAD_ATTEMPT_TIMEOUT_SEC,
        )
        assert bf_timeout == OUTBOUND_HTTP_TIMEOUT_DEFAULT_SEC
        assert upload_timeout == OUTBOUND_ONEDRIVE_UPLOAD_ATTEMPT_TIMEOUT_SEC
        assert upload_timeout > bf_timeout

    @pytest.mark.asyncio
    async def test_upload_retry_uses_sixty_second_attempt_cap(self) -> None:
        seen: list[float] = []

        async def send_once(timeout_sec: float) -> MagicMock:
            seen.append(timeout_sec)
            return _resp(201)

        await request_with_outbound_retry(
            send_once,
            call_site="test.upload",
            max_attempts=1,
            total_budget_sec=120.0,
            attempt_timeout_sec=OUTBOUND_ONEDRIVE_UPLOAD_ATTEMPT_TIMEOUT_SEC,
        )
        assert seen == [OUTBOUND_ONEDRIVE_UPLOAD_ATTEMPT_TIMEOUT_SEC]
        assert seen[0] > OUTBOUND_HTTP_TIMEOUT_DEFAULT_SEC


@pytest.mark.asyncio
class TestRequestWithOutboundRetry:
    async def test_retry_after_seconds_waits_then_succeeds(self) -> None:
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        calls = 0

        async def send_once(_timeout_sec: float) -> MagicMock:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _resp(429, "2")
            return _resp(200)

        result = await request_with_outbound_retry(
            send_once,
            call_site="test",
            max_attempts=3,
            sleep=sleep,
        )
        assert result.status_code == 200
        assert calls == 2
        assert sleeps == [2.0]

    async def test_gives_up_after_max_attempts(self) -> None:
        calls = 0

        async def send_once(_timeout_sec: float) -> MagicMock:
            nonlocal calls
            calls += 1
            return _resp(429, "1")

        result = await request_with_outbound_retry(
            send_once,
            call_site="test",
            max_attempts=3,
            sleep=AsyncMock(),
        )
        assert result.status_code == 429
        assert calls == 3

    async def test_no_retry_on_400(self) -> None:
        calls = 0

        async def send_once(_timeout_sec: float) -> MagicMock:
            nonlocal calls
            calls += 1
            return _resp(400)

        result = await request_with_outbound_retry(
            send_once,
            call_site="test",
            max_attempts=OUTBOUND_DEFAULT_MAX_ATTEMPTS,
            sleep=AsyncMock(),
        )
        assert result.status_code == 400
        assert calls == 1

    async def test_no_retry_on_503_by_default(self) -> None:
        calls = 0

        async def send_once(_timeout_sec: float) -> MagicMock:
            nonlocal calls
            calls += 1
            return _resp(503)

        result = await request_with_outbound_retry(
            send_once,
            call_site="test",
            max_attempts=3,
            sleep=AsyncMock(),
        )
        assert result.status_code == 503
        assert calls == 1

    async def test_transient_5xx_when_enabled(self) -> None:
        calls = 0

        async def send_once(_timeout_sec: float) -> MagicMock:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _resp(503)
            return _resp(201)

        result = await request_with_outbound_retry(
            send_once,
            call_site="test",
            max_attempts=2,
            allow_transient_5xx=True,
            sleep=AsyncMock(),
        )
        assert result.status_code == 201
        assert calls == 2

    async def test_fail_fast_when_sleep_would_leave_no_attempt_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = 0.0

        def monotonic() -> float:
            return clock

        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            nonlocal clock
            sleeps.append(delay)
            clock += delay

        monkeypatch.setattr("hermes_a365.outbound_retry.time.monotonic", monotonic)

        async def send_once(_timeout_sec: float) -> MagicMock:
            return _resp(429, "1")

        result = await request_with_outbound_retry(
            send_once,
            call_site="test",
            max_attempts=4,
            total_budget_sec=1.5,
            sleep=sleep,
        )
        assert result.status_code == 429
        assert sleeps == []

    async def test_total_sleep_stays_within_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        clock = 0.0

        def monotonic() -> float:
            return clock

        monkeypatch.setattr("hermes_a365.outbound_retry.time.monotonic", monotonic)

        async def sleep(delay: float) -> None:
            nonlocal clock
            sleeps.append(delay)
            clock += delay

        calls = 0
        attempt_starts: list[float] = []

        async def send_once(_timeout_sec: float) -> MagicMock:
            nonlocal calls
            attempt_starts.append(clock)
            calls += 1
            return _resp(429, "20")

        result = await request_with_outbound_retry(
            send_once,
            call_site="test",
            max_attempts=5,
            total_budget_sec=10.0,
            sleep=sleep,
        )
        assert result.status_code == 429
        assert sum(sleeps) <= 10.0
        assert all(s <= 10.0 for s in sleeps)
        assert all(t < 10.0 for t in attempt_starts)

    async def test_retry_after_exceeding_budget_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = 0.0

        def monotonic() -> float:
            return clock

        calls = 0

        async def send_once(_timeout_sec: float) -> MagicMock:
            nonlocal calls
            calls += 1
            return _resp(429, "120")

        monkeypatch.setattr("hermes_a365.outbound_retry.time.monotonic", monotonic)
        result = await request_with_outbound_retry(
            send_once,
            call_site="test",
            max_attempts=4,
            total_budget_sec=5.0,
            sleep=AsyncMock(),
        )
        assert result.status_code == 429
        assert calls == 1


class TestComputeBackoff:
    def test_uses_retry_after_when_present(self) -> None:
        delay = compute_backoff_delay_sec(1, _resp(429, "3"))
        assert delay == 3.0

    def test_fallback_when_header_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hermes_a365.outbound_retry.random.uniform",
            lambda _a, _b: 1.5,
        )
        delay = compute_backoff_delay_sec(
            1,
            _resp(503),
            fallback_delay_sec=0.0,
            max_exponential_sec=OUTBOUND_RETRY_AFTER_CAP_SEC,
        )
        assert delay == 1.5


@pytest.mark.asyncio
class TestCallSiteFamilies:
    """One integration-style test per wired call-site family."""

    async def test_activity_bridge_send_reply_retries_429(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365 import activity_bridge as bridge

        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr(
            "hermes_a365.outbound_retry.asyncio.sleep",
            fake_sleep,
        )
        posts = [
            MagicMock(status_code=429, headers={"Retry-After": "1"}, text="t"),
            MagicMock(status_code=200, headers={}, text="ok", json=lambda: {"id": "x"}),
        ]
        client = MagicMock()
        client.post = AsyncMock(side_effect=posts)
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("tok", "A")),
        )
        inbound = {
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "conversation": {"id": "c1"},
            "id": "a1",
        }
        cfg = MagicMock()
        await bridge.send_reply(
            inbound=inbound,
            reply={"type": "message", "text": "hi"},
            cfg=cfg,
            client=client,
            fmi_cache=MagicMock(),
            user_cache=MagicMock(),
            validated_path="A",
        )
        assert client.post.await_count == 2
        assert sleeps == [1.0]
        assert sum(sleeps) <= OUTBOUND_BF_REPLY_TOTAL_BUDGET_SEC

    async def test_activity_bridge_send_reply_does_not_retry_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365 import activity_bridge as bridge
        from hermes_a365.activity_bridge import ReplyPostError

        client = MagicMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=503, headers={}, text=""))
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("tok", "A")),
        )
        inbound = {
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "conversation": {"id": "c1"},
            "id": "a1",
        }
        with pytest.raises(ReplyPostError):
            await bridge.send_reply(
                inbound=inbound,
                reply={"type": "message", "text": "hi"},
                cfg=MagicMock(),
                client=client,
                fmi_cache=MagicMock(),
                user_cache=MagicMock(),
                validated_path="A",
            )
        assert client.post.await_count == 1

    async def test_adapter_post_activity_typing_at_most_one_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = plugin_tests._make_adapter(monkeypatch)
        inbound = plugin_tests._make_inbound(conv_id="conv-typing")
        posts = [
            MagicMock(status_code=429, headers={"Retry-After": "1"}, text=""),
            MagicMock(status_code=429, headers={"Retry-After": "1"}, text=""),
        ]
        post_mock = plugin_tests.TestEditMessage._wire_adapter(
            a, inbound=inbound, post_responses=posts
        )
        plugin_tests.TestEditMessage._patch_token_mint(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        sleep_mock = AsyncMock()
        monkeypatch.setattr("hermes_a365.outbound_retry.asyncio.sleep", sleep_mock)

        with pytest.raises(RuntimeError, match="_post_activity"):
            await a._post_activity(
                inbound=inbound,
                activity={"type": "typing"},
            )
        assert post_mock.await_count == OUTBOUND_TYPING_MAX_ATTEMPTS
        assert sleep_mock.await_count == 1

    async def test_proactive_send_does_not_retry_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes_a365.plugin.conversations import ConversationRef

        a = plugin_tests._make_adapter(monkeypatch)
        inbound = plugin_tests._make_inbound(conv_id="conv-pro")
        a._conversations.upsert(ConversationRef.from_activity(inbound), now=100.0)
        post_mock = plugin_tests.TestEditMessage._wire_adapter(
            a,
            inbound=inbound,
            post_responses=[MagicMock(status_code=503, headers={}, text="")],
        )
        plugin_tests.TestEditMessage._patch_token_mint(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)

        result = await a._send_proactive("conv-pro", "hello")
        assert result.success is False
        assert post_mock.await_count == 1

    async def test_file_consent_upload_retries_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        a = plugin_tests._make_adapter(monkeypatch)
        pending_tasks: list[asyncio.Task[Any]] = []
        real_create_task = asyncio.create_task

        def _capture_upload_task(coro: Any) -> asyncio.Task[Any]:
            task = real_create_task(coro)
            pending_tasks.append(task)
            return task

        monkeypatch.setattr(asyncio, "create_task", _capture_upload_task)
        inbound = plugin_tests._make_inbound(conv_id="conv-file")
        puts = [
            MagicMock(status_code=429, headers={"Retry-After": "1"}),
            MagicMock(status_code=201, headers={}),
        ]
        client = MagicMock()
        client.put = AsyncMock(side_effect=puts)
        a._http_client = client
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        monkeypatch.setattr("hermes_a365.outbound_retry.asyncio.sleep", AsyncMock())
        monkeypatch.setattr(
            a,
            "_remove_file_consent_card",
            AsyncMock(),
        )
        monkeypatch.setattr(
            a,
            "validate_media_delivery_path",
            lambda _p: "/tmp/x",
        )
        monkeypatch.setattr(
            "hermes_a365.plugin.adapter._read_file_bounded",
            lambda _p, _c: b"data",
        )
        monkeypatch.setattr(
            "hermes_a365.plugin.adapter._is_allowed_file_host",
            lambda _u, _a: True,
        )
        a._file_host_allowlist = ("tenant.sharepoint.com",)
        activity = {
            "type": "invoke",
            "conversation": inbound["conversation"],
            "from": inbound["from"],
            "serviceUrl": inbound["serviceUrl"],
            "value": {
                "action": "accept",
                "context": {"consentId": "abc"},
                "uploadInfo": {
                    "uploadUrl": "https://tenant.sharepoint.com/upload",
                    "contentUrl": "https://tenant.sharepoint.com/x",
                    "name": "f.txt",
                },
            },
        }
        a._pending_file_uploads["abc"] = {
            "path": "/tmp/x",
            "name": "f.txt",
            "size": 4,
            "sha256": __import__("hashlib").sha256(b"data").hexdigest(),
            "conversation_id": inbound["conversation"]["id"],
            "user_id": inbound["from"]["id"],
            "service_url": inbound["serviceUrl"],
            "validated_path": "A",
            "lifecycle_generation": a._lifecycle_generation,
            "chat_generation": a._chat_generation(inbound["conversation"]["id"]),
            "created_at": __import__("time").time(),
        }
        resp = await a._handle_file_consent(activity, validated_path="A")
        assert resp.status == 200
        if pending_tasks:
            await asyncio.gather(*pending_tasks)
        assert client.put.await_count == 2

    async def test_stream_edit_honours_retry_after_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.plugin.adapter as adapter_mod

        a = plugin_tests._make_adapter(monkeypatch)
        inbound = plugin_tests._make_inbound(conv_id="conv-stream")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-s"})
        rate = MagicMock(
            status_code=429,
            text="",
            headers={"Retry-After": "0"},
            json=lambda: {"error": {"code": "TooManyRequests", "message": "wait"}},
        )
        ok = MagicMock(status_code=202, text="", json=lambda: {})
        plugin_tests.TestEditMessage._wire_adapter(
            a, inbound=inbound, post_responses=[first, rate, ok]
        )
        plugin_tests.TestEditMessage._patch_token_mint(monkeypatch)
        sleep_mock = plugin_tests.TestEditMessage._no_sleep(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)

        await a.edit_message("conv-stream", "m1", "A")
        result = await a.edit_message("conv-stream", "m1", "A B")
        assert result.success is True
        assert sleep_mock.await_args_list[-1].args[0] == adapter_mod._STREAMING_MIN_GAP_SEC


class TestStreamEditRetryCap:
    def test_delay_cap_shrinks_as_loop_clock_advances(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        a = plugin_tests._make_adapter(monkeypatch)
        state = {"opened_ts": 100.0}
        clock = 100.0
        loop = MagicMock()
        loop.time = lambda: clock
        monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)

        first = a._stream_edit_retry_delay_sec(1, _resp(429, "30"), state)
        assert first is not None
        clock += 110.0
        second = a._stream_edit_retry_delay_sec(1, _resp(429, "30"), state)
        assert second is not None
        assert second < first

    def test_no_retry_when_remaining_below_streaming_gap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        import hermes_a365.plugin.adapter as adapter_mod

        a = plugin_tests._make_adapter(monkeypatch)
        margin = adapter_mod._ANSWER_FINAL_STREAM_CAP_MARGIN_SEC
        cap = adapter_mod._TURN_BUFFER_KEEPALIVE_STREAM_CAP_SEC
        state = {"opened_ts": 0.0}
        clock = cap - margin - 0.25
        loop = MagicMock()
        loop.time = lambda: clock
        monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)

        assert a._stream_edit_retry_delay_sec(1, _resp(429, "2"), state) is None


@pytest.mark.asyncio
class TestWebhookReplyBudget:
    async def test_send_reply_uses_webhook_budget_when_requested(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_a365 import activity_bridge as bridge

        seen: dict[str, float | None] = {}

        async def capture_retry(
            send_once: Any,
            *,
            call_site: str,
            max_attempts: int = 4,
            allow_transient_5xx: bool = False,
            allow_500: bool = False,
            retry_after_cap: float = 30.0,
            fallback_delay_sec: float = 0.0,
            deadline: float | None = None,
            total_budget_sec: float | None = None,
            sleep: Any = None,
            before_retry: Any = None,
        ) -> Any:
            seen["deadline"] = deadline
            seen["budget"] = total_budget_sec
            return await send_once(15.0)

        monkeypatch.setattr(
            "hermes_a365.outbound_retry.request_with_outbound_retry",
            capture_retry,
        )
        monkeypatch.setattr(
            bridge,
            "acquire_reply_token",
            AsyncMock(return_value=("tok", "A")),
        )
        client = MagicMock()
        client.post = AsyncMock(
            return_value=MagicMock(status_code=200, headers={}, text="", json=lambda: {})
        )
        inbound = {
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "conversation": {"id": "c1"},
            "id": "a1",
        }
        ack_deadline = 9999.0
        await bridge.send_reply(
            inbound=inbound,
            reply={"type": "message", "text": "hi"},
            cfg=MagicMock(),
            client=client,
            fmi_cache=MagicMock(),
            user_cache=MagicMock(),
            validated_path="A",
            deadline=ack_deadline,
        )
        assert seen["deadline"] == ack_deadline
        assert seen["budget"] is None
        assert OUTBOUND_WEBHOOK_ACK_DEADLINE_SEC < OUTBOUND_BF_REPLY_TOTAL_BUDGET_SEC


@pytest.mark.asyncio
class TestAckPathTiming:
    async def test_file_consent_invoke_acks_before_upload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import hashlib
        import time

        a = plugin_tests._make_adapter(monkeypatch)
        inbound = plugin_tests._make_inbound(conv_id="conv-file-ack")
        gate = asyncio.Event()

        async def slow_put(*_args: Any, **_kwargs: Any) -> MagicMock:
            await gate.wait()
            return MagicMock(status_code=201, headers={})

        client = MagicMock()
        client.put = AsyncMock(side_effect=slow_put)
        a._http_client = client
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        monkeypatch.setattr(a, "_remove_file_consent_card", AsyncMock())
        monkeypatch.setattr(a, "validate_media_delivery_path", lambda _p: "/tmp/x")
        monkeypatch.setattr(
            "hermes_a365.plugin.adapter._read_file_bounded",
            lambda _p, _c: b"data",
        )
        monkeypatch.setattr(
            "hermes_a365.plugin.adapter._is_allowed_file_host",
            lambda _u, _a: True,
        )
        a._file_host_allowlist = ("tenant.sharepoint.com",)
        activity = {
            "type": "invoke",
            "conversation": inbound["conversation"],
            "from": inbound["from"],
            "serviceUrl": inbound["serviceUrl"],
            "value": {
                "action": "accept",
                "context": {"consentId": "ack-abc"},
                "uploadInfo": {
                    "uploadUrl": "https://tenant.sharepoint.com/upload",
                    "contentUrl": "https://tenant.sharepoint.com/x",
                    "name": "f.txt",
                },
            },
        }
        a._pending_file_uploads["ack-abc"] = {
            "path": "/tmp/x",
            "name": "f.txt",
            "size": 4,
            "sha256": hashlib.sha256(b"data").hexdigest(),
            "conversation_id": inbound["conversation"]["id"],
            "user_id": inbound["from"]["id"],
            "service_url": inbound["serviceUrl"],
            "validated_path": "A",
            "lifecycle_generation": a._lifecycle_generation,
            "chat_generation": a._chat_generation(inbound["conversation"]["id"]),
            "created_at": time.time(),
        }
        started = time.monotonic()
        resp = await a._handle_file_consent(activity, validated_path="A")
        ack_elapsed = time.monotonic() - started
        assert resp.status == 200
        assert ack_elapsed < 1.0
        assert client.put.await_count == 0
        gate.set()
        for _ in range(50):
            if client.put.await_count:
                break
            await asyncio.sleep(0)
        assert client.put.await_count == 1

    async def test_card_action_acks_before_slash_followup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import time

        a = plugin_tests._make_adapter(monkeypatch)
        gate = asyncio.Event()

        async def slow_send(*_args: Any, **_kwargs: Any) -> None:
            await gate.wait()

        monkeypatch.setattr(a, "_send_reply_activity", slow_send)
        monkeypatch.setattr(
            a,
            "_gw_resolve_slash_confirm",
            AsyncMock(return_value="Done."),
        )
        activity = plugin_tests._make_inbound(conv_id="conv-card-ack")
        value = plugin_tests._seed_card_capability(
            a,
            activity,
            kind="slash_confirm",
            choice="once",
            resolver={"session_key": "sess-1", "confirm_id": "cfm-1"},
        )
        started = time.monotonic()
        resp = await a._handle_card_action(activity, value)
        ack_elapsed = time.monotonic() - started
        assert resp.status_code == 200
        assert ack_elapsed < 1.0
        gate.set()
        await asyncio.sleep(0)

    async def test_edit_message_no_retry_when_delay_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = plugin_tests._make_adapter(monkeypatch)
        inbound = plugin_tests._make_inbound(conv_id="conv-none-delay")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-s"})
        post_mock = plugin_tests.TestEditMessage._wire_adapter(
            a, inbound=inbound, post_responses=[first]
        )
        plugin_tests.TestEditMessage._patch_token_mint(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        await a.edit_message("conv-none-delay", "m1", "A")
        post_mock.reset_mock()
        post_mock.side_effect = RuntimeError("network blip")
        monkeypatch.setattr(a, "_stream_edit_retry_delay_sec", lambda *_a, **_k: None)
        result = await a.edit_message("conv-none-delay", "m1", "A B")
        assert result.success is False
        assert post_mock.await_count == 1


@pytest.mark.asyncio
class TestTurnBufferPlainFlushRetry:
    async def test_concurrent_flush_skipped_during_plain_reply_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        import hermes_a365.plugin.adapter as adapter_mod

        a = plugin_tests._make_adapter(monkeypatch)
        chat = "conv-plain-flush-race"
        inbound = plugin_tests._make_inbound(conv_id=chat, activity_id="act-1")
        inbound["conversation"]["conversationType"] = "groupChat"
        rate = MagicMock(
            status_code=429,
            text="",
            headers={"Retry-After": "0"},
            json=lambda: {"error": {"code": "TooManyRequests"}},
        )
        ok = MagicMock(status_code=200, text="", json=lambda: {"id": "reply-1"})
        post_mock = plugin_tests.TestEditMessage._wire_adapter(
            a, inbound=inbound, post_responses=[rate, ok]
        )
        plugin_tests.TestEditMessage._patch_token_mint(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        retry_entered = asyncio.Event()
        release_retry = asyncio.Event()
        orig_sleep = adapter_mod.Agent365Adapter._sleep_turn_buffer_locked

        async def gated_sleep(
            self: adapter_mod.Agent365Adapter,
            chat_id: str,
            buf: dict[str, Any],
            delay: float,
        ) -> None:
            retry_entered.set()
            await orig_sleep(self, chat_id, buf, 0.0)
            await a._flush_turn_buffer(chat_id)
            await release_retry.wait()
            await orig_sleep(self, chat_id, buf, 0.0)

        monkeypatch.setattr(adapter_mod.Agent365Adapter, "_sleep_turn_buffer_locked", gated_sleep)
        a._turn_buffers[chat] = {
            "turns": [
                {
                    "seq": "1",
                    "inbound": inbound,
                    "open": False,
                    "validated_path": "A",
                    "activity_id": "act-1",
                }
            ],
            "segments": [{"id": "turnbuf:1", "text": "hello", "turn": "1"}],
            "depth": 0,
            "lock": asyncio.Lock(),
        }
        buf = a._turn_buffers[chat]
        send_task = asyncio.create_task(
            a._send_reply_activity(
                inbound=inbound,
                content="hello",
                log_context="turn buffer",
                turn_buffer_flush=(chat, buf, ("turnbuf:1",)),
            )
        )
        await asyncio.wait_for(retry_entered.wait(), timeout=2.0)
        release_retry.set()
        result = await asyncio.wait_for(send_task, timeout=2.0)
        assert result.success is True
        assert post_mock.await_count == 2

    async def test_keepalive_blocked_during_plain_reply_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        import hermes_a365.plugin.adapter as adapter_mod

        a = plugin_tests._make_adapter(monkeypatch)
        chat = "conv-plain-keepalive-block"
        inbound = plugin_tests._make_inbound(conv_id=chat, activity_id="act-1")
        inbound["conversation"]["conversationType"] = "groupChat"
        rate = MagicMock(
            status_code=429,
            text="",
            headers={"Retry-After": "0"},
            json=lambda: {"error": {"code": "TooManyRequests"}},
        )
        ok = MagicMock(status_code=200, text="", json=lambda: {"id": "reply-1"})
        post_mock = plugin_tests.TestEditMessage._wire_adapter(
            a, inbound=inbound, post_responses=[rate, ok]
        )
        plugin_tests.TestEditMessage._patch_token_mint(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        monkeypatch.setattr(adapter_mod, "_TURN_BUFFER_KEEPALIVE_DISABLE_SLEEPER", True)
        retry_entered = asyncio.Event()
        release_retry = asyncio.Event()
        orig_sleep = adapter_mod.Agent365Adapter._sleep_turn_buffer_locked

        async def gated_sleep(
            self: adapter_mod.Agent365Adapter,
            chat_id: str,
            buf: dict[str, Any],
            delay: float,
        ) -> None:
            retry_entered.set()
            await orig_sleep(self, chat_id, buf, 0.0)
            await a._emit_turn_buffer_keepalive(chat)
            await release_retry.wait()
            await orig_sleep(self, chat_id, buf, 0.0)

        monkeypatch.setattr(adapter_mod.Agent365Adapter, "_sleep_turn_buffer_locked", gated_sleep)
        a._turn_buffers[chat] = {
            "turns": [
                {
                    "seq": "1",
                    "inbound": inbound,
                    "open": True,
                    "validated_path": "A",
                    "activity_id": "act-1",
                    "started_mono": __import__("time").monotonic(),
                }
            ],
            "segments": [{"id": "turnbuf:1", "text": "hello", "turn": "1"}],
            "depth": 1,
            "lock": asyncio.Lock(),
            "keepalive": {
                "stream_id": "ka-plain-block",
                "sequence": 1,
                "opened_mono": __import__("time").monotonic(),
                "last_emit_mono": __import__("time").monotonic(),
            },
        }
        buf = a._turn_buffers[chat]
        before = post_mock.await_count
        send_task = asyncio.create_task(
            a._send_reply_activity(
                inbound=inbound,
                content="hello",
                log_context="turn buffer",
                turn_buffer_flush=(chat, buf, ("turnbuf:1",)),
            )
        )
        await asyncio.wait_for(retry_entered.wait(), timeout=2.0)
        release_retry.set()
        result = await asyncio.wait_for(send_task, timeout=2.0)
        assert result.success is True
        assert post_mock.await_count - before == 2

    async def test_handoff_link_flush_retry_rechecks_drop_segment_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        import hermes_a365.plugin.adapter as adapter_mod
        from hermes_a365.plugin.conversations import ConversationRef

        a = plugin_tests._make_adapter(monkeypatch)
        chat = "conv-handoff-drop-recheck"
        inbound = plugin_tests._make_inbound(conv_id=chat, activity_id="act-1")
        inbound["conversation"]["conversationType"] = "groupChat"
        a._handoff_link_enabled = True
        a._conversations.upsert(
            ConversationRef(conversation_id=chat, service_url="https://s/", chat_type="groupChat")
        )
        ok = MagicMock(status_code=200, text="", json=lambda: {"id": "reply-body"})
        rate = MagicMock(
            status_code=429,
            text="",
            headers={"Retry-After": "0"},
            json=lambda: {"error": {"code": "TooManyRequests"}},
        )
        post_mock = plugin_tests.TestEditMessage._wire_adapter(
            a, inbound=inbound, post_responses=[ok, rate, ok]
        )
        plugin_tests.TestEditMessage._patch_token_mint(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        handoff_suffix = "\n\n[Continue in Teams](https://teams.microsoft.com/l/chat/0/0?users=bot)"
        monkeypatch.setattr(a, "_turn_buffer_handoff_suffix", lambda _cid: handoff_suffix)

        async def force_plain_fallback(*_a: Any, **_k: Any) -> tuple[str, None]:
            return "fallback", None

        monkeypatch.setattr(a, "_deliver_turn_answer_locked", force_plain_fallback)
        monkeypatch.setattr(a, "_close_turn_buffer_keepalive_locked", AsyncMock(return_value=True))
        drop_id = "turnbuf:informative"
        a._turn_buffers[chat] = {
            "turns": [
                {
                    "seq": "1",
                    "inbound": inbound,
                    "open": False,
                    "validated_path": "A",
                    "activity_id": "act-1",
                }
            ],
            "segments": [
                {
                    "id": "turnbuf:1",
                    "text": "A" * 3950,
                    "turn": "1",
                    "reply_to": "act-1",
                },
                {
                    "id": drop_id,
                    "text": "Using the tool.",
                    "turn": "1",
                    "kind": "commentary",
                    "informative_displayed": True,
                },
            ],
            "depth": 0,
            "lock": asyncio.Lock(),
        }
        retry_entered = asyncio.Event()
        release_retry = asyncio.Event()
        orig_sleep = adapter_mod.Agent365Adapter._sleep_turn_buffer_locked

        async def gated_sleep(
            self: adapter_mod.Agent365Adapter,
            chat_id: str,
            buf: dict[str, Any],
            delay: float,
        ) -> None:
            retry_entered.set()
            await orig_sleep(self, chat_id, buf, 0.0)
            a._remove_turn_segments(buf, [drop_id])
            await release_retry.wait()
            await orig_sleep(self, chat_id, buf, 0.0)

        monkeypatch.setattr(adapter_mod.Agent365Adapter, "_sleep_turn_buffer_locked", gated_sleep)
        flush_task = asyncio.create_task(a._flush_closed_turns(chat))
        await asyncio.wait_for(retry_entered.wait(), timeout=2.0)
        release_retry.set()
        await asyncio.wait_for(flush_task, timeout=2.0)
        assert post_mock.await_count == 2

    async def test_plain_flush_busy_arms_followup_flush_for_later_segments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        import hermes_a365.plugin.adapter as adapter_mod

        a = plugin_tests._make_adapter(monkeypatch)
        chat = "conv-plain-followup-arm"
        inbound = plugin_tests._make_inbound(conv_id=chat, activity_id="act-1")
        inbound["conversation"]["conversationType"] = "groupChat"
        post_mock = plugin_tests.TestEditMessage._wire_adapter(a, inbound=inbound)
        plugin_tests.TestEditMessage._patch_token_mint(monkeypatch)
        bridge = adapter_mod._import_bridge()
        real_send = bridge.send_reply
        in_flight = asyncio.Event()
        hold = asyncio.Event()
        send_calls = {"n": 0}

        async def gated_send(**kwargs: Any) -> None:
            send_calls["n"] += 1
            if send_calls["n"] == 1:
                in_flight.set()
                await hold.wait()
            return await real_send(**kwargs)

        monkeypatch.setattr(bridge, "send_reply", gated_send)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)

        async def force_plain_fallback(*_a: Any, **_k: Any) -> tuple[str, None]:
            return "fallback", None

        monkeypatch.setattr(a, "_deliver_turn_answer_locked", force_plain_fallback)
        a._turn_buffers[chat] = {
            "turns": [
                {
                    "seq": "1",
                    "inbound": inbound,
                    "open": False,
                    "validated_path": "A",
                    "activity_id": "act-1",
                }
            ],
            "segments": [{"id": "turnbuf:1", "text": "hello", "turn": "1"}],
            "depth": 0,
            "lock": asyncio.Lock(),
        }
        buf = a._turn_buffers[chat]
        plain_send = asyncio.create_task(
            a._send_reply_activity(
                inbound=inbound,
                content="hello",
                log_context="turn buffer",
                turn_buffer_flush=(chat, buf, ("turnbuf:1",)),
            )
        )
        await asyncio.wait_for(in_flight.wait(), timeout=2.0)
        buf["segments"].append({"id": "turnbuf:2", "text": "also", "turn": "1"})
        await a._flush_turn_buffer(chat)
        assert buf.get("plain_flush_followup") is True
        plain_send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await plain_send
        followup = buf.get("plain_flush_followup_task")
        assert isinstance(followup, asyncio.Task)
        hold.set()
        await asyncio.wait_for(followup, timeout=2.0)
        assert send_calls["n"] == 2
        assert post_mock.await_count == 1
        assert chat not in a._turn_buffers or not a._turn_buffers[chat].get("segments")
