"""Shared outbound HTTP retry for Bot Framework and Graph uploads (#190).

Retries are safe only when the connector rejected the request before accepting
the activity or upload (HTTP 429 or transient 5xx). A non-idempotent POST is
not retried on 502/503/504 unless ``allow_transient_5xx`` is set for call
sites where a duplicate visible message is unlikely (stream edits on an open
stream, idempotent OneDrive range PUTs).

Call sites wired through :func:`request_with_outbound_retry`:

- ``activity_bridge.send_reply`` — BF reply POST (429 + transient 5xx).
- ``adapter._post_activity`` — typing and other fresh activities (429 only,
  at most one retry).
- ``adapter._send_proactive`` — proactive ``sendToConversation`` POST (429 +
  transient 5xx).
- ``adapter._send_reply_activity`` — via ``send_reply``.
- ``adapter.edit_message`` stream POST loop — 429/500/502/503/504 with
  stream-edit attempt budget; honours ``Retry-After`` when present, else the
  legacy streaming gap.
- ``adapter._handle_file_consent`` — OneDrive upload PUT (429 + transient 5xx).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)

# Honour Retry-After for general outbound sends (issue #190).
OUTBOUND_RETRY_AFTER_CAP_SEC = 30.0
OUTBOUND_DEFAULT_MAX_ATTEMPTS = 4
OUTBOUND_TYPING_MAX_ATTEMPTS = 2  # initial try + at most one retry

RETRYABLE_THROTTLE = frozenset({429})
RETRYABLE_TRANSIENT_5XX = frozenset({502, 503, 504})
# Teams stream edits also retry 500 (existing _STREAM_EDIT_RETRY_STATUSES).
RETRYABLE_STREAM_EDIT = RETRYABLE_THROTTLE | RETRYABLE_TRANSIENT_5XX | frozenset({500})

SleepFn = Callable[[float], Awaitable[Any]]


def parse_retry_after_seconds(
    response: Any,
    *,
    cap_sec: float = OUTBOUND_RETRY_AFTER_CAP_SEC,
    now: float | None = None,
) -> float:
    """Parse ``Retry-After`` as delta-seconds or HTTP-date; return capped delay."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0.0
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return 0.0
    text = str(raw).strip()
    if not text:
        return 0.0
    try:
        seconds = max(0.0, float(text))
        return min(seconds, cap_sec)
    except (TypeError, ValueError):
        pass
    try:
        target = parsedate_to_datetime(text)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        now_dt = datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)
        delta = (target - now_dt).total_seconds()
        return min(max(0.0, delta), cap_sec)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def http_status_code(response: Any) -> int | None:
    status = getattr(response, "status_code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def is_retryable_http_status(
    status: int,
    *,
    allow_transient_5xx: bool,
    allow_500: bool = False,
) -> bool:
    if status in RETRYABLE_THROTTLE:
        return True
    if allow_500 and status == 500:
        return True
    return bool(allow_transient_5xx and status in RETRYABLE_TRANSIENT_5XX)


def compute_backoff_delay_sec(
    attempt: int,
    response: Any | None,
    *,
    retry_after_cap: float = OUTBOUND_RETRY_AFTER_CAP_SEC,
    fallback_delay_sec: float = 0.0,
    max_exponential_sec: float = OUTBOUND_RETRY_AFTER_CAP_SEC,
) -> float:
    """Delay before retry ``attempt`` (1-based index of the attempt that failed)."""
    retry_after = 0.0
    if response is not None:
        retry_after = parse_retry_after_seconds(response, cap_sec=retry_after_cap)
    if retry_after > 0.0:
        return retry_after
    if fallback_delay_sec > 0.0:
        return fallback_delay_sec
    # attempt 1 failed → wait ~1s before attempt 2, etc.
    exp = min(max_exponential_sec, float(2 ** max(0, attempt - 1)))
    jittered = random.uniform(0.5 * exp, exp)
    return min(jittered, max_exponential_sec)


async def request_with_outbound_retry(
    send_once: Callable[[], Awaitable[Any]],
    *,
    call_site: str,
    max_attempts: int = OUTBOUND_DEFAULT_MAX_ATTEMPTS,
    allow_transient_5xx: bool = True,
    allow_500: bool = False,
    retry_after_cap: float = OUTBOUND_RETRY_AFTER_CAP_SEC,
    fallback_delay_sec: float = 0.0,
    sleep: SleepFn | None = None,
    before_retry: Callable[[int, int, Any], Awaitable[bool]] | None = None,
) -> Any:
    """Execute ``send_once`` until success, non-retryable status, or attempts exhausted.

    ``send_once`` must perform one HTTP round-trip and return a response-like object
    with ``status_code``. Exceptions propagate immediately (not retried here).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    sleep_fn: SleepFn = sleep if sleep is not None else asyncio.sleep
    last_response: Any = None
    for attempt in range(1, max_attempts + 1):
        last_response = await send_once()
        status = http_status_code(last_response)
        if status is None:
            return last_response
        if 200 <= status < 300:
            return last_response
        if not is_retryable_http_status(
            status,
            allow_transient_5xx=allow_transient_5xx,
            allow_500=allow_500,
        ):
            return last_response
        if attempt >= max_attempts:
            logger.warning(
                "outbound send gave up: call_site=%s status=%s attempts=%s",
                call_site,
                status,
                attempt,
            )
            return last_response
        delay = compute_backoff_delay_sec(
            attempt,
            last_response,
            retry_after_cap=retry_after_cap,
            fallback_delay_sec=fallback_delay_sec,
        )
        logger.info(
            "outbound send retry: call_site=%s status=%s attempt=%s/%s delay_sec=%.2f",
            call_site,
            status,
            attempt,
            max_attempts,
            delay,
        )
        if before_retry is not None:
            ok = await before_retry(attempt, status, last_response)
            if not ok:
                return last_response
        await sleep_fn(delay)
    return last_response
