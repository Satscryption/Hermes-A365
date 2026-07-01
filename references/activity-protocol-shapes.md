# Bot Framework activity shapes

Snapshot date: 2026-05-04 (SPEC §10 Q1 framing); content additions
through 2026-05-07. Path B streaming + reply-delivery section added
2026-05-29 (v0.7.2).

The activity bridge has shipped — slice 19a `verify`, 19b–19c `serve`
+ reference responder, 19e outbound user-FIC chain, 19m–19o Hermes
gateway plugin path. SPEC §10 Q1 was resolved by slice 19l (the
plugin contract was already documented in the upstream Hermes
harness). This file documents the Bot Framework activity shapes the
bridge handles in production today plus the invoke-shape work
in-flight under [#18](../../issues/18) (slice 19w).

## Subscription endpoint

`a365 query-entra --instance-channel --instance=<id>` returns:

```json
{
  "subscription_url": "https://<tenant>.api.agent365.microsoft.com/instances/<id>/activities",
  "auth": "bearer",
  "ws_supported": true
}
```

The bridge subscribes via WebSocket when supported, falling back to long
poll. Long-poll TTL is 60 s; the bridge re-subscribes on disconnect.

## Inbound activity types (consumed)

| Type | Source channels | Routed to | Notes |
|---|---|---|---|
| `message` | Teams / Outlook | Local Hermes agent (request/response) | Reply MUST be posted as `message` with the same `conversation.id`. |
| `invoke` (`adaptiveCard/action`) | Teams (Adaptive Card actions) | Card builder (`emit_card.py`) | Reply is an Adaptive Card refresh; SPEC §6.7 example. |
| `conversationUpdate` | Teams (members added/removed) | Bridge bookkeeping only | No agent invocation. |
| `messageReaction` | Teams | Optional telemetry event | Treat as `agent.received` with reaction context. |
| `event` | M365 Copilot agent picker | Local Hermes agent | Same routing as `message`. |

## Outbound activity types (emitted by bridge)

| Type | Triggered by | Payload |
|---|---|---|
| `message` | `agent.responded` | Plain text or Adaptive Card. |
| `invokeResponse` | Inbound `invoke` | Card refresh JSON; uses `templates/adaptive-cards/`. |
| `typing` | Long-running tool calls | Optional; emit at most every 1 s. |

## Channel-specific quirks

| Channel | Quirk | Handling |
|---|---|---|
| Teams | Conversation TTL of ~24 h after last activity | Bridge re-resolves `conversation.id` on `NotFound`. |
| Teams | `text` field has 28 KB cap | Long replies are split or rendered as Adaptive Card. |
| Outlook | `attachments` may include voice transcripts (preview) | Treat unknown attachment kinds as opaque; do not block reply. |
| M365 Copilot | `replyToId` semantics differ — Copilot expects threaded replies | Bridge sets `replyToId` from the inbound activity. |
| M365 Copilot Chat (Custom Engine Agent, Path B) | Arrives as `conversationType=groupChat` with `channelId=msteams` and a `19:…@thread.v2` id — **shape-indistinguishable from a real Teams group chat**. Accepts BF streaming activities (HTTP 2xx) but does **not visibly render** them → silent reply. | Treat non-`personal` turns as non-streaming: coalesce the turn's chunks into one `send_reply`. Never stream to Copilot Chat. See *Streaming and reply delivery* below. |

## Streaming and reply delivery (Path B)

Hermes' gateway stream-consumer produces a reply as a sequence of
growing chunks. How the bridge puts those on the wire depends on
whether the conversation actually renders Bot Framework streaming:

- **Personal chats (Teams 1:1, `conversationType=personal`)** render BF
  streaming. The bridge opens **one** streaming sequence per user turn —
  a `typing` activity plus `streamType=streaming` / `streamSequence`
  entities, finalized with `streamType=final` (`Agent365Adapter`'s
  `REQUIRES_EDIT_FINALIZE` path). Fresh chunk message-ids continue that
  one sequence instead of starting a second bubble; one-shot
  progress/fallback sends and separate image activities are suppressed
  mid-stream (#54). A **stale-stream liveness guard** force-drops a
  sequence whose finalization repeatedly fails or that exceeds a bounded
  age, so a stuck stream can't silence the chat (#62).

- **Copilot Chat and other non-`personal` turns** do **not** render BF
  streaming (see the quirks row above): Bot Framework returns 2xx and
  the gateway may even log `content_delivered=True`, yet nothing appears
  in the client. So the bridge **coalesces** the stream-consumer's
  chunks locally (buffered under a synthetic message id) and emits
  **one** ordinary `send_reply` when the turn finalizes
  (`edit_message(finalize=True)`) — one bubble per turn, no streaming
  (#54). The clean single send also removed the duplicated agent-name
  lines the old multi-activity fallback produced (#55).

> **`content_delivered` is not ground truth.** It has been observed both
> false-but-delivered and true-but-not-rendered; validate Path B reply
> rendering visually in the target client, never from the gateway log
> alone.

**Liveness fallback (#65):** the coalesce buffer normally flushes on
`edit_message(finalize=True)`. If finalize never fires (consumer error,
dropped final chunk, crash mid-turn), a watchdog flushes the latest
buffered text as one normal `send_reply` after the stale-stream
threshold. If that timeout flush fails, the adapter logs a warning and
drops the buffer so the failure is observable instead of silently
wedging the conversation.

## Adaptive Card targets

The skill ships v1.6 templates in `templates/adaptive-cards/`. Renderer
compatibility:

| Channel | Adaptive Cards version supported |
|---|---|
| Teams | up to v1.6 (newer features render as no-ops) |
| Outlook | v1.5 |
| M365 Copilot | v1.6 (with Copilot-specific `Action.Execute` extensions) |

When a target channel doesn't support a feature in the v1.6 template
(e.g. `Refresh.action`), Adaptive Card host config gracefully degrades.
The bridge does not currently negotiate per-channel rendering.

## Conversation reference shape

Every reply needs a `conversationReference` block reconstructed from the
inbound activity:

```json
{
  "channelId": "msteams",
  "conversation": { "id": "19:abc..." },
  "user": { "aadObjectId": "<oid>" },
  "serviceUrl": "https://smba.trafficmanager.net/teams/"
}
```

The bridge persists the reference for proactive messages (e.g. agent
reaches out first) at `~/.hermes/agents/<slug>/conversations.json`,
mode 0600. Implemented in slice 19o (`ConversationRegistry`); see
`hermes_a365.plugin.conversations` for the schema.

## Invoke activities — `task/fetch` (v0.8.0 walk-validated, 2026-07-01)

The BF `invoke` request/response wire, validated end-to-end against the
satscryption tenant on the v0.8.0 walk (#18 / slice 19w).

**`task/fetch` request** — triggered on **Teams 1:1** by an Adaptive Card
`Action.Submit` carrying `data.msteams.type = "task/fetch"`:
`type: "invoke"`, `name: "task/fetch"`, `channelId: "msteams"`,
`conversation.conversationType: "personal"`, Path B (inbound token
`iss = https://api.botframework.com`). `value` carries the invoking action's
`data`.

**`task/fetch` response** — the HTTP body **IS** the taskInfo, and the HTTP
status = the invoke status. Do **NOT** wrap it in `{"status", "body"}` (that
is an SDK abstraction the transport unwraps; sending it makes Teams reject
the response as *"Unable to reach app"*):

```json
{ "task": { "type": "continue",
            "value": { "title": "...",
                       "card": { "contentType": "application/vnd.microsoft.card.adaptive",
                                 "content": { "type": "AdaptiveCard", "version": "1.5", "body": [] } } } } }
```

(`type: "message"` with a string `value` is the close-with-message variant.)

**Surface behaviour (decisive finding):**

- **Teams** delivers `task/fetch` to the CEA endpoint and renders the returned
  task module. ✅
- **Copilot Chat** renders the Adaptive Card *body* but **strips the
  `Action.Submit` task button** — it is an `Action.Execute` /
  `adaptiveCard/action` surface, so `task/fetch` is not triggerable there. The
  Copilot-native invoke is `adaptiveCard/action` (a v0.8.1 child).

## Open snapshots

Forward-looking documentation gaps; capture during the next live walk:

- The exact inbound `value` payload of a `task/fetch` invoke (not logged on
  the v0.8.0 walk) and the BF error envelope for a malformed invoke response.
- Catalogue the actual error envelopes BF returns on bad activity
  (round-N walkthroughs have only validated happy paths).
- Snapshot a real `query-entra --instance-channel` payload from a
  Frontier-Preview tenant for inclusion above.
- Remaining invoke-activity shapes (`task/submit`, `composeExtension/*`,
  `signin/*`, `search`, `adaptiveCard/action`) are tracked under
  [#18](../../issues/18) (slice 19w); `task/fetch` captured above.
