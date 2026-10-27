# walk095 Copilot fast-reply / empty bubble (Refs #227)

## Head SHA

Update after push: `git ls-remote origin cursor/walk095-copilot-fast-reply-0782`.

## Root cause (what fixed live on ac7e7463)

Live walk gateway evidence (S0b/S0c/L1/G1/L3) on `ac7e7463` used the **production stream path** for marked turns:

1. `type=typing` / `streamType=informative` opener (HTTP 201, new `stream_id`)
2. One `turn buffer answer final` on the same `stream_id` (after `#217` min-stream-age wait where applicable)

The **`_answer_plain_before_keepalive_interval` → `send_reply` branch did not run** in production: Hermes marks answers (`reply_to` set), so that gate is always false.

Compared to `dd036fc3`, the live-relevant delta on `ac7e7463` was:

| Change | Ran in live marked stream path? | Effect |
|--------|----------------------------------|--------|
| Plain fast `fallback` before opener | No (marked answer) | None live |
| `_placeholder_informative_opener_allowed` (`for_answer` removed) | Only on `for_answer=False` keep-alive posts | No direct opener effect |
| **`_sanitize_informative_stream_text` on keep-alive informative posts** | Yes (seq ≥ 2 when interval elapses) | Stops answer segment text on informative updates (L1/L3/G1) |
| Delivery opener still used `answer_preview` with `for_answer=True` sanitizer bypass | Yes (fast S0b/S0c opener) | Opener could still echo answer until sanitizer applied to `for_answer` (this fix round) |

**Conclusion (a):** Live turns rendered because the adapter kept **stream opener + one stream final** (with min-stream-age), while **`_sanitize_informative_stream_text` prevented answer bodies on mid-turn informative posts**. The plain-reply path was not involved. This PR round removes the dead plain branch and extends sanitization to **all** informative/typing bodies (including the delivery opener), using heartbeat text when the body would leak answer text, while still allowing a placeholder opener when `for_answer=True` opens the stream.

## Payload shape (unchanged)

Final activities remain:

```json
{
  "type": "message",
  "text": "<answer>",
  "entities": [
    {"type": "streaminfo", "streamId": "<id>", "streamType": "final"},
    {"type": "https://schema.org/Message", ...}
  ]
}
```

Informative/typing activities must not carry final answer bodies (including joined, truncated preview, or marked answer segments); pre-tool facts that are classified as answer segments are sanitized off informative and kept for the final.

## Code changes (fix round 1)

- Removed `_answer_plain_before_keepalive_interval` and the early `fallback` in `_deliver_turn_answer_locked`.
- `_informative_text_is_answer_body_leak` + expanded `_sanitize_informative_stream_text` (applies to `for_answer=True` openers too).
- `_placeholder_informative_opener_allowed(for_answer=True)` so a heartbeat opener can open the stream before the first keep-alive interval.
- `for_answer` passed into `_post_keepalive_activity` for placeholder deferral on delivery openers.

## Tests (real runs)

- `pytest tests/test_agent365_plugin.py -k walk095` — marked `reply_to='act-1'`, typing bodies exclude answer text, one final per turn, min-stream-age wait on fast path, G1 fact + closing on final only, direct sanitizer mutation guard.
- `pytest tests/test_agent365_plugin.py` — full plugin module.
- `ruff check src tests` — pass.
- `tests/test_release_integrity.py` — **4 failures pre-existing on `dd036fc3`** (version metadata); not introduced here.

## Relaxed checks

None.
