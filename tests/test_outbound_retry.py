"""Unit tests for shared outbound HTTP retry (#190)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_a365.outbound_retry import (
    OUTBOUND_DEFAULT_MAX_ATTEMPTS,
    OUTBOUND_RETRY_AFTER_CAP_SEC,
    OUTBOUND_TYPING_MAX_ATTEMPTS,
    compute_backoff_delay_sec,
    is_retryable_http_status,
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
        assert (
            parse_retry_after_seconds(_resp(429, "120"), cap_sec=30.0) == 30.0
        )

    def test_missing_header_returns_zero(self) -> None:
        assert parse_retry_after_seconds(_resp(429), cap_sec=30.0) == 0.0


class TestIsRetryable:
    def test_429_always(self) -> None:
        assert is_retryable_http_status(429, allow_transient_5xx=False)

    def test_503_only_when_allowed(self) -> None:
        assert not is_retryable_http_status(503, allow_transient_5xx=False)
        assert is_retryable_http_status(503, allow_transient_5xx=True)

    def test_400_not_retryable(self) -> None:
        assert not is_retryable_http_status(400, allow_transient_5xx=True)


@pytest.mark.asyncio
class TestRequestWithOutboundRetry:
    async def test_retry_after_seconds_waits_then_succeeds(self) -> None:
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        calls = 0

        async def send_once() -> MagicMock:
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

        async def send_once() -> MagicMock:
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

        async def send_once() -> MagicMock:
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

    async def test_transient_5xx_when_enabled(self) -> None:
        calls = 0

        async def send_once() -> MagicMock:
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

    async def test_adapter_post_activity_typing_at_most_one_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_agent365_plugin import TestEditMessage, _make_adapter, _make_inbound

        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-typing")
        posts = [
            MagicMock(status_code=429, headers={"Retry-After": "1"}, text=""),
            MagicMock(status_code=200, headers={}, text=""),
        ]
        post_mock = TestEditMessage._wire_adapter(a, inbound=inbound, post_responses=posts)
        TestEditMessage._patch_token_mint(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        sleep_mock = AsyncMock()
        monkeypatch.setattr("hermes_a365.outbound_retry.asyncio.sleep", sleep_mock)

        await a._post_activity(
            inbound=inbound,
            activity={"type": "typing"},
        )
        assert post_mock.await_count == OUTBOUND_TYPING_MAX_ATTEMPTS

    async def test_proactive_send_retries_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes_a365.plugin.conversations import ConversationRef
        from tests.test_agent365_plugin import TestEditMessage, _make_adapter, _make_inbound

        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-pro")
        a._conversations.upsert(ConversationRef.from_activity(inbound), now=100.0)
        posts = [
            MagicMock(status_code=503, headers={}, text=""),
            MagicMock(status_code=200, headers={}, text="", json=lambda: {"id": "new"}),
        ]
        post_mock = TestEditMessage._wire_adapter(a, inbound=inbound, post_responses=posts)
        TestEditMessage._patch_token_mint(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)
        monkeypatch.setattr("hermes_a365.outbound_retry.asyncio.sleep", AsyncMock())

        result = await a._send_proactive("conv-pro", "hello")
        assert result.success is True
        assert post_mock.await_count == 2

    async def test_file_consent_upload_retries_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_agent365_plugin import _make_adapter, _make_inbound

        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-file")
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
        assert client.put.await_count == 2

    async def test_stream_edit_honours_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_agent365_plugin import TestEditMessage, _make_adapter, _make_inbound

        a = _make_adapter(monkeypatch)
        inbound = _make_inbound(conv_id="conv-stream")
        first = MagicMock(status_code=201, text="", json=lambda: {"id": "bf-s"})
        rate = MagicMock(
            status_code=429,
            text="",
            headers={"Retry-After": "2"},
            json=lambda: {"error": {"code": "TooManyRequests", "message": "wait"}},
        )
        ok = MagicMock(status_code=202, text="", json=lambda: {})
        TestEditMessage._wire_adapter(a, inbound=inbound, post_responses=[first, rate, ok])
        TestEditMessage._patch_token_mint(monkeypatch)
        sleep_mock = TestEditMessage._no_sleep(monkeypatch)
        monkeypatch.setattr(a, "_await_outbound", lambda _c, coro: coro)

        await a.edit_message("conv-stream", "m1", "A")
        result = await a.edit_message("conv-stream", "m1", "A B")
        assert result.success is True
        assert sleep_mock.await_args_list[-1].args[0] == 2.0
