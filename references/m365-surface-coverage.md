# M365 surface coverage for the `agent365` plugin

**Snapshot:** 2026-05-29 (v0.7.2; Path B Copilot Chat GA + streaming/coalesce reframe). Earlier base: 2026-05-12 (slice 19u-a).

This file maps every Microsoft 365 / Agent 365 / Copilot surface where
a Hermes agent could plausibly appear, with our adapter's coverage
status for each. Microsoft's surface inventory drifts fast — refresh
this on every walkthrough.

## Positioning: Hermes-A365 vs sibling Hermes Teams plugins

Hermes ships its own **classic-Bot-Framework Microsoft Teams adapter**
(see `plugins/platforms/teams/adapter.py` in the harness, shipped
v2026.4.30; in-flight enhancements in
[hermes-agent#10037](https://github.com/NousResearch/hermes-agent/pull/10037)
and [hermes-agent#13767](https://github.com/NousResearch/hermes-agent/pull/13767)).
**That is the right tool when you want Hermes as a generic Teams chat
bot** — Azure App Registration + client secret / certificate / Managed
Identity + Teams app manifest with `bots[]`; reach is DM, channels,
group chats, threading, file attachments.

**Hermes-A365 covers what classic Teams bots structurally cannot:**

| Path | Surfacing | Operator prerequisites | Status |
|---|---|---|---|
| **A — AI Teammate** (M365 agentic user) | Agent appears in M365 tenant directory + "Built for your org" picker + M365 People search + agentic-user audit trails. Teams 1:1 chat with M365-native identity (distinct from a classic bot's user-facing affordance). | M365 tenant + Frontier Preview Program + Tier 3 license. **No Azure subscription.** | ✅ Round-8 end-to-end 2026-05-11 (v0.2.0 wizard, v0.3.0 streaming); v0.5.0/v0.5.1 proactive soak 2026-05-13 |
| **B — Custom Engine Agent** (Azure Bot Service + 1.21 manifest) | Agent appears in M365 Copilot Chat's agents picker, side-panels in Word/Excel/PowerPoint/Outlook, and Copilot-fabric search. Also reaches classic Teams surfaces (1:1 / group / channel) as a side effect of the Microsoft Teams channel on Bot Service. | Path A's prerequisites **+ Azure subscription** + a **separate non-agentic Entra app** (`A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET`) for Bot Service S2S — the blueprint/agentic app can't mint BF tokens (`AADSTS82001`). Provision via the `bot-service` wrapper family (slice 20). | ✅ GA since v0.6.0 (2026-05-18); live Copilot Chat round-trip validated, re-walked through v0.7.2. |

Both paths share the same blueprint Entra app + service principal + bot
endpoint (`/api/messages`), so an operator with both prerequisites can
run both surfaces from one Hermes-A365 install. They are not mutually
exclusive.

**When to pick what:**

- *"Hermes is a chat bot in Teams (DM / channels / group / file uploads)"* →
  sibling Hermes Teams adapter (classic BF). No M365 directory identity,
  no Copilot Chat surfacing.
- *"Hermes is a first-class agentic user in our M365 tenant directory"* →
  Hermes-A365 Path A (AI Teammate). Teams 1:1 with M365-native identity.
- *"Hermes surfaces in M365 Copilot Chat and the Copilot side-panels"* →
  Hermes-A365 Path B (Custom Engine Agent). Requires Azure subscription.
- *"All of the above"* → install both Hermes' Teams adapter AND
  Hermes-A365; configure each for its lane.

## Architectural framing

Microsoft has three orthogonal layers an "agent" can sit in. **Hermes-A365
operates in layers 1 and 3 (identity + surface); the sibling Teams
adapter operates in layer 3 only.**

| layer | what lives here | sibling Teams adapter | Hermes-A365 Path A | Hermes-A365 Path B |
|---|---|---|---|---|
| Identity / governance ("Agent 365") | agent registry, agentic users, audit, Purview / Defender hooks | bot identity only (Entra app, no agentic user) | ✅ agentic user template + M365 directory entry | bot identity only; uses Bot Service registration |
| Authoring / runtime ("M365 Copilot extensibility") | **declarative agents** vs **custom-engine agents** | custom-engine via classic BF | custom-engine via M365 agentic user routing | custom-engine via Bot Service routing |
| Surface ("the channel the user is using") | Teams / Copilot Chat / Outlook / etc. | Teams 1:1, group, channel | Teams 1:1 ("Built for your org" picker) | Copilot Chat picker, side-panels, classic Teams reach as side effect |

All three reach the surface layer via the **Bot Framework activity
protocol** over a single `/api/messages` endpoint — every channel below
that says "BF activity" gets normalized into the same inbound shape.
What differs is **how Microsoft's routing layer decides to forward
activities to that endpoint** (agentic user instance vs Bot Service
channel vs classic BF channel) and what the agent's user-facing
identity looks like.

Declarative agents are a *different runtime entirely* — they don't
have a bot endpoint, they have a manifest Microsoft's orchestrator
parses. Surfaces that only work as declarative agents (some Word /
Excel / PowerPoint native experiences) are out of scope for both
Hermes-A365 and the sibling Teams adapter.

## Coverage matrix

Legend:

- ✅ **shipped + validated** — Hermes-A365 covers this end-to-end, validated live
- 🟡 **shipped, validation deferred** — code is in place but live test gated on operator prerequisites (e.g. Azure subscription)
- 🔵 **sibling-plugin lane** — better covered by Hermes' classic Teams adapter; cross-link rather than duplicate
- 🔴 **out of scope** — would need a separate package (declarative agents, Office Add-ins, etc.)
- ⚪ **non-surface** — not a chat/invoke endpoint (content layer, tooling concern)

| Surface | Best Hermes-stack path | Hermes-A365 coverage | Notes |
|---|---|---|---|
| Teams 1:1 chat (M365 agentic identity) | **Hermes-A365 Path A** | ✅ | Round-5 §9d validated 2026-05-06; round-8 E2E re-validated 2026-05-11 with streaming. Agent appears in "Built for your org" picker. |
| Teams 1:1 chat (generic chat bot, no M365 directory identity) | **Sibling Teams adapter** ([`plugins/platforms/teams/`](https://github.com/NousResearch/hermes-agent/tree/main/plugins/platforms/teams)) | 🔵 | Use the sibling for classic Teams 1:1 if you don't need M365-native agentic identity. |
| Teams group chat (`conversationType=groupChat`) | **Sibling Teams adapter** | 🔵 | Classic BF handles this natively. Hermes-A365 Path A doesn't reach group chats (agentic users are 1:1-only); Path B reaches it as a side effect of the Microsoft Teams channel, but the sibling is the cleaner tool. **Note:** M365 Copilot Chat arrives with this *same* `groupChat` shape but routes through Path B, not the sibling — see the Copilot Chat row below. |
| Teams team channels (incl. `replyToId` threading) | **Sibling Teams adapter** | 🔵 | Same reasoning as group chat. |
| Teams meetings (in-call agent, `participantsAdded` / `meetingStart`) | **Sibling Teams adapter** | 🔵 | BF meeting events; sibling adapter's lane. |
| Mobile Teams | **Sibling Teams adapter** or Hermes-A365 Path A (1:1) | 🔵 / ✅ | Identical wire shape; either path's surfacing applies. |
| **M365 Copilot Chat** (standalone web app) | **Hermes-A365 Path B** | ✅ | GA since v0.6.0; provisioned via the `bot-service` wrapper family (slice 20), live-walked 2026-05-18 → 2026-05-29. **On the wire, Copilot Chat arrives as `conversationType=groupChat` + `channelId=msteams` + a `19:…@thread.v2` id — shape-indistinguishable from a real Teams group chat (row above)** — and does NOT render BF streaming, so replies are coalesced into one non-streaming send (#54 / #55, v0.7.2). See [`activity-protocol-shapes.md`](activity-protocol-shapes.md) → *Streaming and reply delivery*. |
| Word / Excel / PowerPoint Copilot **side-panels** (Hermes as Copilot agent) | **Hermes-A365 Path B** | ✅ | Same Custom Engine Agent registration as Copilot Chat lights these up. Distinct from declarative Office Copilot agents (those are a different runtime — see "Out of scope" below). |
| Outlook — chat-style invocation (Copilot Chat side-panel inside Outlook) | **Hermes-A365 Path B** | ✅ | Same Custom Engine Agent path. |
| Microsoft Search invocation (`search` invoke) | **Hermes-A365 Path B** + #18 | 🟡 | Path B is GA; the `search` invoke handler is still pending (#18). Routed via Bot Service Microsoft Teams channel; specific to Copilot fabric. |
| Outlook — compose-action panels (`task/fetch` / `task/submit`) | Sibling Teams adapter OR Hermes-A365 Path B | 🔵 / 🟡 | Depends on whether the operator wants the compose-action backed by classic BF (sibling) or by the Copilot-fabric Custom Engine Agent (Path B). Either way needs invoke handling (#18). |
| Teams compose extensions ("@Hermes search …", `composeExtension/*` invokes) | **Sibling Teams adapter** + #18-shape work | 🔵 | Classic BF surface; sibling's lane. Hermes-A365 doesn't add value over the sibling here. |
| Outlook — email-only flow (agent receives + sends real email) | **Sibling Teams adapter** (`emailoffice365` channel) or Graph email skill | 🔵 | Niche; sibling can take it via the BF email channel. Hermes-A365 has no specific advantage. |
| Web chat / Direct Line embed | Sibling adapter or separate skill | 🔵 | Bypasses M365 entirely; not Hermes-A365's lane. |
| SharePoint embedded chat (`SPEmbedded`) | Sibling adapter or separate skill | 🔵 | Same — Direct Line, no M365 directory presence. |
| Slack / Telegram / WhatsApp / Twilio / Line / Kik / GroupMe | Use the respective Hermes platform adapters | 🔵 | Each external messenger has its own first-class Hermes adapter; Hermes-A365 not the right path. |
| Cron / proactive sends (Path A surfaces) | **Hermes-A365** | ✅ | Shipped in v0.5.0 / v0.5.1 (slices 19x-a..e; #4 closed, #27 closed). `Agent365Adapter.send()` falls through to `_send_proactive` when this gateway lifetime hasn't captured an inbound for `chat_id`; POSTs to `<serviceUrl>/v3/conversations/<conv_id>/activities` (`sendToConversation` — no `replyToId`). Path B *replies* are GA; Path B agent-initiated proactive sends are not yet a validated path. Sibling Teams adapter handles its own proactive sends independently. |
| Word / Excel / PowerPoint Copilot side-panel as **declarative agent** | Separate skill (not this one) | 🔴 | Declarative agents are a different runtime — Microsoft hosts the orchestrator + LLM. Hermes' value prop is the orchestrator, so we're a custom-engine agent (see Path B), not a declarative one. |
| Office Add-ins (ribbon button, task pane) | Separate skill | 🔴 | Different SDK entirely; would be a complementary `office-addin-*` package. |
| Loop components | Separate skill | 🔴 | Loop SDK; not a BF-protocol surface. |
| OneNote agent | Separate skill | 🔴 | Declarative-agent shape; not a custom-engine route. |
| SharePoint Embedded containers (file storage) | Graph tool integration | ⚪ | Content layer, not a surface. |
| Direct Line Speech | Separate skill if voice becomes a priority | 🔴 | Voice surface; needs STT/TTS pipeline Hermes doesn't have today. |
| Power Platform / Copilot Studio publish | Copilot Studio's own publish flow | ⚪ | Microsoft publishes the agent through Copilot Studio; not Hermes-A365's lane. |

## Surface vs publish flow (2026-05-11)

Two parallel publish flows from the same blueprint Entra app +
service principal. The runtime (Hermes plugin + activity bridge +
streaming) is identical for both; only the registration manifest
+ admin-centre flow differ.

| Flow | Wrapper command | Manifest type | `manifestVersion` | Admin upload | Surfaces it lights up |
|---|---|---|---|---|---|
| **AI Teammate** | `hermes a365 publish --aiteammate --apply` | `agenticUserTemplates` shape | `devPreview` | M365 Admin Centre → Agents → Upload custom agent → Agent 365 admin centre per-user activation | Teams 1:1 ("Built for your org" list); shows up under the user's agentic identity |
| **Custom Engine Agent** | `hermes a365 publish --copilot-chat --apply` (add `--manifest-id auto` or a GUID when publishing beside AI Teammate) | `bots` + `copilotAgents.customEngineAgents` blocks | `1.21+` | Microsoft Admin Portal → Agents → Upload custom agent | M365 Copilot Chat (standalone web + side-panels in Word/Excel/PowerPoint/Outlook), Teams as a classic bot. **Hard prerequisite:** Azure subscription + Azure Bot Service registration (Microsoft Teams channel enabled) bound to a **separate non-agentic Entra app** (`A365_BF_APP_ID`) — the blueprint/agentic app can't mint BF S2S tokens (`AADSTS82001`). Provision with `hermes a365 bot-service create --apply`. Without Bot Service registration, the agent enters the catalog but doesn't route in Copilot Chat. AI Teammate path bypasses this because M365's agentic user infrastructure routes Teams 1:1 traffic without Azure Bot Service. |

Reasoning for keeping both: AI Teammates surface as agentic users
(distinct identity in the user's tenant directory) which is the
right shape for "Hermes is its own thing in your inbox"; Custom
Engine Agents surface as Copilot agents (`@`-mention in the
Copilot Chat prompt box) which is the right shape for "Hermes is
a Copilot specialist". Operators may want both for the same
underlying blueprint.

Per Microsoft's [Custom Engine Agents overview](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-custom-engine-agent#design-and-deployment-considerations):

> Custom engine agents are supported in app manifest version 1.21
> and later versions.

— meaning the `devPreview` AI Teammate manifest is structurally
incompatible with Copilot Chat surfacing. Emitter shipped 2026-05-12
in slice 19u-a (`hermes a365 publish --copilot-chat`). Live
walkthrough 2026-05-12 surfaced an additional hard prerequisite:
the Custom Engine Agent route requires an **Azure subscription** so a
**separate non-agentic Entra app** can be registered as an Azure Bot
Service resource with the Microsoft Teams channel enabled (the
blueprint/agentic app can't mint BF S2S tokens — `AADSTS82001`).
Without that, the manifest uploads and lands in the Teams App Catalog
but Microsoft's routing layer doesn't forward Copilot Chat activities
to our `/api/messages` endpoint. The AI Teammate path bypasses this
because M365's agentic user infrastructure handles Teams 1:1
routing without Azure Bot Service. Shipped GA in v0.6.0 (#16 / #34 /
#36); provisioned via the `bot-service` wrapper family (slice 20).

## Surfaces we explicitly do NOT cover

Out-of-scope decisions, with reasons:

- **Generic Teams chat as a bot (1:1, group, channel, threading,
  attachments)** — sibling-plugin lane. Use Hermes' classic
  Bot-Framework Teams adapter (`plugins/platforms/teams/`,
  shipped v2026.4.30). Hermes-A365 adds nothing over the sibling
  for these surfaces unless the operator wants M365-native
  agentic-user identity (Path A) or Copilot-fabric surfacing
  (Path B).
- **Declarative agents** (Word / Excel / PowerPoint / OneNote
  native Copilot agents) — wrong runtime layer. Microsoft's
  orchestrator + foundation model handle the reasoning; we'd
  contribute *knowledge* and *actions*, not the agent loop.
  Hermes' value prop is the loop, so Hermes-A365 only does
  custom-engine agents.
- **Office Add-ins** — different SDK, different security model.
  Would be a separate *complementary* package
  (`office-addin-*`).
- **Loop components** — same shape as Office Add-ins.
- **Cortana / Direct Line Speech** — voice surface. Architecturally
  fine but no audio handling in Hermes today; non-priority.
- **Web chat / Direct Line / SharePoint Embedded** — bypass M365
  entirely. If you genuinely want web-chat reach, use a generic
  Direct Line skill rather than Hermes-A365.

## Validation status

| Surface | Walkthrough | Last validated | Result |
|---|---|---|---|
| Path A Teams 1:1 (round-5 §9d) | round-5 §9d | 2026-05-06 | ✅ E2E via Hermes plugin path |
| Path A Teams 1:1 (gateway-restart durability) | round-5 §9d.6 | 2026-05-06 | ✅ slice 19o registry hydrated |
| Path A Teams 1:1 (full E2E + streaming on v0.3.0) | round-8 | 2026-05-11 | ✅ BF streaming protocol via `edit_message` validated (closes #3) |
| Path A Teams 1:1 (proactive sendToConversation, v0.5.0 wire) | proactive soak | 2026-05-13 | ✅ token mint + `sendToConversation` POST + 201 wire-validated against live tenant; surfaced gate bug closed in v0.5.1 (#27) |
| Path B Copilot Chat end-to-end (inbound + reply) | §11 runbook | 2026-05-18 → 2026-05-29 | ✅ GA since v0.6.0 (#16 / #34 / #36); reply-quality (coalesce non-streaming → single bubble, no name-dup) validated through v0.7.2 (#54 / #55 / #62). |
| All other surfaces | — | — | NOT YET WALKED (most are sibling-plugin lane) |

## Highest-value next walkthroughs

Ranked by "what would tell us most about Hermes-A365's lane for least effort":

1. ~~**Path B Copilot Chat end-to-end** (#16)~~ — **DONE (GA v0.6.0,
   re-walked through v0.7.2).** Custom Engine Agent surfacing in the
   Copilot Chat agents picker, the reply round-trip (coalesced
   non-streaming — Copilot Chat doesn't render BF streaming), and the
   side-panel reach all validated. Remaining Path B walk targets are
   the invoke types below.
2. **Path A AI Teammate on a fresh tenant** — re-walk the
   register → publish → instance → Teams 1:1 round-trip against
   the latest CLI on a clean tenant to catch wizard / setup
   regressions. Cheap; runs in the existing playbook
   (`references/live-tenant-test.md`).
3. **Outlook compose-action** (`task/fetch` / `task/submit`) via
   Path B — first invoke-type test; gated on #18 (invoke handlers).
   Path B Azure is live as of v0.6.0.
4. **Microsoft Search invocation** via Path B — similar shape to
   compose-action; gated on #18 (invoke handlers).

Sibling-plugin lane walks (Teams group / channel / meetings) are
**deliberately omitted** — they belong on the sibling adapter's
roadmap, not Hermes-A365's.

## Backlog impact

Existing open issues whose scope is touched by this matrix, with
their state under the reframed positioning:

- **#3 (streaming)** — **closed 2026-05-11** by slices 19s + 19s-bis.
  Custom-engine streaming protocol via `edit_message`. Applies to
  both Path A and Path B.
- **#4 (proactive)** — **closed 2026-05-13** by slices 19x-a..d in
  v0.5.0; v0.5.1 (#27, slice 19x-e) fixed the production gate.
  Path A proactive ships; Path B proactive is not yet a validated path.
- **#27 (proactive gate)** — **closed 2026-05-13** by slice 19x-e
  in v0.5.1. Per-lifetime `_seen_inbounds_this_lifetime` set drives
  `send()`'s decision between `replyToActivity` and
  `sendToConversation`.
- **#13 (setup wizard)** — **closed 2026-05-11** by slice 19r.
  Interactive setup + drift detection ship in v0.2.0.
- **#14 (secret regression)** — closed 2026-05-07 by slice
  `--auto-recover-secret`.
- **#16 (Copilot Chat walkthrough)** — **closed (v0.6.0, 2026-05-18).**
  Path B's primary validation; live-walked end-to-end, re-walked for
  reply-quality through v0.7.2.
- **#17 (Teams group + channel walkthrough)** — **superseded by
  the sibling Teams adapter's roadmap**. Hermes-A365 does not own
  this lane. Recommend closing in favour of cross-link to the
  sibling.
- **#18 (invoke activities)** — scope narrowed to Path B-relevant
  invokes: `task/fetch` / `task/submit` for Outlook compose-action
  inside Copilot, `search` for Microsoft Search inside Copilot,
  `signin/verifyState` for OAuth tools. Compose-extension invokes
  (`composeExtension/*`) move to the sibling adapter's roadmap.
- **#24 (Custom Engine Agent emitter)** — **closed 2026-05-12** by
  slice 19u-a.
- **#25 (setup wizard XDG symlink gap)** — **closed 2026-05-12**
  in v0.4.0 (slice 19r-bis).
- **#26 (`--manifest-id` flag)** — **closed (v0.7.2).** Path B-specific;
  `--manifest-id auto|<guid>` lets operators run A + B simultaneously
  with distinct Teams App Catalog ids while keeping `bots[0].botId` on
  the Bot Framework app id. The same slice also hardens emitted zip
  path parsing for workspaces containing spaces and keeps the CEA bot
  scope/command shape aligned with the 2026-05-18 live walk.

## Sources

- [Microsoft Agent 365 overview](https://learn.microsoft.com/en-us/microsoft-agent-365/overview)
- [Custom Engine Agents for Microsoft 365](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-custom-engine-agent)
  — channel list (Microsoft 365 Copilot, Teams, partner apps,
  mobile apps, custom websites for Agents SDK)
- [Create and Deploy a Custom Engine Agent with M365 Agents SDK](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/create-deploy-agents-sdk)
- [Publish agents to channels and clients (Copilot Studio)](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-fundamentals-publish-channels)
  — full channel list including external messaging channels
- [Manage agent instances in Microsoft 365 admin center](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-agent-instances?view=o365-worldwide)
  — AI Teammate lifecycle (used during round-5 cleanup decision)
- [Governance and Lifecycle actions for agents](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/agent-actions?view=o365-worldwide)
  — block / delete behaviour (used during round-5 to decide block-vs-delete)
