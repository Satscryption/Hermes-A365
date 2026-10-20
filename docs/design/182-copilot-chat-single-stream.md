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

Tool-progress lines that match `_is_hermes_tool_progress_segment` are still dropped from the final body. Commentary is still joined into the answer with `\n\n`. Changing that split is #186.

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

`_deliver_turn_answer_locked` decision order, lock already held:

1. **`stopped`** (user cancel 403). The stream is already closed. One plain `send_reply` with the answer. Do not POST on that `streamId`.
2. **`untracked` and still inside the two-minute cap.** No id, so the answer cannot name the stream, and a second start would be a second bubble (#154). Return not-delivered. The existing deferred retry waits out the cap, then posts. Do not open another stream.
3. **No `stream_id`.** POST one informative opener (`streamSequence` 1, no `streamId`, text = keep-alive phrase). 
   - 201 with an id: store it on `buf["keepalive"]` the same way `_post_informative_locked` does, then continue to step 4.
   - 2xx without an id, or `ReadTimeout` / `WriteTimeout` on that opening POST: mark `untracked` and return not-delivered (same as today's opener). `ConnectTimeout` / `PoolTimeout` are not untracked.
   - Other failure (connector down, 4xx/5xx, including `403` `Content stream is not allowed`): do **not** remember a stream id. One plain `send_reply`. Log a warning that names `chat_id` and status. This is the real-Teams-group escape and the "streaming rejected" escape.
4. **`stream_id` set.** Do not POST a keep-alive-text final first. If `last_emit` is within `_STREAMING_MIN_GAP_SEC`, sleep the remainder **outside** the lock (same pacing as personal `edit_message`). Re-check the buffer and the id after the sleep. Then POST `type=message`, `streamType=final`, that `streamId`, no `streamSequence`, `text` = the chunk, `message_chrome=True`.
   - 2xx: clear `buf["keepalive"]`, drop the chunk's segment ids, done. No `send_reply`.
   - `ReadTimeout` / `WriteTimeout`: treat as delivered (the request may be stored). Drop the chunk. Do not plain-post a duplicate.
   - `403` already completed, exceeded streaming time, or canceled by user: the stream cannot carry this text. Clear the id. One plain `send_reply`.
   - Other failure: leave the id and the segments. Schedule the existing deferred retry. Do not plain-post while the id is still held (a second activity into an open stream is the #154 swallow).

Record `last_emit_mono` on the keep-alive dict whenever an informative or final POST returns. The 25s keep-alive interval already exceeds the gap; the new back-to-back case is "opener then final" inside one flush.

Progress-only turns (every segment omitted, nothing kept) do not open a stream. If one is already open, the existing end-of-flush `_close_turn_buffer_keepalive_locked` still finals it with keep-alive text so the client is not left inside it.

Nested Hermes turns share one buffer. The innermost complete finals the open stream with the follow-up answer. The outer complete runs later, finds no open stream, and opens its own informative-then-final. That is two user turns, two streams, which matches "do not continue a stream after `endStream()`". Do not hold one stream open across both answers.

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
- Rotation before the two-minute cap stays: final the old id (keep-alive text is acceptable there; the answer has not been produced), then start a new informative sequence. The answer finals whichever id is current.
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
| `test_nested_complete_closes_keepalive_before_followup_reply` | Follow-up text is the final on the open id. Outer answer is a new informative + final. No plain replies. |
| `test_turn_buffer_merges_commentary_edited_segment_and_duplicate_final` | Wire is the final. Body is still commentary plus answer plus the handoff line. `send_reply` stays 0. This test is the #186 seam; do not drop commentary here. |
| `test_turn_buffer_omits_hermes_tool_progress` and `test_turn_buffer_keeps_one_line_answers_sharing_progress_shape` | Same bodies as now, carried on the final. |
| `test_untracked_open_waits_out_the_cap_then_posts_one_reply` | Unchanged: no second stream; one plain reply after the cap. |
| `test_personal_chat_is_not_turn_buffered`, `test_personal_chat_does_not_schedule_keepalive`, `TestSendStreamStart` | Unchanged. Personal final is still `streamType=streaming` then `streamType=final`. |

Add:

- Answer final entities are `streaminfo` plus the AI message entity. Feedback `channelData` is present only on that final, and only when feedback is enabled. The informative activity has neither.
- Back-to-back opener and final sleep `_STREAMING_MIN_GAP_SEC`. A keep-alive update older than the gap does not sleep again.
- Opening `403` `Content stream is not allowed` → one plain reply, empty keep-alive state.
- User-cancel `stopped` → plain reply, no POST on the old `streamId`.
- Merged body over `MAX_MESSAGE_LENGTH`: first chunk is the final; the next chunk is one plain `send_reply` after the final, not a second `streamType=final`.
- `ReadTimeout` on the answer final drops the segment and does not plain-post.

Run the existing personal streaming tests in `TestSendStreamStart` and `TestEditMessage` so 1:1 `streamType=streaming` did not pick up `informative`.

A live Copilot Chat check is not a unit test. Residual for the #116 / #89 walk: the short hello fills the placeholder as one bubble; a turn under 25s still does it; Teams 1:1 still streams one bubble and clears Stop. `content_delivered` does not count as that check.

## Risks

- **Copilot still does not paint the stream.** #54 saw 2xx and a blank client for `streamType=streaming` chunks. This shape is the one the current CEA and streaming-ux docs describe, but the docs do not say a plain message cannot fill the placeholder. If the walk shows two bubbles again, or a blank slot, the next change is to call the same opener from `on_processing_start` (the #181 hook), not to send `streamType=streaming` chunks from `send`.
- **Flush-time open is late for the 15s window.** Acceptable for the 4s hello. Not acceptable as a substitute for #181 on a 45s turn.
- **1.5s gap on short replies.** A hello that used to be one POST becomes informative, gap, final. That is the documented pacing. Do not skip it on the opener/final pair.
- **Real Teams group chats** share the Copilot Chat shape. streaming-ux says streaming is 1:1-only. An opening `403` `Content stream is not allowed` must fall back to one plain message. A 201 followed by a client that does not render the stream would go silent; the keep-alive already takes that bet for long non-personal turns. There is no shape test that splits the two surfaces.
- **Answer final vs keep-alive final success rules differ.** A 403 "already completed" means the *close* succeeded for a keep-alive-text final. For the answer final it means the answer was not stored. Treating it as success would drop the text. The plain fallback exists for that case only after the id is cleared.
- **Timeout double-send.** `ReadTimeout` / `WriteTimeout` on the final must not be followed by `send_reply`.
- **Lock deadlock.** Flush holds the buffer lock. The keep-alive task takes the same lock. Do not wait for the keep-alive task while holding the lock except for the existing `_drop_turn_buffer` cancel, which already has a 5s bound.
- **AI label / feedback regression.** They move from `render_reply_activity` onto the final. Leaving them off the final makes the Copilot bubble lose the #73 label and thumbs. Putting them on informative updates violates streaming-ux.
- **#186 text still in the bubble.** Expected until #186. Do not "fix" it inside #182.
