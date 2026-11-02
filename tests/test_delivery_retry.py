"""Delivery retries exercise the real serve route and outbound HTTP transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import asdict
from typing import Any

import httpx
import pytest
from test_activity_bridge import _cfg, _inbound_message_activity

from hermes_a365 import activity_bridge as bridge
from hermes_a365 import outbound_retry


class Delivery:
    def __init__(
        self,
        *,
        replies: list[int | Exception] | None = None,
        webhook: Any = None,
        webhook_failure: int | Exception | None = None,
        token_failure: Exception | None = None,
        cache: bridge._IdempotencyCache | None = None,
    ) -> None:
        self.cache = cache if cache is not None else bridge._IdempotencyCache()
        self.cfg = _cfg()
        self.cfg.skip_jwt_validation = True
        self.activity = _inbound_message_activity()
        self.replies = list(replies or [200])
        self.webhook = {"text": "private reply sentinel"} if webhook is None else webhook
        self.webhook_failure = webhook_failure
        self.token_failure = token_failure
        self.webhook_calls = 0
        self.posts: list[httpx.Request] = []
        self.block_at = ""
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.outbound = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.app = bridge.make_app(
            self.cfg, http_client=self.outbound, idempotency_cache=self.cache
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://bridge.test"
        )

    @property
    def key(self) -> str:
        key = bridge._activity_delivery_id(
            self.activity, validated_path="dev", tenant_id="tenant-1"
        )
        assert key is not None
        return key

    async def pause(self, where: str) -> None:
        if self.block_at == where:
            self.entered.set()
            await self.resume.wait()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "hook.test":
            self.webhook_calls += 1
            await self.pause("webhook")
            if isinstance(self.webhook_failure, Exception):
                raise self.webhook_failure
            if self.webhook_failure:
                return httpx.Response(self.webhook_failure, text="private webhook sentinel")
            return httpx.Response(200, json=self.webhook)
        if "/v3/conversations/" in request.url.path:
            self.posts.append(request)
            await self.pause("reply")
            result = self.replies.pop(0) if self.replies else 200
            if isinstance(result, Exception):
                raise result
            return httpx.Response(result, text="private connector sentinel")
        if request.url.host == "login.microsoftonline.com":
            await self.pause("token")
            if self.token_failure is not None:
                failure, self.token_failure = self.token_failure, None
                raise failure
            return httpx.Response(
                200, json={"access_token": "private token sentinel", "expires_in": 3600}
            )
        raise AssertionError("unexpected outbound destination")

    async def post(self, **updates: Any) -> httpx.Response:
        return await self.client.post("/api/messages", json={**self.activity, **updates})


@pytest.fixture
async def delivery() -> Any:
    async with AsyncExitStack() as stack:

        def make(**kwargs: Any) -> Delivery:
            item = Delivery(**kwargs)
            stack.push_async_callback(item.outbound.aclose)
            stack.push_async_callback(item.client.aclose)
            return item

        yield make


def response_is(response: httpx.Response, code: int, status: str) -> None:
    assert response.status_code == code
    assert response.json() == {"status": status}
    assert "private" not in response.text
    assert "HTTP " not in response.text


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_connect_error_resends_reply_not_webhook(
    delivery: Callable[..., Delivery], error: Any
):
    d = delivery(replies=[error("private failure sentinel"), 200])
    response_is(await d.post(), 502, "reply_failed")
    response_is(await d.post(), 200, "replied")
    response_is(await d.post(), 200, "duplicate")
    assert d.webhook_calls == 1
    assert len(d.posts) == 2
    assert json.loads(d.posts[0].content) == json.loads(d.posts[1].content)


async def test_exhausted_429_resends_reply_not_webhook(delivery, monkeypatch):
    # Exercise all in-call retries without waiting on real backoff.
    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(outbound_retry.asyncio, "sleep", no_sleep)
    d = delivery(replies=[429] * outbound_retry.OUTBOUND_DEFAULT_MAX_ATTEMPTS + [200])
    response_is(await d.post(), 502, "reply_failed")
    assert len(d.posts) == outbound_retry.OUTBOUND_DEFAULT_MAX_ATTEMPTS
    response_is(await d.post(), 200, "replied")
    assert len(d.posts) == outbound_retry.OUTBOUND_DEFAULT_MAX_ATTEMPTS + 1
    assert d.webhook_calls == 1
    assert d.cache.record(d.key).attempts == 2


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError])
async def test_token_read_timeout_is_not_sent(delivery, error):
    d = delivery(token_failure=error("private token failure sentinel"))
    response_is(await d.post(), 502, "reply_failed")
    assert len(d.posts) == 0
    response_is(await d.post(), 200, "replied")
    assert d.webhook_calls == 1
    assert len(d.posts) == 1


async def assert_no_resend(d: Delivery, status: str = "reply_unconfirmed") -> None:
    response_is(await d.post(), 200, status)
    response_is(await d.post(), 200, "duplicate")
    assert d.webhook_calls == 1
    assert len(d.posts) == 1
    assert d.cache.result_bytes == 0


async def test_connector_502_does_not_resend(delivery):
    await assert_no_resend(delivery(replies=[502, 200]))


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.CloseError,
        httpx.RequestError,
        RuntimeError,
    ],
)
async def test_read_error_does_not_resend(delivery, error):
    await assert_no_resend(delivery(replies=[error("private failure sentinel"), 200]))


async def test_408_does_not_forget_or_resend(delivery):
    await assert_no_resend(delivery(replies=[408, 200]))


@pytest.mark.parametrize("status", [500, 503, 504, 599, 0, 199, 302])
async def test_500_and_504_do_not_resend(delivery, status):
    await assert_no_resend(delivery(replies=[status, 200]))


@pytest.mark.parametrize("status", [400, 403, 404, 409, 413, 499])
async def test_terminal_4xx_does_not_resend(delivery, status):
    await assert_no_resend(delivery(replies=[status, 200]), "reply_rejected")


async def test_401_resends_reply_not_webhook(delivery):
    d = delivery(replies=[401, 200])
    response_is(await d.post(), 502, "reply_failed")
    response_is(await d.post(), 200, "replied")
    assert d.webhook_calls == 1
    assert len(d.posts) == 2


@pytest.mark.parametrize("where", ["webhook", "reply"])
async def test_overlap_one_post(delivery, where):
    d = delivery()
    d.block_at = where

    async def overlap():
        await d.entered.wait()
        try:
            return await d.post()
        finally:
            d.resume.set()

    first, second = await asyncio.gather(d.post(), overlap())
    response_is(first, 200, "replied")
    response_is(second, 503, "delivery_in_progress")
    assert d.webhook_calls == 1
    assert len(d.posts) == 1


@pytest.mark.parametrize("body", [[], ["value"], "value", 1, {}, {"unused": True}])
async def test_non_dict_webhook_is_not_replayed(delivery, body):
    d = delivery(webhook=body)
    response_is(await d.post(), 200, "no_reply")
    response_is(await d.post(), 200, "duplicate")
    assert d.webhook_calls == 1
    assert len(d.posts) == 0


@pytest.mark.parametrize("failure", [400, 500, httpx.ConnectError("private webhook sentinel")])
async def test_webhook_http_error_retries_error_card_only(delivery, failure):
    d = delivery(webhook_failure=failure, replies=[httpx.ConnectError("failure"), 200])
    response_is(await d.post(), 502, "reply_failed")
    response_is(await d.post(), 200, "replied")
    assert d.webhook_calls == 1
    assert len(d.posts) == 2
    first, second = [json.loads(request.content) for request in d.posts]
    assert first == second
    assert first["attachments"][0]["content"] == bridge.render_error_card(
        "The agent backend could not be reached."
    )


async def test_forget_noops_after_webhook_entry(delivery):
    d = delivery(replies=[httpx.ConnectError("failure"), 200])
    response_is(await d.post(), 502, "reply_failed")
    record = d.cache.record(d.key)
    d.cache.forget(d.key)
    assert d.cache.record(d.key) is record
    response_is(await d.post(), 200, "replied")
    assert d.webhook_calls == 1
    assert len(d.posts) == 2
    # The plugin's legacy call path must still release pre-dispatch records.
    assert d.cache.is_duplicate("plugin-received") is False
    d.cache.forget("plugin-received")
    assert d.cache.record("plugin-received") is None


async def test_pending_payload_evicts_with_record(delivery):
    cache = bridge._IdempotencyCache(max_entries=1, ttl_seconds=60)
    d = delivery(cache=cache, replies=[httpx.ConnectError("failure"), 200])
    response_is(await d.post(), 502, "reply_failed")
    assert len(cache.seen) == 1
    assert cache.result_bytes > 0
    # Pending replies cannot be evicted by a new key.
    response_is(await d.post(id="new"), 503, "delivery_busy")
    assert d.webhook_calls == 1
    # Expiry removes the record and its body together. Release the fresh
    # pre-dispatch probe to inspect the empty cache, as the plugin may do.
    expires = cache.record(d.key).inserted_at + cache.ttl_seconds
    assert cache.claim("ttl-probe", now=expires) == "new"
    cache.release_if_received("ttl-probe", cache.record("ttl-probe"))
    assert len(cache.seen) == 0
    assert cache.result_bytes == 0
    assert cache.record(d.key) is None

    # Completed tombstones are evictable at max_entries=1.
    response_is(await d.post(), 200, "replied")
    old_record = cache.record(d.key)
    response_is(await d.post(id="new"), 200, "replied")
    assert len(cache.seen) == 1
    assert old_record not in cache.seen.values()
    assert cache.result_bytes == 0

    oversize = delivery(
        webhook={"text": "x" * (256 * 1024)}, replies=[httpx.ConnectError("failure"), 200]
    )
    response_is(await oversize.post(), 200, "reply_unconfirmed")
    assert oversize.cache.result_bytes == 0
    assert oversize.cache.record(oversize.key).result is None
    response_is(await oversize.post(), 200, "duplicate")
    assert len(oversize.posts) == 1

    # Five almost-256-KiB replies force the oldest body's shedding at 1 MiB.
    pressure = delivery(
        webhook={"text": "x" * (256 * 1024 - 20)},
        replies=[httpx.ConnectError("failure") for _ in range(5)],
    )
    for index in range(5):
        response_is(await pressure.post(id=str(index)), 502, "reply_failed")
        assert pressure.cache.result_bytes <= 1024 * 1024
    assert len(pressure.cache.seen) == 5
    oldest = next(iter(pressure.cache.seen.values()))
    assert oldest.result is None
    assert oldest.result_bytes == 0
    assert oldest.reply == "held"
    response_is(await pressure.post(id="0"), 200, "duplicate")
    assert pressure.webhook_calls == 5
    assert len(pressure.posts) == 5


async def test_attempt_cap_stops(delivery):
    d = delivery(replies=[httpx.ConnectError("failure") for _ in range(4)])
    response_is(await d.post(), 502, "reply_failed")
    response_is(await d.post(), 502, "reply_failed")
    response_is(await d.post(), 200, "reply_rejected")
    response_is(await d.post(), 200, "duplicate")
    assert d.webhook_calls == 1
    assert len(d.posts) == 3
    assert d.cache.record(d.key).attempts == 3
    assert d.cache.result_bytes == 0


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout])
async def test_send_reply_keeps_post_send_timeout_marker(delivery, monkeypatch, error):
    real_send = bridge.send_reply
    failures = []

    async def observe(**kwargs):
        try:
            return await real_send(**kwargs)
        except Exception as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(bridge, "send_reply", observe)
    d = delivery(replies=[error("private failure sentinel"), 200])
    await assert_no_resend(d)
    assert isinstance(failures[0], bridge.ReplyAmbiguous)
    assert "timeout after the request was sent" in str(failures[0])
    token = delivery(token_failure=error("private token failure sentinel"))
    response_is(await token.post(), 502, "reply_failed")
    assert isinstance(failures[1], error)
    assert "timeout after the request was sent" not in str(failures[1])
    assert len(token.posts) == 0
    response_is(await token.post(), 200, "replied")
    assert token.webhook_calls == 1


@pytest.mark.parametrize("kind", ["message", "invoke"])
async def test_sequential_duplicate_unchanged(delivery, kind):
    d = delivery(
        webhook={"invokeResponse": {"status": 200, "body": {"answer": 42}}}
        if kind == "invoke"
        else None
    )
    d.activity["type"] = kind
    if kind == "invoke":
        d.activity["name"] = "task/fetch"
    first, second = await d.post(), await d.post()
    assert first.status_code == second.status_code == 200
    if kind == "invoke":
        assert first.json() == {"answer": 42}
        assert second.json() == {}
        assert len(d.posts) == 0
        assert d.cache.record(d.key).state == "done"
        assert d.cache.record(d.key).hold_reason == "invoke_done"
    else:
        assert first.json() == {"status": "replied"}
        assert second.json() == {"status": "duplicate"}
        assert len(d.posts) == 1
        assert d.cache.record(d.key).reply == "sent"
    assert d.webhook_calls == 1


async def test_429_then_read_timeout_is_ambiguous(delivery, monkeypatch):
    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(outbound_retry.asyncio, "sleep", no_sleep)
    d = delivery(replies=[429, httpx.ReadTimeout("failure"), 200])
    response_is(await d.post(), 200, "reply_unconfirmed")
    response_is(await d.post(), 200, "duplicate")
    assert d.webhook_calls == 1
    assert len(d.posts) == 2
    assert d.cache.record(d.key).attempts == 1


async def test_budget_exhausted_before_reply_is_not_sent(delivery, monkeypatch):
    real_budget = outbound_retry.can_start_outbound_attempt
    d = delivery()
    # Token acquisition finishes, but no connector POST may start.
    real_token = bridge.acquire_reply_token

    async def expire_after_token(**kwargs):
        result = await real_token(**kwargs)
        monkeypatch.setattr(outbound_retry, "can_start_outbound_attempt", lambda _deadline: False)
        return result

    monkeypatch.setattr(bridge, "acquire_reply_token", expire_after_token)
    response_is(await d.post(), 502, "reply_failed")
    assert len(d.posts) == 0
    monkeypatch.setattr(bridge, "acquire_reply_token", real_token)
    monkeypatch.setattr(outbound_retry, "can_start_outbound_attempt", real_budget)
    response_is(await d.post(), 200, "replied")
    assert d.webhook_calls == 1
    assert len(d.posts) == 1


@pytest.mark.parametrize("where", ["webhook", "reply", "token"])
async def test_cancellation_keeps_webhook_tombstone(delivery, where):
    d = delivery()
    d.block_at = where
    first = asyncio.create_task(d.post())
    await d.entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    d.resume.set()
    if where == "token":
        # The result survived and no POST began; redelivery may send it.
        response_is(await d.post(), 200, "replied")
        assert len(d.posts) == 1
    else:
        response_is(await d.post(), 200, "duplicate")
        assert len(d.posts) == (1 if where == "reply" else 0)
    assert d.webhook_calls == 1


async def test_retry_uses_current_trusted_inbound(delivery):
    d = delivery(replies=[httpx.ConnectError("failure"), 200])
    response_is(await d.post(), 502, "reply_failed")
    rejected = await d.post(serviceUrl="https://untrusted.test/")
    assert rejected.status_code == 403
    assert len(d.posts) == 1
    new_url = "https://other.botframework.com/new/"
    response_is(await d.post(serviceUrl=new_url), 200, "replied")
    assert str(d.posts[1].url).startswith(new_url)
    assert d.webhook_calls == 1


async def test_transition_logs_omit_payloads(delivery, caplog):
    caplog.set_level("INFO", logger="hermes_a365.activity_bridge")
    d = delivery(replies=[502])
    d.activity["text"] = "private activity sentinel"
    await assert_no_resend(d)
    logs = [
        record.message for record in caplog.records if record.name == "hermes_a365.activity_bridge"
    ]
    assert any("delivery_reply_ambiguous" in line for line in logs)
    assert any("delivery_webhook_once" in line for line in logs)
    assert all("private" not in line for line in logs)
    assert all("serviceUrl" not in line and "trafficmanager" not in line for line in logs)


async def test_cancellation_before_webhook_releases_claim(delivery, monkeypatch):
    d = delivery()
    real_envelope = bridge.build_webhook_envelope

    def cancelled_envelope(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(bridge, "build_webhook_envelope", cancelled_envelope)
    with pytest.raises(asyncio.CancelledError):
        await d.post()
    assert d.cache.record(d.key) is None
    monkeypatch.setattr(bridge, "build_webhook_envelope", real_envelope)
    response_is(await d.post(), 200, "replied")
    response_is(await d.post(), 200, "duplicate")
    assert d.webhook_calls == 1
    assert len(d.posts) == 1


@pytest.mark.parametrize("oversize", [False, True])
async def test_cancellation_before_post_preserves_ready(delivery, monkeypatch, oversize):
    d = delivery(webhook={"text": "x" * (256 * 1024)} if oversize else None)
    real_render = bridge.render_reply_activity

    def cancelled_render(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(bridge, "render_reply_activity", cancelled_render)
    with pytest.raises(asyncio.CancelledError):
        await d.post()
    assert d.cache.record(d.key).reply == "ready"
    assert len(d.posts) == 0
    monkeypatch.setattr(bridge, "render_reply_activity", real_render)
    response_is(await d.post(), 200, "reply_unconfirmed" if oversize else "replied")
    response_is(await d.post(), 200, "duplicate")
    assert d.webhook_calls == 1
    assert len(d.posts) == (0 if oversize else 1)


@pytest.mark.parametrize("outcome", [200, httpx.ConnectError("failure"), 400, 502])
async def test_expired_reply_outcome_does_not_change_replacement(delivery, outcome):
    cache = bridge._IdempotencyCache(ttl_seconds=60)
    old = delivery(cache=cache, replies=[outcome])
    new = delivery(cache=cache)
    old.block_at = new.block_at = "reply"
    first = asyncio.create_task(old.post())
    await old.entered.wait()
    old_record = cache.record(old.key)
    old_record.inserted_at -= cache.ttl_seconds
    second = asyncio.create_task(new.post())
    await new.entered.wait()
    new_record = cache.record(new.key)
    assert new_record is not old_record
    retained_bytes = cache.result_bytes
    try:
        old.resume.set()
        await first
        assert cache.record(new.key) is new_record
        assert new_record.reply == "inflight"
        assert new_record.result == new.webhook
        assert new_record.attempts == 1
        assert cache.result_bytes == retained_bytes > 0
    finally:
        old.resume.set()
        new.resume.set()
        await asyncio.gather(first, second)
    response_is(second.result(), 200, "replied")
    response_is(await new.post(), 200, "duplicate")
    assert old.webhook_calls == new.webhook_calls == 1
    assert len(old.posts) == len(new.posts) == 1
    assert cache.result_bytes == 0


@pytest.mark.parametrize(
    ("stage", "update"),
    [
        ("received", "enter_webhook"),
        ("received", "release_if_received"),
        ("webhook_running", "complete"),
        ("webhook_running", "store_result"),
        ("ready", "begin_reply"),
        ("inflight", "finish_reply"),
    ],
)
def test_stale_record_cannot_update_replacement(stage, update):
    cache = bridge._IdempotencyCache(ttl_seconds=60)

    def prepare(now):
        assert cache.claim("key", now=now) == "new"
        record = cache.record("key")
        if stage != "received":
            assert cache.enter_webhook("key", record)
            if stage != "webhook_running":
                cache.store_result("key", record, {"text": "retained"})
                if stage == "inflight":
                    assert cache.begin_reply("key", record)
        return record

    old = prepare(100)
    new = prepare(160)
    before = asdict(new)
    retained_bytes = cache.result_bytes
    args = {
        "complete": ("no_reply",),
        "store_result": ({"text": "stale"},),
        "finish_reply": ("sent",),
    }.get(update, ())
    getattr(cache, update)("key", old, *args)
    assert cache.record("key") is new
    assert asdict(new) == before
    assert cache.result_bytes == retained_bytes


def test_payload_shedding_scans_cache_linearly(monkeypatch):
    monkeypatch.setattr(bridge, "REPLY_PAYLOAD_MAX_BYTES", 256)
    monkeypatch.setattr(bridge, "REPLY_PAYLOAD_TOTAL_MAX_BYTES", 256)

    class CountedRecords(dict):
        visits = 0

        def values(self):
            for record in super().values():
                self.visits += 1
                yield record

        def items(self):
            for item in super().items():
                self.visits += 1
                yield item

    records = CountedRecords()
    cache = bridge._IdempotencyCache(seen=records)
    for index in range(200):
        cache.is_duplicate(f"tombstone-{index}", now=100)

    def store(key, text):
        assert cache.claim(key, now=100) == "new"
        record = cache.record(key)
        assert cache.enter_webhook(key, record)
        cache.store_result(key, record, {"text": text})
        return record

    small = [store(f"small-{index}", "x" * 20) for index in range(8)]
    assert cache.result_bytes == 256
    records.visits = 0
    large = store("large", "x" * 200)
    assert records.visits <= 2 * len(records)
    assert [record.reply for record in small] == ["held"] * 7 + ["ready"]
    assert large.result == {"text": "x" * 200}
    assert cache.result_bytes == 244
    assert cache.result_bytes == sum(record.result_bytes for record in records.values())


@pytest.mark.parametrize("release", ["complete", "finish", "expire", "evict", "forget"])
def test_payload_total_tracks_every_release_path(release):
    cache = bridge._IdempotencyCache(ttl_seconds=60, max_entries=1)
    assert cache.claim("key", now=100) == "new"
    record = cache.record("key")
    assert cache.enter_webhook("key", record)
    cache.store_result("key", record, {"text": "retained"})
    assert cache.result_bytes == record.result_bytes > 0
    if release == "complete":
        cache.complete("key", record)
    elif release == "finish":
        assert cache.begin_reply("key", record)
        cache.finish_reply("key", record, "sent")
    elif release == "expire":
        assert cache.claim("new", now=160) == "new"
    elif release == "evict":
        # Shedding normally clears held records first. Exercise removal's
        # accounting even if an evictable record still owns a body.
        record.reply = "held"
        assert cache.claim("new", now=101) == "new"
    else:
        record.state = "received"
        cache.forget("key")
    assert record.result is None
    assert record.result_bytes == 0
    assert cache.result_bytes == 0


@pytest.mark.parametrize("later_post", [False, True])
async def test_cancellation_during_429_retry(delivery, monkeypatch, later_post):
    d = delivery(replies=[429, 200])
    sleeping = asyncio.Event()
    resume_sleep = asyncio.Event()

    async def backoff(_seconds):
        sleeping.set()
        await resume_sleep.wait()

    monkeypatch.setattr(outbound_retry.asyncio, "sleep", backoff)
    first = asyncio.create_task(d.post())
    await sleeping.wait()
    if later_post:
        d.block_at = "reply"
        resume_sleep.set()
        await d.entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    record = d.cache.record(d.key)
    assert record.reply == ("held" if later_post else "ready")
    assert (d.cache.result_bytes > 0) is (not later_post)
    assert (record.result is not None) is (not later_post)
    assert len(d.posts) == (2 if later_post else 1)
    d.block_at = ""
    response_is(await d.post(), 200, "duplicate" if later_post else "replied")
    assert d.webhook_calls == 1
    assert len(d.posts) == 2
