# Changelog

All notable changes to the `hermes-a365` skill / plugin live here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [SemVer](https://semver.org/spec/v2.0.0.html).

## [0.8.4] — Unreleased (#89 walk ran 2026-07-16; three lanes carried to #116)

Milestone v0.8.4 — **rich Teams / Copilot Chat surfaces on the #18 invoke
foundation**: file transfer both directions (#76), interactive approval/clarify
cards (#77), outbound AI-content entities (#73), and a Copilot→Teams handoff
**foundation** (#82). The terminal arc walk (#89) exercised these on the live
tenant and **validated the load-bearing card/entity paths on Copilot Chat**;
three personal-Teams-1:1-only lanes (#76c FileConsent accept, personal
streaming, #73c reaction round-trip) were blocked by an environmental
personal-1:1 delivery gap and are carried to **#116**. **#89 stays open** until
that lane completes; **#82 stays open** pending a Hermes-core session-import
hook.

### Added

- **#76 — Teams file/media both directions.** Inbound: image + file attachments
  download into the platform media cache (`{HERMES_HOME}/platforms/agent365/
  media`) and their local paths flow into `MessageEvent.media_urls` for the
  gateway's auto-vision / document path (25 MiB cap, path-traversal-safe names,
  best-effort). Outbound: `send_document` / `send_image_file` deliver a local
  file to a Teams 1:1 via a **FileConsentCard → OneDrive upload** — on Accept the
  `fileConsent/invoke` carries a pre-authenticated `uploadUrl` we PUT the bytes
  to, then confirm with a FileInfoCard (pending offer popped before upload →
  at-most-once). Non-personal chats (Copilot Chat / group / channel) degrade to a
  text fallback; group/channel file transfer needs Graph and is deferred. Manifest
  `supportsFiles` → **true**.
- **#77 — interactive-UI cards.** `send_exec_approval`, `send_slash_confirm`, and
  `send_clarify` render Adaptive Cards with **`Action.Submit`** buttons (documented
  Copilot-Chat-supported; both surfaces). A click returns a message-with-`value`
  tagged `hermes_kind`; the route intercepts it ahead of the agent loop and
  resolves back through the gateway's module resolvers (`tools.approval` /
  `tools.slash_confirm` / `tools.clarify_gateway`). Open-ended clarify sends the
  question as text and arms the gateway text-intercept. `send_model_picker`
  deferred (optional / lowest priority).
- **#73(b) — citations.** `metadata["citations"]` maps to a `citation` array on
  the root `https://schema.org/Message` entity (1-based `position` matching the
  agent's in-text `[N]` markers; capped at 20; malformed entries skipped).
- **#73(c) — feedback loop.** `channelData.feedbackLoop:{type:"default"}` on agent
  replies (plain + streaming-final), env-gated **`A365_FEEDBACK_LOOP`** (default
  on). `message/submitAction` + `message/fetchTask` land as per-name invoke
  children; the reaction is recorded keyed by the replied message id (Teams stores
  nothing).
- **#82 — Copilot→Teams handoff (foundation only).** A `handoff/action` invoke
  child mints/validates/consumes continuation tokens (in-memory map), and a
  policy-gated **"continue in Teams"** deep link (env **`A365_HANDOFF_LINK`**,
  default off) is appended to degraded coalesced-from-stream Copilot Chat replies.
  It does **not** yet import the Copilot session into the Teams turn — that needs
  a Hermes-core conversation-import hook, so **#82 remains open** tracking that
  dependency (this release ships the token lifecycle + deep link only).

### Security hardening (PR #119 review)

- **Inbound attachment download URLs are validated before any fetch** (same #100
  activity-body threat model). Image `contentUrl` (which receives the reply
  bearer) is pinned to the Bot Framework connector allowlist; file `downloadUrl`
  to Microsoft SharePoint/OneDrive hosts. IP-literal / private / link-local hosts
  and non-https URLs are rejected, and redirects are not followed — closing a
  bearer-exfil / SSRF path.
- **Outbound `fileConsent/invoke` is bound + validated.** The pending consent is
  bound to the conversation + user that received the card and verified on accept;
  the `uploadInfo.uploadUrl` is allowlisted to SharePoint/OneDrive before any
  bytes are read or POSTed; and the file is re-stat'd at accept time (reject
  empty / over-cap / changed-since-offer) so a raced or swapped file can't be
  uploaded. `_pending_file_uploads` is now capped, TTL-expired, and cleared on
  disconnect.

### Changed

- Invoke dispatch now uses a **per-instance registry** (the module
  `INVOKE_REGISTRY` plus adapter-bound children for feedback + handoff) passed to
  `dispatch_invoke`, keeping `invoke.py` free of any `plugin/` import.
- `render_reply_activity` gained `build_ai_message_entity` (shared by both
  runtimes) and optional `citations` / `feedback` handling; existing serve callers
  are unaffected unless they opt in.

### Validated (#89 walk, 2026-07-16 — Copilot Chat / Path B)

- **#77 interactive cards — the load-bearing check:** an Adaptive Card
  `Action.Submit` **renders and resolves on Copilot Chat** end-to-end
  (`/reset` → slash-confirm card → click → `tools.slash_confirm.resolve` →
  follow-up posted; the submit is intercepted by the route, never dispatched to
  the agent loop).
- **#73b citations** render as numbered clickable references (validated by
  bench-posting the real `build_ai_message_entity` wire format).
- **#73c feedbackLoop** renders (thumbs + form). **#82** continuation deep link
  renders on degraded CC replies. Plain text turns round-trip on Path B.
- **#82 fix (walk-caught):** `_handoff_deep_link` now targets the Teams-routable
  BF/messaging bot id (`bf_app_id or blueprint_app_id`), not the CEA blueprint —
  the walk caught the blueprint variant opening the wrong bot in Teams.

### Not yet live-validated (tracked in #116)

- **#76c FileConsent→OneDrive accept**, **personal BF streaming**, and the
  **#73c feedback reaction round-trip** are personal-Teams-1:1-only and were
  blocked by an environmental personal-1:1 message-delivery gap on the fresh walk
  bot (channel + Copilot delivered fine; personal DM did not). All three remain
  unit-tested + wire-format-sound. The Copilot Chat feedback caveat (reaction not
  surfaced to the developer) was **confirmed live**. Provisioning finding: the CEA
  `bots` block requires an Azure Bot Service (#117).

### Notes

- **#82 and #89 are NOT closed by this release.** #82 ships the handoff
  foundation only (token lifecycle + deep link); full session import needs a
  Hermes-core conversation-import hook. #89's terminal-walk acceptance (both paths
  × all three surfaces) is incomplete — the three personal-1:1 lanes are tracked
  in #116, and #89 stays open until they are validated.
- Provisioning finding: the CEA `bots` block requires an Azure Bot Service
  resource; the Path A blueprint alone yields "Invalid bot" in Teams (#117).

## [0.8.3] — 2026-07-06

Milestone v0.8.3 — **Copilot Chat prompt starters + manifest 1.27 bump** (#74).
Walk-validated live on the satscryption tenant (see *Validated* below).

### Added

- **#74:** the Custom Engine Agent manifest now emits **`type:"prompt"` prompt
  starters** as the Copilot Chat zero-state UX, replacing the single generic
  `title`/`description` command. Each starter is `{title, description, type:
  "prompt", prompt}`; `title` is the card label, `prompt` is the text sent on
  tap, and `description` (= the prompt) drives the card's subtitle. A sensible
  **default set of 3** ships out of the box, and operators can override them with
  a repeatable **`--prompt-starter "Title|Prompt text"`** flag (capped at 10).
  Scoped to `copilot` + `personal` (the zero-state surfaces); `team` is the
  @mention command menu and is deliberately excluded.

### Changed

- **#74:** `_COPILOT_CHAT_MANIFEST_VERSION` bumped **`1.21` → `1.27`** — the
  minimum manifest version that carries the command `type`/`prompt` fields
  (verified against Microsoft's live v1.27 schema, May 2026). The rest of the CEA
  shape (`bots` + `copilotAgents`) is unchanged and remains valid at 1.27.

### Validated (v0.8.3 walk, 2026-07-06)

- **1.27 manifest accepted** by the Microsoft Admin Center on upload — the
  load-bearing check for the version bump + `type:"prompt"` schema.
- **Prompt starters render** in the Copilot Chat zero-state (3 cards, tappable
  → prompt drops into the composer) against a throwaway `Hermes Inbox Helper R9`
  blueprint. The walk surfaced (and this release fixes) that omitting
  `description` makes Copilot repeat the title on the card's subtitle line.

### Notes

- The walk also surfaced that `publish --copilot-chat` re-stamps the *same* app
  `version` each run, so an operator can't re-upload an updated package without
  hand-bumping it — filed as a v0.8.x follow-up (auto-increment / `--app-version`).

## [0.8.2] — 2026-07-02

Milestone v0.8.2 — **inbound trust-boundary & secret-handling hardening**. The
verified subset of the multi-model security red-team (#100 / #101). Every finding
was independently re-verified against the code (two diverse-lens verifiers each)
before fixing, which downgraded one "critical" to defense-in-depth, promoted a
"PLAUSIBLE" to the top live finding, and rejected two proposed fixes as wrong. A
follow-up multi-model review of the PR (#106) then caught the first-cut L4 fix
wiring only the serve reference runtime; the entries below are the post-review
versions. No walk — all unit-tested.

### Security

- **#100 H1 — body-supplied identity no longer steers token minting (app-id +
  tenant axes).** `recipient.agenticAppId` and `tenantId` are unauthenticated body
  fields, yet they named the identity/tenant every FMI stage minted under
  (`fmi_path`, T2 / user_fic `client_id`, the tenant token endpoint).
  `acquire_outbound_token` now asserts `agenticAppId == blueprint_client_id` (the
  round-3-confirmed A365 invariant that the blueprint Entra app *is* the agent
  identity) **and** `tenantId == cfg.tenant_id`, refusing to mint under a
  body-named identity or tenant. Entra's FIC grant backstops this server-side;
  this is fail-fast local defense-in-depth. The *user* axis (`agenticUserId`) has
  no JWT claim to bind to — the A365 inbound token is a service token (`azp` = the
  platform SP, no end-user claim), so the agentic user is asserted by the
  azp-allowlisted platform and gated by Entra's `user_fic` grant, not a local
  check. *(The #106 review's "bind agenticUserId to the JWT" was verified
  infeasible for this reason.)*
- **#100 M2 — the outbound `serviceUrl` allowlist is tightened to non-registrable
  hosts.** `.trafficmanager.net` and `.azure.com` are customer-registrable (any
  tenant can stand up `<label>.trafficmanager.net` and receive our freshly-minted
  user bearer — token exfil). The allowlist now pins the exact Teams host
  `smba.trafficmanager.net` and keeps only non-registrable Microsoft zones
  (`.botframework.com` / `.us`, `.cloud.microsoft`) as suffixes: a bare entry is
  an exact-host match, a leading-dot entry a subdomain-suffix match.
- **#100 M1 — the per-user token cache is keyed on the full identity tuple**
  `(tenant_id, agent_app_instance_id, agentic_user_id, scope)` (matching
  `_FmiCache`) instead of `(agentic_user_id, scope)`, closing a latent
  cross-identity handout. The agentic ids are also `str`-coerced so a non-string
  value (`True` vs `1`, which are equal and hash-equal in Python) can't collide in
  the key (#106 review).
- **#100 L4 — the outbound mint path is bound to the validated JWT path, in both
  runtimes.** The path is captured at inbound-validation time (which validator
  passed), stored on the `ConversationRef`, and threaded into every mint site: the
  serve route *and* all plugin-adapter sends (proactive / stream / status / edit /
  reply), including the decoupled agent-loop sends that reply off a cached ref.
  `acquire_reply_token` and `send_reply` **require** `validated_path` (fail-closed:
  an un-plumbed caller is a `TypeError`, never a silent body-derived fallback), so
  a BF-validated (Path B) inbound carrying injected agentic ids can no longer be
  minted through the Path A user-FIC chain. *(The #106 review caught the first cut
  wiring only the serve reference runtime, leaving the production plugin exposed.)*
- **#101 H2 — secrets are redacted from `hermes a365 status` output.**
  `gather_local_config` masks secret-valued keys — matched on a normalized key
  (uppercased, non-alphanumerics stripped) so `CLIENT_SECRET`, camelCase
  `clientSecret`, glued `APIKEY`, connection strings, `CREDENTIAL`, and `*_KEY` /
  `*_PAT` names are all caught (#106 review — the first-cut `_KEY` substring missed
  `APIKEY` / camelCase) — in the per-agent `.env` before storing them in the
  component `data`, so the default JSON output no longer emits
  `A365_BF_CLIENT_SECRET` verbatim into shell history / CI logs / support tickets.
  (`doctor.py` only counts keys, so it needs no change.) The `serviceUrl` allowlist
  check (M2) also now fails closed on a malformed URL and tolerates a trailing-dot
  FQDN (#106 review).

### Deferred / accepted

- **#100 M3** (dedupe pre-seed) → **v0.9.0**: real but low, and the proposed fix
  is a no-op on Path A — `azp` is the shared platform SP, so scoping dedupe "per
  authenticated sender" does not distinguish senders. Needs a different signal;
  deferred rather than shipped ineffective.
- **#101 L1** (Keychain secret on argv) → **accepted risk**: a same-UID attacker
  can already read the secret from the Keychain directly (`security
  find-generic-password -w`), so argv exposure adds no capability; the `security`
  CLI has no stdin mode, and the trade-off is already documented in `keychain.py`.

## [0.8.1] — 2026-07-02

Milestone v0.8.1 — **invoke-foundation hardening**. The three follow-ups the
#94 review surfaced against the v0.8.0 invoke foundation. No walk — all
unit-tested; the rich-surface invoke children split forward to v0.8.2–v0.8.5.

### Fixed

- **#96:** a Bot Framework retry of an `invoke` (same
  `conversationId:activityId` within the idempotency TTL) no longer returns the
  `{"status": "duplicate"}` dedupe marker as its body. Teams reads an invoke's
  HTTP body as the invokeResponse body, and the marker is not a valid
  taskInfo/result envelope, so the task module failed to render on the retry.
  The two runtimes are fixed differently by design:
  - **Plugin:** the invoke branch is now intercepted *before* the idempotency
    dedupe. Today's names (`task/fetch`) are local + idempotent, so a retry
    safely re-renders; invokes never enter the agent loop, so bypassing the
    message dedupe cannot double-fire it.
  - **Serve:** the operator webhook may be non-idempotent (the reason serve
    dedupes at all), so a deduped invoke does *not* re-forward — it returns a
    benign empty `200` invoke ack instead of the marker.

  Full per-name response replay (caching the original invokeResponse to replay
  on retry, for future *side-effectful* names) remains deferred to 19w-g.
- **#97:** the `serve` invoke pass-through now coerces the operator-supplied
  invokeResponse defensively — a non-`dict` envelope, or a `status` that is
  non-numeric / `Infinity` / `NaN` / wrong-type, degrades to a `200` ack
  instead of raising an `AttributeError` / `ValueError` / `OverflowError` /
  `TypeError` that surfaced as an unhandled HTTP 500. (The #99 red-team caught
  the non-dict-envelope and `Infinity` gaps in the first-cut fix.)

### Documentation

- **#95:** the `invoke.py` module docstring no longer re-teaches the removed
  `{"status", "body"}` wire wrapper (the shape the v0.8.0 walk fix corrected);
  it now describes the actual wire — HTTP status = the invoke status, HTTP body
  = the taskInfo / result payload — matching the `InvokeResponse` class
  docstring below it.

## [0.8.0] — 2026-07-01

Milestone v0.8.0 — the **BF invoke-activity foundation** (#18 / slice 19w),
the floor the 0.8 arc builds on. Ships on unit tests; a coordinated live
invoke walk (the #18 closure criterion) runs after the foundation lands as
an early v0.8.0 gate.

### Added

- **#18 (19w-a):** typed, synchronous invoke dispatch. Invoke activities are
  a request/response wire — the reply must return in the *same* HTTP turn as
  a `{status, body}` invokeResponse, not via the async `send()` path.
  Previously the plugin adapter let invokes fall through to `handle_message`
  (fire-and-forget), so Microsoft logged a 200 and the user saw no response.
  New shared `hermes_a365/invoke.py` — `InvokeResponse` / `InvokeContext`, a
  per-`name` dispatch registry with a total 501 default (unknown names never
  500 the endpoint), `classify_chat_type` (distinguishes Copilot Chat from a
  genuine Teams group via the inbound path tag), and response builders that
  build `card.invoke.response` envelopes **directly** — never through
  `render_reply_activity`, so an invoke response never inherits the #73a
  "AI generated" content-label entity. The plugin route intercepts `invoke`
  after auth + dedupe + lifecycle, before `_should_dispatch`, and returns the
  synchronous body; the JWT validators' claims (previously discarded) now
  feed `InvokeContext` identity.
- **#18 (19w-a): `task/fetch`** is the first end-to-end invoke name —
  user-action-triggered (so live-walk-able on demand), a self-contained
  `taskInfo` card echo needing no tool token. The parent wire shape for the
  v0.8.1 children (#73 `message/*`, #77 `adaptiveCard/action`, …).
- **#18 (19w-b): `TokenFactory`** — the seam for minting outbound *tool*
  tokens (Graph/app) a per-name handler needs, reusing the existing
  `_inbound_path_tag` Path-A/B dispatch (not re-deriving the chain).
  `for_graph` is implemented (Graph works on both paths); `for_app` /
  `for_workiq` (#21) are hooks their consuming v0.8.1 slices implement. No
  v0.8.0 handler needs a tool token, so this is a landed foundation seam.

### Fixed

- **#18 (review finding 3):** the standalone `serve` runtime now dual-
  dispatches inbound JWTs — BF-issuer tokens through `validate_inbound_jwt_bf`,
  A365/AAD-v2 through `validate_inbound_jwt` — matching the plugin adapter.
  Previously `serve` validated only the AAD-v2 shape and would reject
  legitimate Path B Bot Framework Connector tokens (Direct Line / Teams via
  Bot Service / Copilot fabric).
- **#18 (walk-caught):** the invokeResponse was serialised as a
  `{"status", "body"}` wrapper; the BF wire wants the `body` (the taskInfo /
  result) as the **HTTP body** with the **HTTP status** = the invoke status.
  Teams rejected the wrapper with "Unable to reach app". Both runtimes now
  serialise as `JSONResponse(resp.body, status_code=resp.status)`; the
  misleading `InvokeResponse.as_dict()` was removed. (Neither the unit tests
  nor the red-team caught this — both deferred to the never-live-validated
  pre-existing `serve` shape; the walk broke it.)

### Validated (v0.8.0 walk, 2026-07-01)

- **`task/fetch` end-to-end on Teams 1:1** against the satscryption tenant:
  a real `task/fetch` invoke arrives → Path B auth → typed dispatch →
  synchronous taskInfo → Teams renders the task module ("Request received").
  #18's live-walkthrough closure criterion is met.
- **Copilot Chat does not surface `task/fetch`** — it renders the Adaptive
  Card body but strips the `Action.Submit` task button (Copilot is an
  `Action.Execute` / `adaptiveCard/action` surface). So `task/fetch` is a
  Teams-surface invoke; the Copilot-native invoke (`adaptiveCard/action`) is
  the v0.8.1 child for that surface. Real shapes captured in
  [`references/activity-protocol-shapes.md`](references/activity-protocol-shapes.md).

### Deferred (→ v0.8.1)

- All other per-name invoke handlers (#73 `message/submitAction`+`message/fetchTask`,
  #76 `fileConsent/invoke`, #82 `handoff/action`, #77 `adaptiveCard/action`,
  `search`, `signin/*`); `composeExtension/*` moves to the sibling Teams
  adapter. 19w-g invoke idempotency replay (`task/fetch` is idempotent).
  Typed invoke dispatch on `serve` (its operator webhook owns invoke
  responses). The agent-loop→invoke bridge (the plugin's `handle_message` is
  fire-and-forget); `task/fetch` synthesizes its first card locally.

## [0.7.6] — 2026-06-30

Milestone v0.7.6 — collapsed pre-0.8 polish + manifest currency (absorbed
the former v0.7.7/v0.7.8). All items ship on unit tests; live Copilot
Chat / Teams validation of #78's mention behaviour is pooled into the #89
v0.8.1 walk. **#74 (prompt starters) was deferred to v0.8.1** — a red-team
pass (verified against Microsoft's v1.21 schema) found `type:"prompt"`
command fields require manifest **1.27**, and per #75 the manifest version
is not bumped without walk validation, so #74 lands together with that
bump at the #89 walk.

### Added

- **#71:** `bot-service create` / `update-endpoint` now defensively
  validate the operator-supplied endpoint via `urlparse`: a non-HTTPS URL
  on a remote host is refused (BF Bot Service requires TLS), and a
  `localhost` / `127.0.0.1` / `::1` host is refused unless the new
  `--allow-local` flag is passed (which then also permits `http://` for
  that loopback dev-tunnel case). Remote `http://` stays refused even with
  `--allow-local`. Mirrors the Path A `activity-bridge update-endpoint`
  HTTPS guard. The `/api/messages` normalization tail is unchanged.
  *(`--probe-reachability` HEAD check deferred to a follow-up.)*
- **#73:** outbound message activities now carry the BF/Teams
  **"AI generated" content label** (`https://schema.org/Message` with
  `additionalType: ["AIGeneratedContent"]`) — on text replies, the Copilot
  Chat coalesced flush, card sends, the streaming-final activity, and
  proactive sends. Inert on channels that don't render it; never attached
  to typing / intermediate-streaming chunks. *(Part (b) citations and (c)
  feedback-loop deferred: there is no citation data source today, and the
  feedback loop depends on #18 invoke activities — splits forward to v0.8.1.)*

### Documentation

- **#75:** added [`references/manifest-schema-currency.md`](references/manifest-schema-currency.md)
  — investigation of the `1.21` CEA manifest pin vs `1.25 agenticUserTemplates`
  / `1.27 agentConnectors`. Records that Path A (`devPreview`) and Path B
  (`1.21`) are *separate* manifest tracks, and that the concrete capability
  motivating a future bump is **prompt starters (#74)**: `type:"prompt"`
  command fields were introduced in manifest **1.27** (the `1.21` command
  schema is `additionalProperties:false` — `title`/`description` only), so
  #74 must land with a `1.27` bump, walk-validated at #89. **The default
  manifest version stays `1.21`** in v0.7.6 — no schema change, no opt-in flag.

### Fixed

- **#78:** inbound recipient `@mention` markup (`<at>AgentName</at>`) is now
  stripped from the text handed to the agent in group/channel surfaces —
  entity-driven, matched on `mentioned.id == recipient.id` so user-to-user
  mentions are preserved. Whitespace is tidied only when a mention was
  actually removed (multi-line bodies are not reflowed). `raw_message` and
  the `entities` list are left untouched. Verified no-op on Copilot Chat
  (which pre-strips). *(Outbound mention-entity support deferred — demand-gated.)*

## [0.7.5] — 2026-06-30

Milestone v0.7.5. Headline is #79 (BF lifecycle capture/evict); the live
walk that validated it also caught two real regressions in the *shipped*
v0.7.4 (`connect()` + `publish`), both fixed here.

### Added

- **#79:** BF lifecycle activities (`installationUpdate` add/remove,
  `conversationUpdate` membersAdded/membersRemoved) are now routed through
  the conversation registry instead of being dropped (`conversationUpdate`)
  or wasting an agent turn (`installationUpdate` fell through to
  `handle_message`). On a bot add → capture the conversation reference so
  proactive delivery (#33/#67) can reach a chat the operator installed the
  agent into; on a bot remove → evict so we stop POSTing into a removed
  conversation rather than waiting out the 30-day prune. A pure
  `_lifecycle_registry_action` classifier gates capture/evict strictly on
  the *bot itself* being the added/removed member (`recipient.id`), screens
  synthetic `agents`-channel probes, and uses **capture-if-missing** so a
  lifecycle activity never clobbers a richer captured user-message ref.
  Lifecycle capture deliberately does not mark the chat seen-this-lifetime
  (a lifecycle activity has no replyToActivity-able id, so `send()` routes
  proactively). New `ConversationRegistry.evict`.
  *Live-walk-validated:* capture fires correctly against real Microsoft
  payloads on Copilot Chat, Teams 1:1, and Teams channel; evict is
  unit-validated (its live trigger was blocked by Teams admin policy).
- **Request-level inbound logging** (closes the finding-5 observability
  gap): every inbound activity's shape (`type`/`action`/`channelId`/`from`/
  `conversationType`/`conv`/`membersAdded`/`membersRemoved`) is logged at
  the top of `/api/messages`, before any gate — so the log shows whether a
  given activity (e.g. an `installationUpdate`) even reached the endpoint.

### Fixed

- **`Agent365Adapter.connect()` gateway-contract drift:** the base contract
  is `connect(self, *, is_reconnect: bool = False)` and the gateway's
  reconnection watcher always calls `adapter.connect(is_reconnect=...)`. Our
  override had drifted to `connect(self)`, so against the current
  hermes-agent core the platform failed to connect
  (`unexpected keyword argument 'is_reconnect'`) and never bound its port —
  affecting **shipped v0.7.4 and `main`**, not just this branch. Surfaced by
  the live walk (unit tests mock `connect`, so the drift was invisible).
- **`publish --copilot-chat` broke against Microsoft a365 CLI ≥ 1.1.181:**
  the wrapper post-processed the `.zip` the CLI used to emit, but 1.1.181
  changed to extract a manifest *template* and stop for customisation
  (emitting no zip), so `--copilot-chat` silently produced only the raw
  blueprint template (no `bots`/`copilotAgents`). Now the wrapper detects
  the extracted directory and packages it itself, then the existing CEA
  transform runs unchanged → valid 1.21 Custom Engine Agent zip. Also
  affected **shipped v0.7.4**.

### Validated (v0.7.5 live walk)

- **#67:** Path B proactive `sendToConversation` round-trip — token mint →
  POST → **visibly rendered** in Copilot Chat (not just a 2xx). Closes the
  separately-tracked live-validation.
- **#53** (shipped v0.7.4): both halves re-confirmed on the live CC surface
  — per-turn fallback noise is invisible, and the terminal-failure flush
  renders as **one** coalesced bubble (A/B: 3 separate bubbles vs 1).

## [0.7.4] — 2026-06-20

### Fixed

- **#53:** Hermes' internal status/lifecycle notifications (retry/fallback
  traces, the terminal-failure summary) no longer spray Copilot Chat with
  a separate bubble per line. The adapter now implements the gateway's
  optional `send_or_update_status` hook (the same seam Telegram uses to
  edit one status bubble in place, hermes-agent issue #30045). Copilot
  Chat arrives as a `groupChat` and does not render a Bot Framework edit
  (the v0.7.0 walk finding), so instead of editing we **coalesce**: a
  burst of same-key status lines buffers under one synthetic id and
  flushes as a single combined bubble once the burst settles. Status that
  would interleave into an active streaming/coalesced reply is suppressed
  (Custom-Engine-Agent message-ordering rule). Teams 1:1 (`personal`)
  status is passed straight through to `send` unchanged — Path A
  behaviour is preserved, no filtering. This is permanent, upstream-
  sanctioned adapter surface, not a temporary content filter.

  Note: the *per-turn* fallback noise that originally motivated this issue
  (two status bubbles before every reply, from the xai-oauth 403 fallback)
  was already eliminated upstream by hermes-agent#33816, which buffers
  retry/fallback status and drops it on a successful turn; that fix is in
  the running gateway. This change addresses the remaining Copilot Chat
  residual — the terminal-failure flush rendering as N raw bubbles. The
  one-shot `/sethome` onboarding notice is a separate path with no clean
  private surface in Bot Framework/Copilot Chat and is left as a
  follow-up.

  Pre-release hardening (adversarial red-team): the flush now guards two
  races — a same-key line appended while the consolidated bubble is
  mid-send is re-armed into a trailing bubble instead of being dropped,
  and a turn that opens *during* the debounce no longer lets a stray
  status bubble interleave into it. Substantive `warn` notices bypass
  coalescing (they post immediately), and a status for a chat not seen
  this gateway lifetime falls back to the proactive send path rather than
  a possibly-stale `replyToActivity`.

  Tests: 915 → 926 (+11 `TestSendOrUpdateStatus`: personal/unknown
  pass-through, groupChat burst coalescing, exact-repeat dedup,
  suppress-while-turn-active, buffered-then-turn-opens suppression,
  append-during-flush re-arm, `warn` pass-through, registry-only
  fallback, empty no-op, disconnected flush drops state).

## [0.7.3] — 2026-06-02

Cleanup + correctness release on top of v0.7.2. `register --aiteammate`
becomes an explicit unsupported surface with a redirect at the real AI
Teammate flow (was a misleading no-op). Slice 20's Bot Service cleanup
and diagnostics seams tightened — structured `blueprint_preserved` flag
instead of substring matching, single source of truth for sidecar
required-field validation. The new non-personal coalesced reply buffer
gains a liveness watchdog so a missing `edit_message(finalize=True)` no
longer leaves a silent pending reply. Plus a comprehensive
documentation sweep across `README.md` and all reference docs capturing
the post-v0.6.0 Path B GA state and the Copilot Chat `groupChat` shape
finding.

### Changed

- **#37:** `hermes a365 register --aiteammate` is now an explicit
  unsupported/deprecated surface — accepted for backward compatibility
  but rejected before any plan or apply work with exit code `2` and a
  pointer at the real AI Teammate flow (`publish --aiteammate` →
  M365 Admin Centre upload → per-user activation). README + SKILL.md +
  `references/a365-cli-reference.md` no longer claim `register
  --aiteammate` creates the agentic Entra user.
- **#48:** Slice 21b internal cleanup tightened Bot Service cleanup and
  diagnostics maintenance seams: generated-config help now documents
  the cwd default, cleanup uses a structured blueprint-preserved flag
  instead of message substring matching, bot-service cleanup argv is
  explicitly render-only, and diagnostics reuses the sidecar dataclass
  required-field validation.

### Fixed

- **#65:** Non-personal coalesced reply buffers now have a liveness
  fallback. If Hermes' stream consumer never calls
  `edit_message(finalize=True)`, a watchdog flushes the latest
  buffered content as one normal `send_reply()` after the stale-stream
  threshold; failed timeout flushes are logged and dropped instead of
  remaining silently buffered forever.

### Documentation

- Documented the Path B Copilot Chat reply-delivery model in
  `references/activity-protocol-shapes.md` (new *Streaming and reply
  delivery* section): Copilot Chat arrives as `groupChat` and does not
  render BF streaming, so non-personal turns coalesce into one
  `send_reply` (#54 / #55), while personal chats stream with
  single-stream-per-turn + a stale-stream liveness guard (#62);
  `content_delivered` is unreliable and must be confirmed visually.
- Refreshed the stale Path B bullet in `SKILL.md` — Copilot Chat is GA
  since v0.6.0 (provisioned via the `bot-service` wrappers against a
  separate non-agentic Entra app), not "deferred pending #16".
- Added §11.9 / §11.10 runbook notes that `bot-service cleanup` does not
  remove the Managed App Catalog entry (remove it manually via MAC →
  Agents) and that `az bot delete` propagates immediately.
- Swept `README.md` and all reference docs for Path B GA drift: most
  predated v0.6.0 and still framed Copilot Chat as "deferred pending
  #16". Corrected version/status (`README` v0.5.1 → v0.7.2, Path B
  surface rows 🟡/⏸ → ✅), documented that Copilot Chat arrives as
  `groupChat` (`m365-surface-coverage.md`), the separate non-agentic
  Path B Entra app + `AADSTS82001` (`entra-blueprint-properties.md`,
  `error-codes.md`), and that §11 is the GA Path B runbook
  (`live-tenant-test.md`). Removed the v0.1-era "skill doesn't drive the
  CLI" note and clarified there is no Microsoft#408 fix-floor
  (`a365-cli-reference.md`). Path B *replies* are GA; Path B *proactive*
  code is shipped and unit-covered via #33 (BF S2S +
  `sendToConversation`) but remains described as not separately
  live-walked until #67 validates the round trip.
- Reconciled `_send_proactive` / `send()` docstrings with the shipped
  #33 Path B proactive path: only `path == "unknown"` is refused; Path B
  targets use the BF S2S dispatcher branch and `sendToConversation`
  rather than a stale "#16 deferred" guard.

## [0.7.2] — 2026-05-29

Copilot Chat reply-quality release. Custom Engine Agent (Copilot
Chat) replies that previously fragmented into multiple bubbles per
turn now arrive as a single message, and the duplicated agent-name
lines are gone. Path B streaming also gained a stale-stream liveness
guard, side-by-side AI Teammate + Copilot Chat publishing landed, and
non-2xx Bot Framework reply POSTs are now treated as failures.
Validated end-to-end against the live tenant via a pre-merge branch
walk — Copilot Chat rendered one bubble per turn across 7+ turns
(including multi-tool replies) while Teams 1:1 streaming was preserved.

### Fixed

- **#54:** Path B streaming now enforces one active Custom Engine
  Agent stream per chat turn. Streams opened via `edit_message()`
  register as the chat's active stream, fresh message ids continue
  that stream instead of starting a second bubble, one-shot
  progress/fallback sends are suppressed while streaming, separate
  image activities are blocked mid-stream, and stale-stream
  finalization must succeed before a replacement stream can start
  unless bounded retry/age guards identify the stale stream as dead
  and force-drop it to preserve chat liveness. After the #54 branch
  walk showed Copilot Chat `groupChat` accepts but does not render BF
  streaming activities, non-personal stream-consumer chunks are now
  buffered locally and emitted as one normal `send_reply()` only on
  `edit_message(finalize=True)`.
- **#55:** Copilot Chat replies no longer repeat the agent
  display-name line. With non-personal turns coalesced into one
  `send_reply()` (see #54), the duplicate name-lines produced by the
  old multi-activity stream-consumer fallback no longer appear —
  confirmed in the #54 branch walk.
- **#62:** Path B streaming gained a stale-stream liveness guard. A
  stream whose finalization repeatedly fails, or that exceeds a
  bounded age, is force-dropped so a fresh turn can start — preventing
  a stuck stream from silencing the chat.
- **#26:** `publish --copilot-chat` now supports
  `--manifest-id auto|<guid>` so operators can publish AI Teammate
  and Custom Engine Agent zips side-by-side without Teams App Catalog
  duplicate-id rejection. In both-surface mode the Copilot Chat zip
  auto-generates a fresh catalog id, keeps `bots[0].botId` on the
  bot identity, applies a `name.short` `CC` suffix within the 30-char
  cap, parses emitted zip paths that contain spaces, and omits
  AI-Teammate template sidecars from CEA packages. Copilot Chat
  post-apply guidance now points at Microsoft Admin Portal → Agents.
- **#38:** `activity-bridge` reply POSTs now treat non-2xx Bot
  Framework connector responses as failures instead of reporting
  success. The shared `send_reply()` path raises a typed error with
  HTTP status and a bounded response-body excerpt; serve mode returns
  `reply_failed`, and plugin `send()` / `send_image()` surface
  `SendResult(success=False, error=...)`.

## [0.7.1] — 2026-05-26

Slice 21a operator-visible polish + docs corrections — the first
patch after v0.7.0. `bot-service create --region` now defaults
from `az config get defaults.location` before falling back to
`westeurope`. App-id drift refusal prints paste-ready
delete/recreate recovery commands. Path B `doctor`/`status` probes
no longer cascade Azure resource/channel errors after `az account
show` fails. The §11 runbook surfaces `bot-service create --apply`
as the canonical Path B command with raw `az` demoted to detail.
SKILL.md no longer frames CLI 1.1.178 as the Microsoft#408 fix
floor — no build is currently live-verified clean (1.1.181 still
reproduced the secret-persistence regression in the R9 walk).

### Fixed

- **#47:** operator-visible slice 20 polish. `bot-service create`
  now defaults `--region` from `az config defaults.location` before
  falling back to `westeurope`, app-id drift refusal includes
  paste-ready delete/recreate recovery commands, Path B doctor/status
  diagnostics stop cascading Azure resource/channel errors after
  `az account show` fails, and the §11 runbook presents
  `bot-service create --apply` as the canonical provisioning command
  with raw `az` calls demoted to implementation/debug detail.
- **#51:** `SKILL.md` no longer frames A365 CLI 1.1.178 as the
  Microsoft#408 fix floor. The docs now match the conservative
  `doctor` probe and the 2026-05-15 R9 finding: no CLI build is
  live-verified clean yet, 1.1.181 still reproduced the
  secret-persistence regression, and operators should keep
  `--auto-recover-secret` enabled until a later build is walked clean.

## [0.7.0] — 2026-05-26

Slice 20 wrapper family graduates to general availability:
operators can now run `hermes-a365 bot-service enable-channel`,
`update-endpoint`, and `cleanup` (slice 20a `create` shipped in
v0.6.0) instead of raw `az bot` commands plus the acceptedTerms
ARM PATCH. `doctor` and `status` gained read-only Path B Bot
Service diagnostics. Three bug fixes surfaced during the v0.7.0
release walk against the satscryption tenant (see §11 of
`references/live-tenant-test.md` for the full walk record):
`bot-service verify --directline-probe` JSON-path drift (#49),
`bot-service cleanup` invalid `az bot delete --yes` flag (#50),
and `publish --apply` 180 s timeout truncating device-code auth
(#52).

### Fixed

- **#52:** `hermes-a365 publish --apply` no longer truncates the
  underlying `a365 publish` device-code auth flow at 180 s. The
  publish step is the only interactive call in the wrapper chain;
  when MSAL cannot silent-token (fresh shell, stale cache), `a365`
  falls back to device-code auth (browser open → sign-in → optional
  MFA → return), and Microsoft's device-code lifetime is 15 min =
  900 s. The previous 180 s override was *tighter* than the
  mutator's 900 s default and killed valid auth flows mid-handshake
  on every fresh-tenant walk. Surfaced by the v0.7.0 release walk
  Step 3d against the satscryption tenant. The timeout is now a
  named module constant `_PUBLISH_APPLY_TIMEOUT_SECONDS = 900.0`;
  regression tests pin the call site to the constant and assert the
  constant is generous enough (≥ 600 s) to survive accidental
  tightening.
- **#50:** `hermes-a365 bot-service cleanup --apply` no longer
  invokes `az bot delete --yes`. The `--yes` argument is rejected by
  `az bot delete` (which is non-interactive by default), so cleanup
  failed mid-flight against every live install after `az bot msteams
  delete` had already succeeded — leaving the operator with the
  msteams channel gone but the bot resource and sidecar still
  present and drifted. The previous regression test sliced calls to
  the first three argv elements so `--yes` was invisible; the new
  test asserts `--yes` does not appear in the full `az bot delete`
  arg list.
- **#49:** `hermes-a365 bot-service verify --directline-probe` now
  finds the Direct Line channel secret in real `az bot directline
  show --with-secrets` output. The probe walked
  `properties.sites[]`; live az returns the channel at
  `properties.properties.sites[]` (and a sibling
  `resource.properties.sites[]` copy), so the prior code raised
  `BotServiceError` against every real install. Surfaced by the
  v0.7.0 release walk against the satscryption tenant. Regression
  tests now feed a realistic az response shape through
  `_extract_directline_secret`.

### Added

- **Slice 20d (#32):** `hermes-a365 doctor` and
  `hermes-a365 status` now include read-only Path B Bot Service
  diagnostics. Doctor only fires these probes when
  `a365.bot-service.config.json` is present, so Path-A-only installs
  keep their existing exit-code behaviour. Status adds a `bot_service`
  row between `instance_scopes` and `activity_bridge`, skipped when
  the sidecar is absent and escalated to warn/error for MSA app-id
  drift, disabled Teams channel state, or incomplete BF-token runtime
  auth wiring.
- **Slice 20c (#31):** `hermes-a365 bot-service cleanup`
  deletes the Path B Azure Bot resource, backs up then removes
  `a365.bot-service.config.json`, preserves the Path A blueprint
  Entra app/service principal, and only purges the resource group
  when `--purge-resource-group` is set and the sidecar marks the
  group as wrapper-managed. Top-level `hermes-a365 cleanup` now
  has a `bot-service` kind and runs it before `azure → instance →
  blueprint` so Bot Service teardown happens before Path A identity
  teardown.
- **Slice 20b (#30):** `hermes-a365 bot-service enable-channel`
  and `bot-service update-endpoint`. `enable-channel --apply`
  idempotently enables the Microsoft Teams channel from the sidecar
  and reapplies the accepted-terms ARM PATCH when needed.
  `update-endpoint --apply` updates Azure Bot Service's Path B
  messaging endpoint, preserves sidecar channel state, and leaves
  Path A's independent `activity-bridge update-endpoint` flow alone.
  `bot-service verify` now warns when Path A's generated-config
  endpoint and Path B's Bot Service endpoint drift apart.

## [0.6.0] — 2026-05-18

Path B (Custom Engine Agent + Azure Bot Service) Copilot Chat
surfacing closes the headline value-prop gap vs. v0.5.x's Path A-only
shape. Agents now reach M365 Copilot Chat, Word/Excel/PowerPoint/Outlook
side-panels, and classic Teams via the same `/api/messages` route
that already handles AI Teammate traffic. First initial-walkthrough
operator wrapper (`bot-service create + verify`) ships alongside.

Live-validated end-to-end against the satscryption Azure GA tenant
on 2026-05-18 — M365 Copilot Chat round-trip + Teams round-trip +
WebChat API round-trip all green.

### Added

- **Path B inbound (#34)** — `validate_inbound_jwt_bf` in
  `activity_bridge.py` accepts classic Bot Framework S2S tokens
  (`iss=https://api.botframework.com`) alongside the slice 19f A365
  AAD-v2 path. Route handler at `plugin/adapter.py:419` peeks the
  unverified `iss` claim and dispatches to the right validator.
  BF JWKS via `https://login.botframework.com/v1/.well-known/openidconfiguration`,
  RS256, 5-min skew, audience matches the bot's `--appid`.
  `serviceUrl` claim match is validated when present but treated as
  optional after the 2026-05-15 walk showed real BF Connector→Bot
  tokens don't carry the claim despite Microsoft's docs (requirement
  7) saying they must.
- **Path B outbound (#33)** — `acquire_bf_s2s_token` mints classic
  BF `client_credentials` bearers against the bot's tenant token
  endpoint, scope `https://api.botframework.com/.default`. Cached
  per `(tenant_id, scope)` in a new `_BfTokenCache`. Dispatched via
  a new `acquire_reply_token` that routes Path A → user-FIC chain
  (existing) and Path B → BF S2S, raises on unknown. All five
  outbound surfaces (`send_reply`, `_send_proactive`,
  `_send_stream_start`, `_post_activity`, `edit_message`) funnel
  through the dispatcher. AADSTS82001 is detected specifically and
  re-raised as a `TokenAcquisitionError` whose message points
  operators at `A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET` for the
  non-agentic identity fix (#36).
- **Path B Entra identity threading (#36)** — `BridgeConfig` gains
  optional `bf_app_id` + `bf_client_secret` fields; `load_bridge_config`
  reads `A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET` from the per-agent
  `.env`. When set, both the inbound `expected_app_id` audience check
  and the outbound BF S2S mint use the separate non-agentic Path B
  identity. Empty defaults fall back to the blueprint app for
  backwards compat with Path A-only operators. Half-configured
  (only one of the two fields set) falls back defensively.
- **`hermes-a365 bot-service` verb (#29, slice 20a)** — new CLI
  surface with `create` and `verify` subcommands wraps every step
  of §11.3 + §11.4 + parts of §11.2.5:
  - auto-registers `Microsoft.BotService` resource provider on the
    sub (deterministic blocker on fresh subs)
  - ensures the resource group at a regional `--location <region>`
  - creates the Azure Bot resource with
    `--app-type SingleTenant --appid <A365_BF_APP_ID> --location global`
  - refuses unsafe in-place fix when an existing bot's `msaAppId`
    drifts from the configured Path B app id (Azure can't change
    `--appid` post-creation; forces deliberate delete+recreate)
  - updates the bot endpoint in-place via `az bot update --endpoint`
    when only the tunnel URL drifted
  - enables the Microsoft Teams channel
  - applies the load-bearing `acceptedTerms` ARM PATCH that
    `az bot msteams create` alone leaves un-set (silent
    traffic-drop without it)
  - writes `a365.bot-service.config.json` at mode 0600 as a
    gitignored sidecar
  - `verify --directline-probe` mints a real Direct Line conversation
    + posts an activity, watching for the Path B 403 / BotError
    failure shape. Collapses §11.10's multi-step Direct Line recipe
    into a single CLI flag.
- **Custom Engine Agent manifest scope expansion** — `publish
  --copilot-chat` now emits `scopes: ["copilot", "personal", "team"]`
  (was `["personal"]`) and includes an `isNotificationOnly: false`
  flag plus a `commandLists` entry. Required for the agent to
  actually surface in M365 Copilot Chat (the 2026-05-18 walk
  uncovered that `personal`-only `scopes` produced an `Oops!
  Something happened. Can you try again?` error in Copilot Chat).
- **`publish --bot-id <bf-app-id>` flag for Path B** — emits CEA
  zips whose `bots[]` block references the separate non-agentic
  Path B app id rather than the default-extracted blueprint app id.
- **`doctor a365_cli` probe (#35)** — version-floor check for the
  Microsoft#408 secret-persistence regression. CLI ≤ 1.1.181
  triggers a `WARN` with an upgrade hint; > 1.1.181 also `WARN`s
  (not yet live-verified clean); unparseable `WARN`s with a
  diagnostic message. The `OK` state is deliberately unreachable
  until a future CLI build is live-walked and confirmed clean —
  observed reality (CLI 1.1.181 still reproduces #408 against
  macOS, despite Microsoft's reported fix) takes precedence over
  the published release notes.

### Changed

- **`instance create --apply` propagates Path B env vars (#40)** —
  `A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET` set in the operator
  `~/.hermes/.env` now flow into the per-agent `.env` rendered by
  `instance create`. Existing user-managed env keys outside the
  renderer's managed set are preserved across re-runs (so e.g.
  `A365_ALLOW_ALL_USERS=true` set by hand for testing survives an
  `instance create --apply`).

### Documented

- **`references/live-tenant-test.md` §11** — Path B end-to-end runbook
  drafted from Microsoft docs (Phase 1, 2026-05-14) + walked live
  against the satscryption Azure GA sub (Phase 2, 2026-05-14 + 2026-05-18).
  New §11.2.5 covers the operator-side Entra app registration for
  Path B, `Bot.Connector` admin consent, env-var write, and the
  bot-resource migration recipe (because `az bot update` can't change
  `--appid`). §11.4 documents the load-bearing `acceptedTerms` ARM
  PATCH (no CLI flag exposes it; channel creation silently leaves
  it `false` and Microsoft drops traffic). §11.6 references
  `--bot-id <bf-app-id>` for Path B publish. §11.7 resolved the
  upload destination uncertainty to MAC → Agents → Upload custom
  agent. §11.10 logs every walking finding for future maintainers.

### Closed issues

- **#16** Slice 19u: M365 Copilot Chat surfacing — validated
  end-to-end against the satscryption tenant.
- **#28** Slice 20-pre Path B runbook — Phase 1 + Phase 2 walked.
- **#29** Slice 20a `bot-service create + verify + sidecar`.
- **#33** Slice 20e Path B outbound dispatcher + BF S2S mint.
- **#34** Slice 20 inbound Path B JWT validator branch.
- **#36** Slice 20e follow-up: non-agentic Entra app for Path B
  outbound (wrapper-side; operator walk closed it).
- **#40** `instance create` propagates Path B BF env vars.
- **#35** doctor probe for Microsoft#408 (still upstream-open as of
  2026-05-18 against CLI 1.1.181 — probe stays conservative).

### Operator notes

Operators on Path A can ignore most of this release — the dispatcher
falls back to the existing user-FIC chain by default. Path B Copilot
Chat surfacing requires the operator-side §11.2.5 walk (register a
separate non-agentic Entra app + grant `Bot.Connector` admin consent
+ set `A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET` in `~/.hermes/.env`).
Once the env vars are set, `hermes-a365 bot-service create --apply`
handles the Azure side end-to-end including the `acceptedTerms`
ARM PATCH that `az bot msteams create` alone leaves un-set.

The `--auto-recover-secret` flag on `register --apply` stays opt-in
and remains the recommended workaround for the Microsoft#408
regression on CLI 1.1.181 and earlier.

## [0.5.2] — 2026-05-13

Patch release: documentation accuracy pass for v0.5.0 + v0.5.1.
No code changes.

### Documented

- **`README.md`** — §Status rewritten around v0.5.1 (proactive
  long-running reply pattern shipped, #4 + #27 closed). §What
  works today matrix marks "Cron / proactive sends (Path A)" as
  ✅ shipped. §Known limitations dropped the "Proactive replies
  are not implemented" bullet; replaced with the Path B-only
  proactive deferred-error note. Repo-layout test count 720 →
  773. §Open work tree: "Ready to work" section retired (Path A
  active-development front is currently empty); #4 + #27 added
  to recent closures.
- **`SKILL.md`** — Surfaces-that-work-today gained a "Path A
  cron-driven proactive sends" bullet (slice 19x-a..e, v0.5.0 +
  v0.5.1) with the live-soak validation date. Path B proactive
  noted as gated on #16 alongside the surfacing test.
- **`references/m365-surface-coverage.md`** — Path A status row
  in the positioning table now lists the v0.5.0/v0.5.1 proactive
  soak. "Cron / proactive sends" coverage row flipped from 🟡
  pending to ✅ shipped (Path A) with the gating note for Path
  B. Backlog impact updates #4 + #27 as closed and #25 as
  closed. Validation status table gained a row for the v0.5.0
  proactive soak.
- **`references/live-tenant-test.md`** — Title bumped v0.4 →
  v0.5. §9d.5 acceptance gates reframed: cron-driven proactive
  uses `sendToConversation` (v0.5.1 gate fix) rather than being
  "out of scope". §9d.6 restart-durability runbook gained an
  explicit "send to a chat before any inbound this lifetime"
  proactive-path checkbox.
- **`references/webhook-contract.md`** — long-running responder
  note rewritten: streaming via `edit_message` (slices 19s +
  19s-bis, #3 closed) handles in-turn waits; proactive via
  `sendToConversation` (slices 19x-a..e, #4 + #27 closed)
  handles cron-driven outbound. No more "not yet implemented".

## [0.5.1] — 2026-05-13

Patch release: fix the v0.5.0 proactive-path production gate (closes
#27).

### Fixed

- **Slice 19x-e (#27):** `Agent365Adapter.send()`'s decision to use
  `replyToActivity` vs `sendToConversation` now keys on
  **whether this gateway lifetime has captured an inbound for
  `chat_id`**, not on whether the registry's `raw` field is
  populated. Surfaced during the v0.5.0 soak (2026-05-13): the
  registry persists `raw` to disk (slice 19o), so on every gateway
  restart `_cached_inbound_for` returned the persisted value and
  `send()` never fell through to `_send_proactive`. The proactive
  path I shipped was wire-correct (validated against the live
  satscryption tenant) but production-unreachable.

  Fix is a per-lifetime `set[str]` of chat_ids — populated by the
  `/api/messages` inbound capture point, consulted by `send()`'s
  gate, not persisted (every gateway boot starts empty). When the
  set has `chat_id`, `send()` mints a `replyToActivity` against
  the cached inbound's activity_id. When the set doesn't, `send()`
  routes through `_send_proactive` (sendToConversation — no stale
  `replyToId` risk).

  Behavioural changes: in-flight reply flow unchanged; cron-driven
  send after gateway restart now correctly uses `sendToConversation`
  (was using `replyToActivity` with a potentially stale
  `activity_id`). No new public API.

### Test count

769 → 773 (+4 new gate tests covering the four state-combinations
of seen/not-seen × registry-has-raw/not). Eight existing tests
that bypass `/api/messages` and call `adapter.send()` directly
updated to populate `_seen_inbounds_this_lifetime` after their
`upsert` — production flow already does this automatically.

## [0.5.0] — 2026-05-13

Feature release: proactive long-running reply pattern for Path A
(closes #4). Four slices: 19x-a (target-spec read), 19x-b (send
fall-through), 19x-c (registry pruning + pin/mark_used), 19x-d
(adapter lifecycle wiring).

Path A users with the registry hydrated can now send to chats the
gateway hasn't seen an inbound for this lifetime — cron-driven
flows, scheduled reminders, and proactive nudges all unblock from
this release. Path B proactive remains deferred behind #16
(Azure Bot Service registration); the adapter refuses Path B
target specs with a clear deferred-error referencing #16 rather
than 401-ing with the wrong token chain.

### Added

- **Slice 19x-a (#4):** `Agent365Adapter._build_proactive_target_spec(chat_id) → dict | None`
  — pure-function read over `ConversationRegistry`. Builds the
  minimal spec (`service_url`, `conversation_id`, `channel_id`,
  `chat_type`, `tenant_id`, `agentic_app_id`, `agentic_user_id`,
  `from`, `recipient`, `path`) needed to construct an outbound
  Activity + mint the outbound token chain. Path-tags entries as
  `"A"` only when the cached inbound's recipient carries both
  `agenticAppId` and `agenticUserId`; `"unknown"` otherwise.
- **Slice 19x-b (#4):** `Agent365Adapter.send()` falls through to
  `_send_proactive(chat_id, content)` when `_cached_inbound_for`
  returns `None`. Mints the agentic user-FIC chain against a
  synthetic activity-shape and POSTs to
  `<serviceUrl>/v3/conversations/<conv_id>/activities` (the
  `sendToConversation` BF endpoint — no `replyToId`, no
  `/activities/<id>` suffix). Path B target specs surface a clear
  "Path B proactive not yet implemented — gated on #16" error.
- **Slice 19x-c (#4):** `ConversationRegistry.prune_old_entries(max_age_days, *, active_session_keys, now) → int`
  mirrors `gateway/session.py:1031`'s
  `SessionStore.prune_old_entries` shape. Three skip conditions:
  active, pinned, no-stamp. Adds `last_used_at: float | None` and
  `pinned: bool` fields to `ConversationRef` with
  backward-compatible read of older payloads. New explicit
  mutators on `ConversationRegistry`: `pin(id)`, `unpin(id)`,
  `mark_used(id, *, now=None)`. `upsert(ref, *, now=None)`
  auto-stamps `last_used_at` and preserves `pinned` across
  merges.
- **Slice 19x-d (#4):** `Agent365Adapter.prune_conversations() → int`
  — reads `self._active_sessions.keys()` for the skip set, calls
  the registry's `prune_old_entries` with
  `extra.conversations_prune_max_age_days` (default 30 days),
  persists when anything drops, logs the count. One-shot;
  operators wire from cron via Hermes' `cronjob_tools`.
  `mark_used` calls added to every outbound path (`send`,
  `_send_proactive`, `send_typing`, `send_image`, `edit_message`)
  so conversations with outbound-only traffic resist prune
  correctly.

### Test count

720 → 769 (+49 across the four slices). Ruff clean throughout.

### Deferred (separately tracked)

- **Path B proactive send** (BF S2S outbound via Azure Bot
  Service) — gated on #16.
- **Built-in periodic prune loop** — explicitly out of scope
  per #4's "less moving machinery" framing. Operators run
  `await adapter.prune_conversations()` from cron at their
  preferred cadence.

## [0.4.1] — 2026-05-12

Patch release: documentation accuracy pass for v0.4.0 + CI workflow
modernisation. No code changes.

### Documented

- **README.md** refreshed for v0.4.0 across §Status, §Known
  limitations, §Repo layout, §Operator setup (wizard description
  + XDG-symlink drift item), and §Open work (restructured around
  the new `priority:next|ready|conditional|blocked` labels; #3,
  #13, #17, #22, #24, #25 moved to closures).
- **SKILL.md** — `hermes a365 publish` core procedure updated to
  document `--copilot-chat` + `--bot-id` flags (slice 19u-a),
  the dual-emit mode (`--aiteammate --copilot-chat`), the
  `botId` extraction fallback order (v0.4.0), and the Azure Bot
  Service prerequisite for live Copilot Chat surfacing.
- **references/live-tenant-test.md** — v0.2 → v0.4.0 label
  refresh; §6 publish gained a Path B / `--copilot-chat`
  cross-link; §9d.5 acceptance gates split streaming round-trip
  (slices 19s + 19s-bis, #3 closed) from proactive pattern
  (#4 still open), with an "incremental bubble growth" checkbox.

### Changed

- `.github/workflows/test.yml` and `publish.yml` bumped to
  Node.js 24-compatible action versions per a GitHub deprecation
  notice on the v0.4.0 publish run:
  - `actions/checkout` v4 → v6
  - `astral-sh/setup-uv` v5 → **v8.1.0** (pinned — upstream
    stopped maintaining a moving v8 tag)
  - `actions/setup-python` v5 → v6
  - `pypa/gh-action-pypi-publish@release/v1` unchanged (Docker
    action, unaffected by the Node runtime deprecation).

## [0.4.0] — 2026-05-12

Feature release: Custom Engine Agent publish path for M365 Copilot
Chat + M365 ecosystem positioning reframe + setup wizard hardening
pass. **Slices:** 19u-a (#24), 19r-bis (#25), 19r-a-bis (#22).

### Added

- **Slice 19u-a (#24):** `hermes a365 publish --copilot-chat` emits a
  **Custom Engine Agent** manifest zip for M365 Copilot Chat. The
  flag post-processes the GA CLI's AI Teammate zip into a
  `manifestVersion: "1.21"` shape with `bots` (referencing the
  blueprint Entra app id) and `copilotAgents.customEngineAgents`
  blocks; AI Teammate-specific `agenticUserTemplates` is stripped.
  Combine with `--aiteammate` to emit both zips side-by-side (the
  Copilot Chat zip lands at `<original>.copilot-chat.zip`); the
  `name.short` 30-char truncation (slice 19r-c) applies to both.
  `--bot-id` overrides botId extraction (default falls through
  `webApplicationInfo.id` → `bots[0].botId` → top-level `id` from
  the emitted manifest; the GA CLI 1.1.174+ AI Teammate emit only
  populates top-level `id`, surfaced during the 2026-05-12 live
  walkthrough). Unblocks the emitter for #16 (Copilot Chat live
  walkthrough).

- **Slice 19r-bis (#25):** Setup wizard now creates / repairs the
  XDG symlink at `~/.config/a365/a365.generated.config.json`
  pointing at the operator's chosen
  `A365_GENERATED_CONFIG_PATH`. The GA `a365` CLI hard-codes the
  XDG path and does not honour the env var; without the symlink,
  `a365 publish` fails with `agentBlueprintId missing`. The
  helper is idempotent: it creates when missing, repairs when
  pointing at a wrong target, no-ops when correct, and refuses
  to clobber a non-symlink file at the XDG path. `_detect_drift`
  surfaces `xdg_symlink_missing` and `xdg_symlink_wrong_target`
  with an auto-fixer attached. Surfaced during the 2026-05-12
  live walkthrough.

- **Slice 19r-a-bis (#22):** Setup wizard polish.
  - Slug prompt: when there are multiple per-agent dirs and no
    `AGENT_IDENTITY` env, use `prompt_choice` instead of a
    freeform prompt that could silently drop the slug on Enter.
    When there are no per-agent dirs, re-prompt on blank up to
    3 times before giving up — previously dropped slug silently.
  - `~/.hermes/config.yaml` write now skipped when the stanza
    hasn't changed (previously emitted ~270-line YAML
    normalisation diffs per wizard run from `hermes_cli.config.save_config`
    expanding implicit-default keys).

### Changed

- **Positioning reframe (commit `e33dd7f`, 2026-05-12):**
  Hermes-A365 now positions explicitly as the **M365 Copilot
  ecosystem path** for Hermes agents, distinct from Hermes'
  sibling classic-Bot-Framework Teams adapter
  (`plugins/platforms/teams/`, shipped Hermes v2026.4.30; PRs
  `NousResearch/hermes-agent#10037` and `#13767`). Two value
  props:
  - **Path A (AI Teammate / M365 agentic user):** agent appears
    in the M365 tenant directory + "Built for your org" picker
    + agentic-user audit trails. Teams 1:1 with M365-native
    identity. No Azure subscription required. Already validated
    end-to-end through round-8 (2026-05-11) with streaming.
  - **Path B (Custom Engine Agent + Azure Bot Service):**
    agent appears in M365 Copilot Chat's agents picker + Word /
    Excel / PowerPoint / Outlook Copilot side-panels. Requires
    Azure subscription for Bot Service registration. Emitter
    shipped in this release; live surfacing test deferred
    (#16).

  Reframe updates `README.md`, `SKILL.md`,
  `references/m365-surface-coverage.md`, and
  `references/live-tenant-test.md`. Upstream check-in
  `NousResearch/hermes-agent#20133` updated with the
  non-overlap clarification. `#17` (Teams group + channel
  walkthrough) closed as sibling-plugin lane. `#18` scope
  narrowed via comment to Path B-relevant invokes only.

### Documented

- **Custom Engine Agent surfacing prerequisite (slice 19u-a, live
  walkthrough finding 2026-05-12):** the Copilot Chat surface
  additionally requires an **Azure subscription** so the blueprint
  Entra app can be registered as an Azure Bot Service resource with
  the Microsoft Teams channel enabled. Without Bot Service
  registration the 1.21 manifest uploads to the Teams App Catalog
  successfully but Microsoft's routing layer doesn't forward
  Copilot Chat activities to our `/api/messages` endpoint — the
  agent stays AI-Teammate-shaped (instance creation → Teams
  notification only). The AI Teammate path bypasses this because
  M365's agentic user infrastructure routes Teams 1:1 traffic
  without Azure. #16 deferred pending Azure subscription.

- **Playbook scope callout** (commit `1772729`): clear Path A vs
  Path B framing at the top of `references/live-tenant-test.md`.
  Existing AZ operations untouched (all Path A; remain correct).
  Path B-specific Azure Bot Service registration steps deferred
  until #16 walks green.

## [0.3.0] — 2026-05-11

Feature release: Bot Framework streaming-response protocol. Closes
#3. Validated live in Microsoft Teams 1:1 chat against the
`satscryption.io` tenant over multiple long prompts.

### Added

- **Slice 19s:** `Agent365Adapter.edit_message` implements the BF
  streaming-response wire protocol — `typing` activity with
  `streaminfo` entity, monotonic `streamSequence` (omitted on
  final), `type=message` swap on the close, captured `streamId`
  from the first 201. `REQUIRES_EDIT_FINALIZE = True` so Hermes'
  stream consumer routes the final `endStream()` through. 1.5 s
  pacing (Microsoft's recommended), DM-only refusal, full error-code
  mapping (`ContentStreamNotAllowed`, sequence-order failures, rate
  limits).
- **Slice 19s-bis:** `send()` participates in the same BF stream
  as `edit_message` so each Hermes segment renders as a single
  growing bubble (no more separate send-bubble + stream-bubble
  per segment). Three live-test fixes:
  - Stream-aware `send()`: when `reply_to is not None` AND chat is
    personal AND no active stream, `send()` POSTs the streaming-start
    activity and captures the BF stream id as the message_id.
  - `_strip_streaming_cursor`: removes Hermes' cursor character
    (` ▉`) before POSTing — BF's "chunk N+1 must start with chunk N"
    rule was failing because of the trailing cursor.
  - `_auto_finalize_stale_stream` + recently-finalized no-op:
    handles two Hermes stream-consumer quirks (segment break without
    finalize, double-finalize after the legitimate close) that left
    stuck "thinking" bubbles.

### Tests

- 14 new tests in `TestEditMessage` (slice 19s) covering each wire-
  shape branch + every error code in the mapping.
- 8 new tests in `TestSendStreamStart` (slice 19s-bis) covering
  stream-start happy path, reply_to=None fallback, group/channel
  refusal, stream-start failure fallback, auto-finalize on stale
  stream, no-op for post-finalize calls.
- 673 total tests pass. ruff clean.

### Known scope

- **M365 Copilot Chat surface** (#16) is gated on **#24** (Custom
  Engine Agent publish path), not on streaming. Copilot Chat
  surfaces Custom Engine Agents (`manifestVersion: 1.21+`, `bots`
  + `copilotAgents` blocks, Teams App Catalog upload) which are a
  different manifest type from AI Teammates (current
  `--aiteammate` publish flow). The streaming work in this
  release applies to either surface; only the registration
  manifest needs to change.
- **Tool progress mid-stream** can theoretically conflict with
  the "one streaming sequence per user turn" rule. Auto-finalize
  closes the prior stream first; subsequent UX may show two
  consecutive bubbles per turn (tool progress → content stream).
  Acceptable for now; if a future operator wants tool progress
  suppressed on the agent365 platform, set
  `display.platforms.agent365.tool_progress: off` in config.yaml.

## [0.2.0] — 2026-05-11

First feature release after the v0.1 PyPI series. Closes #13 (setup
wizard) end-to-end. Operators on a fresh tenant can now go from
`pip install hermes-a365` to a running gateway-connected bot with no
hand-edits to `~/.hermes/config.yaml` or `~/.hermes/.env`, and the
emitted manifest zip is always Admin-Centre-upload-ready.

### Added — `hermes gateway setup --platform agent365` wizard

- **Slice 19r-a:** `interactive_setup()` in
  `hermes_a365.plugin.adapter`, wired via `setup_fn=` in
  `ctx.register_platform(...)`. Walks the operator through generated
  config path → tenant id → blueprint app id → slug → bridge port →
  client-secret bootstrap → allow-all toggle, then patches
  `~/.hermes/.env` and `~/.hermes/config.yaml`. Idempotent —
  re-running detects existing values and offers update-vs-keep.
  Available out of the box once the plugin is installed (Hermes
  v0.13.0+ required for the `register_cli_command` wiring).
- **Slice 19r-b:** `_detect_drift()` runs first when the wizard is
  invoked. Surfaces four scenarios from the round-8 walkthrough:
  - `app_id_stale` — operator `.env::A365_APP_ID` diverges from
    `agentBlueprintId` in the generated config.
  - `slug_orphan` — config.yaml stanza references a slug that
    doesn't exist under `~/.hermes/agents/`.
  - `a365_config_empty` — `~/a365.config.json` exists with empty
    `tenantId` / `clientAppId`; auto-fixer reseeds with the
    well-known `Agent 365 CLI` GUID + `az account show` tenant.
  - `generated_config_missing` / `generated_config_blank` —
    config.yaml's `generated_config_path` is unreachable or has an
    empty `agentBlueprintId`.

### Fixed — manifest emission

- **Slice 19r-c:** `hermes a365 publish --apply` now auto-truncates
  `manifest.json::name.short` to ≤30 chars before re-zipping.
  Strategy: drop trailing " Blueprint" if present; else truncate at
  the last word boundary that fits. GA CLI 1.1.174 emits 32-char
  `name.short` whenever the agent-name has the " Blueprint" suffix —
  Admin Centre rejected the upload at validation time, surfacing a
  generic "Upload failed" toast that round-8 spent two retries
  diagnosing. The wrapper now emits a `[applied] truncated
  name.short: 'X' (32) → 'Y' (22)` line when a patch was applied.

### Changed — documentation

- **Slice 19r-d:** `references/live-tenant-test.md` §9d.2 + §9d.3
  collapse to a single `hermes gateway setup --platform agent365`
  callout. README "Operator setup" section rewritten to lead with
  the wizard; manual-edit YAML preserved as a hand-edit fallback for
  CI / automation use cases.

### Tested

- 650 tests pass against both editable install and built wheel.
- Live-validated against `satscryption.io` (round-8 install): wizard
  fires from `hermes gateway setup`, detects 0 drift on a clean R8
  setup, correctly flags synthetic drift; publish auto-truncates the
  R8 manifest from 32 to 22 chars; Teams round-trip continues to
  work end-to-end.

### Upstream

- Filed NousResearch/hermes-agent#23802 — `hermes plugins
  enable/list` filters out entry-point-discovered plugins. The
  wizard works around this via the internal
  `hermes_cli.plugins_cmd._save_enabled_set` helper; the slice
  comment in `adapter.py` points at the upstream fix.

## [0.1.2] — 2026-05-11

Cosmetic patch surfaced by the first round-7 read-only walkthrough
against `satscryption.io` from a real PyPI install.

### Fixed

- `hermes-a365 license`'s "Next step" footer recommended `python
  scripts/doctor.py`; now correctly points at `hermes-a365 doctor`
  (`src/hermes_a365/license.py:170`).
- Module docstring "CLI use" examples across `activity_bridge.py`,
  `consent.py`, `hermes_responder.py`, `license.py`, and the plugin
  README/`adapter.py` doc-comments now reference `hermes-a365 <verb>`
  / `python -m hermes_a365.<x>` / `hermes_a365.activity_bridge` instead
  of the retired `python scripts/<x>.py` / `scripts/activity_bridge.py`
  paths. No behavioural change.

## [0.1.1] — 2026-05-11

Repackaging-only release: `hermes-a365` is now `pip install`-able from
PyPI. No behavioural changes; the apply paths, read paths, and Bot
Framework activity bridge are identical to `v0.1.0`.

### Changed

- **Distribution.** Source tree moved to a real `src/hermes_a365/`
  layout. `[tool.uv] package = false` is gone; the wheel is built via
  `hatchling` and published to PyPI. Two install paths now supported:
  - **Standalone CLI:** `pipx install 'hermes-a365[bridge]'` exposes a
    `hermes-a365 <verb>` console script for operators who drive the
    wrappers without spinning up a Hermes gateway.
  - **Gateway plugin:** `~/.hermes/hermes-agent/venv/bin/pip install
    'hermes-a365[bridge]'`. The Hermes plugin loader auto-discovers
    `agent365` via the `hermes_agent.plugins` entry point — no
    `~/.hermes/plugins/agent365/` directory, no symlink.
- **Imports.** Every module is now `hermes_a365.<x>`. The
  symlink-walking `Path(__file__).resolve().parent.parent.parent /
  "scripts"` trick in the plugin (`plugins/agent365/{adapter,cli}.py`)
  is retired; the plugin imports `from hermes_a365 import
  activity_bridge` directly.
- **Templates.** `templates/` is now packaged as `hermes_a365._data/
  templates/` and resolved via `importlib.resources` so lookups work
  for both editable installs and wheels.
- **Tests.** Bare imports (`from a365_config import …`) rewritten to
  `from hermes_a365.a365_config import …`. `tests/conftest.py` no
  longer pokes `scripts/` onto `sys.path`. 624 tests still passing.
- **Docs.** README, SKILL.md, and the `references/` runbooks updated
  to drop the symlink instructions and the `uv run python scripts/<x>.py`
  invocation style in favour of `pipx install` + `hermes-a365 <verb>`
  (or `python -m hermes_a365.<x>` for the modules that aren't surfaced
  as CLI subcommands).

## [0.1.0] — 2026-05-08

First operator-targeted release. Validated end-to-end against
Microsoft.Agents.A365.DevTools.Cli **1.1.171** (round-5 walkthrough,
2026-05-06) and the secret-null regression-recovery path on **1.1.174**
(round-6, 2026-05-07).

### Added — apply path (operator-side wrappers)

- `hermes a365 register` — orchestrates `a365 setup blueprint` +
  `setup permissions mcp` + `setup permissions bot` with AADSTS-aware
  retry, layer-1 client-secret regression detection, and opt-in
  `--auto-recover-secret` (handles Microsoft#408 on macOS / Linux).
- `hermes a365 consent` — render admin-consent URL, optionally launch a
  browser, poll `query-entra blueprint-scopes` until consent is granted.
- `hermes a365 instance create <slug>` — write the per-agent runtime
  `~/.hermes/agents/<slug>/.env` (no cloud step).
- `hermes a365 publish` — package the AI Teammate manifest zip for
  Microsoft 365 Admin Centre upload.
- `hermes a365 cleanup` — destructive teardown with `--purge-orphans`
  for blueprint-flow agentic users + agentRegistry instances. AI
  Teammate-flow store-managed instances always 403 on delete (Microsoft
  platform limitation, documented in `references/live-tenant-test.md`).
- `hermes a365 license` — recommends an A365 license tier given a user
  + agent count and plan.

### Added — read path

- `hermes a365 doctor` — read-only environment probe (CLI version, az
  signed-in, pwsh on PATH, network reachability, OS keychain backend).
- `hermes a365 status [<slug>]` — per-component status report against
  `query-entra` (local config, blueprint scopes, instance scopes, local
  bridge PID).

### Added — runtime

- **`agent365` Hermes platform adapter** (`plugins/agent365/`).
  Validated end-to-end against a Frontier-Preview tenant on Microsoft
  Teams 1:1 chat (rounds 3 → 5). Inbound activities go through AAD-v2
  JWT validation, BF idempotency dedupe, and `serviceUrl` host-suffix
  allowlist before reaching the agent loop.
- **`hermes a365 activity-bridge`** — Bot Framework adapter daemon.
  - `verify` — one-shot diagnostic (config + auth + reachability).
  - `serve` — long-running `/api/messages` webhook (FastAPI + uvicorn
    via the optional `bridge` extras).
  - `update-endpoint` — re-points the agent's messaging endpoint at a
    public tunnel URL.
- **Three-stage user-FIC token chain** for outbound replies (BF S2S →
  agent FMI delegation → user FIC), plus per-conversation
  durable registry that survives gateway restarts
  (`~/.hermes/agents/<slug>/conversations.json`).
- **`agents`-channel synthetic-event filter** — drops M365 onboarding
  probes + email-template render activities so they don't waste an
  agent turn (round-5 walkthrough finding).

### Added — CLI surface

- `hermes a365 <verb>` is wired via the supported Hermes plugin
  `register_cli_command` API (slice 19x-a, this release). Each verb
  delegates to the matching `scripts/<x>.py` module; running
  `python scripts/<x>.py` continues to work for development.

### Added — references / runbooks

- `references/live-tenant-test.md` — end-to-end runbook for a
  Frontier-Preview tenant; flags macOS 26 device-code prompt-volume
  failure mode (~10–12 prompts per `register --apply --m365`).
- `references/m365-surface-coverage.md` — per-surface coverage matrix.
- `references/exposing-the-bot-endpoint.md` — operator-side options
  (cloudflared, devtunnels, ngrok, reverse-proxy) — non-prescriptive.
- `references/a365-cli-reference.md`, `webhook-contract.md`,
  `activity-protocol-shapes.md`, `error-codes.md`,
  `entra-blueprint-properties.md`, `opentelemetry-config.md`,
  `license-cost-table.md`.

### Filed upstream

- **microsoft/Agent365-devTools#402** — cosmetic logging fixes when
  Observability-only S2S app-role assignment is the intended state.
  Microsoft confirmed intent + shipped fixes in CLI 1.1.174.
- **microsoft/Agent365-devTools#408** — `agentBlueprintClientSecret`
  null-on-disk regression on macOS (DPAPI unavailable). Layer 1
  detection + auto-recovery shipped in this release; Layer 2 is the
  upstream fix.

### Known limitations

- **M365 Copilot streaming** ([#3](https://github.com/satscryption/Hermes-A365/issues/3))
  not yet implemented — `Agent365Adapter.edit_message` is a no-op and
  `REQUIRES_EDIT_FINALIZE` is unset. Required for Copilot Chat surface.
- **Proactive replies for >10 s agent thinking**
  ([#4](https://github.com/satscryption/Hermes-A365/issues/4)) — `send()`
  still requires a cached inbound; cron-driven sends do not work yet.
- **`hermes gateway setup` wizard**
  ([#13](https://github.com/satscryption/Hermes-A365/issues/13)) not yet
  shipped — operators must hand-edit `~/.hermes/config.yaml` and
  `~/.hermes/.env` per the README quickstart.
- **Invoke activities** (Outlook compose-action, Teams compose
  extensions, search, signin) tracked under
  [#18](https://github.com/satscryption/Hermes-A365/issues/18); umbrella
  not yet implemented.
- **Plaintext on-disk secret on macOS / Linux.** DPAPI is Windows-only;
  the keychain shim in `scripts/keychain.py` writes the agent blueprint
  client secret to `a365.generated.config.json` with mode `0600`. See
  README "Security model".
- **AI Teammate-flow agentRegistry entries cannot be deleted** by
  operators (only "blocked" via the M365 Admin Centre). Microsoft
  platform limitation; not a wrapper bug.

[Unreleased]: https://github.com/satscryption/Hermes-A365/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/satscryption/Hermes-A365/releases/tag/v0.1.0
