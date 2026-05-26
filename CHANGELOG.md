# Changelog

All notable changes to the `hermes-a365` skill / plugin live here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
