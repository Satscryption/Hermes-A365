# #247 — keep-alive rotation and plain answer (round 2)

## Design (chosen)

1. **Rotation at 115s stream age** (`120s` cap minus `5s` `_ANSWER_FINAL_STREAM_CAP_MARGIN_SEC`). No second Copilot informative stream after rotation.
2. **Rotation final** uses lead-in / marked-answer preview, else informative segment text, else `_KEEPALIVE_ROTATION_STATUS_TEXT` — never `_TURN_BUFFER_KEEPALIVE_TEXT`.
3. **After rotation**: `keepalive_rotated`, `skip_stream_open`, `plain_due` on the turn; optional plain BF `typing` pulses (no `streaminfo`).
4. **Answer**: one plain `send_reply` with partitioned flush body (facts + confirmation). `keepalive_rotated` / `plain_due` cleared only after plain post succeeds.
5. **No rotation, stream age > 90s** (`cap - FINAL_BUDGET`): close stream with real rotation text, then plain answer (no answer `streamType=final`).
6. **#246 preserved** (merged `9d882081`): answer-preview opener and sanitizer only after `keepalive_first_interval_elapsed`.

## Round 1 live gap

Closing the rotated stream at answer time with placeholder text left orphan “Working on it.” bubbles. Round 2 retires the stream once at 115s with real text and does not open stream 2.

## Tests

`test_issue247_*`, updated rotation/walk094 tests, failed-close plain path, age 100 vs 115 rotation.

## Verification

Full `pytest` (see CI / local run). `test_release_integrity` may fail on editable installs without a clean wheel.

## Relaxed checks

None.
