# v0.7.5 combined live walk — #67 + #79 + #53

**Status: planned, not yet walked.** This is a one-time, milestone-scoped
validation plan (like the locked v0.7.0 walk). It bundles three Path B /
Copilot Chat validations into a single tenant standup so we only bring up
the bot + tunnel + gateway once. Findings land in
[`live-tenant-test.md` §11.10](live-tenant-test.md) after the walk; on a
green walk this file can be deleted or archived under `docs/historical/`.

Tenant standup (bot resource, endpoint re-point, manifest, Copilot Chat
upload) is **not** duplicated here — follow
[`live-tenant-test.md` §11](live-tenant-test.md) §§11.2–11.8. This plan
only adds the three milestone-specific validations and their acceptance
gates.

## What this walk validates

| Issue | Milestone | What the walk confirms |
|---|---|---|
| **#67** | v0.7.5 | A Path B proactive send (outside the in-turn reply path) mints a BF S2S token, POSTs via `sendToConversation`, and **visibly appears** in Copilot Chat. |
| **#79** | v0.7.5 | An `installationUpdate`/`conversationUpdate` at install time captures the conversation reference so proactive reaches a chat **nobody has messaged yet**; uninstall **evicts** so we stop POSTing into a removed conversation. |
| **#53** | v0.7.4 (shipped unwalked) | The CC status-coalescing change renders the terminal-failure flush as **one** bubble (not N), and Teams 1:1 status is unchanged. Confirms the v0.7.4 fix on the live surface. |

#79 and #67 are coupled: #79's new capture path is exactly the
"install-then-proactive-without-prior-message" step of #67's walk.

## Pre-flight (do these first, every time)

1. **Port ownership — do NOT assume `:3978`/`:3979` are free.** Per the
   2026-05-26 incident, the operator runs other gateway profiles; both
   ports have had listeners. Before starting anything:
   ```bash
   lsof -nP -iTCP:3978 -sTCP:LISTEN ; lsof -nP -iTCP:3979 -sTCP:LISTEN
   ps -axo pid,etime,command | grep -iE "hermes_cli|cloudflared|uvicorn" | grep -v grep
   ```
   Pick a free port for the walk gateway; if `~/.hermes/.env` pins
   `HERMES_BRIDGE_PORT`, it overrides a shell prefix (env_loader loads with
   `override=True`) — edit the `.env` line for the walk and revert after.
2. **Secrets: narrow grep only.** Read `~/.hermes/.env` with key-anchored
   greps (`grep -nE '^A365_BF_APP_ID=' …`), never `sed`/`awk` line ranges —
   adjacent lines hold `A365_*_CLIENT_SECRET`. Treat any secret reaching
   stdout as leaked. Confirm `A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET` are
   set (Path B identity, `1c2b61bc…`).
3. **Runtime baseline.** The gateway at `~/.hermes/hermes-agent` should be
   ≥ the build carrying upstream `NousResearch/hermes-agent#33816` (per-turn
   fallback-status buffering — the v0.7.0-era per-turn noise is fixed in the
   runtime; this walk is about the *residual* terminal-failure flush). Refresh
   the editable install of this branch:
   `~/.hermes/hermes-agent/venv/bin/python -m pip install -e '<repo>[bridge]'`.
4. **Cold device-code auth.** First Path B publish on a cold token cache
   needs **two** device codes back-to-back (AAD client, then resource).
   Not a bug — surfaces once, silent-tokens after.
5. **Manifest UUID patch** (if re-publishing): mutate via Python `zipfile`
   surgical replace, never `zip -j -u` (that overwrites the post-transform
   in-zip manifest with the pre-transform template — corrupted-zip incident
   2026-05-26).

## Step A — #53: CC status coalescing renders one bubble

The account's `xai-oauth` 403 makes every turn fall back, so a normal turn
exercises the success path; a terminal failure exercises the flush.

1. Send a normal message in Copilot Chat → confirm the reply renders and
   **no** per-turn fallback bubbles appear (runtime #33816).
2. Force a **terminal failure** (all providers exhausted) — e.g. exhaust /
   misconfigure the fallback chain for one turn so the agent gives up.
3. Confirm in CC: the buffered retry/fallback trace + summary render as
   **one** consolidated bubble, not N raw bubbles.
4. Confirm a `personal` (Teams 1:1) turn's status is **unchanged**
   (pass-through), and that a `warn`-key notice posts immediately.

**Acceptance:** one coalesced bubble on terminal failure in CC; Teams 1:1
unchanged. If it still renders N bubbles or drops the trace, capture the
gateway log (`send_or_update_status` routing) and reopen #53.

## Step B — #67: Path B proactive round trip

1. Seed a real Path B CC registry entry by sending one normal turn (this
   populates `_seen_inbounds_this_lifetime` + the registry).
2. Trigger a proactive `adapter.send(chat_id, content)` **outside** the
   in-turn reply path (cron job, or the soak-style direct trigger — see the
   `/tmp/soak_proactive.py` pattern noted in project memory).
3. Confirm in the gateway log: routing took `_send_proactive` →
   `sendToConversation` (POST URL ends `/v3/conversations/<id>/activities`,
   no `replyToId`), BF S2S mint succeeded via `acquire_reply_token(..., path=B)`,
   and the POST returned 2xx (+ new activity id when present).
4. Confirm the proactive message **visibly appears** in Copilot Chat.

**Acceptance:** #67's full checklist (proactive path + S2S mint + 2xx +
visible). On pass, flip docs from "implemented but not separately
live-validated" → "live-validated". On fail, capture status/body/logs and
open follow-up implementation work.

## Step C — #79: install-then-proactive + uninstall eviction

This is the new path #79 adds; it has **no prior user message**.

1. **Install / add** the agent into a CC chat (or re-add) and do **not**
   send any user message.
2. Confirm in the gateway log: `inbound lifecycle type=installationUpdate
   action=upsert conv=…` (or `conversationUpdate`/membersAdded), the
   conversation is in the registry, the activity did **not** spend an agent
   turn, and the chat is **not** in `_seen_inbounds_this_lifetime`.
3. Trigger a proactive send to that chat (as in Step B) and confirm it
   appears in CC **with no prior user message** — the capture-before-message
   path that errored "no registry entry" before #79.
4. **Uninstall / remove** the agent from the chat.
5. Confirm the log shows `inbound lifecycle … action=evict`, the registry
   entry is gone, and a subsequent proactive send errors "no registry
   entry" (i.e. we stop POSTing into the removed conversation).

**Acceptance:** proactive reaches an install-only chat; uninstall evicts and
halts further proactive. If `installationUpdate` doesn't fire on this
surface, note it (some channels signal install via `conversationUpdate`
membersAdded only) and validate via whichever lifecycle activity arrives.

## Teardown

- Revert any `~/.hermes/.env` `HERMES_BRIDGE_PORT` edit.
- Kill the walk gateway + cloudflared (confirm PIDs are *yours* via `lsof`).
- Leave the operator's other gateway profiles running.
- Bot resource / MAC catalog: tear down per §11.9 only if this was a
  throwaway bot; otherwise leave the persistent Path B install intact.
- `bot-service cleanup` does **not** touch the MAC catalog — remove any
  throwaway catalog entry manually.

## On a fully green walk

- Close **#67** (live-validated) and **#79**; confirm **#53** on the live
  surface.
- Record findings in [`live-tenant-test.md` §11.10](live-tenant-test.md).
- Merge `claude/milestone-0.7.5` and cut v0.7.5.
