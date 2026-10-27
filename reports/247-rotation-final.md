# #247 — keep-alive rotation and plain answer (round 3)

## Design (chosen)

1. **Rotation at 115s** (`120s` cap minus `5s` margin). No second informative stream after rotation.
2. **Rotation final** uses latest tool progress / commentary or `_KEEPALIVE_ROTATION_STATUS_TEXT` — never marked-answer or fact text.
3. **Sleeper**: after each keep-alive emit, sleep `_keepalive_sleeper_wait_after_emit` (`rotation_wait` if `> 0`, else interval) so the close starts near **115s**, not 125s. Never extend outbound deadline past `opened + 120s`; if past cap, skip close POST and route plain.
4. **After rotation**: `keepalive_rotated`, `skip_stream_open`, `plain_due`; keep-alive task stops (no bare typing pulses, #154).
5. **Answer**: one plain `send_reply`; flags cleared only after plain success. Stream age **> 90s** without rotation → close + plain. Gap sleep, #217, #246 fast-turn preserved.

## Live gap (f0ec1ba3 walk095 L2-r3 / G2-r3)

| Observation | Fix |
|-------------|-----|
| Close final ~121s → **403** | Rotation-aware sleeper + cap deadline (no `now + 25s` extension) |
| Bubble 1 **empty** (header only) whole turn | Post-interval informative posts used sanitizer fallback to bare `Working on it.`, which Copilot Chat rendered as empty chrome; heartbeats now use `_visible_informative_fallback_text` (tool progress / commentary / status line) |
| Fact in close + plain (#226) | Close text excludes marked answer; fact only in plain bubble |
| G2 sidebar ghost | Client flake (out of scope) |

### Blank bubble root cause

After #246, once `keepalive_first_interval_elapsed` is true, answer-shaped informative text is replaced with `_TURN_BUFFER_KEEPALIVE_TEXT`. On long marked sleep turns, later heartbeats had no undisplayed progress segment in the pending queue (tool progress already shown), so every seq ≥ 2 posted only that placeholder. Copilot Chat accepted the activities (202) but displayed an **empty** stream shell—no “Working on it.”, status, or lead-in—until the late failed close and plain answer bubble.

## Tests

`test_issue247_*` (sleeper ~115s, visible informative text, plain path, no posts after rotation), updated walk095 sanitizer/fallback expectations, rotation close without fact duplication.

## Verification

`pytest --ignore=tests/test_release_integrity.py` → **2288** collected, **2287 passed**, **1 skipped** (`test_keychain_live`).

## Relaxed checks

None.
