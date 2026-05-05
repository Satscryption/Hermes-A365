# Hermes-A365

Integrate Hermes agents into the Microsoft 365 ecosystem using
**Microsoft Agent 365** (A365), the governance / identity / observability
control plane that GA'd 2026-05-01.

## Status

**v0.2 functionally complete pending live-tenant validation of the
agentic auth refactor.** Setup, status, license, cleanup, `bridge
verify` paths all work end-to-end against a live tenant. `bridge
serve` outbound auth was rewritten in slice 19e to use the canonical
A365 three-stage `user_fic` chain (issue
[#6](https://github.com/satscryption/Hermes-A365/issues/6) closed in
code; live-tenant validation pending round-4 walkthrough).

```
                     ┌─────────────────────────────────────────────────┐
                     │ All wrappers drive the real a365 CLI v1.1.171   │
                     │ Tests: 444 passing, ruff clean                  │
                     └─────────────────────────────────────────────────┘
```

| Layer | What's there |
|---|---|
| **Doctor / status** | Read-only env probes; status reports against `a365 query-entra`. |
| **License recommender** | Read-only; surfaces the actual `subscribedSkus` partNumbers. |
| **Setup orchestrator** (`register`) | Drives `setup blueprint` + `setup permissions {mcp, bot}` end-to-end with line-streamed output (device-code prompts visible in real time). |
| **Per-agent runtime config** (`instance create`) | Local-only `.env` writer; UUID generation deferred to apply-time; secret never on disk. |
| **Manifest publish** (`publish`) | Branches between AI-Teammate (zip) and blueprint-only (Graph API instance registration); operator messaging is honest about which artefact each flow produces. |
| **Cleanup** | Drives `cleanup azure → instance → blueprint`, pre-feeds `y\n` to defeat the GA CLI's prompts, leaves `chmod 600` on backup files. |
| **Activity bridge** | `verify` (config + auth + reachability + FMI exchange) ships and works. `serve` (long-running BF webhook adapter) ships with the correct A365 three-stage `user_fic` outbound auth (slice 19e). Live-tenant validation pending round-4 walkthrough. |
| **Live-tenant runbook** | [`references/live-tenant-test.md`](references/live-tenant-test.md). Walked round-2 successfully; round-3 (with the bridge) pending operator action. |

**Known external blocker.** The GA `a365 setup permissions bot` only
ever assigns the `Agent365Observability` S2S app role to the blueprint
SP, despite claiming "permissions configured successfully" in its
output. `Messaging Bot API` and `Power Platform API` S2S grants
silently skip. Reproducible. Filed with Microsoft as
[Agent365-devTools#402](https://github.com/microsoft/Agent365-devTools/issues/402);
operator-side reproduction lives in bug #18 of
[`references/live-tenant-test.md`](references/live-tenant-test.md).

**Upstream contribution.** Proposal to add `hermes-a365` as an official
optional skill is open at
[NousResearch/hermes-agent#20133](https://github.com/NousResearch/hermes-agent/issues/20133).
Awaiting upstream guidance on placement (light-touch link-stub vs full
vendoring under `optional-skills/cloud-platforms/`).

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
│   ├── license-cost-table.md
│   ├── live-tenant-test.md      # End-to-end runbook (operator-side)
│   ├── opentelemetry-config.md
│   ├── README.md                # Index
│   └── webhook-contract.md      # Bridge → responder JSON contract
├── scripts/                 # One module per subcommand + shared helpers
│   ├── _common.py               # parse_env, slugify, safe_run, jinja_env, deep_diff
│   ├── a365_config.py           # a365.config.json round-trip
│   ├── activity_bridge.py       # verify + serve + update-endpoint
│   ├── cleanup.py
│   ├── consent.py
│   ├── doctor.py
│   ├── emit_card.py
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
├── templates/
│   ├── blueprint.json.j2
│   ├── consent-url.txt.j2
│   ├── instance.env.j2
│   └── adaptive-cards/          # greeting / confirmation / error
└── tests/                   # 444 tests (pytest + ruff clean)
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

# 7. Re-point the messaging endpoint at your tunnel + run the bridge
uv run python scripts/activity_bridge.py update-endpoint \
    --agent-name "Inbox Helper" \
    --url https://<tunnel>.trycloudflare.com/api/messages --apply
HERMES_BRIDGE_WEBHOOK=https://my-responder/respond \
    uv run python scripts/activity_bridge.py serve --slug inbox-helper

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
  `setup permissions bot` silently drops two of three S2S app-role
  assignments. Filed 2026-05-05; awaiting Microsoft triage.
  Operator-side reproduction in
  [`references/live-tenant-test.md`](references/live-tenant-test.md)
  (bug #18).
- **[Hermes#20133](https://github.com/NousResearch/hermes-agent/issues/20133)** —
  upstream proposal to add `hermes-a365` as an official optional
  skill. Filed 2026-05-05; awaiting NousResearch guidance.

Open issues in this repo (run `gh issue list` for current state):

- **[#1](../../issues/1)** — Tier 3 responder (Hermes-native). Blocked
  on Hermes#20133 + SPEC §10 Q1.
- **[#2](../../issues/2)** — Tier 2 responder (LLM-backed,
  no Hermes coupling).
- **[#3](../../issues/3)** — Activity bridge streaming responses.
  Required for M365 Copilot substantive replies.
- **[#4](../../issues/4)** — Proactive long-running reply pattern (>10s
  agent thinking via captured `ConversationReference`).
- **[#5](../../issues/5)** — Invoke action types beyond Adaptive Card
  (`signin/verifyState`, `task/{fetch,submit}`, `composeExtension/*`).

Operator action (not a code issue):

- **Round-3 walkthrough.** Live-tenant validation of `serve` mode +
  reference responder (Teams round-trip, `bridge.pid` lifecycle, JWT
  validation against real BF JWKS). Runbook step 9c in
  [`references/live-tenant-test.md`](references/live-tenant-test.md).

## Status meta

Slice timeline since v0.2 work began:

- **2026-05-04** — slices 18a–18g landed: foundation reset
  (`mutator.py` thin run-argv wrapper + `a365_config.py`), apply-path
  rebuild (`register`, `instance_create`, `cleanup`, `publish`),
  read-path rework (`doctor` + `status` against the GA `query-entra`
  surface), `SKILL.md` 0.2.0. v0.1 subcommands that targeted
  non-existent CLI verbs (`deploy`, `workiq`, `telemetry`,
  `fic_rotate`, `blueprint_create`) deleted.
- **2026-05-04** — slice 18h: live-tenant runbook
  ([`references/live-tenant-test.md`](references/live-tenant-test.md)).
- **2026-05-05** — round-2 walkthrough surfaced 18 wrapper bugs and CLI
  realities. Slices 18i–18u fixed all 17 in code/docs; #18 is a
  Microsoft CLI defect.
- **2026-05-05** — slices 18v–18x: scope-classifier hint corrections
  against the verified GA output (`Inheritable Scopes:` /
  `Successfully retrieved`), `Mutator` `stdin_input` kwarg so cleanup
  can answer the CLI's prompt that `-y` doesn't actually suppress, and
  file-permission hardening (0600 on per-agent .env + `chmod 600` on
  the cleanup-emitted `*.backup-*.json` files that hold the secret).
- **2026-05-05** — slices 19a–19b: activity bridge `verify` mode
  (config + auth + reachability diagnostic), then `serve` mode (BF
  webhook adapter — JWT validation, webhook forwarding, Adaptive Card
  reply rendering, `serviceUrl` POST-back) plus the
  [`references/webhook-contract.md`](references/webhook-contract.md)
  contract for operator-defined responders. Validation against
  Microsoft Learn before coding: 8 GO, 2 CAUTION, 0 NO-GO on the
  10 protocol assumptions.
- **2026-05-05** — two upstream submissions filed:
  [Microsoft#402](https://github.com/microsoft/Agent365-devTools/issues/402)
  for the bot-permissions S2S defect, and
  [Hermes#20133](https://github.com/NousResearch/hermes-agent/issues/20133)
  proposing `hermes-a365` as an official optional skill. Drafts
  archived under [`docs/submissions/`](docs/submissions/).
- **2026-05-05** — round-3 walkthrough: provisioned a fresh blueprint,
  ran update-endpoint to register a `cloudflared` tunnel as the
  messaging endpoint, and discovered Microsoft's `AADSTS82001` policy
  blocks the bridge's `client_credentials` outbound auth for A365
  blueprint apps on messaging resources. The bridge's wire is correct
  but the auth model was wrong. Filed as
  [#6](https://github.com/satscryption/Hermes-A365/issues/6) (slice
  19d findings; CLI quirk that `a365 publish` clobbers
  `agentBlueprintClientSecret` from the local generated config also
  documented). Tenant cleaned up post-walkthrough.
- **2026-05-05** — slice 19e: refactored bridge outbound auth from
  `client_credentials` (broken for agentic apps) to the canonical
  three-stage `user_fic` chain documented at
  https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow.
  T1 = blueprint impersonates agent identity via FMI; T2 = agent
  identity asserts itself; final = user-context token at the
  Messaging Bot API resource. Two-tier cache (T1/T2 shared across
  users; final per-user). `bridge verify` gains an FMI exchange
  probe; messaging-resource `client_credentials` probe dropped.
  60 bridge tests passing.

Older v0.1 slice history (1–17) lived in this README until 2026-05-05;
it was a slice-by-slice trail that grew unwieldy. The canonical
per-slice detail now lives in the commit log; salient findings are
captured in the relevant `references/*.md` snapshots.

## License

MIT — see [`LICENSE`](LICENSE).
