# `agent365` — Hermes gateway platform plugin

Hermes-side entry point for the A365 / Microsoft Teams integration. This
directory is the *plugin shape* — a third-party install that drops into
`~/.hermes/plugins/agent365/` and registers with the Hermes plugin loader
on gateway startup. No core Hermes changes required.

## Layout

```
plugins/agent365/
  PLUGIN.yaml         # plugin manifest (loader reads name, version, env reqs)
  __init__.py         # re-exports register()
  adapter.py          # Agent365Adapter(BasePlatformAdapter) + register(ctx)
  README.md           # this file
```

## Status — slices 19m + 19n + 19o

The plugin now runs the bridge end-to-end:

- **Inbound** (`/api/messages` route) — JWT validation (slice 19f),
  idempotency dedupe (slice 19i), serviceUrl host-suffix gate
  (slice 19j), then `MessageEvent` dispatch via
  `self.handle_message(event)`. Each accepted activity is
  upserted into the durable conversation registry (slice 19o).
- **Outbound** (`Agent365Adapter.send`) — looks up the cached inbound
  for the target chat from the registry, mints an outbound user-FIC
  bearer (slice 19e), and POSTs the reply via `serviceUrl`.
- **Typing** (`send_typing`) — POSTs a BF `typing` activity to the
  conversation; renders as the trailing-dots indicator on Teams 1:1.
  Best-effort — failures swallow silently so the gateway's typing
  pulse loop can't crash on a transient.
- **Image** (`send_image`) — builds an Adaptive Card with an `Image`
  element + optional caption `TextBlock`; routes through the same
  outbound POST path as `send`.
- **Durable session table** — `~/.hermes/agents/<slug>/conversations.json`
  persists `(conversation_id → {service_url, chat_type, chat_name,
  user info, last activity id, raw inbound})` so the adapter can
  resume conversations across uvicorn restarts and queue proactive
  sends (#4) against rooms it hasn't seen this run.
- **Lifecycle** — `connect()` builds the FastAPI app and runs uvicorn
  in a background task; `disconnect()` shuts uvicorn cleanly and
  closes the httpx client.

Bridge helpers (`validate_inbound_jwt`, `_IdempotencyCache`,
`_is_trusted_service_url`, `acquire_outbound_token`, `send_reply`,
…) are imported from `scripts/activity_bridge.py` rather than
copy-pasted; that module remains the single source of truth and
keeps working as a standalone `serve` entrypoint.

Still TODO:
- `send_document`, `send_voice`, `send_video`, `send_animation` —
  default no-op stubs from `BasePlatformAdapter` are fine for now.
- Group / channel surfaces — registry has the right schema
  (`team_id`, `channel_id` queued), but Teams 1:1 is the validated
  path so far.

Tracking issue: [#1 — Activity bridge — Hermes gateway platform plugin](https://github.com/satscryption/Hermes-A365/issues/1).

## Install (development)

While iterating on slices 19m–19p, symlink this directory into the
Hermes plugin path so the harness picks it up:

```bash
ln -s "$PWD/plugins/agent365" ~/.hermes/plugins/agent365
```

Then enable the platform in `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    agent365:
      enabled: true
      extra:
        slug: inbox-helper
        port: 3978
```

Required env vars (already populated by the wrapper's
`register --apply` + `instance create --apply` flow):

- `A365_TENANT_ID`
- `A365_APP_ID`
- `AA_INSTANCE_ID`
- `HERMES_BRIDGE_PORT` (optional; default `3978`)

## Slice plan

| slice | scope | status |
|---|---|---|
| 19m | skeleton — `PLUGIN.yaml`, adapter class, `register(ctx)` | ✅ |
| 19n | port the FastAPI webhook + bridge runtime under `Agent365Adapter`; map inbound → `handle_message(event)`, outbound → `send()` | ✅ |
| **19o** | durable session table (`conversations.json`) + `send_typing` + `send_image` | ✅ |
| 19p | round-N walkthrough validation against satscryption.io | next |

## Reference

- Upstream contract: `gateway/platforms/ADDING_A_PLATFORM.md` in
  `NousResearch/hermes-agent`.
- Reference plugin: `plugins/platforms/irc/` in the same repo.
- Existing bridge (source for the 19n port): `scripts/activity_bridge.py`.
