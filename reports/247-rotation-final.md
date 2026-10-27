# #247 — keep-alive rotation timing and post-rotation answer delivery

## Symptom (walk095 L2/G2)

Long turns (~140–150s) showed Copilot stuck on “Working on it.” Gateway logs showed `keep-alive final` on the first informative stream ~70s after open, then a second rotation, then `answer final` on the third stream with HTTP 202 while the UI never replaced the bubble.

## Root cause — early rotation (~70s)

`_keepalive_rotation_due` used `_TURN_BUFFER_KEEPALIVE_STREAM_MAX_AGE_SEC`, defined as `120 − 30` (`_TURN_BUFFER_KEEPALIVE_FINAL_BUDGET_SEC`). That 30s budget is for answer-final delivery margin (`_answer_final_cap_remaining_sec`), not for keep-alive rotation.

With a 25s informative interval, `age + interval >= 90` fired rotation at stream age ~65–70s (after seq 2/3 at 25s steps), matching gateway timestamps.

## Root cause — final on rotated stream not rendered

After rotation, `_post_keepalive_final_locked` clears `stream_id` and sets `closed_by = keepalive`, then a new informative stream opens (“Working on it.”). Adoption replaces keepalive state, so `closed_by` is lost. Answer delivery then posts `streamType=final` on the **rotated** informative `streamId`. Live walk095 showed 202 responses that Copilot did not paint (likely one visible stream per `replyToId` / second stream not honouring answer finals).

Design doc #182 rule 6 assumes opening `for_answer` on an empty id after `closed_by=keepalive`; that path does not apply when a rotated informative stream is already open with a live id.

## Fix

1. **Rotation timing:** `_keepalive_rotation_due` now uses the full `_TURN_BUFFER_KEEPALIVE_STREAM_CAP_SEC` (120s): rotate when `age + interval >= cap` (e.g. age ≥ ~95s with a 25s interval), not at ~70s. Turns whose stream never reaches that age do not rotate.

2. **Post-rotation answer:** On rotation, set `buf["keepalive_rotated"] = True`. At answer flush, if that flag is set and a stream id remains, POST a keep-alive-text final on that stream (`for_answer_delivery=True` bypasses `answer_final_in_progress` guard), then **plain** `send_reply` with the merged answer (safe default when stream finals on a rotated id are unreliable).

## Tests added/updated

- `test_issue247_rotation_not_due_before_stream_cap_minus_interval` — 70s age must not rotate; cap−interval+ε must.
- `test_issue247_answer_plain_after_keepalive_rotation_with_reply_to` — `reply_to='act-1'`, rotation, one plain answer, no answer stream final, two keep-alive finals.
- `test_issue247_short_turn_under_cap_never_rotates` — age 70s emit does not rotate; single answer stream final.
- Updated rotation threshold tests to use 120s cap arithmetic.

## Verification (real runs)

```text
python3 -m pytest -q --ignore=tests/test_release_integrity.py
```

All tests passed except one skipped live keychain test.

Full suite including `tests/test_release_integrity.py`: **4 failures** — `hermes-a365 --help` / wheel metadata checks after editable `pip install -e ".[dev]"` in this VM (`ModuleNotFoundError: hermes_a365` on CLI shim). Not caused by this patch; re-run release_integrity in a clean release build environment.

## Relaxed checks

None.

## Open questions

- Whether Copilot will ever render an answer `streamType=final` on a post-rotation informative stream if we later want to retry streaming instead of plain fallback.
- Optimal rotation threshold if the service hard-stops slightly before 120s wall clock (current logic matches interval-before-cap model from #182 comments).
