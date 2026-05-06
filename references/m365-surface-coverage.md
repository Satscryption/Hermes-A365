# M365 surface coverage for the `agent365` plugin

**Snapshot:** 2026-05-06 (slice 19t quick pass).

This file maps every Microsoft 365 / Agent 365 / Copilot surface where
a Hermes agent could plausibly appear, with our adapter's coverage
status for each. Microsoft's surface inventory drifts fast — refresh
this on every walkthrough.

## Architectural framing

Microsoft has three orthogonal layers an "agent" can sit in. **The
agent365 plugin lives in the third layer.**

| layer | what lives here | our involvement |
|---|---|---|
| Identity / governance ("Agent 365") | Observe / govern / secure: agent registry, agentic users, audit, Purview / Defender hooks | Consumed — `register --apply` + `publish --aiteammate` register us in this layer |
| Authoring / runtime ("M365 Copilot extensibility") | **Declarative agents** (manifest + Copilot's orchestrator) vs **custom-engine agents** (bring your own orchestrator + LLM) | We are a **custom-engine agent** built on the M365 Agents SDK shape — Hermes is the orchestrator + model, our `/api/messages` route is the bot endpoint Microsoft routes to |
| Surface ("the channel the user is using") | Teams 1:1 / channel / meeting, Copilot Chat (web / Word side-panel / Excel side-panel / etc.), Outlook compose, mobile, web chat, Slack-via-bridge, … | This file's coverage matrix |

Custom-engine agents reach the surface layer via the **Bot Framework
activity protocol** over a single `/api/messages` endpoint — every
channel below that says "BF activity" gets normalized into the same
inbound shape. Our adapter validates, dedupes, dispatches into Hermes,
and replies via `serviceUrl`.

Declarative agents are a *different runtime entirely* — they don't
have a bot endpoint, they have a manifest Microsoft's orchestrator
parses. Surfaces that only work as declarative agents (some Word /
Excel / PowerPoint native experiences) are out of scope for this
plugin.

## Coverage matrix

Legend:

- ✅ **works as-is** — adapter handles, validated live or trivial extension
- 🟢 **works with extensions** — BF route handles, needs minor `chat_type` / activity-type mapping
- 🟡 **works with new code** — different invoke type or new auth flow needed (often a tracked issue below)
- 🔴 **needs new plugin** — protocol mismatch, can't reuse this adapter
- ⚪ **out of scope** — not a meaningful Hermes surface

| Surface | Protocol | `channelId` | Hosting model | Auth | Coverage | Depends on | Notes |
|---|---|---|---|---|---|---|---|
| Teams 1:1 chat | BF activity | `msteams` | persistent conversation | user-FIC ✅ | ✅ | — | round-5 §9d validated 2026-05-06 |
| Teams group chat | BF activity | `msteams` | persistent conversation, `conversationType=groupChat` | same | 🟢 | — | adapter maps `chat_type=group`; needs live walkthrough |
| Teams team channels (incl. threading) | BF activity | `msteams` | `conversationType=channel`, `replyToId` for threading | same | 🟢 | — | `SessionSource.thread_id` should populate from `conversation.id` thread-suffix; minor extension |
| Teams meetings (in-call agent) | BF activity | `msteams` | meeting-scoped conversation; meeting-specific events (`participantsAdded`, `meetingStart`) | same | 🟢 | #5 (extra event types) | `_should_dispatch` may need additional filters |
| Teams compose extensions ("@Hermes search …") | BF activity | `msteams` | invoke activities `composeExtension/*` | same | 🟡 | **#5** | invokes return SYNC payload, not async reply |
| Mobile Teams | BF activity | `msteams` | same as desktop | same | 🟢 | — | identical wire shape |
| M365 Copilot Chat (standalone web app) | BF activity | likely `msteams` (per agents SDK) or `copilot` | persistent conversation; entry point that surfaces in Word/Excel/PowerPoint/Outlook side-panels too | same | 🟢 | **#3 streaming** | Copilot Chat **requires streaming** for substantive replies (BF connector cuts off non-streaming at ~15s); without #3, only short turns work |
| Outlook — chat-style invocation | BF activity | `emailoffice365` or `msteams` (TBC) | conversation per email thread | same | 🟢 | — | reach via Copilot Chat side-panel inside Outlook |
| Outlook — compose-action panels | BF activity (invoke) | as Outlook | `task/fetch` / `task/submit` invokes | same | 🟡 | **#5** | each invoke returns a `taskInfo` envelope synchronously, not via `serviceUrl` |
| Outlook — email-only flow (agent receives + sends real email) | BF activity | `emailoffice365` | one-off activities; reply via outbound email channel | same (likely) | 🟡 | **#5**, possibly #3 | low-priority unless email-driven workflows become a use case |
| Microsoft Search invocation | BF activity (invoke) | `msteams` | invoke `search` shape | same | 🟡 | **#5** | unconfirmed protocol path |
| Web chat / Direct Line embed | BF activity | `webchat` / `directline` | persistent conversation; no Microsoft tenant context | own bearer | 🟡 | new auth | bypasses A365 user-FIC entirely; would need a separate auth path |
| SharePoint embedded chat (`SPEmbedded`) | BF activity | `directline` (Direct Line shared with web chat) | embed surface in SP site | own bearer | 🟡 | new auth | similar to web chat |
| Mobile / custom-app chat (Direct Line) | BF activity | `directline` | tenant ↔ direct line | own bearer | 🟡 | new auth | — |
| Slack | BF activity (Azure Bot Service channel) | `slack` | external messaging | Slack OAuth tokens | 🟢 | — | adapter handles; channel mapping needs a `chat_type` row |
| Telegram, WhatsApp, Facebook, Twilio SMS, Line, Kik, GroupMe | BF activity | each respective `channelId` | external messaging | per-channel auth | 🟢 | — | architecturally same; out of A365's primary scope but listed for completeness |
| Direct Line Speech | BF activity | `directlinespeech` | speech-driven; uses STT/TTS pipeline | own bearer | 🟡 | new auth + audio | unlikely value prop for Hermes-A365 |
| Power Platform / Copilot Studio publish | BF activity (Azure Bot Service) | varies | depends on publish target | depends | 🟢 | — | publishing path goes through Copilot Studio, not us |
| Word / Excel / PowerPoint Copilot side-panel (declarative agent) | declarative agent manifest | n/a | manifest + Copilot's orchestrator (NOT custom code) | declarative agent uses signed-in-user identity | 🔴 | new plugin | Hermes can't be a declarative agent — declarative means "Microsoft hosts the orchestrator". To appear *inside* Word as Hermes itself, user invokes Copilot Chat side-panel which already routes to us |
| Word / Excel / PowerPoint as Office Add-in (ribbon button, task pane) | Office Add-in API | n/a | TS/JS code in iframe | Office identity + add-in scopes | 🔴 | new plugin | different SDK entirely; would be a *separate* `office-addin-agent365` package, not this plugin |
| Loop components | Loop component SDK | n/a | embedded React component | Loop identity | 🔴 | new plugin | similar story to Office Add-ins |
| OneNote agent | declarative agent + page-context | n/a | declarative manifest | declarative agent identity | 🔴 | new plugin | not a custom-engine route |
| SharePoint Embedded containers (file storage) | Graph (`/storage`) | n/a | not a surface — content layer the agent reaches into via tools | Graph delegated | ⚪ | — | tooling concern, not surface concern; covered by Hermes' Graph tool integration if any |
| Cron / proactive (any surface) | BF activity | matches the target surface | scheduled outbound; agent posts unsolicited via `serviceUrl` of cached `ConversationRef` | user-FIC ✅ | 🟡 | **#4** | slice 19o registry already has `ConversationRef`; #4 is the agent-side trigger mechanism |

## Surfaces we explicitly do NOT cover

Out-of-scope decisions, with reasons:

- **Declarative agents** — wrong runtime layer. Microsoft's orchestrator + foundation model handle the reasoning; we'd contribute *knowledge* and *actions*, not the agent loop. Hermes' value prop is the loop.
- **Office Add-ins** — different SDK, different security model. Would be a separate *complementary* package (e.g. an Outlook add-in that opens a side-pane backed by our existing API). Out of this slice's scope.
- **Loop components** — same as Office Add-ins.
- **Cortana / Direct Line Speech** — voice surface. Architecturally fine but no audio handling in Hermes today; non-priority.

## Validation status

| Surface | Walkthrough | Last validated | Result |
|---|---|---|---|
| Teams 1:1 chat | round-5 §9d | 2026-05-06 | ✅ end-to-end via Hermes plugin path |
| Teams 1:1 chat (gateway-restart durability) | round-5 §9d.6 | 2026-05-06 | ✅ slice 19o registry hydrated |
| All other surfaces | — | — | NOT YET WALKED |

## Highest-value next walkthroughs

Ranked by "what would tell us most for least effort":

1. **Microsoft 365 Copilot Chat (standalone)** — same protocol, different channel context. Tests whether our `_should_dispatch` filter handles the Copilot Chat shape and whether replies render. Requires #3 (streaming) for non-trivial replies. **High value** — Copilot Chat is the surface most users will discover the agent through.
2. **Teams group chat** — same protocol, different `conversationType`. Tests `chat_type=group` mapping. **Medium value** — likely Just Works.
3. **Outlook compose-action (`task/fetch`/`task/submit`)** — first invoke-type test, needs #5. **High value** — Outlook is the most common user surface in many orgs.
4. **Teams team channel + threading** — proves the `replyToId` outbound shape across thread boundaries. **Medium value** — tests slice 19o registry under multi-thread shape.

## Backlog impact

Existing open issues whose scope is touched by this matrix (Phase 4
recommendations follow in slice 19t's commit / issue updates):

- **#3 (streaming)** — gates **Microsoft 365 Copilot Chat substantive
  replies**. The matrix elevates this from "nice to have" to "needed
  before Copilot Chat surface validates".
- **#4 (proactive)** — surface-agnostic, but the matrix surfaces it
  as the prerequisite for cron-driven flows on any surface.
- **#5 (invoke action types)** — gates **Outlook compose-actions**,
  **Teams compose extensions**, and **Microsoft Search invocation**.
  Each invoke name is a separate child issue; #5 should be split if
  any of them gets an actual user-driven priority.
- **#13 (setup wizard)** — surface-agnostic.
- **#14 (secret regression)** — surface-agnostic.

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
