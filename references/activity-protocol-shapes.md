# Bot Framework activity shapes

Snapshot date: 2026-05-04 (SPEC §10 Q1 framing); content additions
through 2026-05-07.

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

## Open snapshots

Forward-looking documentation gaps; capture during the next live walk:

- Catalogue the actual error envelopes BF returns on bad activity
  (round-N walkthroughs have only validated happy paths).
- Snapshot a real `query-entra --instance-channel` payload from a
  Frontier-Preview tenant for inclusion above.
- Invoke-activity shapes (`task/{fetch,submit}`, `composeExtension/*`,
  `signin/*`, `search`) are tracked under
  [#18](../../issues/18) (slice 19w).
