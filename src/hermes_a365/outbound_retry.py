"""Shared outbound HTTP retry for Bot Framework and Graph uploads (#190).

Retries are safe only when the connector rejected the request before accepting
the activity or upload. Non-idempotent activity **creates** (BF reply POST,
proactive ``sendToConversation`` POST) retry **HTTP 429 only** — a 502/503/504
may already have stored the activity, matching existing ReadTimeout handling.

Idempotent or safe-to-repeat calls may opt into transient 5xx retries (full-range
OneDrive upload PUT after invoke ack; stream edits on an open stream).

Call sites wired through :func:`request_with_outbound_retry`:

- ``activity_bridge.send_reply`` — BF reply POST (**429 only**); adapter callers
  use a 25s relative budget (#214 keepalive); serve ``/api/messages`` shares one
  absolute ack deadline for webhook forward, token mint, and reply.
- ``adapter._post_activity`` — typing (**429 only**, at most one retry).
- ``adapter._send_proactive`` — proactive POST (**429 only**).
- ``adapter.edit_message`` stream POST loop — honours ``Retry-After`` floored at
  the streaming gap and capped by live remaining stream lifetime; no retry when
  remainder is below the gap.
- ``adapter._handle_file_consent`` upload — post-ack background task (60s budget).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)

# Honour Retry-After for general outbound sends (issue #190).
OUTBOUND_RETRY_AFTER_CAP_SEC = 30.0
OUTBOUND_DEFAULT_MAX_ATTEMPTS = 4
OUTBOUND_TYPING_MAX_ATTEMPTS = 2  # initial try + at most one retry
OUTBOUND_HTTP_TIMEOUT_DEFAULT_SEC = 15.0
OUTBOUND_ONEDRIVE_UPLOAD_ATTEMPT_TIMEOUT_SEC = 60.0
# Post-ack adapter sends (send, proactive, typing, turn-buffer).
OUTBOUND_BF_REPLY_TOTAL_BUDGET_SEC = 25.0
# Serve ``/api/messages``: absolute end-to-end budget under BF ~15s activity timeout.
OUTBOUND_WEBHOOK_ACK_DEADLINE_SEC = 14.0
OUTBOUND_MIN_ATTEMPT_BUDGET_SEC = 1.0
OUTBOUND_FILE_UPLOAD_TOTAL_BUDGET_SEC = 60.0

RETRYABLE_THROTTLE = frozenset({429})
RETRYABLE_TRANSIENT_5XX = frozenset({502, 503, 504})
RETRYABLE_STREAM_EDIT = RETRYABLE_THROTTLE | RETRYABLE_TRANSIENT_5XX | frozenset({500})

SleepFn = Callable[[float], Awaitable[Any]]
SendOnceFn = Callable[[float], Awaitable[Any]]


def remaining_until_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def outbound_attempt_timeout_sec(
    deadline: float | None,
    *,
    attempt_cap_sec: float = OUTBOUND_HTTP_TIMEOUT_DEFAULT_SEC,
) -> float:
    remaining = remaining_until_deadline(deadline)
    if remaining is None:
        return attempt_cap_sec
    return min(attempt_cap_sec, remaining)


def can_start_outbound_attempt(deadline: float | None) -> bool:
    remaining = remaining_until_deadline(deadline)
    if remaining is None:
        return True
    return remaining >= OUTBOUND_MIN_ATTEMPT_BUDGET_SEC


def resolve_outbound_deadline(
    *,
    deadline: float | None = None,
    total_budget_sec: float | None = None,
) -> float | None:
    if deadline is not None:
        return deadline
    if total_budget_sec is not None and total_budget_sec > 0.0:
        return time.monotonic() + total_budget_sec
    return None


def new_webhook_ack_deadline() -> float:
    return time.monotonic() + OUTBOUND_WEBHOOK_ACK_DEADLINE_SEC


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
    exp = min(max_exponential_sec, float(2 ** max(0, attempt - 1)))
    jittered = random.uniform(0.5 * exp, exp)
    return min(jittered, max_exponential_sec)


def clamp_retry_delay_sec(delay: float, remaining_budget_sec: float | None) -> float:
    """Clamp a computed delay to the remaining cumulative retry budget."""
    if remaining_budget_sec is None:
        return max(0.0, delay)
    if remaining_budget_sec <= 0.0:
        return 0.0
    return min(max(0.0, delay), remaining_budget_sec)


async def request_with_outbound_retry(
    send_once: SendOnceFn,
    *,
    call_site: str,
    max_attempts: int = OUTBOUND_DEFAULT_MAX_ATTEMPTS,
    allow_transient_5xx: bool = False,
    allow_500: bool = False,
    retry_after_cap: float = OUTBOUND_RETRY_AFTER_CAP_SEC,
    fallback_delay_sec: float = 0.0,
    deadline: float | None = None,
    total_budget_sec: float | None = None,
    attempt_timeout_sec: float | None = None,
    sleep: SleepFn | None = None,
    before_retry: Callable[[int, int, Any], Awaitable[bool]] | None = None,
) -> Any:
    """Execute ``send_once(timeout_sec)`` until success, failure, or budget exhausted."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    sleep_fn: SleepFn = sleep if sleep is not None else asyncio.sleep
    eff_deadline = resolve_outbound_deadline(deadline=deadline, total_budget_sec=total_budget_sec)
    attempt_cap = (
        attempt_timeout_sec
        if attempt_timeout_sec is not None
        else OUTBOUND_HTTP_TIMEOUT_DEFAULT_SEC
    )

    last_response: Any = None
    for attempt in range(1, max_attempts + 1):
        if not can_start_outbound_attempt(eff_deadline):
            logger.warning(
                "outbound send gave up: call_site=%s reason=ack_budget_before_attempt",
                call_site,
            )
            break
        timeout = outbound_attempt_timeout_sec(
            eff_deadline,
            attempt_cap_sec=attempt_cap,
        )
        if eff_deadline is not None and timeout <= 0.0:
            logger.warning(
                "outbound send gave up: call_site=%s reason=ack_budget_before_attempt",
                call_site,
            )
            break
        last_response = await send_once(timeout)
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
        remaining = remaining_until_deadline(eff_deadline)
        retry_after = (
            parse_retry_after_seconds(last_response, cap_sec=retry_after_cap)
            if status in RETRYABLE_THROTTLE
            else 0.0
        )
        if remaining is not None and retry_after > remaining:
            logger.warning(
                "outbound send gave up: call_site=%s status=%s reason=retry_after_exceeds_budget",
                call_site,
                status,
            )
            return last_response
        delay = compute_backoff_delay_sec(
            attempt,
            last_response,
            retry_after_cap=retry_after_cap,
            fallback_delay_sec=fallback_delay_sec,
        )
        delay = clamp_retry_delay_sec(delay, remaining)
        if delay <= 0.0 or not can_start_outbound_attempt(eff_deadline):
            logger.warning(
                "outbound send gave up: call_site=%s status=%s reason=retry_budget_exhausted",
                call_site,
                status,
            )
            return last_response
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
