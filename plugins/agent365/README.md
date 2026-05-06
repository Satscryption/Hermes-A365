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

## Status — slice 19m

This is the **registration skeleton only**. Every method on
`Agent365Adapter` that touches the network is a stub that logs a
`TODO 19n` warning and either no-ops or returns `SendResult(success=True)`.
The runtime logic (FastAPI webhook, JWT validation, idempotency dedupe,
serviceUrl gate, outbound user-FIC chain) lives in
`scripts/activity_bridge.py` today and gets ported under
`Agent365Adapter` in slice 19n.

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
| **19m** | skeleton — `PLUGIN.yaml`, adapter class, `register(ctx)` | ✅ this commit |
| 19n | port the FastAPI webhook + bridge runtime under `Agent365Adapter`; map inbound → `handle_message(event)`, outbound → `send()` | next |
| 19o | session table — BF `conversation.id` → Hermes session; conversation memory across turns | |
| 19p | round-N walkthrough validation against satscryption.io | |

## Reference

- Upstream contract: `gateway/platforms/ADDING_A_PLATFORM.md` in
  `NousResearch/hermes-agent`.
- Reference plugin: `plugins/platforms/irc/` in the same repo.
- Existing bridge (source for the 19n port): `scripts/activity_bridge.py`.
