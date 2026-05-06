# Hermes-A365

Integrate Hermes agents into the Microsoft 365 ecosystem using
**Microsoft Agent 365** (A365), the governance / identity / observability
control plane that GA'd 2026-05-01.

## Status

**v0.2 validated end-to-end against the satscryption.io tenant via the
Hermes plugin path.** Round-5 §9d walkthrough on 2026-05-06 drove a
real Teams DM through `Agent365Adapter.handle_message` into the
Hermes agent loop and a reply landed back in Teams via the agentic
user-FIC outbound chain. Restart-durability check passed —
`conversations.json` registry hydrated and the same chat picked up
without a fresh inbound. **Issue
[#1](https://github.com/satscryption/Hermes-A365/issues/1) closed.**

```
                     ┌─────────────────────────────────────────────────┐
                     │ All wrappers drive the real a365 CLI v1.1.171   │
                     │ Tests: 579 passing, ruff clean                  │
                     └─────────────────────────────────────────────────┘
```

| Layer | What's there |
|---|---|
| **Doctor / status** | Read-only env probes; status reports against `a365 query-entra`. |
| **License recommender** | Read-only; surfaces the actual `subscribedSkus` partNumbers. |
| **Setup orchestrator** (`register`) | Drives `setup blueprint` + `setup permissions {mcp, bot}` end-to-end with line-streamed output (device-code prompts visible in real time). |
| **Per-agent runtime config** (`instance create`) | Local-only `.env` writer; UUID generation deferred to apply-time; secret never on disk. |
| **Manifest publish** (`publish`) | Branches between AI-Teammate (zip) and blueprint-only (Graph API instance registration); operator messaging is honest about which artefact each flow produces. |
| **Cleanup** | Drives `cleanup azure → instance → blueprint`, pre-feeds `y\n` to defeat the GA CLI's prompts, leaves `chmod 600` on backup files. `--purge-orphans` clears agentic users + agentRegistry instances the GA CLI fails to delete; `--orphan-instance-id` plumbs in ids the AI-Teammate flow creates server-side. |
| **Activity bridge — standalone** | `verify` (config + auth + reachability + FMI exchange) and `serve` (long-running BF webhook adapter) both validated end-to-end on the satscryption tenant in round-3+ walkthroughs. AAD-v2 inbound JWT, idempotency dedupe, serviceUrl gate, agentic user-FIC outbound. |
| **Activity bridge — Hermes plugin** | `plugins/agent365/` ships the same runtime as a `BasePlatformAdapter` subclass, registered via the upstream Hermes plugin loader. Inbound dispatches via `handle_message(event)`, outbound via `send(chat_id, content)`. Durable session table (`~/.hermes/agents/<slug>/conversations.json`) for restart durability + proactive-send precondition. Round-5 §9d validated 2026-05-06. |
| **Live-tenant runbook** | [`references/live-tenant-test.md`](references/live-tenant-test.md). §9b verify, §9c bridge-standalone, §9d Hermes-plugin paths all documented + round-walked. |
| **M365 surface coverage** | [`references/m365-surface-coverage.md`](references/m365-surface-coverage.md) maps every M365 / Agent 365 / Copilot surface where the plugin could appear, with adapter coverage status. Validated: Teams 1:1. Architecturally covered: Teams group / channel / mobile, M365 Copilot Chat, Outlook chat, partner messaging channels. Out of scope: declarative agents, Office Add-ins, Loop components (different runtime layer). |

**Cosmetic CLI logging gap (no operator impact).** The GA `a365 setup
permissions bot` emits a `Configuring S2S app role assignments...`
header followed by a single S2S grant for `Agent365Observability`,
which initially looked like "two of three grants silently dropped".
Filed as [Agent365-devTools#402](https://github.com/microsoft/Agent365-devTools/issues/402);
Microsoft's 2026-05-05 reply confirms the Observability-only S2S
assignment is intended (Messaging Bot API and Power Platform API use
delegated OAuth2 grants only), and that the misleading header,
mid-run "non-admin user" line, and unconditional success log will be
fixed in the next CLI release. No wrapper-side work needed; runbook
entry #18 in [`references/live-tenant-test.md`](references/live-tenant-test.md)
captures the upstream resolution.

**Upstream contribution.** Proposal to add `hermes-a365` as an official
optional skill is open at
[NousResearch/hermes-agent#20133](https://github.com/NousResearch/hermes-agent/issues/20133).
Reframed during slice 19l after inspecting the bundled harness — the
SPEC §10 Q1 IPC-contract question turned out to be a non-question
(Hermes already documents the gateway-platform-plugin contract). The
upstream issue is now a "going plugin-path; sanity-check?" check-in
rather than an open design ask. Plugin layout under
[`plugins/agent365/`](plugins/agent365/) follows
`gateway/platforms/ADDING_A_PLATFORM.md` exactly.

## What is A365?

[**Microsoft Agent 365**](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/)
is a governance / identity / observability control plane for AI agents
that GA'd 2026-05-01. It is **not** an agent framework — it bolts on top
of whichever agent stack you use (Microsoft Agent Framework, Microsoft
365 Agents SDK, OpenAI Agents SDK, OpenClaw, Claude Code SDK, etc.) and
adds:

- Entra-backed agent identity (delegated permissions only)
- Tenant licensing (Agent 365 Tier 3 / Microsoft 365 E7)
- Agent blueprints, registered via `a365 setup blueprint`
- MCP-mediated access to Microsoft 365 data ("Work IQ" tools)
- Bot Framework Activity protocol for messaging + Adaptive Card invokes
- OpenTelemetry observability surfaced in admin centre
- Teams / Outlook / Microsoft 365 Copilot channel adapters

`hermes-a365` is the Hermes-side skill that drives these from inside
the Hermes harness.

## Repo split

This repo holds the **design artefacts** — spec, references, scripts,
templates, and the bridge daemon. The eventual upstream `SKILL.md` is
contributed into the Hermes Agent harness at
`hermes-agent/optional-skills/cloud-platforms/hermes-a365/SKILL.md`,
pulling these artefacts in at contribution time. See
[`SPEC.md` §3.1](SPEC.md) for the full rationale.

## Repo layout

```
.
├── SPEC.md                  # Authoritative design spec
├── SKILL.md                 # Validator-compliant upstream contribution
├── README.md                # This file
├── LICENSE                  # MIT
├── pyproject.toml           # Python 3.11+; uv-managed; optional [bridge] extras
├── docs/
│   └── submissions/         # Archived drafts of upstream issues we've filed
├── references/              # Dated snapshots + operator runbooks
│   ├── a365-cli-reference.md
│   ├── activity-protocol-shapes.md
│   ├── entra-blueprint-properties.md
│   ├── error-codes.md
│   ├── exposing-the-bot-endpoint.md # Tunnel/reverse-proxy options (non-prescriptive)
│   ├── license-cost-table.md
│   ├── live-tenant-test.md          # End-to-end runbook (operator-side)
│   ├── m365-surface-coverage.md     # Surface matrix per slice 19t
│   ├── opentelemetry-config.md
│   ├── README.md                    # Index
│   └── webhook-contract.md          # Bridge → responder JSON contract
├── scripts/                 # One module per subcommand + shared helpers
│   ├── _common.py               # parse_env, slugify, safe_run, jinja_env, deep_diff
│   ├── a365_config.py           # a365.config.json round-trip
│   ├── activity_bridge.py       # verify + serve + update-endpoint (standalone)
│   ├── cleanup.py
│   ├── consent.py
│   ├── doctor.py
│   ├── emit_card.py
│   ├── hermes_responder.py      # Reference responder (slice 19c)
│   ├── instance_create.py
│   ├── keychain.py              # OS-keychain wrapper (macOS + Linux)
│   ├── license.py
│   ├── mutator.py               # Line-streamed subprocess driver + AADSTS handling
│   ├── publish.py
│   ├── reconcile_app.py
│   ├── reconcile_blueprint.py
│   ├── register.py
│   ├── render_instance_env.py
│   └── status.py
├── plugins/agent365/        # Hermes gateway platform plugin (slice 19m–19q)
│   ├── plugin.yaml              # Manifest (loader globs lowercase)
│   ├── __init__.py
│   ├── adapter.py               # Agent365Adapter(BasePlatformAdapter)
│   ├── conversations.py         # ConversationRef + ConversationRegistry
│   └── README.md
├── templates/
│   ├── blueprint.json.j2
│   ├── consent-url.txt.j2
│   ├── instance.env.j2
│   └── adaptive-cards/          # greeting / confirmation / error
└── tests/                   # 579 tests (pytest + ruff clean)
    ├── conftest.py
    ├── golden/
    └── test_*.py
```

## Quick start

The canonical end-to-end walkthrough is
[`references/live-tenant-test.md`](references/live-tenant-test.md). At a
glance, against a Frontier-Preview-enrolled M365 tenant where you hold
Global Admin and a `MICROSOFT_AGENT_365_TIER_3` license:

```bash
# 0. Install
git clone https://github.com/satscryption/Hermes-A365.git
cd Hermes-A365
uv sync --extra bridge      # `bridge` is optional; only needed for serve mode

# 1. Pre-deploy diagnostic
uv run python scripts/doctor.py --human                    # exit 0/1/2

# 2. Decide a license model (read-only, never purchases)
uv run python scripts/license.py --users 12 --agents 3 --plan E5

# 3. Register the blueprint + MCP/Bot permissions
uv run python scripts/register.py --agent-name "Inbox Helper" --apply

# 4. (Verify admin consent — granted automatically by setup blueprint)
uv run python scripts/consent.py "Inbox Helper" --no-open

# 5. Per-agent runtime config
uv run python scripts/instance_create.py inbox-helper \
    --owner sadiq@contoso.com --owner-aad-id <oid> --apply

# 6. Register the agent instance via Graph (no zip for blueprint-only)
uv run python scripts/publish.py --agent-name "Inbox Helper" --apply

# 7. Re-point the messaging endpoint at whatever public HTTPS URL
#    fronts your local port 3978. The skill is tunnel-agnostic —
#    Cloudflare quick-tunnel example shown for expedience; see
#    references/exposing-the-bot-endpoint.md for named tunnels,
#    Microsoft devtunnels, ngrok, Azure App Service, custom
#    reverse-proxy alternatives.
uv run python scripts/activity_bridge.py update-endpoint \
    --agent-name "Inbox Helper" \
    --url https://<your-public-host>/api/messages --apply

# 8a. Bridge-standalone path (debug / no Hermes harness involved)
HERMES_BRIDGE_WEBHOOK=https://my-responder/respond \
    uv run python scripts/activity_bridge.py serve --slug inbox-helper

# 8b. Hermes plugin path (production: the agent loop runs)
ln -sfn "$PWD/plugins/agent365" ~/.hermes/plugins/agent365
# add gateway.platforms.agent365 + plugins.enabled to ~/.hermes/config.yaml
# export A365_TENANT_ID, A365_APP_ID, A365_BLUEPRINT_CLIENT_SECRET, AA_INSTANCE_ID
hermes gateway run

# 9. Status sanity (any time)
uv run python scripts/status.py inbox-helper --human

# 10. Tear down
uv run python scripts/cleanup.py --agent-name "Inbox Helper" \
    --slug inbox-helper --apply --confirm "Inbox Helper"
```

## Subcommand reference

```bash
# === Read-only diagnostics ===
uv run python scripts/doctor.py [--human|--no-network]
uv run python scripts/license.py --users <n> --agents <n> --plan E3|E5|E7 [--bundled-security]
uv run python scripts/status.py [<slug>] [--human]
uv run python scripts/activity_bridge.py verify --slug <slug> [--human]

# === Apply-path orchestrators ===
uv run python scripts/register.py --agent-name "<display>" [--m365] [--aiteammate] [--no-endpoint] [--apply]
uv run python scripts/instance_create.py <slug> --owner <email> --owner-aad-id <oid> [--apply]
uv run python scripts/publish.py --agent-name "<display>" [--aiteammate] [--apply]
uv run python scripts/cleanup.py --agent-name "<display>" [--slug <slug>] [--kinds=...] --apply --confirm "<display>"

# === Activity bridge (slice 19) ===
uv run python scripts/activity_bridge.py verify --slug <slug>
uv run python scripts/activity_bridge.py serve --slug <slug> --port 3978
uv run python scripts/activity_bridge.py update-endpoint --agent-name "<display>" --url <https://...> [--apply]

# === Templates / utilities ===
uv run python scripts/consent.py "<agent-name>" [--no-open] [--timeout 60]
uv run python scripts/emit_card.py greeting --command "..." [--command "..."]
uv run python scripts/keychain.py {store|get|delete} --tenant <t> --app-id <id>
```

> **macOS note for `keychain.py`.** First write to the login keychain
> pops a UI dialog. Click "Always Allow" to avoid further prompts. CI
> / headless contexts may fail with `rc=36 User interaction is not
> allowed` — `security unlock-keychain` first.

## Open work

External issues filed:

- **[Microsoft#402](https://github.com/microsoft/Agent365-devTools/issues/402)** —
  `setup permissions bot` cosmetic logging gap. Filed 2026-05-05;
  Microsoft replied same day confirming Observability-only S2S
  assignment is intended (the other two resources use delegated
  OAuth2 only) — three message/log fixes queued for the next CLI
  release. Resolution captured in
  [`references/live-tenant-test.md`](references/live-tenant-test.md)
  (bug #18).
- **[Hermes#20133](https://github.com/NousResearch/hermes-agent/issues/20133)** —
  upstream proposal to add `hermes-a365` as an official optional
  skill. Filed 2026-05-05. Reframed in slice 19l after the SPEC §10
  Q1 contract turned out to already exist in the harness; awaiting
  NousResearch guidance on naming + placement.

Open issues in this repo (run `gh issue list` for current state):

**Active build tracks:**

- **[#3](../../issues/3)** — Activity bridge streaming responses.
  **Hard prerequisite for #16** (M365 Copilot Chat surface validation
  per slice 19u) — Copilot Chat enforces a ~15s non-streaming reply
  timeout.
- **[#4](../../issues/4)** — Proactive long-running reply pattern.
  Surface-agnostic. Slice 19o registry (`ConversationRef` +
  `conversations.json`) is the **already-shipped** prerequisite;
  what's missing is the Hermes-side trigger.

**Surface-validation walkthroughs:**

- **[#16](../../issues/16)** — Slice 19u: validate M365 Copilot Chat
  surface (gates on #3).
- **[#17](../../issues/17)** — Slice 19v: validate Teams group +
  channel surfaces (architecturally covered, just needs a live walk).

**Adapter quality / operator UX:**

- **[#13](../../issues/13)** — Slice 19r: `interactive_setup()` for
  `hermes gateway setup` wizard. Surface-agnostic.
- **[#14](../../issues/14)** — Slice 19s: surface + auto-recover the
  GA CLI's client-secret persistence regression (hit on three
  consecutive walkthroughs).
- **[#18](../../issues/18)** — Slice 19w: handle invoke activities
  (`task/{fetch,submit}` for Outlook compose-action,
  `composeExtension/*` for Teams compose, `signin/verifyState` for
  OAuth tools). Umbrella with per-name children. Supersedes the
  older #5.

**Recent closures (round-5 deliverables):**

- ~~#1~~ — Hermes gateway platform plugin. **Closed 2026-05-06** after
  §9d round-5 walkthrough validated the plugin path end-to-end.
  Slices 19m / 19n / 19o / 19o-followup / 19p delivered.
- ~~#5~~ — Invoke action types. **Closed 2026-05-06** as superseded
  by #18 (per-name split).
- ~~#6~~ — Outbound auth refactor. **Closed 2026-05-05** by slice 19e
  (agentic three-stage user-FIC chain).
- ~~#7~~ — AAD-v2 inbound JWT validator. **Closed 2026-05-05** by
  slice 19f.
- ~~#8 / #9 / #10 / #11~~ — orphan agentic-user purge / orphan
  agentRegistry surface / inbound idempotency / serviceUrl host
  allowlist. **Closed 2026-05-05** by slices 19g / 19h / 19i / 19j.
- ~~#12~~ — Filter agents-channel synthetic events. **Closed
  2026-05-06** by slice 19q + follow-up.
- ~~#15~~ — M365 surface coverage audit. **Closed 2026-05-06** by
  slice 19t (`references/m365-surface-coverage.md` + 3 child issues).

## Status meta

Slice timeline at week-grain (per-slice detail is in the commit log):

- **2026-05-04** — v0.2 foundation: slices 18a–18g land
  (`mutator.py` + `a365_config.py` + apply-path rebuild for
  `register` / `instance_create` / `cleanup` / `publish` + read-path
  rework against `query-entra`). Live-tenant runbook 18h.
- **2026-05-05** — round-2 walkthrough surfaces 18 wrapper bugs;
  slices 18i–18x fix all 17 in-code/docs. Bug #18 filed upstream as
  [Microsoft#402](https://github.com/microsoft/Agent365-devTools/issues/402)
  and resolved same-day as cosmetic-logging-only.
- **2026-05-05** — slices 19a–19c: bridge `verify` + `serve` +
  reference responder; round-3 walkthrough exposes the
  AADSTS82001 outbound-auth defect on agentic apps. Slice 19e
  refactors outbound to the canonical
  [agentic three-stage user-FIC chain](https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow).
- **2026-05-05** — round-3 + round-4 walkthroughs end-to-end:
  inbound AAD-v2 JWT (19f, [#7](https://github.com/satscryption/Hermes-A365/issues/7)),
  orphan agentic-user purge (19g, [#8](https://github.com/satscryption/Hermes-A365/issues/8)),
  inbound idempotency (19i, [#10](https://github.com/satscryption/Hermes-A365/issues/10)),
  orphan agentRegistry surface + `--orphan-instance-id` flag
  (19h + round-4, [#9](https://github.com/satscryption/Hermes-A365/issues/9)),
  serviceUrl host allowlist (19j,
  [#11](https://github.com/satscryption/Hermes-A365/issues/11)).
  Microsoft#402 framing realignment (19k); SPEC §10 Q1 resolution
  via the upstream Hermes plugin contract (19l). Bridge-standalone
  Teams round-trip with JWT-on validated end-to-end.
- **2026-05-06** — Hermes plugin path: slice 19m skeleton, 19n
  bridge-runtime port, 19o durable session table + `send_typing` /
  `send_image`, 19o follow-up (lowercase `plugin.yaml` + 1-arg
  `is_connected`). §9d runbook drafted with explicit prerequisites
  checklist.
- **2026-05-06** — round-5 §9d walkthrough validates the **Hermes
  plugin path end-to-end**: agent loads through `hermes gateway
  run`, Teams DM dispatches via `handle_message`, agent reasons,
  reply lands; gateway-restart durability check passes.
  [#1](https://github.com/satscryption/Hermes-A365/issues/1) closed.
- **2026-05-06** — slice 19q filters `agents`-channel synthetic
  events from the agent loop (eliminates onboarding-typing 404
  spam). Slice 19t M365 surface coverage audit produces
  [`references/m365-surface-coverage.md`](references/m365-surface-coverage.md)
  + child issues [#16](https://github.com/satscryption/Hermes-A365/issues/16)
  / [#17](https://github.com/satscryption/Hermes-A365/issues/17) /
  [#18](https://github.com/satscryption/Hermes-A365/issues/18).

## License

MIT — see [`LICENSE`](LICENSE).
