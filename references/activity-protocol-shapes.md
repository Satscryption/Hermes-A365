# Bot Framework activity shapes

Snapshot date: 2026-05-04

The activity bridge (SPEC §6.7) is currently TODO, blocked on §10 Q1
(Hermes IPC contract). This file is a forward-looking snapshot of the
Bot Framework activity shapes the bridge will need to handle once it
ships, so the templates and renderers are ready.

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
reaches out first). Persistence path TBD — likely
`~/.hermes/agents/<slug>/conversations.json`, encrypted at rest.

## TODO once bridge ships

- Catalogue the actual error envelopes BF returns on bad activity.
- Snapshot a real `query-entra --instance-channel` payload from a test
  tenant.
- Document the IPC contract Hermes harness exposes for the bridge to
  invoke (this is the §10 Q1 unblocker).
