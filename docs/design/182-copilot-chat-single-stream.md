# #182 — Copilot Chat: one Bot Framework stream per turn

Status: design only. Do not treat this file as shipped behaviour.
Target issue: [#182](https://github.com/Satscryption/Hermes-A365/issues/182).
Tree this was written against: `main` `f4d85bcd` (hermes-a365 0.9.2).
Out of scope for the implementation PR: [#181](https://github.com/Satscryption/Hermes-A365/issues/181), [#186](https://github.com/Satscryption/Hermes-A365/issues/186), unless a change below is impossible without touching them. Do not merge the implementation until a live Copilot Chat check confirms the placeholder fills.

The implementation PR should say `Fixes #182` and stay draft.

## Root cause (walk092)

Walk: 2026-10-19, profile `walk092`, hermes-a365 0.9.2 at `f4d85bcd`, CEA manifest v1.1.4, Path B, `streaming.enabled: true`, DEBUG logs. Times in the issue are BST (UTC+1). Log path: `~/.hermes/profiles/walk092/logs/agent.log`. The walk is not in this repo.

Repro: Copilot Chat, `walk092 cc-recheck: reply with a short hello`.

What the client did:

- About 1s after send (19:29:15.5) Copilot drew an empty placeholder bubble.
- The gateway had not received the activity yet. Inbound was 19:29:18.100, after a `conversationUpdate` at 19:29:17.866. The placeholder is the client's response slot, not a bubble we posted.

What we posted:

- The turn is non-personal. Copilot Chat arrives as `conversationType=groupChat`, `channelId=msteams`, `19:…@thread.v2`, so `on_processing_start` opens a turn buffer and `send` only appends local `turnbuf:` segments.
- The answer left as one plain `message` from `_flush_closed_turns` → `_send_reply_activity` → `activity_bridge.render_reply_activity`. Issue cites `adapter.py` comments at 1583–1603, the flush call at 6075, and the reply build at 4945–4951. Those lines match `f4d85bcd`.
- The issue quotes `turn reply delivered: segments=1 chars=6` at 19:29:22.384. That exact string is not in this tree; the flush path above is the post it describes. "Hello." showed up about 1s later as a second bubble beside the empty placeholder.
- Same shape on C2 (19:30:43, the 45s `sleep 40` turn, three segments) and C3 (19:29:40). A 30–50s text lag in the 19:17 smoke did not recur.

Why the placeholder stays empty:

The #155 keep-alive is a different activity sequence from the answer. It posts `type=typing` / `streamType=informative`, then, immediately before the plain reply, `type=message` / `streamType=final` whose **text is `Working on it.`**, then a **second** plain `message` with the answer and no `streamId`. On the short hello the keep-alive never starts (`_TURN_BUFFER_KEEPALIVE_FIRST_SEC` is 25s; the turn finished in ~4s), so the only outbound activity is the plain message.

Microsoft's ordering rule is that anything sent after the stream's final is a new activity. A plain message is not an update to the stream that occupies the response slot. #154 already showed a bare `typing` (no `streaminfo`) is accepted and then swallows the following reply, so the fix is not "send typing, then the plain message".

This revises the #54 conclusion that Copilot Chat must not be given a streaming answer. #54 / PR #63 sent the **personal** path (`streamType=streaming` chunk edits) at Copilot Chat. Those activities returned 2xx and nothing rendered, so v0.7.2 coalesced the turn into one `send_reply`. That coalesce is still right for *how Hermes produces chunks* (see below). It is the wrong *wire shape for the finished answer*. The answer has to be `streamType=final` on one informative stream.

## Protocol (checked 2026-10-20)

Two Microsoft docs govern this. They disagree with the current "do not stream the answer" row in `references/activity-protocol-shapes.md`.

1. [Stream agent messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux) (Teams platform, page updated 2026-08-24). REST section "Stream message through REST API".
2. [Custom Engine Agents for Microsoft 365](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-custom-engine-agent), section "Message ordering and streaming behavior".
3. Copilot's idle limits, cited from the custom-engine async flow and from the Agents SDK discussion of that flow: an initial response is expected within about 15s, then about 45s between streaming updates drops the turn. A stream must finish within two minutes (`Content stream finished due to exceeded streaming time`). Informative updates do not extend that two-minute cap. The adapter comments at `adapter.py` ~778–796 already record this.

Agents SDK `StreamingResponse` is the same sequence under different names: `queueInformativeUpdate`, then text chunks, then `endStream()`. `endStream()` is the final activity. `streamId` appears on the response after the first update. Do not call the personal `streamType=streaming` editor for Copilot Chat; the SDK's informative-then-final mapping is the one #182 needs.

### Activity shapes

All three are POSTs to `{serviceUrl}/v3/conversations/{id}/activities`. They are not PUTs. The next POST waits for the previous response.

| Step | `type` | `entities[streaminfo]` | `text` | Success |
|---|---|---|---|---|
| Start | `typing` | `streamType` `informative` or `streaming`, `streamSequence` 1, **no** `streamId` | required, non-empty | **201** body `{"id": "<streamId>"}` |
| Continue | `typing` | same `streamId`, `streamSequence` 2, 3, …, `streamType` `informative` or `streaming` | required | **202** |
| Final | `message` | `streamType` `final`, same `streamId`, **no** `streamSequence` | the answer | **202** |

`entities.type` is `streaminfo` (the doc's property table says `streamInfo`; the JSON samples and this code use `streaminfo`).

Rules that the implementation has to keep:

- One stream per user turn. Do not start another until the previous one is finalized. Do not send streaming updates after the final; they are separate activities and show up out of order. A deliberate follow-up after the final uses `replyToId`.
- Informative text stays in the bubble until the next informative update or the first streamed/final text replaces it. Cap: 1000 characters (1 KB). Empty or whitespace start text is `400` `Start streaming activities should include text`.
- `streamType=streaming` chunks must be prefix-cumulative (`A brown`, then `A brown fox`). A chunk that does not start with the previous text is `403` `Request streamed content should contain the previously streamed content`. **#182 does not send `streamType=streaming`.** The answer is a single final, not token chunks. That avoids the prefix rule and avoids re-entering the path #54 found Copilot Chat does not render.
- `streamSequence` starts at 1 and increments by 1 on every non-final POST. It is omitted on the final. `streamType=final` without `streamId` is `400`.
- Attachments, the AI-generated label, the feedback button, and sensitivity labels are allowed only on the final message.
- Pace: hard throttle about 1 request/second; the doc asks senders to buffer streaming chunks 1.5–2s. This repo already uses `_STREAMING_MIN_GAP_SEC = 1.5` between personal `edit_message` POSTs.
- Two-minute cap from stream open. `403` `Content stream finished due to exceeded streaming time` / `Content stream was canceled by user` / `Content stream is not allowed on an already completed streamed message` / `Content stream is not allowed`.
- The Teams page also says streaming is supported only in 1:1 chats. Copilot Chat is not 1:1 on the wire, and it still accepts these activities (walk092 and #54 both saw 2xx). A real Teams group chat is the same shape. See Risks.

Personal Teams 1:1 stays on the other path: `_send_stream_start` / `edit_message` with `streamType=streaming` and a final that requires the 201 id (#161). Do not send those chats through the turn buffer.

## Delivery path

### What stays

Hermes cancels the stream-consumer task at turn end. A POST inside `send` dies as `outbound operation cancelled` and the rest of the answer is dropped (#123). So `send` / `edit_message` on an open turn buffer keep appending and replacing local segments only. They must not POST.

`on_processing_complete` still flushes on the session task, still closes only the innermost turn, and still leaves outer `turnbuf:` ids editable.

The keep-alive task (#155) still waits `_TURN_BUFFER_KEEPALIVE_FIRST_SEC` (25s) and still posts informative updates on `buf["keepalive"]`. This design does not make it fire, and does not move its first post. That is #181.

Tool-progress and interim commentary are routed to informative stream text (#186); only `kind=answer` segments are joined into the `streamType=final` body.

Personal chats never open `_turn_buffers`.

### What changes

The turn buffer has one reply stream. `buf["keepalive"]` is that stream (`stream_id`, `sequence`, `opened_mono`, `untracked`, `stopped`). Do not add a second map.

The merged answer is the stream's `streamType=final`, not a plain message posted after a final whose text is `Working on it.`

```
informative (keep-alive, or one opener at flush)  →  201 streamId
        … more informative updates, same streamId, sequence + 1 …
final message, same streamId, text = merged answer, no streamSequence
```

No `send_reply` for that answer when the final is accepted.

### Functions

All in `src/hermes_a365/plugin/adapter.py` unless noted.

| Function | Change |
|---|---|
| `_post_keepalive_activity` | Add an explicit `text` argument. Default remains `_TURN_BUFFER_KEEPALIVE_TEXT` so rotation, teardown, and informative updates stay "Working on it." Add `message_chrome: bool = False`. When true **and** `final`, append `build_ai_message_entity()` and, if `_feedback_enabled`, `channelData` feedbackLoop. Informative POSTs never get that chrome. Do not set `replyToId` on stream activities (the current keep-alive POST and the streaming-ux samples omit it). |
| New `_deliver_turn_answer_locked` | Caller holds the buffer lock. Posts the answer as the final, or one plain fallback in the cases below. Returns whether the answer was accepted. |
| `_flush_closed_turns` | For each non-empty kept chunk, replace "close keep-alive, then `_send_reply_activity`" with `_deliver_turn_answer_locked`. The first chunk of a turn uses the open stream. A later chunk that does not fit in `MAX_MESSAGE_LENGTH` (4000) is a plain `send_reply` **after** the final, with `replyToId`, because a second final on the same id is `403` already completed. Handoff suffix stays on the last chunk when it fits; `link_alone` stays a plain follow-up after the stream is closed. |
| `_close_turn_buffer_keepalive_locked` | Still the close that sends **keep-alive text**. Use it for rotation, for a progress-only turn that already has a `stream_id`, and for teardown. Do not call it in front of the answer. |
| `_emit_turn_buffer_keepalive` / `_post_informative_locked` | Unchanged posting rules. They must keep using the same `stream_id`. After this change their final is no longer "the thing before the reply"; the reply is the final. |
| `activity_bridge.render_reply_activity` | Unchanged. Plain fallback and post-final follow-ups still go through it. |
| Personal `_send_stream_start`, `edit_message`, `_finalize_open_personal_stream` | No behaviour change. |

`_deliver_turn_answer_locked` decision order, lock already held. Answer-final outcomes, nested turns, and user cancel are normative in [Answer delivery state machine](#answer-delivery-state-machine). Where that section disagrees with a step below, that section wins.

1. **`stopped`** (user cancel 403). Run `apply_user_cancel` on this turn's delivery record. Do not plain-post, and do not skip the plain post, without reading that record.
2. **`untracked` and still inside the two-minute cap.** No id, so the answer cannot name the stream, and a second start would be a second bubble (#154). Return not-delivered. The existing deferred retry waits out the cap, then posts. Do not open another stream.
3. **No `stream_id`.** POST one informative opener (`streamSequence` 1, no `streamId`, text = keep-alive phrase). 
   - 201 with an id: store it on `buf["keepalive"]` the same way `_post_informative_locked` does, then continue to step 4.
   - 2xx without an id, or `ReadTimeout` / `WriteTimeout` on that opening POST: mark `untracked` and return not-delivered (same as today's opener). `ConnectTimeout` / `PoolTimeout` are not untracked.
   - Other failure (connector down, 4xx/5xx, including `403` `Content stream is not allowed`): do **not** remember a stream id. One plain `send_reply`. Log a warning that names `chat_id` and status. This is the real-Teams-group escape and the "streaming rejected" escape.
4. **`stream_id` set, and this turn is allowed to final it** ([Stream sharing](#stream-sharing)). Do not POST a keep-alive-text final first. If `last_emit` is within `_STREAMING_MIN_GAP_SEC`, sleep the remainder **outside** the lock (same pacing as personal `edit_message`). Re-check the buffer and the id after the sleep. Then POST `type=message`, `streamType=final`, that `streamId`, no `streamSequence`, `text` = the chunk, `message_chrome=True`. Classify the result with `_classify_answer_final_post` and take exactly one transition from the answer-delivery table. The old four-way split (2xx / post-send timeout / any of those 403s / other failure) is withdrawn.

Record `last_emit_mono` on the keep-alive dict whenever an informative or final POST returns. The 25s keep-alive interval already exceeds the gap; the new back-to-back case is "opener then final" inside one flush.

Progress-only turns (every segment omitted, nothing kept) do not open a stream. If one is already open, the existing end-of-flush `_close_turn_buffer_keepalive_locked` still finals it with keep-alive text so the client is not left inside it.

Nested Hermes turns share one buffer and may share one `streamId`. They do not share a delivery record. Who may final that id, and how the other turn's text is posted, is [Stream sharing](#stream-sharing). Do not hold one stream open across both answers, and do not open a second informative stream for a turn whose text was not the final.

### Final payload (answer)

```json
{
  "type": "message",
  "text": "<merged kept segments>",
  "from": "<inbound.recipient>",
  "recipient": "<inbound.from>",
  "conversation": "<inbound.conversation>",
  "entities": [
    {"type": "streaminfo", "streamId": "<id from 201>", "streamType": "final"},
    {"type": "https://schema.org/Message", "@type": "Message",
     "@context": "https://schema.org", "additionalType": ["AIGeneratedContent"]}
  ],
  "channelData": {"feedbackLoop": {"type": "default"}}
}
```

`channelData` only when feedback is enabled (`A365_FEEDBACK_LOOP`, default on). No `streamSequence`. No `replyToId` on this activity. Citations are not on the turn-buffer reply today (`_append_turn_buffer_segment` stores text only, and the flush calls `_send_reply_activity` without citations). Do not add citation plumbing in this PR.

Informative opener / keep-alive update:

```json
{
  "type": "typing",
  "text": "Working on it.",
  "entities": [
    {"type": "streaminfo", "streamType": "informative", "streamSequence": 1}
  ]
}
```

Continuations add `streamId` and increment `streamSequence`. No AI entity, no feedback `channelData`.

### Why the opener is at flush, not at `on_processing_start`

#182's repro is a ~4s turn. The placeholder is empty because the answer is not the stream final. Opening at flush supplies the 201 id and the final before anything else is sent.

Opening at turn start would fill the slot sooner and would also be the first activity inside Copilot's ~15s initial-response window. It is the right follow-up for #181, not for this PR:

- Every short reply would wait out the 1.5s pacing gap plus a second POST before the user sees the answer.
- It changes when the keep-alive task posts, which is #181's bug surface.

Put the opener behind a function the flush calls, for example by reusing `_post_informative_locked` rather than a one-off POST in the flush. #181 must be able to call that same opener at turn start later **without** the flush starting a second stream. The flush rule "if `stream_id` is set, final that id" is the contract that makes a turn-start open safe.

### Logging

On an accepted answer final, INFO: `chat_id`, `stream_id`, character count. On a plain fallback, WARNING with `chat_id`, status, and the reason (`open rejected`, `stream already closed`, `user cancelled`). Do not add the #181 silent-exit lines here.

`content_delivered` is still not ground truth. A 2xx final can fail to paint. Only a Copilot Chat window can confirm the placeholder became the answer.

### Docs the implementation updates

- `references/activity-protocol-shapes.md` — the Copilot Chat quirks row and "Streaming and reply delivery". Replace "do not stream the answer / one `send_reply`" with: buffer chunks locally; deliver the merged body as `streamType=final` on one informative stream; plain `send_reply` only for the fallbacks above. Keep the #54 note that `streamType=streaming` chunk edits were accepted and not rendered, so this path does not use them.
- `references/m365-surface-coverage.md` — Copilot Chat row, same correction.
- `CHANGELOG.md` under `[Unreleased]` / Fixed, with `#182`.
- The turn-buffer and `send` / `edit_message` comments that still say the non-personal reply is one normal `send_reply` after a keep-alive-text final (around 1551–1607, 4645–4655, 6063–6075, and the `edit_message` docstring near 9773).

Do not rewrite history in `references/live-tenant-test.md` walk notes. A short pointer from the protocol doc is enough.

## How #181 and #186 sit on this

### #181 — keep-alive never posts on a 45s turn

Walk092 C2: inbound 19:29:57, `sleep 40`, turn end 19:30:42, reply 19:30:43. The informative POST was due ~19:30:22. The log has no `started`, `keep-alive:`, `failed`, `POST failed`, `opened without an id`, or `final`. The buffer existed (`segments=3`). Copilot accepted the reply at ~46s this time; the idle drop #155 targets is still open.

#182 does not fix that. A 45s turn still has no outbound activity until flush, so Copilot can still abandon the slot before the answer final.

#181, after this lands:

- Add DEBUG on every silent exit the issue lists: `_turn_buffer_keepalive_needed` false, `stopped` / `untracked`, `_keepalive_turn` is None, `_post_keepalive_activity` returning before POST (`_http_client` / `_bridge_cfg` / lifecycle), task cancel, and the swallowed `RuntimeError("agent365 outbound operation cancelled")`.
- Fix whichever of those is the live cause, so the 25s informative POST actually runs.
- Call the same opener #182 uses. If it runs at turn start or at ≤15s, it must store `stream_id` on `buf["keepalive"]` and then only send `streamSequence` n+1. The flush already finals that id and must not open a second stream.
- Rotation before the two-minute cap stays: final the old id (keep-alive text is acceptable there; the answer has not been produced), then start a new informative sequence. The answer finals whichever id is current only when [Stream sharing](#stream-sharing) says this turn may final it. A `closed_by` of `turn:<other>` is a plain `send_reply`, not a final on that id.
- Do not have the keep-alive post its own `streamType=final` when segments are waiting. That final would close the bubble before the answer and bring #182 back.

### #186 — final text includes progress and commentary

Walk092 C2 final included the tool-progress line and interim commentary (`segments=3`, 58 characters). The intended split is progress in the informative text and only the answer in the final.

Today `_flush_closed_turns` already drops a segment when `_is_hermes_tool_progress_segment` is true (exact Hermes `{emoji} {verb}{connector}{preview}` lines, including `💻 Running …`). A segment that mixes progress with other text is kept whole. Commentary (`reply_to is None`, not a progress line) is joined into the final. `test_turn_buffer_merges_commentary_edited_segment_and_duplicate_final` locks that join. #182 keeps it.

#186, after this lands:

- Informative `text` is already a parameter, not the answer. Pass the latest progress or commentary segment (trimmed to `_INFORMATIVE_TEXT_MAX_CHARS`) on the keep-alive update and on the flush-time opener. Leave the answer out of that text.
- Stop joining those segments into the final. The final `text` is the answer segment only.
- Do not put progress on the final activity, and do not put the answer on an informative activity. After the first `streamType=streaming` chunk, informative updates stop rendering; stay on `informative` for progress.
- The pure-progress filter can stay as a backstop, but #186 should classify by segment role (`reply_to` scoped stream chunk vs unscoped progress/commentary), not only by emoji shape. The walk line may already match the emoji filter and still have been merged because it shared a segment with commentary, or the quoted log may be counting buffer segments before the filter. Check the walk log before changing the filter.
- Per-platform `display.tool_progress: off` remains the workaround that does not need this delivery path.

### Segment classification (#186 shipped)

Each turn-buffer segment carries a `kind` set at append time (and refreshed on `edit_message` when a progress line becomes answer text):

| `kind` | How it is assigned | Informative stream | Answer `streamType=final` |
|---|---|---|---|
| `answer` | default for `send` / `edit_message` unless progress or informative metadata (below) | never | yes — joined into the final body |
| `progress` | text matches `_is_hermes_tool_progress_segment` (Hermes tool-progress bubbles) | latest progress/commentary text on keep-alive and on flush opener | never (emoji filter remains a backstop) |
| `commentary` | `send` metadata `informative_update: true` (interim assistant lines) | same as progress | never |

Plain unscoped `send()` text with no progress shape and no `informative_update` metadata stays `answer` and reaches the final, so existing single-reply turns are unchanged.

When an informative stream is already open (`keepalive.stream_id` set), new progress or commentary segments schedule a continuation POST with the latest informative text. If no stream is open yet, that text is included on the first informative opener at flush (or on the keep-alive opener once #181 fires). Personal Teams 1:1 does not use the turn buffer; this split applies only to Copilot Chat / non-personal buffered turns.

## Test plan

File: `tests/test_agent365_plugin.py`, class `TestEditMessage`. The keep-alive tests start near `test_short_buffered_turn_does_not_send_keepalive`.

Rewrite the assertions that require `send_reply` of the answer and a keep-alive final whose text is `Working on it.`:

| Existing test | New expectation |
|---|---|
| `test_short_buffered_turn_does_not_send_keepalive` | No POST until complete. Complete posts informative (`typing`, sequence 1, no `streamId`, text `Working on it.`) then final (`message`, `streamId` from the 201, no `streamSequence`, text = answer). `send_reply` count stays 0. The keep-alive task does not add a third POST. |
| `test_long_turn_keepalive_final_precedes_one_reply` | Two informative updates, then one final whose text is the answer, same `streamId`. Order is not `final` + `reply`. |
| `test_keepalive_post_failure_keeps_the_reply` | Informative open raises. One plain `send_reply` with the answer. No second open. |
| `test_failed_final_keeps_stream_and_segments` | The failing POSTs are finals whose text is the answer. `send_reply` stays 0. `stream_id` and segments remain. |
| `test_failed_final_retry_then_posts_one_reply` | First answer-final fails, retry final succeeds, no `send_reply`. |
| `test_nested_complete_closes_keepalive_before_followup_reply` | Withdrawn as an expectation. Replace it with the nested rows in the [test matrix](#test-matrix). |
| `test_turn_buffer_merges_commentary_edited_segment_and_duplicate_final` | Wire is the final. Body is still commentary plus answer plus the handoff line. `send_reply` stays 0. This test is the #186 seam; do not drop commentary here. |
| `test_turn_buffer_omits_hermes_tool_progress` and `test_turn_buffer_keeps_one_line_answers_sharing_progress_shape` | Same bodies as now, carried on the final. |
| `test_untracked_open_waits_out_the_cap_then_posts_one_reply` | Unchanged: no second stream; one plain reply after the cap. |
| `test_personal_chat_is_not_turn_buffered`, `test_personal_chat_does_not_schedule_keepalive`, `TestSendStreamStart` | Unchanged. Personal final is still `streamType=streaming` then `streamType=final`. |

Add:

- Answer final entities are `streaminfo` plus the AI message entity. Feedback `channelData` is present only on that final, and only when feedback is enabled. The informative activity has neither.
- Back-to-back opener and final sleep `_STREAMING_MIN_GAP_SEC`. A keep-alive update older than the gap does not sleep again.
- Opening `403` `Content stream is not allowed` → one plain reply, empty keep-alive state.
- User-cancel `stopped` follows `apply_user_cancel` in the state machine (`test_user_cancel_without_landing_plain_posts_once` and `test_user_cancel_after_may_have_landed_does_not_plain_post`).
- Merged body over `MAX_MESSAGE_LENGTH`: first chunk is the final; the next chunk is one plain `send_reply` after the final, not a second `streamType=final`.
- `ReadTimeout` on the answer final drops the segment and does not plain-post.

Run the existing personal streaming tests in `TestSendStreamStart` and `TestEditMessage` so 1:1 `streamType=streaming` did not pick up `informative`.

A live Copilot Chat check is not a unit test. Residual for the #116 / #89 walk: the short hello fills the placeholder as one bubble; a turn under 25s still does it; Teams 1:1 still streams one bubble and clears Stop. `content_delivered` does not count as that check.

## Risks

- **Copilot still does not paint the stream.** #54 saw 2xx and a blank client for `streamType=streaming` chunks. This shape is the one the current CEA and streaming-ux docs describe, but the docs do not say a plain message cannot fill the placeholder. If the walk shows two bubbles again, or a blank slot, the next change is to call the same opener from `on_processing_start` (the #181 hook), not to send `streamType=streaming` chunks from `send`.
- **Flush-time open is late for the 15s window.** Acceptable for the 4s hello. Not acceptable as a substitute for #181 on a 45s turn.
- **1.5s gap on short replies.** A hello that used to be one POST becomes informative, gap, final. That is the documented pacing. Do not skip it on the opener/final pair.
- **Real Teams group chats** share the Copilot Chat shape. streaming-ux says streaming is 1:1-only. An opening `403` `Content stream is not allowed` must fall back to one plain message. A 201 followed by a client that does not render the stream would go silent; the keep-alive already takes that bet for long non-personal turns. There is no shape test that splits the two surfaces.
- **Answer final vs keep-alive final success rules differ.** A 403 "already completed" on a keep-alive-text final means that close succeeded and sets no turn's landing bits. On an answer final, the same status is `ALREADY_COMPLETED` in the state machine: delivered only when that turn's own record already has `may_have_landed` or `had_landed`; otherwise one plain `send_reply` of that turn's text. Do not apply one turn's result to another turn.
- **Timeout double-send.** `ReadTimeout` / `WriteTimeout` on the final must not be followed by `send_reply`.
- **Lock deadlock.** Flush holds the buffer lock. The keep-alive task takes the same lock. Do not wait for the keep-alive task while holding the lock except for the existing `_drop_turn_buffer` cancel, which already has a 5s bound.
- **AI label / feedback regression.** They move from `render_reply_activity` onto the final. Leaving them off the final makes the Copilot bubble lose the #73 label and thumbs. Putting them on informative updates violates streaming-ux.
- **#186 text still in the bubble.** Expected until #186. Do not "fix" it inside #182.

## Answer delivery state machine

Normative for non-personal turn-buffer answer delivery. Written against `16c30731e822c94e7b26dbc1c35ee2f83c349cae`. This section does not change personal Teams 1:1 (`streamType=streaming`), the informative opener, overflow chunks after the first final, or the #181 / #186 hooks except where a rule below names them. An implementer follows this section as written. There is no second legal choice beside a rule that says "the only path".

Classifier, constants, and return codes stay the ones in that tree unless this section renames a field:

| Name | Value |
|---|---|
| `_ANSWER_FINAL_MAX_ATTEMPTS` | 5, counted inside one `_deliver_turn_answer_locked` call |
| `_ANSWER_FINAL_RETRY_STATUSES` | 429, 500, 502, 503, 504 |
| `_ANSWER_FINAL_RETRY_AFTER_CAP_SEC` | 60 |
| `_TURN_BUFFER_KEEPALIVE_STREAM_CAP_SEC` | 120 |
| `_ANSWER_FINAL_STREAM_CAP_MARGIN_SEC` | 5, so the retry clock stops at 115s from `opened_mono` |
| HTTP client timeout on the activity POST | 15s. One in-flight POST may return as late as about 130s from open and is then classified. Do not start another POST once cap remaining is 0. |
| `_TURN_BUFFER_RETRY_DELAY_SEC` | 0.2 |
| `_TURN_BUFFER_DEFERRED_RETRY_LIMIT` | 3 |
| `_TURN_BUFFER_FORCE_PLAIN_MAX_ATTEMPTS` | 3 |
| Force-plain backoff | `min(2 ** plain_attempts * 0.2, 5)` seconds |
| `_STREAMING_MIN_GAP_SEC` | 1.5, unchanged, outside the buffer lock |

`_deliver_turn_answer_locked` still returns `("delivered", id)`, `("retry", None)`, `("fallback", reason)`, `("blocked", None)`, or `("uncertain", None)`. Fallback reasons stay `user cancelled`, `stream already closed`, `stream retry exhausted`, and `open rejected`.

### Ownership

Delivery state is per turn. The chat buffer (`_turn_buffers[chat_id]`) still holds `turns`, `segments`, the one `keepalive` stream, the lock, one `retry_task`, and `answer_final_in_progress`. It does not hold `may_have_landed`, `had_landed`, attempt counts, or `force_plain`.

The map `buf["delivery_by_turn"]` may sit on that buffer object. Every read and every write selects exactly one record by `turn_key`. A transition never reads or writes a different key.

`turn_key` is `f"{seq}:{activity_id}"`. `seq` is the turn's `"1"`, `"2"`, … assigned in `_open_or_nest_turn_buffer`. `activity_id` is the inbound activity id snapshotted on that turn (empty string if missing). `seq` makes the key unique inside the buffer. `activity_id` is the `replyToId` of a later plain post. **`stream_id` is not part of the key.** It is the field `bound_stream_id` on the record. Two turns that share one Teams/Copilot stream id have two keys and two records. Putting `stream_id` in the key would merge those turns or would drop `may_have_landed` when the id is cleared.

Create the record on the first segment appended for that `turn_key`, and also at the start of deliver if flush reaches a turn that has no record yet. Creating turn B's record does not read or write turn A's record. `_open_or_nest_turn_buffer` does not create, reset, or delete any record. `on_processing_complete` does not either.

Record fields. Defaults apply at creation. Nothing else resets them except the delete rule at the end of this subsection.

| Field | Default | Meaning |
|---|---|---|
| `turn_key`, `seq`, `activity_id` | from the turn | Identity. Immutable. |
| `segment_ids` | `[]` | Ids of `buf["segments"]` whose `turn` equals this `seq`. Text stays in `buf["segments"]`. Recompute this list from that filter at the start of each deliver of this turn. |
| `bound_stream_id` | `""` | Stream id this turn has issued an answer-final POST on. Set immediately before that POST, not after it returns. `""` until then. |
| `state` | `PENDING` | One of the states below. |
| `may_have_landed` | false | This turn's answer-final POST may have left the client. |
| `had_landed` | false | This turn has observed `DELIVERED`, or any `MAY_HAVE_LANDED` (timeout or not). |
| `answer_final_attempts` | 0 | Answer-final POSTs in the current deliver call. Set to 1 when the first final POST of that call is issued. Reset to 0 at the start of each deliver call. Not reset by a nested start. |
| `not_sent_probes_left` | 1 | Reset to 1 at the start of each deliver call. |
| `may_have_landed_probes_left` | 1 | Reset to 1 at the start of each deliver call. |
| `deferral_count` | 0 | Scheduled re-flushes for this turn. Not reset between deliver calls. |
| `force_plain` | false | Latched when a schedule is entered with `deferral_count >= 3`. |
| `plain_due` | false | Next deliver of this turn takes the force-plain entry and does not POST a final. |
| `plain_attempts` | 0 | Failed plain `send_reply`s while `force_plain` is true. |
| `force_plain_exhausted` | false | `plain_attempts` reached 3. The scheduler stops. |

Delete the record only when this `seq` has no remaining segments and `state` is `DONE`. Deleting it does not delete or reset any other key. A new turn appended into a buffer that still holds another turn's segments gets a new record at the defaults. Do not run a buffer-wide reset in that case, and do not run one when the segment list is empty: the empty list means every record that reached `DONE` was already deleted.

`answer_final_in_progress` stays buffer-scoped. It means some turn holds the in-flight final POST, the 1.5s gap before it, or a retry sleep. It is not a landing bit. Set it immediately before the gap sleep. Clear it in `finally` on every exit, including cancel. It must not stick on the live buffer.

The turn argument of `_deliver_turn_answer_locked` is the turn `_flush_closed_turns` is delivering. The delivery record is that turn's. Do not call `_keepalive_turn` to choose the record or the inbound snapshot of an answer final. `_keepalive_turn` remains the inbound for informative keep-alive posts only.

### States

| State | Resting? | What is true |
|---|---|---|
| `PENDING` | yes | Record exists. This turn has not posted an answer final in this call. Landing bits are still false unless a previous call set them (then the state would not be `PENDING`). |
| `POSTING` | no | `answer_final_in_progress` is true for this buffer and this turn is the one inside `_deliver_turn_answer_locked`. Segments stay. |
| `MAY_HAVE_LANDED` | no | Both landing bits are true on this record. The same loop takes the next transition before the function returns. |
| `DELIVERED` | no | This turn's text was accepted, or a post-send timeout is treated as accepted, or `ALREADY_COMPLETED` after this record's own landing bit. Immediately go to `DONE`. |
| `DEFERRED` | yes | Same `stream_id` and this turn's segments stay. A later flush will try again. No plain post on the response that entered this state. |
| `FORCED_PLAIN` | yes | `force_plain` is true. The next deliver does not POST a final. It plain-posts, or it goes to `UNCERTAIN` when `may_have_landed` is true. |
| `UNCERTAIN` | no | The accepted silent drop for this turn only. Immediately go to `DONE`. |
| `DONE` | yes | Terminal. Chunk segments for the settled post are removed. No further final and no further plain post of those segments. |

Flush order stays list order: lower `seq` first. The transitions do not depend on which turn runs first.

### Classified outcomes

`_classify_answer_final_post` is unchanged. Apply it in this order; do not reorder the 403 checks:

1. `during_mint` → `NOT_SENT`.
2. Status 2xx → `DELIVERED`.
3. Status 403 and the body contains `already completed` → `ALREADY_COMPLETED`. This includes `Content stream is not allowed on an already completed streamed message`.
4. Status 403 and `_answer_final_terminal_403` → `REJECTED_TERMINAL`. The helper returns true only for `exceeded streaming time`, `canceled by user`, `cancelled by user`, or `content stream is not allowed`, and it returns false when the body contains `already completed`.
5. Any other 403 → `DEFER`.
6. Status in `{429, 500, 502, 503, 504}` → `RETRYABLE`.
7. Status `None` and `timeout_after_send` (`ReadTimeout` or `WriteTimeout`, and not `during_mint`) → `MAY_HAVE_LANDED` timeout.
8. Status `None` and `connect_before_send` (`ConnectTimeout`, `PoolTimeout`, or `ConnectError`, and not `during_mint`) → `NOT_SENT`.
9. Status `None`, empty detail, not `untracked` → `NOT_SENT`.
10. Any other status `None` (non-empty detail: `RemoteProtocolError`, `RuntimeError`, read/write errors) → `MAY_HAVE_LANDED` non-timeout.
11. Any other status `>= 400` → `DEFER`.
12. Anything left → `NOT_SENT`.

Every answer-final POST is classified once and takes exactly one row of the transition table. Writes in that row touch only the posting turn's record.

### Stream sharing

`buf["keepalive"]` remains the single stream (`stream_id`, `sequence`, `opened_mono`, `untracked`, `stopped`, `open_failed`, `last_emit_mono`). Add one field, `closed_by`:

| `closed_by` | Meaning |
|---|---|
| `""` | This id is open, or there is no id. |
| `turn:<turn_key>` | That turn's answer final closed the id (`DELIVERED`, timeout `MAY_HAVE_LANDED`, or `ALREADY_COMPLETED` with that turn's own landing bit). |
| `turn:other` | The id is dead and no turn record has a landing bit for it (service closed it, or `ALREADY_COMPLETED` on a turn that had not landed). |
| `keepalive` | A keep-alive-text final closed an aging id while no turn had kept segments. |

An inner turn **may** close the shared id. It is the turn that POSTs `streamType=final` with the inner body when the pre-POST check below says this turn may final the id. A 2xx, a timeout `MAY_HAVE_LANDED`, or an `ALREADY_COMPLETED` after the inner record's own landing bit sets `closed_by` to `turn:<inner turn_key>` and clears `stream_id`. That close does not write the outer record.

Before this turn `T` POSTs an answer final, apply the first matching rule. There is no other path.

1. `T.force_plain_exhausted` is true: return without posting and without scheduling. Leave `T`'s segments. State stays `FORCED_PLAIN`.
2. `T.plain_due` is true: do not POST a final. If `T.may_have_landed`, run the `UNCERTAIN` procedure and return `uncertain`. Otherwise clear `stream_id` when it equals `T.bound_stream_id` or `closed_by` is `turn:<T.turn_key>`, and return `fallback` / `stream retry exhausted`.
3. `keepalive.stopped` is true: run `apply_user_cancel(T)`. Do not fall through.
4. `stream_id` is non-empty and `closed_by` is `""`: `T` may final this id. Set `T.state = POSTING` and `T.bound_stream_id = stream_id`, then POST.
5. The shared stream is already closed. Do not POST a final on that id. Do not open a new informative stream. Clear a non-empty `stream_id`.
   - **`closed_by` is `turn:<key>` for a key other than `T.turn_key`** (another turn's answer final was accepted, usually 2xx): return `fallback` / `stream already closed`. The flush does exactly one plain `_send_reply_activity` with `T`'s inbound snapshot (`replyToId` of `T.activity_id`, no `streamId`). This turn's text was not stored on the stream. Run this even when `T.may_have_landed` is true: the ambiguous POST on this turn did not settle this chunk, and the other turn owns the close.
   - **`closed_by` is `turn:other`** (the stream was found `ALREADY_COMPLETED` by an unknown or other closer, not attributed to a specific turn key): if `T.may_have_landed` is true, run the `UNCERTAIN` procedure with context `unknown stream closer` and return `uncertain`. Do not plain-post. If both of `T`'s landing bits are false, return `fallback` / `stream already closed` and one plain `send_reply`.
6. `stream_id` is empty and `closed_by` is `""` or `keepalive`: open one informative stream with the existing opener (`for_answer=True`) and, on a 201 id, final that new id with `T`'s text under rule 4. A keep-alive rotation is this case. Do not plain-post only because `closed_by` is `keepalive`.
7. No `stream_id`, opener `untracked` and still inside the 120s cap: return `blocked`. Do not open a second stream. Do not set either landing bit. When the cap has expired, `_clear_expired_untracked` clears an `untracked` opener or an abandoned named `stream_id` that is past the 120s cap, and sets `skip_stream_open`. The next deliver takes rule 8: one plain `send_reply`, no new informative stream.
8. No `stream_id` and the opener failed (`open_failed`, connector down, 4xx/5xx, `403` `Content stream is not allowed` on the opener): do not store an id. Do not set either landing bit. Return `fallback` / `open rejected`.

Rule 5 is the shared-stream rule. The outer turn must not POST on a stream the inner turn completed. The outer text is one plain `send_reply`. It is not a second informative stream, and it is not another final on the completed id.

A keep-alive-text final that closes an id sets `closed_by = keepalive` and clears `stream_id`. It does not set any turn's `may_have_landed` or `had_landed`.

### Procedures

**`UNCERTAIN` procedure** for turn `T`. This is the only accepted silent drop. Log WARNING with `chat_id`, `stream_id`, and one of `terminal stream response`, `stream retry exhausted`, `forced plain after stream retries`, `user cancelled`, `unknown stream closer`. Set `T.state = DONE`. Clear `T.plain_due`. Set `T.answer_final_settled = true`. Clear `stream_id`. If `closed_by` is `""`, set `closed_by = turn:<T.turn_key>`. Remove the chunk's segment ids. Do not call `send_reply`. Return `uncertain`. The flush removes those ids and continues to a later chunk of the same turn only when one exists; a later chunk is a plain `send_reply` of the overflow text, not a second final, and must not open or final a stream on retry. `content_delivered` is not a signal. There is no stream-status GET. A second POST is the probe defined below, not a license to plain-post after `may_have_landed`.

**`apply_user_cancel(T)`.** Both `stopped` sites call this: the check before the gap, and the check after the gap sleep. Read `T` only.

- `T.may_have_landed` is true: `UNCERTAIN` procedure with context `user cancelled`. Do not `send_reply`. A previous POST on this same turn may already be the visible answer.
- `T.may_have_landed` is false and `T.had_landed` is true and `T.answer_final_settled` is false: `T` is already `DONE` for this stream final. Do not `send_reply`. Do not POST. Remove this chunk's segment ids.
- `T.had_landed` is true and `T.answer_final_settled` is true: the stream final for this turn already settled. Return `fallback` / `user cancelled`. The flush plain-posts this chunk (overflow tail after Stop, or a later segment). Do not remove the segment ids on this path.
- Both bits false: do not POST on `keepalive.stream_id`. Clear `stream_id`. Leave `closed_by` as `turn:other` when it was empty. Return `fallback` / `user cancelled`. The flush sends one plain `_send_reply_activity`. On success set `T.state = DONE` and remove the chunk segments.

A `REJECTED_TERMINAL` whose body is the user-cancel phrase uses the `REJECTED_TERMINAL` row, which consults the same bit. Do not add a third cancel path.

**Keep-alive while `answer_final_in_progress` is true.** `_emit_turn_buffer_keepalive`, `_post_informative_locked` (unless `for_answer=True`), and `_post_keepalive_final_locked` return without posting. Do not `streamType=final` the text `Working on it.` while any turn has kept segments. Rotation (keep-alive-text final, then a new informative start) runs only when no turn has kept segments. `answer_final_in_progress` covers the gap sleep and the retry sleep, and the `finally` clears it.

**Scheduler** (`_schedule_turn_buffer_retry`), when deliver returns `retry` or a plain post fails. One `retry_task` per chat. The task calls `_flush_turn_buffer`, which walks turns and applies each turn's own record.

- `depth != 0`: return immediately. Do not increment `deferral_count`. Do not start the task. The inner record stays as the transition left it (`DEFERRED` or otherwise) until the outer complete flushes again.
- `T.force_plain_exhausted`: return.
- Entered with `T.deferral_count >= 3`: set `T.force_plain = true`, `T.state = FORCED_PLAIN`, `T.plain_due = true`. If `T.plain_attempts >= 3`, set `T.force_plain_exhausted`, clear `T.plain_due`, log ERROR `plain fallback exhausted`, and do not schedule. Otherwise schedule the deferred task. Delay is `min(2 ** T.plain_attempts * 0.2, 5)` seconds. When there are no segments left (for example a progress-only turn whose keep-alive-text final never landed), do not schedule and do not clear `keepalive.stream_id`. The abandoned id stays on the buffer or in `_orphan_keepalive` until a keep-alive final succeeds or `_clear_expired_untracked` clears it after the 120s stream cap. The next turn on that chat must not open a second informative stream while the id is still live: deliver reuses the existing `stream_id` for an answer final, or plain-posts once `skip_stream_open` is set after expiry.
- Otherwise: `T.deferral_count += 1`, `T.state = DEFERRED`, schedule the task at 0.2s (plus the existing untracked-cap wait when the opener is `untracked`).

Three increments is the deferral budget. The next entry force-plains. A `retry` return while `depth != 0` does not consume the budget.

**Plain `_send_reply_activity` after `fallback`.** Success: remove the chunk segments, `T.state = DONE`, clear `T.plain_due`, set `T.answer_final_settled = true` when this was the first chunk of the turn. Body contains `timeout after the request was sent`: treat as delivered, remove the segments, `T.state = DONE`, set `T.answer_final_settled = true` when this was the first chunk, do not retry. Any other failure: if `T.force_plain`, increment `T.plain_attempts` and call the scheduler; if not, call the scheduler so the failure counts as one deferral. Leave the segments.

**Cap remaining** for a `RETRYABLE` sleep: `max(0, 120 - 5 - age)` where `age` is `monotonic - opened_mono`. A missing `opened_mono` is age 0. Delay is `Retry-After` when that header parses as a number `> 0`, otherwise `min(2 ** answer_final_attempts, 60)`. Then `delay = min(delay, 60, cap remaining)`. Sleep only when `delay > 0` and cap remaining `> 0`, with the lock released and `answer_final_in_progress` still set.

### Transitions

Entry of deliver sets `answer_final_attempts = 0`, `not_sent_probes_left = 1`, `may_have_landed_probes_left = 1` on `T`, then applies the pre-POST rules. The first final POST sets `answer_final_attempts = 1` and `T.state = POSTING`. Classify and take one row. Probe POSTs are immediate, on the same `stream_id`, same body, and do not increment `answer_final_attempts`. They do consume the probe counter named in the row.

| From the classified result | Transition |
|---|---|
| `NOT_SENT` and status is `None` and `not_sent_probes_left > 0` | Decrement `not_sent_probes_left`. Do not set either landing bit. Stay `POSTING`. One probe POST. Classify that result and take its row. |
| `NOT_SENT` otherwise (probe already used, or status is not `None`) | Do not set either landing bit. `T.state = DEFERRED`. Keep `stream_id` and segments. Return `retry`. |
| `MAY_HAVE_LANDED` timeout | Set both landing bits. Set `bound_stream_id` if it is not already this id. Do not probe. `closed_by = turn:<T.turn_key>`, clear `stream_id`, remove the chunk segments, `T.state = DONE`. Return `delivered`. No `send_reply`. |
| `MAY_HAVE_LANDED` non-timeout and `may_have_landed_probes_left > 0` | Set both landing bits. `T.state = MAY_HAVE_LANDED`. Decrement the probe counter. One probe POST. Then take the probe's row. A probe that is itself non-timeout `MAY_HAVE_LANDED` hits the next row because the counter is now 0. |
| `MAY_HAVE_LANDED` non-timeout and the probe counter is 0 | Both landing bits stay true. Do not POST again. `T.state = DEFERRED`. Keep `stream_id` and segments. Return `retry`. No `send_reply`. |
| `DELIVERED` (2xx) | Set `had_landed`. Leave `may_have_landed` as it already is. `closed_by = turn:<T.turn_key>`, clear `stream_id`, remember the bot reply id, remove the chunk segments, `T.state = DONE`. Return `delivered`. No `send_reply`. |
| `ALREADY_COMPLETED` and (`T.may_have_landed` or `T.had_landed`) | This turn's earlier POST landed. Same effects as the `DELIVERED` row. Do not read another turn's bits. No `send_reply`. |
| `ALREADY_COMPLETED` and both of `T`'s bits are false | Do not set either of `T`'s bits. Do not write any other record. Clear `stream_id`. If `closed_by` is `""`, set `closed_by = turn:other`. Return `fallback` / `stream already closed`. One plain `send_reply` of `T`'s text. No new informative stream. |
| `REJECTED_TERMINAL` and `T.may_have_landed` | `UNCERTAIN` procedure, context `terminal stream response`. The three bodies are `exceeded streaming time`, user cancel (`canceled by user` or `cancelled by user`), and `content stream is not allowed`. `already completed` is not this row. |
| `REJECTED_TERMINAL` and `T.may_have_landed` is false | Clear `stream_id`. If `closed_by` is `""`, set `closed_by = turn:other`. Return `fallback`. Reason `user cancelled` when the body is the cancel phrase; otherwise `stream already closed`. One plain `send_reply`. |
| `RETRYABLE` and `answer_final_attempts < 5` and cap remaining `> 0` and the capped delay `> 0` | Keep `stream_id` and segments. Sleep the capped delay. Increment `answer_final_attempts`. POST the same final again. Classify that result. |
| `RETRYABLE` otherwise (attempt `>= 5`, cap remaining `<= 0`, or delay `<= 0`) | If `T.may_have_landed`: `UNCERTAIN` procedure, context `stream retry exhausted`. If not: clear `stream_id`, return `fallback` / `stream retry exhausted`. Do not leave the id open with no final and no plain or uncertain result. |
| `DEFER` (unknown 403, or any other status `>= 400` that is not terminal and not retryable) | Do not set either landing bit. Do not plain-post on this response. `T.state = DEFERRED`. Keep `stream_id` and segments. Return `retry`. The scheduler counts one deferral. After three deferrals the next schedule sets `plain_due`. That later deliver is the `plain_due` pre-POST rule: `UNCERTAIN` when `may_have_landed` is already true, otherwise one plain post. |

`FORCED_PLAIN` is the `plain_due` pre-POST rule plus the plain-post failure rule. It is not a classifier outcome. At most three plain attempts, then `force_plain_exhausted`. Exhaustion keeps the segments, logs ERROR, and does not delete them. That stop is not `UNCERTAIN` and is not a license to drop the text.

User cancel and keep-alive are not classifier rows. They are `apply_user_cancel` and the keep-alive procedure above.

### Invariants

1. **No duplicate visible answer for one turn.** A chunk is settled by exactly one of: a 2xx final, a timeout-as-delivered final, `ALREADY_COMPLETED` after that same record's landing bit, or one successful plain `send_reply`. `may_have_landed` forbids every later plain post of that chunk. Removing the segment ids on `DONE` is what stops a second post. A failed plain post may be retried up to the plain budget; a successful one may not be sent again.
2. **No silently dropped answer except `UNCERTAIN`.** `UNCERTAIN` is legal only when `may_have_landed` is true and the later result is `REJECTED_TERMINAL`, `RETRYABLE` exhaustion, `plain_due`, `apply_user_cancel`, or stream-sharing rule 5 with `closed_by = turn:other` (`unknown stream closer`). A turn that has never set `may_have_landed` ends in a plain `send_reply`, or stays `DEFERRED` with its segments until force-plain plain-posts. Force-plain exhaustion retains segments and logs ERROR. It does not remove them and it is not `UNCERTAIN`.
3. **Bounds.** At most 5 answer-final attempts per deliver call on the `RETRYABLE` path. At most one `NOT_SENT` probe and one non-timeout `MAY_HAVE_LANDED` probe per deliver call. Sleep only while cap remaining is `> 0` (115s from `opened_mono`). `Retry-After` and exponential backoff are each capped at 60s and at cap remaining. At most 3 deferrals, then force-plain. At most 3 plain attempts. A `retry` while `depth != 0` does not increment `deferral_count`.
4. **Plain fallback.** Forbidden while `T.may_have_landed` is true except stream-sharing rule 5 when `closed_by` is `turn:<other turn_key>` (another turn closed the stream): one plain `send_reply` of this turn's text even when `may_have_landed` is true. Otherwise `UNCERTAIN` instead of plain. Allowed when both landing bits are false and the result is `ALREADY_COMPLETED`, `REJECTED_TERMINAL`, `RETRYABLE` exhaustion, force-plain, opener rejection, or `apply_user_cancel`. Also allowed for overflow chunks after the first chunk is settled (`answer_final_settled`, including `UNCERTAIN` and plain timeout-as-delivered): those are plain `send_reply`s with `replyToId`, never a second final or informative open on retry, and they do not read `may_have_landed`.
5. **Attribution.** `may_have_landed` and `had_landed` are written only on the record of the turn that issued the POST. `ALREADY_COMPLETED`, `apply_user_cancel`, and force-plain read that same record and no other. An inner landing does not make the outer record `DELIVERED` or `UNCERTAIN`. An inner `ALREADY_COMPLETED` does not set the outer record's bits. This is the fix for Bugbot 4324654453.

### Test matrix

File `tests/test_agent365_plugin.py`, class `TestEditMessage`. One test per row. Existing names stay and their assertions change to the assertion column. New names are added. Do not keep an assertion that the outer turn opens a second informative stream after the inner turn finals the shared id.

| Test | Covers | Assertion |
|---|---|---|
| `test_not_sent_then_not_sent_defers` | `NOT_SENT` → probe → `NOT_SENT` → `DEFERRED` | Mint failure or empty result, then another `NOT_SENT`. Exactly one probe. `may_have_landed` stays false. `stream_id` and segments stay. `send_reply` count is 0. |
| `test_answer_final_mint_failure_then_already_completed_plain_posts` | `NOT_SENT` probe then `ALREADY_COMPLETED`, bits false | One plain `send_reply`. This turn's `had_landed` stays false. |
| `test_connect_error_then_already_completed_not_delivered` | `ConnectTimeout` / `PoolTimeout` / `ConnectError` are `NOT_SENT` | The connect failure does not set `may_have_landed`. A following `ALREADY_COMPLETED` plain-posts once. |
| `test_read_timeout_after_send_is_not_retried` | `MAY_HAVE_LANDED` timeout → `DONE` | `ReadTimeout` or `WriteTimeout` on the activity POST. No probe POST. No `send_reply`. Segments for that chunk are gone. `closed_by` is `turn:<that turn>`. |
| `test_delivered_2xx_clears_stream_and_skips_send_reply` | `DELIVERED` | 202 on the answer final. `send_reply` is 0. `stream_id` cleared. `had_landed` true only on that turn. |
| `test_failed_final_keeps_stream_and_segments` | non-timeout `MAY_HAVE_LANDED` then a second one → `DEFERRED` | `RuntimeError` (or any non-timeout ambiguous error) on every final. Exactly 2 final POSTs, then stop. `stream_id` and segments remain. `send_reply` is 0. The test must finish without a further POST. |
| `test_answer_final_retry_already_completed_is_delivered` | non-timeout `MAY_HAVE_LANDED` then probe `ALREADY_COMPLETED` | No `send_reply`. This turn's record is `DONE`. |
| `test_terminal_403_after_may_have_landed_is_uncertain` | non-timeout `MAY_HAVE_LANDED` then probe `REJECTED_TERMINAL` | `send_reply` is 0. Segments for that chunk are removed. Context `terminal stream response`. |
| `test_ambiguous_final_then_503_does_not_plain_post` | non-timeout `MAY_HAVE_LANDED` then `RETRYABLE` exhaustion | After the one probe, only 503s. `send_reply` is 0. Result `uncertain`. |
| `test_may_have_landed_then_defer_does_not_plain_post` | non-timeout `MAY_HAVE_LANDED` then probe `DEFER` | Unknown 403 or 400 on the probe. `send_reply` is 0 on that response. `stream_id` and segments stay. |
| `test_already_completed_without_landing_plain_posts` | `ALREADY_COMPLETED`, both bits false | Exactly one plain `send_reply`. `had_landed` stays false on this turn and on every other turn. `closed_by` is `turn:other`. No new informative POST. |
| `test_terminal_403_exceeded_streaming_time_plain_posts` | `REJECTED_TERMINAL` `exceeded streaming time`, bits false | One plain `send_reply`. Stream id cleared. |
| `test_user_cancel_terminal_403_plain_posts_once` | `REJECTED_TERMINAL` cancel body, bits false | Body `canceled by user` or `cancelled by user`. One plain `send_reply`. No further POST on that `streamId`. |
| `test_terminal_403_content_stream_not_allowed_plain_posts` | `REJECTED_TERMINAL` `content stream is not allowed`, bits false | One plain `send_reply`. The longer `already completed` sentence must not hit this test; that sentence is `ALREADY_COMPLETED`. |
| `test_answer_final_persistent_503_falls_back_to_plain_reply` | `RETRYABLE` 503, no landing bit, attempts exhausted | One plain `send_reply` once the 5th attempt or the 115s margin stops the loop. Not before. |
| `test_answer_final_retryable_429_respects_retry_after_cap` | `RETRYABLE` 429 | `Retry-After` is slept, capped at 60s and at cap remaining. Same `stream_id`. |
| `test_answer_final_huge_retry_after_respects_stream_cap` | 115s margin | A huge `Retry-After` does not sleep past cap remaining. |
| `test_answer_final_stream_cap_stops_mid_retries` | cap remaining already 0 | No further final POST. One plain `send_reply` when `may_have_landed` is false. |
| `test_unknown_403_defers_without_plain_post` | `DEFER` unknown 403 | First response does not `send_reply`. `stream_id` and segments stay. |
| `test_other_4xx_defers_without_plain_post` | `DEFER` other 4xx (400) | Same as the unknown-403 row. |
| `test_deferral_cap_is_three_then_force_plain` | 3 deferrals then `FORCED_PLAIN` | Three scheduled re-flushes increment `deferral_count` to 3. The next schedule sets `plain_due` and does not POST a final. With both landing bits false, exactly one plain `send_reply` follows. |
| `test_force_plain_after_may_have_landed_is_uncertain` | `FORCED_PLAIN` while `may_have_landed` | `send_reply` is 0. Segments for that chunk are removed. |
| `test_force_plain_fallback_stops_after_bounded_retries` | at most 3 plain attempts | Three failed plain posts set `force_plain_exhausted`. No fourth schedule. Segments remain. |
| `test_user_cancel_without_landing_plain_posts_once` | `apply_user_cancel`, bits false | `keepalive.stopped` before the final POST. One plain `send_reply`. No POST on the old `streamId`. |
| `test_user_cancel_after_may_have_landed_does_not_plain_post` | `apply_user_cancel` reads the per-turn record | A prior non-timeout `MAY_HAVE_LANDED` on this turn, then `stopped` on the early path (before the try, not only on a terminal 403 inside the loop). `send_reply` is 0. The other turn's record, if any, is unchanged. |
| `test_keepalive_skips_rotation_during_answer_final_sleep` | keep-alive during `POSTING` | While `answer_final_in_progress` is set, no informative POST and no keep-alive-text final. |
| `test_keepalive_rotation_skipped_while_segments_pending` | rotation only with no kept segments | Kept answer segments: the aging stream is not finalized with `Working on it.` No kept segments: rotation still finals that id with keep-alive text and sets `closed_by = keepalive`, with every turn's landing bits still false. |
| `test_inner_lands_outer_pending_plain_posts_outer` | inner lands, outer pending (Bugbot 4324654453) | Inner final returns 2xx or timeout `MAY_HAVE_LANDED`. Only the inner record has the landing bits. `closed_by` is `turn:<inner>`. The outer record stays bits-false. The outer flush does not POST a final on that id and does not open a new informative stream. It sends one plain `send_reply` of the outer text. |
| `test_inner_already_completed_does_not_deliver_outer` | inner `ALREADY_COMPLETED`, outer pending | Inner final gets 403 `already completed` with the inner bits false, or with only the inner bits true. The outer record's `had_landed` and `may_have_landed` stay false. The outer body is not removed as delivered. The outer text is one plain `send_reply` when its own bits are false. |
| `test_outer_does_not_final_stream_closed_by_inner` | stream sharing rule 5 | After the inner close, the next outer answer-final POST count on that `streamId` is 0. |
| `test_nested_start_does_not_reset_outer_delivery_record` | ownership across nest | Outer record has `may_have_landed` or a non-zero `deferral_count`. A nested `_open_or_nest_turn_buffer` and the inner turn's first segment leave the outer record's fields unchanged. Replaces the buffer-latch assertion in `test_nested_turn_does_not_clear_outer_answer_delivery_latch`. |
| `test_no_duplicate_after_may_have_landed` | invariant 1 | For one turn, `may_have_landed` plus any of `ALREADY_COMPLETED`, 503 exhaustion, terminal 403, force-plain, or user cancel yields `send_reply` 0. |
| `test_no_silent_drop_without_may_have_landed` | invariant 2 | Persistent 503, unknown 403 through the deferral cap, and `ALREADY_COMPLETED` with both bits false each produce one plain `send_reply`. Segments are not removed before that post succeeds. |
| `test_uncertain_is_the_only_drop` | invariant 2 exception | The only path that removes a chunk with `send_reply` 0 and no 2xx is `UNCERTAIN`, and only after `may_have_landed` on that same record. |
| `test_retry_bounds_five_attempts_margin_and_three_deferrals` | invariant 3 | A deliver call issues at most 5 `RETRYABLE` finals, at most one not-sent probe, and at most one non-timeout ambiguous probe. A chat observes at most 3 deferral increments per turn, then `plain_due`. |
| `test_plain_forbidden_after_may_have_landed` | invariant 4 | `plain_due` or `REJECTED_TERMINAL` or cancel after `may_have_landed` does not call `send_reply`. The same results with both bits false call it once. |
| `test_inner_landing_is_not_outer_had_landed` | invariant 5 | After the inner `DELIVERED` or inner `ALREADY_COMPLETED`, `delivery_by_turn` has two keys. The outer key's `had_landed` is false. Flush does not return `delivered` for the outer body. |
| `test_personal_chat_is_not_turn_buffered` | unchanged personal path | No turn-buffer delivery record is created. |

`test_connect_timeout_remote_error_then_503_uncertain` stays: `ConnectTimeout` (`NOT_SENT`) then `RemoteProtocolError` (`MAY_HAVE_LANDED`) then 503 exhaustion is `uncertain` and does not plain-post. It is the mixed-probe row beside the table, not a second assertion for a row already named above.

### Mapping from `16c30731`

What moves, onto `buf["delivery_by_turn"][turn_key]`:

- `buf["answer_delivery"]["may_have_landed"]` and `buf["final_may_have_landed"]` → `may_have_landed`.
- `buf["answer_delivery"]["had_landed"]` → `had_landed`.
- `buf["retry_attempts"]` → `deferral_count`.
- `buf["force_plain_latched"]` → `force_plain`.
- `buf["force_plain_answer"]` → `plain_due`.
- `buf["plain_fallback_attempts"]` → `plain_attempts`.
- `buf["force_plain_exhausted"]` → `force_plain_exhausted`.
- `_answer_had_landed_before` and `_plain_answer_blocked` become lookups on the posting turn's record. `_plain_answer_blocked` is true only when that record's `may_have_landed` is true.
- `_record_answer_may_have_landed` and `_record_answer_had_landed` take the record. Both bits are set together on every `MAY_HAVE_LANDED`, matching the tree. `had_landed` alone is set on 2xx.
- `_deliver_turn_answer_locked(chat_id, content)` gains the turn being flushed. The answer-final POST uses that turn's inbound. It stops calling `_keepalive_turn`.
- The two `stream.get("stopped")` returns inside `_deliver_turn_answer_locked` (before the `try`, and after the gap sleep) become `apply_user_cancel` on that turn's record.
- `ALREADY_COMPLETED` reads the posting turn's bits, not a buffer flag.
- `keepalive` gains `closed_by`. The inner close and the outer plain post use the pre-POST rules.

What stays:

- `_classify_answer_final_post` and `_answer_final_terminal_403`, including "already completed" before the three terminal phrases.
- The constants in the table at the top of this section.
- One immediate probe after non-timeout `MAY_HAVE_LANDED`, then `retry`. Timeout `MAY_HAVE_LANDED` returns `delivered` with no probe.
- One probe after `NOT_SENT` when status is `None`. Connect and mint failures are `NOT_SENT` and do not set the latch.
- `DEFER` does not plain-post on that response.
- `answer_final_in_progress` on the buffer, set before the gap and cleared in `finally`.
- One `retry_task` per chat. The schedule no-ops while `depth != 0`.
- Opener failure → one plain `send_reply` and no stored id. `untracked` opener → `blocked` until the cap.
- Overflow chunks after the first final stay plain `send_reply`s.
- The accepted `UNCERTAIN` tradeoff: a POST that may have left the client, followed only by terminal 403s, 503/429 exhaustion, or force-plain, drops that turn's chunk instead of duplicating it.

What is deleted:

- Buffer fields `answer_delivery` and `final_may_have_landed`.
- `_reset_turn_answer_delivery_state` and both callers: the empty-segment reset in `_append_turn_buffer_segment`, and `_clear_answer_delivery_if_unpending` inside `_remove_turn_segments`. Replace them with the per-turn delete rule. A nested start does not reset, and `on_processing_complete` does not reset; those two resets are already absent in this tree and must not come back.
- Any read that treats another turn's `ALREADY_COMPLETED` or landing bits as this turn's `had_landed`.
- The `stopped` fallback that returns `("fallback", "user cancelled")` without reading the posting turn's record.
- A final POST on a `stream_id` whose `closed_by` names a different turn. The outer informative-then-final after the inner close, described in earlier sections of this file, is withdrawn.
