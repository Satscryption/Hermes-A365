# #226 — Preamble versus a genuine answer before tool progress

Status: design only. Do not treat this file as shipped behaviour.
Target issue: [#226](https://github.com/Satscryption/Hermes-A365/issues/226).
Tree this was written against: `main` `868362ec` (PR [#223](https://github.com/Satscryption/Hermes-A365/pull/223) merged, the #214 rule).
Related: [#214](https://github.com/Satscryption/Hermes-A365/issues/214), [#228](https://github.com/Satscryption/Hermes-A365/issues/228), [#217](https://github.com/Satscryption/Hermes-A365/issues/217), [#212](https://github.com/Satscryption/Hermes-A365/issues/212), [#227](https://github.com/Satscryption/Hermes-A365/issues/227), [#186](https://github.com/Satscryption/Hermes-A365/issues/186), [#182](https://github.com/Satscryption/Hermes-A365/issues/182).
Copilot single-stream context: [182-copilot-chat-single-stream.md](182-copilot-chat-single-stream.md).

The implementation PR should say `Fixes #226` and stay draft until the rows in [Test cases](#test-cases) pass. This note does not implement them.

## Recommendation

Demote a marked segment to commentary only when it is a short intent-shaped lead-in and it sits at or before the last Hermes tool-progress segment on the same turn. Every other marked segment stays `kind=answer` and is joined into the Copilot final in buffer order. Tool-progress lines stay out of the final.

That is option 1 below. Option 2 puts the walk094 lead-ins back into the final. Option 3, a Hermes `finish_reason` or segment-break bit, marks the same spans the three #226 probes occupy, so it drops "The capital of France is Paris." along with "I'll run the sleep, then reply."

## What #214 does on this tree

`_reclassify_marked_answer_leadin_before_tool_progress` runs from `_append_turn_buffer_segment`, from `_replace_turn_buffer_segment` after an edit, and from the `has_marked_answer` branch of `_reconcile_turn_buffer_kinds_for_flush`.

On one turn seq it finds the last segment whose text is `_is_hermes_tool_progress_segment`. Every marked segment at that index or earlier (`reply_to`, `notify`, or `expect_edits`), other than a tool-progress line, is set to `kind=commentary`. Marks are left in place. `_partition_turn_flush_segments` then drops commentary when any marked segment exists. The final is the later marked answer only. Copilot replaces the informative bubble with that final, so the dropped sentence is gone from the screen.

Segments store no `finish_reason`. A streamed lead-in and a streamed fact use the same shape: `_first_send` with `reply_to` and `expect_edits`, or a later `notify` send. Walk094's "I'll run the sleep, then reply." and the #226 probe "The capital of France is Paris." are the same shape.

`_buffer_has_kept_answer_segments` is true for any `kind=answer`. #214 clears that for the lead-in, so `_emit_turn_buffer_keepalive` may rotate once `_keepalive_rotation_due` (stream age near the 120s cap). The rotation final is `_TURN_BUFFER_KEEPALIVE_TEXT` ("Working on it.").

## Options

### 1. Intent-shape heuristics

A marked segment at or before the last tool-progress segment is a preamble when its text is short and every sentence is a first-person intent cue ("I'll …", "I'm about to …", "Let me …"). Otherwise it stays an answer.

Pros:

- The three #226 probes stay in the final. They are declaratives, so the cue does not match.
- The walk094 lead-ins still drop. They match the cue, stay commentary, still post on the informative stream, and still allow rotation.
- The rule is a pure function of text, marks, and position. Flush reconcile already resets marked segments to `answer` and then reclassifies, so an edit is classified from the edited text.
- No Hermes release and no plugin-floor bump. v0.9.5 can ship it inside the adapter that already owns this presentation (#186, #214).

Cons:

- English cues only. A French lead-in stays in the final.
- One short sentence that starts with "I'll" or "Let me" and holds the only copy of a fact is dropped once a later answer exists.
- A long lead-in over the character cap stays in the final.

Failure cases:

- "I'll send the report to finance." then tool progress then "Sent." Final is "Sent." The recipient was only in the lead-in.
- "Let me know which file you meant." then tool progress then "Done." Final is "Done."
- "Je vais lancer la veille, puis je réponds." then tool progress then "Fait." Final keeps the French lead-in.
- An intent-shaped segment longer than 200 characters stays in the final. A one-line "I'll …" lead-in under that cap still drops.
- "Sure, I'll check." is dropped. "Sure, the capital is Paris." is kept, because the words after the acknowledgement are not a cue.

### 2. Keep demoted text as a final prefix unless a later answer restates it

Keep the #214 demotion (kind commentary, informative post, rotation allowed). At flush, prepend each demoted segment to the final unless a later marked answer restates it.

Pros:

- No fact is dropped for lack of a cue. "The capital of France is Paris.", "The time is 3pm.", and "Found 3 files." all survive, whatever "restate" means, as long as the later line does not count as a restatement.
- Language-agnostic. Rotation still sees commentary, so the 120s cap stays clear.
- The informative bubble is still replaced by the final, and the final now holds the earlier text, so the replacement preserves it.

Cons:

- "Restates" has no mechanical reading that satisfies #214 and #226 together.
- The final joiner and the rotation check disagree about the same segments: rotation treats them as disposable commentary, while the final treats them as answer text. Edit refresh (#223's second commit, and #228) has to keep both behaviours aligned.

Failure cases:

- Restatement means normalized equality or substring. "Done after sleep." does not contain "I'll run the sleep, then reply." The walk094 final grows the lead-in back. #214 reopens.
- Restatement means "any later marked answer exists." That is the current #214 drop. The three probes stay broken.
- Restatement means a model call at flush. Flush holds the buffer lock on the session task. A second completion there is a new failure mode, and it is still a heuristic with the option 1 misses.

### 3. Upstream Hermes preamble signal

Hermes already knows a tool boundary is not the turn answer. `GatewayStreamConsumer._send_or_edit` sets `preamble_finalize = finalize and not is_turn_final`, and `_clear_turn_final_flags` treats that boundary as an interim preamble. `_metadata_for_send` does not send that bit. It sends `expect_edits` on a preview and `notify` on every finalize, including the segment break. `edit_message` on a `turnbuf:` id ignores `finalize` and does not take metadata, so the stored segment keeps the preview's `expect_edits`.

A useful upstream signal would be metadata on that segment-break send, for example `preamble: true`, plumbed through `edit_message` into the segment. `finish_reason=tool_calls` is the same information: inside one user turn the agent loop emits `tool_calls` on every assistant message except the last, and tool progress follows those messages.

Pros:

- The adapter stops guessing from English wording.
- One flag matches the stream consumer's existing `is_turn_final=False` branch, which #214 reconstructed from position.

Cons:

- It needs a Hermes change, a release, and a plugin-floor bump before the adapter can rely on it. #227 gates the v0.9.5 tag on this behaviour.
- On this tree the adapter would still need a fallback for Hermes builds that do not send the flag. That fallback is option 1 or option 2.
- `edit_message` has to grow a metadata parameter. Personal 1:1 streaming must ignore the flag.

Failure cases:

- The signal is per segment, and a tool-call segment is one buffer (`_accumulated`). "The capital of France is Paris." in that buffer is preamble the same way "I'll run the sleep, then reply." is. All three #226 probes sit before a later tool-progress segment, which is the `tool_calls` region. The signal drops them.
- `finish_reason=stop` is the last answer ("Checked.", "Also UTC.", "All done."). It never labels the earlier fact.
- A Hermes that only sets the flag on `edit_message(..., finalize=True)` misses the preview that #214 actually stored: the `expect_edits` send, with no later edit of the marks.

## Normative rules

These rules replace the body of `_reclassify_marked_answer_leadin_before_tool_progress`. Call sites stay the three #223 sites. Personal Teams 1:1 never opens a turn buffer; this section does not apply there.

### Preamble predicate

`_segment_is_intent_preamble(text) -> bool` after `_strip_streaming_cursor`:

1. Collapse whitespace, including newlines, to single spaces. Strip one or more trailing parentheticals that match `\s*\([^)]{0,80}\)`, including the walk094 edit suffix `(edited)`.
2. If the result is empty or longer than 200 characters, it is not a preamble. The constant is `_INTENT_PREAMBLE_MAX_CHARS = 200`.
3. Split sentences on `(?<=[.!?…])\s+`. The ASCII ellipsis and `…` (U+2026) both end a sentence when whitespace follows; with no following whitespace they stay one sentence. "I'm about to sleep 30 seconds…" is one sentence.
4. The segment is a preamble when every sentence matches, case-insensitively:

```text
^(?:(?:ok|okay|sure|alright|right)(?:[,—–-]\s+|\s+))?(?:i['’]ll|i will|i['’]m going to|i am going to|i['’]m about to|i am about to|let me|allow me to)\b
```

Apostrophes are `'` and `’` (U+2019). A sentence that fails the cue keeps the whole segment. "The capital of France is Paris. I'll verify it." stays an answer, both sentences together.

### Position gate

Per turn seq (`_turn_segments_for_seq`), not across the buffer:

1. Let `last` be the index of the last tool-progress segment. No tool-progress segment: return without changing kinds. "Opening line." plus "Closing line." still joins as `Opening line.\n\nClosing line.`
2. For each marked segment that is not itself a tool-progress line, with index `<= last`: set `kind=commentary` when `_segment_is_intent_preamble` is true. Otherwise leave `kind=answer`.
3. Do not clear `reply_to`, `notify`, or `expect_edits`. A sole preamble is still marked, so the partition fallback that keeps the last marked non-progress segment still emits it when the turn ends with no later answer.
4. The helper only assigns `commentary`. It does not assign `answer`. Unmarked text stays on the #186 path.

### Final body

`_partition_turn_flush_segments` stays as it is once kinds are right. Kept `kind=answer` segments, tool-progress lines removed, joined with a blank line, buffer order. A preamble is commentary and drops when any marked segment exists. A genuine pre-tool answer is `kind=answer` and stays.

Expected shape: `The capital of France is Paris.\n\nChecked.`

### Rotation

`_buffer_has_kept_answer_segments` blocks rotation only for an answer-like segment strictly after the last tool-progress segment on its turn.

Answer-like means `kind=="answer"`, or a missing `kind` whose text is not tool progress.

A segment at or before that last progress index does not block rotation. This includes a genuine marked answer and an intent preamble. With no tool-progress segment on the turn, any answer-like segment still blocks, which is the current short-reply behaviour.

The rotation final text stays "Working on it." The pre-tool answer remains in the buffer and is joined into the answer final on the stream that is current when the turn flushes.

### Edit survival

On `edit_message` of a `turnbuf:` id, in this order:

1. Replace `text`.
2. `_refresh_turn_buffer_segment_kind_after_edit` unchanged. Its early return when `kind==answer` is safe because step 3 reads the new text.
3. Run the predicate above. "I'll run the sleep, then reply. (edited)" is a preamble (the parenthetical is stripped) and returns to `commentary`, so rotation stays allowed. "The capital of France is Paris." edited from a lead-in stays `answer`.
4. When the text changed and the resulting kind is `commentary`, set `informative_displayed` false so the edited lead-in is eligible for another informative post. That is the #228 fix and belongs in the same implementation. A resulting `answer` is not posted as informative; it appears in the final.

Flush reconcile still sets every marked non-progress segment to `answer` and then runs step 3, so a stale kind from an edit cannot survive into the final body.

## Interaction with the open issues

### #214 rotation and edit survival

The position gate and the three call sites stay. The change is the extra predicate on the demotion, plus the rotation check ignoring answer-like segments at or before the last progress index.

K4 still rotates: lead-in plus `💻 Running sleep 130`, stream age 121s, no post-tool answer yet, `_buffer_has_kept_answer_segments` is false, keepalive final then a new informative stream. After "Done after sleep." arrives, that post-tool answer blocks further rotation. The answer final is `Done after sleep.`

A genuine fact before a long tool also rotates. The fact is in the later final. Leaving `kind=answer` on that fact and still blocking rotation would bring the walk094 K3 cap back for the #226 probes.

### #228

#228 is the live-display half of an edited preamble: `informative_displayed` stays set, pending-display skips non-progress commentary, and the preamble is also absent from the final, so the edit never appears. Step 4 in [Edit survival](#edit-survival) is the required coordination. This design does not otherwise change pending-display.

An edit that turns a preamble into a fact does not depend on that flag. The fact is `kind=answer` and is in the final even if the flag stays set.

### #217 first turn

#217 is the empty agent bubble on the first turn of a new conversation until reload. The opening informative update and the final are a few seconds apart; a later turn in the same conversation paints.

This design adds no informative POST for a kept pre-tool answer. Those segments stay `kind=answer`, and `_informative_text_for_turn` only takes pending progress and commentary. A first-turn hello with no tool progress never enters the predicate.

Any #217 mitigation that posts an answer-only final (short turn, or the first turn after `conversationUpdate`) uses the same kept-segment join as flush, in order. Posting only the last segment drops "The capital of France is Paris." on the turn #226 is about. This design does not change stream-open timing, the 1.5s gap, or `conversationUpdate` handling. Holding the informative update for a minimum stream age is a #217 decision; the final body that follows still includes every kept pre-tool answer, because the informative text is replaced when the final lands.

### #212 keepalive

#212's in-place reconcile during keepalive is a separate bug: `_text_merged_into_unmarked_final` calls `_partition_turn_flush_segments`, which promotes the latest unmarked commentary to `answer` on the live buffer. The preamble helper must stay a demotion inside the `has_marked_answer` branch so it does not add another promotion. #212's non-mutating copy, when it lands, can run the same demotion on the copy. Live kinds change at append, edit, and flush.

"💻 Running noop" then unmarked "Found 3 issues" is the #212 repro. "Found 3 issues" is not a marked segment, so the predicate leaves it alone. The final stays "Found 3 issues". Heartbeat substitution for that unmarked path stays a #212 fix.

A genuine pre-tool answer is `kind=answer`, so it is not chosen as keepalive or informative text. The keepalive line remains the tool-progress segment or "Working on it." The late informative POST after `streamType=final` (#212's third note) gains no new poster from this design. The dead 429 arm in the test is untouched.

Accepted #212 UX, live commentary on an unmarked turn waiting for the heartbeat or a later marked answer, stays. An intent preamble is marked and is reclassified to commentary, so it still schedules the informative refresh #214 added.

## Test cases

Progress stands for a Hermes tool-progress line such as `💻 Running sleep 40`. Marks are the send shape. The final is the answer `streamType=final` text after `\n\n` joins. "Rotation" is whether `_emit_turn_buffer_keepalive` may rotate while the listed pre-tool segments are buffered and no later answer has arrived yet.

| Row | Buffer, in order | Expected final | Rotation before the post-tool answer |
|---|---|---|---|
| P1 | `The capital of France is Paris.` (`reply_to`, `expect_edits`), progress, `Checked.` (marked) | `The capital of France is Paris.\n\nChecked.` | allowed |
| P2 | `The time is 3pm.` (`notify`), progress, `Also UTC.` (marked) | `The time is 3pm.\n\nAlso UTC.` | allowed |
| P3 | progress, `Found 3 files.` (marked), progress, `All done.` (marked) | `Found 3 files.\n\nAll done.` | allowed until `All done.` |
| L1 | `I'll run the sleep, then reply.` (`reply_to`, `expect_edits`), progress, `Done after sleep.` (marked) | `Done after sleep.` | allowed |
| L2 | `I'll run the sleep, then reply.` (`expect_edits`), `I'm about to sleep 30 seconds…` (`expect_edits`), progress, `Done after sleep.` | `Done after sleep.` | allowed |
| L3 | L1's lead-in, progress, `edit_message` to `I'll run the sleep, then reply. (edited)`, then `Done after sleep.` | `Done after sleep.` Kind of the edited lead-in is `commentary` after the edit. | allowed after the edit |
| L4 | `Opening line.` (`expect_edits`), `Closing line.` (`expect_edits`). No progress. | `Opening line.\n\nClosing line.` | blocked (no progress, answers present) |
| L5 | L1's lead-in, progress, turn ends with no later answer | `I'll run the sleep, then reply.` (partition fallback) | allowed at age 121s (K4) |
| L6 | `The capital of France is Paris.` (`expect_edits`), progress, turn ends with no later answer | `The capital of France is Paris.` | allowed |
| L7 | L1's lead-in edited to `The capital of France is Paris.` after progress exists, then `Checked.` | `The capital of France is Paris.\n\nChecked.` | allowed until `Checked.` |
| M1 | One segment `The capital of France is Paris. I'll verify it.` (`expect_edits`), progress, `Checked.` | `The capital of France is Paris. I'll verify it.\n\nChecked.` | allowed |
| F1 | `I'll send the report to finance.` (`expect_edits`), progress, `Sent.` | `Sent.` Known drop. | allowed |
| F2 | `Je vais lancer la veille, puis je réponds.` (`expect_edits`), progress, `Fait.` | `Je vais lancer la veille, puis je réponds.\n\nFait.` Known keep. | allowed |
| F3 | `Let me know which file you meant.` (`expect_edits`), progress, `Done.` | `Done.` Known drop. | allowed |

P1–P3 are the #226 probes. L1–L5 are the #214 cases PR #223 fixed (walk094 lead-ins, the second lead-in, the edit that must stay commentary, the no-progress merge, and K4). L6–L7 and M1 lock the boundary between those. F1–F3 are accepted heuristic misses; they are unit tests, not walk failures.

## Walk checklist for #227

Record these on the v0.9.5 candidate in the walk095 note. #227's other scope items stay as that issue writes them. A live model that will not emit a row is recorded as unit-covered, with the test name, and is not a walk failure.

1. **P1 fact before a tool.** Ask for a fact the model states, then a tool, then a short confirmation. The final bubble contains the fact and the confirmation. The fact is still visible after the final replaces the informative line.
2. **L1 / L2 lead-in.** The walk094 sleep prompt ("I'll run the sleep, then reply." / "I'm about to sleep 30 seconds…"). The final is only the post-tool answer. The lead-in may show as informative and then disappear. It is not duplicated under the answer.
3. **L5 / K4 rotation.** A tool longer than 120s after that lead-in. The stream rotates (one keepalive final, one new informative stream). The answer final is the post-tool answer, once.
4. **P1 across rotation.** A fact before a tool that runs past 120s, then a short confirmation. Rotation happens. The final still contains the fact and the confirmation.
5. **L3 / #228 edit.** If the turn edits the lead-in in place, the edited lead-in shows on the informative stream and is absent from the final. If a live edit cannot be forced, cite the unit test and say so.
6. **#217 first turn.** A new conversation whose first answer is a pre-tool fact plus a confirmation. The bubble paints without a reload, and the text is the joined final from row P1. This row does not add an extra informative hop for the fact.
7. **#212 keepalive.** A turn whose live text is a tool-progress line heartbeats with that line or "Working on it.", and does not post the pre-tool fact as the keepalive body. Reply delivery matches the #212 notes that are in scope for the candidate; rows this design leaves to #212 stay on #212's own expected result.

## Out of scope

- Implementing the predicate, the rotation change, or the #228 flag clear.
- Option 3's Hermes metadata. Revisit it only if a future Hermes splits lead-in from content inside one tool-call segment. Passing `finish_reason` through unchanged does not do that.
- Personal 1:1 `streamType=streaming`.
- Rewriting the #212 keepalive copy, the #217 stream-age hold, or the #182 delivery state machine.
