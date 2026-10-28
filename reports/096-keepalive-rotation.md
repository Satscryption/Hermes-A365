# Walk096 — keep-alive rotation follow-ups (#251, #255)

Base: `main` @ `eacf09ad` (release v0.9.5).

## Issue #251

1. **Rotation-due zero sleep vs full interval** — Already fixed on main by PR #254: `_keepalive_sleeper_wait_after_emit` returns `0.0` when stream age ≥ rotation age (see `adapter.py` ~1135–1138). Covered by `test_g2_sleeper_wait_distinguishes_rotation_due_from_full_interval` and `test_g2_sleeper_loop_zero_sleep_when_rotation_already_due`. No code change.

2. **Past-cap skip-close path** — `_post_keepalive_rotation_final_locked` already plain-retires when `cap_deadline <= now` without POSTing a final. Added `test_g2_rotation_past_cap_skips_close_post` (real `_emit_turn_buffer_keepalive` path, asserts zero rotation finals and warning log).

3. **Empty opener before first interval** — `_informative_text_for_turn` now uses `_visible_informative_fallback_text` instead of bare `Working on it.` when no pending segment/preview; empty sanitized bodies also get fallback regardless of interval flag. Test: `test_informative_text_before_first_interval_uses_progress_fallback`.

4. **Rotation close text** — `_rotation_stream_close_text` now delegates to `_visible_informative_fallback_text` (latest tool progress beats older commentary). Tests: `test_rotation_close_text_prefers_progress_over_leadin`; `test_g2_plain_answer_after_rotation_*` expects progress in rotation final.

5. **Plain route after gap sleep** — `_maybe_route_plain_turn_answer` re-reads `_keepalive_stream` after the gap sleep and skips a redundant progress final if the stream was retired during the sleep. Test: `test_plain_route_after_gap_skips_close_when_stream_retired`.

## Issue #255

1. **Non-retryable 4xx on rotation close** — `_rotation_close_failure_plain_retire` now plain-retires on 400/401/404 and non-terminal 403 (429 still defers). Tests: `test_rotation_close_failure_plain_retires_client_errors`, parametrized `test_g2_rotation_close_client_error_plain_retires_single_post`.

2. **Real-loop backoff test** — Replaced copied if-condition test with `test_g2_sleeper_backoff_never_double_rotation_due_zero`, which runs `_turn_buffer_keepalive` under fake monotonic clock and fake `asyncio.sleep`.

## Verification (real runs)

- `ruff check` on touched paths (pass)
- Full `pytest` on Python 3.12 (pass except four pre-existing `tests/test_release_integrity.py` failures also present on base `eacf09ad`; metadata validation / smoke harness)
- Python 3.11 not installed on the cloud VM (`python3.11` package unavailable); 3.12 run only

## Stubs

- `test_plain_route_after_gap_skips_close_when_stream_retired` replaces `_sleep_turn_buffer_stream_gap_locked` to simulate keep-alive retirement during the gap (HTTP POST stub still used for finals counter only).

## Relaxed checks

None.

## Open questions

- Run full suite on Python 3.11 in CI or a local 3.11 venv to satisfy the matrix item not covered here.

## Grok review round 1 (PR #258)

- P2: `test_g2_sleeper_backoff_never_double_rotation_due_zero` uses rotation-close 429 defer, fake `asyncio.sleep` recorder, asserts `0.0` then `1.0` in the real `_turn_buffer_keepalive` loop; `test_g2_answer_final_in_progress_uses_rotation_backoff_sleep` rewritten to the same real-loop harness (delivery blocked, no finals).
- P3: `test_informative_text_before_first_interval_uses_status_without_segments` covers empty-segment status fallback; `test_g2_rotation_close_non_terminal_403_plain_retires_single_post` and `test_g2_rotation_close_429_defers_without_plain_retire` added beside 400/401/404.
- Production code unchanged (review agreed it was correct).
