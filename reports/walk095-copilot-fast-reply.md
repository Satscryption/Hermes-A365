# walk095 Copilot fast-reply / empty bubble (Refs #227)

## Head SHA

Verified: `git ls-remote origin cursor/walk095-copilot-fast-reply-0782` → `9e0b359b1e5219878e6c7cf3962bc24c4321acb4`.

## Behavior

### Fast turns (answer ready before `keepalive_first_interval_elapsed`)

Marked Hermes turns (`reply_to='act-1'`) use **stream delivery** like `ac7e7463`:

1. `type=typing` / `streamType=informative` opener with the **answer preview** text (not `Working on it.`)
2. One `streamType=final` on the same `stream_id` (after `#217` min-stream-age when configured)

The sanitizer **does not** rewrite this opener. Placeholder/heartbeat openers stay deferred until the first keep-alive interval.

### Slow turns (after first keep-alive interval)

Informative updates (heartbeat, seq ≥ 2) must not carry answer bodies (exact, joined, truncated, or prefix of merged answer segments). `_sanitize_informative_stream_text` runs only when `keepalive_first_interval_elapsed` is true.

## Root cause summary

| Issue | Cause |
|-------|--------|
| `dd036fc3` empty / double bubbles | Answer text on mid-turn informative posts |
| `858e62c8` fast turn regression | Sanitizer + `for_answer` placeholder bypass forced heartbeat opener before interval |
| Fix round 2 | Restore answer-preview delivery opener on fast turns; sanitize only post-interval informative posts |

Plain `send_reply` fast path remains removed (never ran for marked turns in production).

## Tests

- `pytest tests/test_agent365_plugin.py -k walk095`
- `pytest tests/test_agent365_plugin.py`
- `ruff check src tests`

## Relaxed checks

None.
