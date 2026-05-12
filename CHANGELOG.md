# Changelog

All notable changes to the `hermes-a365` skill / plugin live here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
