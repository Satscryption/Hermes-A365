# Hermes-A365 bridge webhook contract

Snapshot date: 2026-05-05 (initial — slice 19b)

The `hermes a365 activity-bridge serve` daemon is a thin shim between
Microsoft Agent 365's Bot Framework infrastructure and an
operator-defined HTTP responder. The bridge handles BF wire concerns
(JWT validation, reply via `serviceUrl`, Adaptive Card framing); the
responder owns *what* to say back. This document pins the JSON contract
between the two so that any responder — Hermes today, native IPC
tomorrow, your test bot in between — can plug in.

## Where the responder is configured

Set one of:

```bash
export HERMES_BRIDGE_WEBHOOK="https://my-responder.internal/respond"
# or:
hermes a365 activity-bridge serve --slug X --webhook https://...
```

Any HTTPS URL the bridge can `POST` to works. The bridge sends each
inbound BF activity as a single POST and waits up to 10 s
(`DEFAULT_WEBHOOK_TIMEOUT_SECONDS`) for the response.

## Request: bridge → responder

`POST <webhook-url>` with JSON body:

```json
{
  "version": "1",
  "agent": {
    "slug": "inbox-helper",
    "tenant_id": "2699fca3-dac6-40a2-bcea-62ce05e2ee9b",
    "bot_app_id": "8b563a20-2fac-4210-8210-df139c61e8b7"
  },
  "activity": { ... }
}
```

- `version` — schema version. Bumped on breaking changes; current is
  `"1"`.
- `agent` — minimal identity context for routing/multi-agent
  responders.
- `activity` — the inbound Bot Framework Activity, **passed through
  unchanged**. See the [BF Activity protocol](
  https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/activity-protocol)
  for the full schema. The fields you'll most often care about:
  - `type` — `"message"`, `"invoke"`, `"conversationUpdate"`,
    `"typing"`, `"endOfConversation"`. The bridge only forwards the
    first two; the others are acked-and-dropped.
  - `text` — the user's message text (for `type: "message"`).
  - `value` — Adaptive Card action payload (for `type: "invoke"`).
  - `name` — invoke action name, e.g. `"adaptiveCard/action"`.
  - `from`, `recipient`, `conversation` — identity triple. The bridge
    swaps from↔recipient on reply.
  - `channelId` — `"msteams"`, `"m365copilot"`, `"outlook"`, etc.
    Use this to render channel-appropriate output.

## Response: responder → bridge

Return HTTP 200 with one of these JSON shapes (depending on the inbound
activity type):

### For `message` activities

```json
{
  "text": "<plain text reply>",
  "card": { ... }      // optional Adaptive Card v1.6 (object, not stringified)
}
```

- Either `text` or `card` must be present (or both).
- `card` is rendered as an `attachments[]` entry with
  `contentType: "application/vnd.microsoft.card.adaptive"`. **Adaptive
  Card v1.6 only**; older versions render but lose features.
- ⚠️ M365 Copilot has [limited support for rich cards](
  https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/activity-protocol);
  the responder should fall back to `text` when
  `activity.channelId == "m365copilot"` if rich rendering matters.
- An empty response (`{}`) is permitted — the bridge sends nothing back
  and answers the BF turn with `{"status": "no_reply"}`.

### For `invoke` activities

```json
{
  "invokeResponse": {
    "status": 200,
    "body": { ... }
  }
}
```

- `status` and `body` mirror BF's invokeResponse contract.
- For Adaptive Card replace flows: `body = { "statusCode": 200,
  "type": "application/vnd.microsoft.card.adaptive", "value": <new-card> }`.
- ⚠️ Invoke replies are **synchronous** in the BF protocol — the
  responder must answer within the bridge's webhook timeout (10 s
  default), and the bridge must answer the original BF turn within
  ~15 s total. There is no async-reply path for invokes.
- If the responder omits `invokeResponse`, the bridge wraps the entire
  response object as the body with `status: 200` for backwards
  compatibility.

## Error handling

If the responder returns a non-2xx status, raises, or times out, the
bridge:

1. Sends an Adaptive Card error back to the user via
   `serviceUrl` reply: "The agent backend returned an error" with the
   exception message in a separate text block.
2. Acknowledges the BF turn with HTTP 200 + `{"status":
   "webhook_error"}` so BF doesn't retry.
3. Logs the failure to `~/.hermes/agents/<slug>/bridge.log`.

The responder should return 5xx for transient failures it wants the
operator to investigate; the bridge logs the exception text but
doesn't retry the call (BF may itself retry the original activity
delivery if the bridge times out — at-least-once semantics overall).

## Versioning

`version: "1"` is the initial schema. Future breaking changes bump the
top-level `version` and the bridge supports multiple versions
side-by-side via a transition window. Minor additive changes (new
optional fields in the activity passthrough or response object) won't
bump the version.

## Implementation hints

- The bridge passes the BF Activity through whole; you don't need a BF
  SDK to write a responder, just JSON parsing.
- For multi-agent setups, route on `agent.slug` (passed in our
  envelope, not in the BF activity).
- For long-running agent thinking that exceeds the 10 s webhook
  budget: the bridge now handles this in two ways depending on
  context. **In-turn streaming** (BF `edit_message` via the
  `streaminfo` entity, slices 19s + 19s-bis, v0.3.0, `#3` closed) —
  the Teams bubble grows incrementally as the agent emits content;
  no work required from the responder beyond returning incremental
  text. **Cron-driven proactive sends** (slices 19x-a..e, v0.5.0 +
  v0.5.1, `#4` + `#27` closed) — call
  `await adapter.send(chat_id, content)` from Hermes' cron tools;
  the adapter routes via `sendToConversation` for any chat the
  current gateway lifetime hasn't yet captured an inbound for.
  Path B *replies* (responding to an inbound) are GA; Path B
  agent-initiated proactive sends are implemented and unit-covered
  via #33 (BF S2S + `sendToConversation`) but not yet separately
  live-walked.

## Reference responder (slice 19c)

A working demo / smoke-test responder ships at
`hermes_a365.hermes_responder`. Three modes:

```bash
# Echo mode (default) — replies "You said: <text>"
python -m hermes_a365.hermes_responder serve --port 9090

# Greeting mode — Adaptive Card on first message in a conversation,
# echo on subsequent. Useful for screencaps.
python -m hermes_a365.hermes_responder serve --mode greeting

# Canned mode — read response from JSON file, hot-reloaded per
# request so operators iterate without restarting.
echo '{"text": "Hello from canned"}' > responses.json
python -m hermes_a365.hermes_responder serve --mode canned \
    --canned-response-file ./responses.json
```

Optional flags:
- `--slug <slug>` — log JSON-line per turn to
  `~/.hermes/agents/<slug>/responder.log`. Without `--slug`, logs go
  to stdout.
- `--debug-endpoints` — enables `GET /history/<conversation-id>` for
  inspection. Off by default.
- `--history-max <n>` — per-conversation ring-buffer cap (default 50).

The reference responder is an MVP — it doesn't call an LLM or the
Hermes agent loop. It exists to prove the wire end-to-end (tunnel →
bridge JWT validation → webhook → reply via `serviceUrl` → Adaptive
Card render in Teams). Operators with a real responder swap it in
via `HERMES_BRIDGE_WEBHOOK`.
