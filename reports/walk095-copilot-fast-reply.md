# walk095 Copilot fast-reply / empty bubble (Refs #227)

## Head SHA

Verified after push with `git ls-remote origin cursor/walk095-copilot-fast-reply-0782`.

## Root cause

Two related bugs in Path B (`groupChat`) turn-buffer delivery on `dd036fc3`:

1. **Fast turns (S0b/S0c)** — When the model answer is ready before `keepalive_first_interval_elapsed` (25s) and no keep-alive stream exists yet, `_deliver_turn_answer_locked` still opened a `streamType=informative` activity (`for_answer=True` bypassed the deferred placeholder rule). Copilot rendered that opener as an empty shell, then either a second bubble with the answer (turn 1) or a stuck empty bubble and client timeout (turn 2).

2. **Slow turns (L1/L3)** — Informative stream updates could carry **answer body text** as `type=typing` / `streamType=informative` (sequence ≥ 2) when an answer segment was already buffered. That violates the #182 contract (progress/commentary/heartbeat only on informative; answer only on `streamType=final`) and matches live logs: posts succeed with `final_chars>0` while the UI shows a single empty “agent said” bubble.

## Payload diff vs `f612899e` (v0.9.4)

Captured K1-shaped and slow-turn activities via unit harness (`scripts/compare_payloads_k1.py`, real pytest wiring — not fixtures):

| Scenario | `f612899e` vs `dd036fc3` (pre-fix) | After this fix |
|----------|--------------------------------------|----------------|
| K1 marked slow stream (opener + seq=2 + final) | **Identical JSON** (md5 match) | Unchanged final shape; seq=2 informative text forced to heartbeat when it would equal an answer segment |
| Fast unmarked `reply_to=None` (walk095 S0b) | Pre-fix: informative open + final (2 POSTs) | **One plain `send_reply`** (no `streaminfo` POSTs) |

Pre-fix `dd036fc3` already matched v0.9.4 on the wire for the slow marked path; the regression is **routing text into the wrong stream phase**, not a missing field on the final activity. Finals remain:

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

## Code changes

- `_answer_plain_before_keepalive_interval` + early `fallback` in `_deliver_turn_answer_locked` for unmarked fast turns.
- `_placeholder_informative_opener_allowed(for_answer=…)` respects `keepalive_first_interval_elapsed` (no special case for answer openers).
- `_sanitize_informative_stream_text` blocks answer segment text on informative POSTs.

## Tests (real runs)

- `pytest tests/test_agent365_plugin.py -k walk095` — pass on fix; **fail on `dd036fc3` adapter** for `walk095_fast` / `walk095_second` (mutation check: informative opens present pre-fix).
- `pytest tests/test_agent365_plugin.py` — pass (full plugin module).
- `ruff check src tests` — pass.
- `tests/test_release_integrity.py` — **4 failures pre-existing on `dd036fc3`** (version metadata drift); not introduced here.

## Relaxed checks

None.

## Open questions

- Whether Copilot will accept plain `send_reply` for all fast unmarked turns long-term, or we should add a single `streamType=final` without a prior informative when latency budget allows.
- Live re-walk of L1/L3 after deploy to confirm UI renders once plain-fast and sanitized slow paths are live.
