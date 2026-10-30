# v0.9.7 live walk plan (Copilot Chat probes)

Live row plan for the full end-to-end walk in
[#187](https://github.com/Satscryption/Hermes-A365/issues/187). Tenant
standup, gateway modes, and the #90 matrix checklist stay on that issue and in
[`references/live-tenant-test.md`](../references/live-tenant-test.md). This
file records **supplemental Copilot Chat (Path B, B-CC) rows** exercised on
walk094 / walk095 / walk097 profiles (`evidence.sh`, `log.md`, `agent.log` at
DEBUG).

Record pass/fail, timestamps, and sanitized evidence paths on the walk issue.
Do not copy secrets or tokens into the log.

## Shared keep-alive UI expectations (K1–K4)

These rows use a long tool (Hermes terminal `sleep` or equivalent) after optional
lead-in text. They validate informative streaming and rotation, not the #90
matrix **K** card rows (exec approval, clarify, feedback, invoke).

Before the gateway receives the user message, Copilot Chat shows its own
client placeholder (`Lining things up...`). That line is not under Hermes
control.

After the model starts the turn, the first gateway-owned informative text
usually appears on the **first interim send** (commentary or tool-progress
segment), via `_schedule_prompt_keepalive_emit` / `_schedule_informative_segment_refresh`
in `adapter.py`, not only after the scheduled keep-alive sleeper. Wall-clock
delay depends on the model and gateway; treat “about +2s” as order-of-magnitude,
not a hard gate.

Once an informative stream is open:

- The visible line is the **pending tool-progress text** when one exists (for
  example `💻 Running sleep 40`), chosen by `_informative_text_for_turn` from
  the first pending progress/commentary segment.
- **Heartbeats** on the same stream repeat that line about every
  `_TURN_BUFFER_KEEPALIVE_INTERVAL_SEC` (**25.0** s), after the first
  `_TURN_BUFFER_KEEPALIVE_FIRST_SEC` (**25.0** s) sleeper marks
  `keepalive_first_interval_elapsed` (unless an earlier interim send already
  opened the stream).
- When no tool-progress segment is pending and no answer preview applies,
  `_informative_text_for_turn` falls through to `_visible_informative_fallback_text`:
  latest progress/commentary on the turn, else `_KEEPALIVE_ROTATION_STATUS_TEXT`
  (`Still working on this. I'll send the answer as a new message.`). The literal
  `_TURN_BUFFER_KEEPALIVE_TEXT` (`Working on it.`) is the placeholder informative
  body and the default when the turn record is missing; it is not the usual
  heartbeat line when tool progress is present.

Stream **rotation** (K4): Copilot enforces a **120.0** s informative-stream cap
(`_TURN_BUFFER_KEEPALIVE_STREAM_CAP_SEC`). The adapter closes and replaces the
stream at `_keepalive_rotation_age_sec()` (**115.0** s = cap minus
`_ANSWER_FINAL_STREAM_CAP_MARGIN_SEC` **5.0** s) when no kept answer segments
block rotation. Expect one keep-alive final on the old `streamId`, then a new
informative sequence (or plain delivery after rotation when configured).

## S — Copilot Chat smoke (marked fast path)

Path: **B-CC** only unless noted. Prompts are representative; prefix with the
walk profile tag (for example `walk097`).

| Row | Prompt (representative) | Expected UI | Expected log (minimum) | Notes |
| --- | --- | --- | --- | --- |
| S0a | New Copilot conversation → marked short hello (first turn after add) | One stream shell; answer text visible without empty first bubble | `streamType=informative` opener then one `streamType=final`; min stream age before final on first turn (`_COPILOT_FIRST_TURN_STREAM_TIMING_DEFAULT_SEC` **10.0** s when `copilot_post_member_added`) | #217 first-turn rendering |
| S0b | `walk095 smoke: reply with a short hello` (marked turn) | Opener text matches answer; one final on same `streamId` | No `send_reply` for answer; typing informative + one message final | walk095 fast path (#227) |
| S0c | In the **same** Copilot conversation as S0b → second marked short hello (for example `walk095 smoke 2`) | Same as S0b: preview opener + one final; not a bare client hang | Second turn repeats S0b stream path; no second-conversation first-turn bug | Separates first-turn (#217) from follow-turn rendering (#227) |

## K — Copilot Chat keep-alive (long tool)

Path: **B-CC**. Use lead-in + `sleep` probes from walk094 unless noted.

| Row | Prompt (representative) | Expected UI (keep-alive) | Expected log (minimum) | Notes |
| --- | --- | --- | --- | --- |
| K1 | Lead-in + `sleep 40` + short marked answer (walk094 K1) | After client placeholder, informative line shows **tool progress** (`💻 Running …`), not `Working on it.`; heartbeats every **~25 s**; final is answer only | `keep-alive started`, informative POSTs with progress text, `turn reply delivered` | Commentary lead-in must not appear in final (#214) |
| K2 | Two lead-ins + `sleep 40` + answer (walk094 K2) | Same keep-alive text rules as K1 | Same as K1 | Second lead-in stays informative only |
| K3 | Lead-in + `sleep` long enough for stream age **~100 s** before answer (walk094 K3) | Progress line on stream throughout; rotation must remain **allowed** before post-tool answer | Stream age approaches cap without spurious “stuck” shell; no duplicate answer bubbles | Pre-#214: rotation blocked when lead-in was kept as answer |
| K4 | Lead-in + tool **> 120 s** (for example `sleep 130`, walk094 K4) | Near **~115 s** stream age, one rotation close on old id; post-tool answer lands once (stream or plain after rotation per #247) | Rotation final ~115 s (`_keepalive_rotation_age_sec`); cap **120.0** s; `test_walk094_k4_keepalive_rotates_past_two_minute_stream_age_with_leadin` | Exercises stream-cap rotation (#214, #247) |

## References

- Code: `_informative_text_for_turn`, `_visible_informative_fallback_text`,
  `_TURN_BUFFER_KEEPALIVE_*`, `_keepalive_rotation_age_sec` in
  `src/hermes_a365/plugin/adapter.py`.
- Design context: [`docs/design/226-preamble-vs-answer.md`](design/226-preamble-vs-answer.md)
  (L/K overlap), [`reports/247-rotation-final.md`](../reports/247-rotation-final.md).

Refs [#187](https://github.com/Satscryption/Hermes-A365/issues/187),
[#216](https://github.com/Satscryption/Hermes-A365/issues/216).
