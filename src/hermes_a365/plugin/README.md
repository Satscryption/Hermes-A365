# `agent365` — Hermes gateway platform plugin

Hermes-side entry point for the A365 / Microsoft Teams integration. This
subpackage is discovered by the Hermes plugin loader via its
`hermes_agent.plugins` entry point (declared in the top-level
`pyproject.toml`); no manual drop into `~/.hermes/plugins/` is needed.

## Layout

```
hermes_a365/plugin/
  plugin.yaml         # plugin manifest (loader globs for this lowercase form)
  __init__.py         # re-exports register()
  adapter.py          # Agent365Adapter(BasePlatformAdapter) + register(ctx)
  cli.py              # `hermes a365 <verb>` argparse tree
  conversations.py    # ConversationRef + ConversationRegistry (slice 19o)
  README.md           # this file
```

> ⚠️ The Hermes plugin loader globs for **lowercase `plugin.yaml`**
> (`hermes_cli/plugins.py`). The upstream `ADDING_A_PLATFORM.md`
> docs show `PLUGIN.yaml`, but that variant is silently skipped on
> case-sensitive POSIX filesystems. Don't rename.

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
…) are imported from `hermes_a365.activity_bridge` rather than
copy-pasted; that module remains the single source of truth and
keeps working as the standalone `hermes-a365 activity-bridge serve`
entry point.

Still TODO:
- `send_document`, `send_voice`, `send_video`, `send_animation` —
  default no-op stubs from `BasePlatformAdapter` are fine for now.
- Group / channel surfaces — registry has the right schema
  (`team_id`, `channel_id` queued), but Teams 1:1 is the validated
  path so far.

Tracking issue: [#1 — Activity bridge — Hermes gateway platform plugin](https://github.com/satscryption/Hermes-A365/issues/1).

## Install (development)

For local development against an unpublished checkout, install the
package editable into the Hermes venv:

```bash
~/.hermes/hermes-agent/venv/bin/pip install -e ".[bridge]"
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
        # #76 file transfer (Teams 1:1). REQUIRED to enable files — the tenant's
        # exact SharePoint/OneDrive host(s). Unset ⇒ file transfer is disabled
        # (fail-closed) and outbound files degrade to a text notice. A
        # *.sharepoint.com suffix is NOT trusted (customer-registrable), so the
        # tenant host must be pinned exactly. Profile-scoped: with
        # gateway.multiplex_profiles each profile pins its OWN tenant here.
        file_host_allowlist:
          - contoso.sharepoint.com
          - contoso-my.sharepoint.com
        # #80 / #180 — record Teams native reactions on the #73 feedback map
        # without invoking the agent (default). Set true only when you want
        # reaction:added:/reaction:removed: synthetic agent turns.
        reactions_dispatch_agent_turn: false
```

Required env vars (already populated by the wrapper's
`register --apply` + `instance create --apply` flow):

- `A365_TENANT_ID`
- `A365_APP_ID`
- `AA_INSTANCE_ID`
- `HERMES_BRIDGE_PORT` (optional; default `3978`)
- `A365_FILE_HOST_ALLOWLIST` (optional single-profile fallback for
  `extra.file_host_allowlist`; comma-separated exact hosts, e.g.
  `contoso.sharepoint.com,contoso-my.sharepoint.com`). Required to enable #76
  file transfer; unset ⇒ files degrade to text (fail-closed).
- `A365_AGENT_IDENTITY_IDS` (optional comma-separated Entra agentIdentity
  app ids). Teams "Create instance" agents reply as their own agent
  identity, so `recipient.agenticAppId` differs from the blueprint app id
  while `recipient.agenticAppBlueprintId` names the blueprint. Unset ⇒
  only the blueprint id may mint outbound tokens (deny-by-default).
  Gateway fallback when the env var is unset: `extra.agent_identity_ids`
  (a comma-separated string or a list). The activity body is never a
  source for this list.

## Slice plan

| slice | scope | status |
|---|---|---|
| 19m | skeleton — `plugin.yaml`, adapter class, `register(ctx)` | ✅ |
| 19n | port the FastAPI webhook + bridge runtime under `Agent365Adapter`; map inbound → `handle_message(event)`, outbound → `send()` | ✅ |
| **19o** | durable session table (`conversations.json`) + `send_typing` + `send_image` | ✅ |
| 19p | round-N walkthrough validation against satscryption.io | next |

## Reference

- Upstream contract: `gateway/platforms/ADDING_A_PLATFORM.md` in
  `NousResearch/hermes-agent`.
- Reference plugin: `plugins/platforms/irc/` in the same repo.
- Bridge module (source for the 19n port): `hermes_a365.activity_bridge`.
