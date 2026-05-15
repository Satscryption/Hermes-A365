---
name: hermes-a365
description: Use when integrating a Hermes agent into the Microsoft 365 ecosystem — agent-as-tenant-directory-identity (AI Teammate / agentic user) or Copilot Chat surfacing (Custom Engine Agent + Azure Bot Service). Distinct from Hermes' sibling Teams adapter (`plugins/platforms/teams/`) which covers classic Bot-Framework Teams chat. Wraps the GA `Microsoft.Agents.A365.DevTools.Cli` verbs, ships the BF activity bridge that backs the `agent365` gateway platform, and emits both AI Teammate and Custom Engine Agent manifests.
version: 0.5.2
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags:
      - microsoft
      - agent-365
      - a365
      - entra
      - bot-framework
      - mcp
      - cloud-platforms
      - copilot-chat
    related_skills:
      - hermes-agent-skill-authoring
---

# Hermes A365

## Overview

Hermes-A365 is the **M365 Copilot ecosystem path** for Hermes agents.
It covers the surfaces a classic Bot Framework Teams bot structurally
cannot reach:

- **Path A — AI Teammate (M365 agentic user):** the Hermes agent
  appears as a first-class agentic identity in your M365 tenant
  directory, in the "Built for your org" picker, in M365 People
  search, and in agentic-user audit trails. Teams 1:1 chat with
  M365-native identity. No Azure subscription required.
- **Path B — Custom Engine Agent (Azure Bot Service + 1.21 manifest):**
  the Hermes agent appears in M365 Copilot Chat's agents picker, in
  Copilot side-panels inside Word / Excel / PowerPoint / Outlook,
  and reaches the Copilot fabric's invoke surfaces (Microsoft Search,
  Outlook compose-action). Requires an Azure subscription so the
  blueprint Entra app can be registered as an Azure Bot Service
  resource.

Both paths share the same blueprint Entra app + service principal +
bot endpoint, and operators with both prerequisites can run both
surfaces from one Hermes-A365 install.

**Distinct from Hermes' sibling Teams adapter** at
`plugins/platforms/teams/adapter.py` (shipped v2026.4.30 + in-flight
work in `hermes-agent#10037` and `#13767`). That adapter is the right
tool for generic Teams chat bots — DM, channels, group chats,
threading, file attachments — using classic Bot Framework with an
Azure App Registration + client secret / certificate / Managed
Identity. It gives no M365 directory identity and no Copilot Chat
surfacing.

Microsoft Agent 365 (A365, GA 2026-05-01) is the governance / identity
/ observability control plane Hermes-A365 plugs into: Entra-backed
agentic identity, tenant licensing, agent blueprints, MCP-mediated
Microsoft 365 data access ("Work IQ"), OpenTelemetry, and the
channel adapters for Teams / Outlook / M365 Copilot.

The wrapper is built directly on the GA `Microsoft.Agents.A365.DevTools.Cli`
verbs documented in [`references/a365-cli-reference.md`](references/a365-cli-reference.md).
The skill composes them into idempotent plan/apply flows, fills the
gaps the CLI doesn't cover (license decision, admin consent grant,
runtime `.env` generation, Custom Engine Agent manifest transform),
and ships a Bot Framework activity bridge + Hermes gateway platform
plugin (`agent365`) that round-trips messages between A365 and the
Hermes agent loop.

## When to Use

Use when the user wants any of:

- Hermes registered as a first-class M365 **agentic identity** —
  appears in the tenant directory, agentic-user audit, "Built for
  your org" picker. Path A.
- Hermes available in **M365 Copilot Chat** (agents picker /
  `@`-mention / side-panels in Word / Excel / PowerPoint / Outlook).
  Path B. Requires Azure subscription.
- A Bot Framework activity bridge backed by **A365 governance**
  (Entra-backed identity, MCP-mediated Microsoft 365 data, OTLP
  audit trails).
- Migrating an OpenClaw-on-A365 deployment to Hermes (the
  blueprint stays; only the runtime endpoint changes).

Don't use when:

- The goal is a **generic Teams chat bot** with no M365 directory
  identity or Copilot surfacing — use Hermes' sibling Teams adapter
  (`plugins/platforms/teams/`, shipped v2026.4.30). It handles
  DM / channels / group / threading / file attachments via classic
  Bot Framework without A365 prerequisites.
- Generic Microsoft Graph access is the goal — use a Graph-only skill.
- Deploying a Bot Framework bot **outside** any M365 / A365
  governance surface — pick a classic BF skill.
- Setting up OpenAI Agents SDK or another framework end-to-end — A365
  governs the runtime; pick the appropriate framework skill for the
  agent itself.

## Prerequisites

- A Microsoft 365 tenant where the operator has **Global Administrator**
  or **Agent Administrator** role and is enrolled in Microsoft's
  Frontier Preview Program.
- The A365 CLI on PATH: `Microsoft.Agents.A365.DevTools.Cli` (.NET tool,
  ships as `a365`), ≥ 1.1.178 recommended. Only the .NET tool ships at
  GA — the npm `atk` variant referenced in pre-GA documentation never
  landed. Versions < 1.1.178 are allowed but doctor warns because they
  predate the Microsoft#408 secret-persistence fix; use
  `--auto-recover-secret` on those builds.
- `az` CLI ≥ 2.55.0, signed into the target tenant. Many `a365`
  subcommands shell out to `az` for Entra reads.
- **PowerShell 7+ (`pwsh`) on PATH.** The CLI invokes `pwsh` for some
  setup steps; missing `pwsh` causes `a365 setup requirements` to fail.
- A custom Entra client app (Microsoft's convention: display name
  `Agent 365 CLI`) registered in the tenant. The CLI uses it as the
  device-code/auth-code client. Doctor verifies discoverability via the
  signed-in `az` context.
- An OS keychain: macOS Security or Linux libsecret (`secret-tool`).
  Windows is not yet supported.
- A tenant license: either the **Agent 365 add-on** ($15/user/month) or
  **Microsoft 365 E7** ($99/user/month). The skill never purchases — it
  recommends; see [`references/license-cost-table.md`](references/license-cost-table.md).
- A Hermes harness with `hermes-a365` installed into its venv
  (`~/.hermes/hermes-agent/venv/bin/pip install 'hermes-a365[bridge]'`)
  so the plugin loader auto-discovers `agent365` via the
  `hermes_agent.plugins` entry point, plus `plugins.enabled` and a
  `gateway.platforms.agent365` block configured in
  `~/.hermes/config.yaml`. The README quickstart walks through this
  end-to-end.

## Core procedures

State-mutating subcommands default to **dry-run**; pass `--apply` to
execute. Repeated invocation converges to the same state.

### `hermes a365 doctor`

Read-only environment probe. Exit 0/1/2. Probes: `a365`, `az` (signed
in), `pwsh`, the `Agent 365 CLI` Entra app, network reachability
(login.microsoftonline.com / graph.microsoft.com), keychain,
`~/.hermes/.env`, Hermes harness. Frontier Preview enrollment is not
auto-verifiable.

### `hermes a365 license --users <n> --agents <n> --plan <E3|E5|...>`

Read-only. Recommends the A365 add-on or E7 based on the decision matrix
in [`references/license-cost-table.md`](references/license-cost-table.md).
Records the chosen model in `~/.hermes/.env` as `A365_LICENSE_MODEL`.

### `hermes a365 register --agent-name <name> [--tenant-id <id>] [--m365] [--aiteammate] [--no-endpoint] [--skip-requirements]`

Composite plan that orchestrates the three real CLI steps a blueprint
needs:

1. `a365 setup blueprint --agent-name <name>` — registers the Entra app
   and service principal that back the blueprint.
2. `a365 setup permissions mcp --agent-name <name>` — configures MCP
   OAuth grants and inheritable permissions.
3. `a365 setup permissions bot --agent-name <name>` — configures the
   Messaging Bot API OAuth grants.

The CLI itself owns idempotency, JSON shape, and Entra round-trips. The
skill's job is to compose the right argv per step, run them in order via
the `Mutator` protocol, persist derived display names to
`a365.config.json` (so subsequent commands refer to consistent
identities), and surface known auth errors:

- `AADSTS500011` (license not propagated): retried up to `--retries`
  times with `--backoff` seconds (defaults: 3 × 30 s, mockable in tests
  via `sleep_fn`).
- `AADSTS90094` (admin consent required): surfaced as
  "deferred — run `hermes a365 consent`" rather than failing the run.
  The blueprint apps remain created.

`--m365` registers the messaging endpoint via MCP Platform.
`--aiteammate` treats the agent as an AI Teammate (creates an Entra
user + manager). `--no-endpoint` and `--skip-requirements` are
passthroughs to `a365 setup blueprint`.

### `hermes a365 consent`

Renders the admin-consent URL from `templates/consent-url.txt.j2`, opens
it in the default browser (unless `--no-open`), then polls
`a365 query-entra blueprint-scopes` every 5 s up to a 5 min timeout.
Idempotent; re-running after grant is a no-op.

### `hermes a365 instance create <slug> --owner <email> --owner-aad-id <oid> [...]`

Pure local config-file writer. The server-side agent identity is created
by `a365 setup blueprint` (driven by `register`); this command only
produces the per-agent `~/.hermes/agents/<slug>/.env` that runtime
consumers read for slug, owner, OTLP endpoint, and business-hours
metadata.

Inherits `A365_APP_ID`, `A365_TENANT_ID`, `HERMES_OTLP_ENDPOINT` from
`~/.hermes/.env`. An existing `AA_INSTANCE_ID` is preserved across
re-runs; business-hours fields from a prior run are also preserved
unless explicitly overridden. The per-agent .env never contains the
blueprint client secret — see pitfall #7 below for where the secret
actually lives.

### `hermes a365 publish --agent-name <name> [--aiteammate] [--copilot-chat] [--bot-id <guid>] [--use-blueprint] [--tenant-id <id>]`

Wraps `a365 publish` to package the agent manifest into a zip the
operator uploads to the relevant admin surface. The output mode
follows the M365-ecosystem path:

- **`--aiteammate`** (Path A) — emits an AI Teammate manifest
  (`agenticUserTemplates`, `manifestVersion: devPreview`). Upload at
  **M365 Admin Centre → Agents → Upload custom agent**, then activate
  per-user under Agent 365 admin centre. Surfaces in Teams 1:1
  "Built for your org".
- **`--copilot-chat`** (Path B, slice 19u-a in v0.4.0) — emits a
  **Custom Engine Agent** manifest (`manifestVersion: "1.21"`,
  `bots` + `copilotAgents.customEngineAgents` blocks). Upload at
  **Teams Admin Center → Manage apps → Upload + assign per-user
  policy**. Surfaces in M365 Copilot Chat's agents picker + Copilot
  side-panels. Implementation post-processes the GA CLI's AI
  Teammate zip in-place (or to a sibling `.copilot-chat.zip` when
  combined with `--aiteammate`).
- **`--aiteammate --copilot-chat`** — emits both side by side
  (Copilot Chat zip lands at `<original>.copilot-chat.zip`).
- **`--bot-id <guid>`** — overrides the `botId` written into the
  Custom Engine Agent manifest. Default extraction order:
  `webApplicationInfo.id` → `bots[0].botId` → manifest top-level
  `id` (the GA CLI 1.1.174+ AI Teammate emit only populates the
  last).
- **No surface flag** — default `a365 publish` behaviour (registers
  the agent instance via Graph; no zip).

The wrapper surfaces the resulting package path(s) plus the right
admin-surface URL hint for each path. The `name.short` 30-char
auto-truncate (slice 19r-c) applies to both flavours. Channel
deployment is **operator-side** in all cases.

> **Path B additionally requires Azure Bot Service registration**
> of the blueprint Entra app with the Microsoft Teams channel
> enabled — otherwise Microsoft's routing layer won't forward
> Copilot Chat activities to `/api/messages` regardless of manifest
> shape. See `references/m365-surface-coverage.md` for the
> prerequisite detail.

### `hermes a365 status [<slug>]`

Per-component report against the verified `query-entra` surface. Four
components only:

- `local_config`     — parent `~/.hermes/.env` (and per-agent .env if a
  slug is given) parseable + required keys present.
- `blueprint_scopes` — `a365 query-entra blueprint-scopes` for the
  agent's blueprint.
- `instance_scopes`  — `a365 query-entra instance-scopes` for the
  agent's instance.
- `activity_bridge`  — local PID-file probe (only when a slug is given
  AND `bridge.pid` exists). When the bridge is running this row reports
  `ok`; absent pidfile is `missing` (bridge not currently running) and
  a stale or unreadable pidfile is `error`.

Exit codes: `0` ok, `1` partial, `2` broken, `3` skill not yet bootstrapped.

### `hermes a365 cleanup --agent-name <name> [--kinds=...] --confirm=<name> --apply`

Destructive teardown. Drives `a365 cleanup azure` → `instance` →
`blueprint` (safe → unsafe — App Service first so the runtime stops
before the Entra identity is revoked). Local artefacts under
`~/.hermes/agents/<slug>/` are removed after all cloud steps succeed.
`--kinds=<subset>` runs only the requested kinds. `--confirm` must
equal `--agent-name`. The plan is always printed for operator
audit before any mutation.

### `hermes a365 activity-bridge`

The Bot Framework adapter daemon. Two main modes are available either
as a standalone process (operator launches it) or as the runtime that
backs the `agent365` Hermes gateway platform plugin (the gateway
loads it in-process when an agent has the `agent365` platform enabled).

- `verify --slug <slug>` — one-shot diagnostic (config + auth +
  reachability). Exit 0/1/2.
- `serve --slug <slug> --port 3978` — BF webhook adapter daemon.
  Validates inbound activities as **AAD-v2** (A365 / MCP Platform
  issues AAD-v2 tokens directly to bot endpoints, not classic BF
  tokens), dedupes BF retries, gates `serviceUrl` against the
  Microsoft host suffix, and replies through the **agentic three-stage
  user-FIC token chain** (BF S2S → agent FMI → user FIC). Outbound
  goes via `replyToActivity` against the cached inbound `serviceUrl`.
- `update-endpoint --agent-name <n> --url <https>` — wraps
  `a365 setup blueprint --m365 --update-endpoint <url>` so operators
  can pin the agent's messaging endpoint to a tunnel URL. Auto-recovers
  from [duplicate-name error #140](https://github.com/microsoft/Agent365-devTools/issues/140).

Topology: Teams (or other M365 surface) → A365 BF → `<tunnel>/api/messages`
→ bridge → Hermes agent loop → reply via `serviceUrl`. Activity-shape
catalogue: [`references/activity-protocol-shapes.md`](references/activity-protocol-shapes.md);
operator-side network exposure options:
[`references/exposing-the-bot-endpoint.md`](references/exposing-the-bot-endpoint.md).

**Surfaces that work today:**

- **Path A (AI Teammate) Teams 1:1 chat** — validated end-to-end
  across rounds 3 → 8, with BF streaming protocol round-trip
  on round-8 (2026-05-11, v0.3.0). Agent appears in "Built for
  your org" picker.
- **Path A cron-driven proactive sends** — shipped in v0.5.0 +
  v0.5.1 (slices 19x-a..e, closes #4 and #27). Wire-validated
  against the live tenant 2026-05-13. `Agent365Adapter.send()`
  routes through `sendToConversation` (no `replyToId`) when the
  current gateway lifetime hasn't captured an inbound for
  `chat_id`; mints the agentic three-stage user-FIC chain against
  a target-spec built from the persisted `ConversationRegistry`.
  `prune_old_entries` + `pin` / `unpin` / `mark_used` mutators
  let operators manage the registry without restarting.
- **Path B (Custom Engine Agent) manifest emitter** — shipped
  2026-05-12 (slice 19u-a, `hermes a365 publish --copilot-chat`);
  manifest validated by Teams App Catalog upload. **Live Copilot
  Chat surfacing requires Azure Bot Service registration of the
  blueprint Entra app**, deferred pending Azure subscription
  decision (#16). Path B proactive (BF S2S outbound) is also
  gated on #16 — `_send_proactive` refuses Path B target specs
  with a clear deferred-error.

Sibling-plugin lane (Teams group chat / channels / threading /
file attachments / compose-extension invokes) is **out of scope
for Hermes-A365** — use Hermes' classic Teams adapter for
those. See [`references/m365-surface-coverage.md`](references/m365-surface-coverage.md)
for the per-surface matrix and the architectural reasoning behind
the split.

## Conflict resolution

| Conflict | Behaviour |
|---|---|
| Blueprint app already exists for the same name | `a365 setup blueprint` is itself idempotent; `register` re-runs are safe. |
| Permissions step fails with `AADSTS90094` | Reported as "deferred — run `hermes a365 consent`"; blueprint stays created. |
| Instance .env exists with a previous `AA_INSTANCE_ID` | `instance create` preserves the id (cloud state is unchanged). |
| Cleanup target has no recorded state | The corresponding kind is skipped, not errored. |
| License missing or insufficient | `register` retries `AADSTS500011` with backoff; on exhaustion the operator runs `hermes a365 license` and re-tries. |
| `pwsh` missing on PATH | Doctor flags it; `a365 setup` itself fails fast with a CLI error referencing `setup requirements`. |

## Common pitfalls

1. **Channel deployment is operator-side.** There is no `deploy` verb.
   `publish` produces the zip; the M365 admin uploads and approves it.
2. **Delegated permissions, not application permissions.** A365
   explicitly requires delegated permissions. Pasting an application-
   permission consent URL silently breaks at runtime.
3. **One agent name, derived sub-names.** `--agent-name "Inbox Helper"`
   produces `Inbox Helper Identity` and `Inbox Helper Blueprint` inside
   the CLI. Don't pass those derived names directly — pass the base.
4. **Blueprint slug ≠ agent name.** `register` operates on the CLI
   `--agent-name`. The local `<slug>` used in `instance create` /
   `status` / `cleanup` is the per-agent dir name under
   `~/.hermes/agents/`. Keep them aligned (lowercased / hyphenated) by
   convention; the skill never silently re-derives one from the other.
5. **License propagation lag.** A365 license assignment can lag 5–30
   min after purchase. `register` retries `AADSTS500011` 3× with 30 s
   backoff; if you're outside that window, `hermes a365 doctor` is the
   first port of call.
6. **`AA_INSTANCE_ID` reuse across re-runs.** `instance create`
   preserves the existing id. Don't manually edit the per-agent .env to
   "reset" it without first running `cleanup` — the cloud instance will
   linger.
7. **Blueprint client secret on disk in plaintext (macOS / Linux).**
   `a365 setup blueprint` writes the secret to
   `a365.generated.config.json` — DPAPI-encrypted on Windows,
   plaintext elsewhere. That file and the `cleanup -y`-emitted
   `*.backup-*.json` are gitignored; treat both as keychain-grade.
   CLI versions 1.1.171 through 1.1.174 also had Microsoft#408, where
   the secret was minted but persisted as `null` on macOS / Linux. Keep
   `--auto-recover-secret` for those versions; doctor recommends
   upgrading to ≥ 1.1.178.

## Verification checklist

- [ ] `hermes a365 doctor` exits 0.
- [ ] `hermes a365 status <slug>` shows: local_config ok,
      blueprint_scopes ok, instance_scopes ok.
- [ ] `a365 publish` zip uploaded via the M365 Admin Centre and
      approved for the target DLP scope.
- [ ] Test message in Teams (or other approved channel) returns an
      Adaptive Card from the agent.
- [ ] OTLP trace visible in the A365 admin centre for the test message.
- [ ] `hermes a365 cleanup --agent-name <name>` (dry-run) lists exactly
      the resources the operator expects to remove.

## One-shot recipes

### Bootstrap a single agent on a clean tenant

```
hermes a365 doctor                                                     # health check
hermes a365 license --users <n> --agents <n> --plan E5                 # decide license
hermes a365 register --agent-name "<Display Name>"                     # plan
hermes a365 register --agent-name "<Display Name>" --apply --auto-recover-secret
hermes a365 consent                                                    # in-browser grant
hermes a365 instance create <slug> --owner <email> --owner-aad-id <oid> --apply
hermes a365 publish --agent-name "<Display Name>" --aiteammate --apply # produce zip
# Operator: upload the zip in the M365 Admin Centre and approve for users.
hermes a365 activity-bridge verify --slug <slug>                       # bridge preflight
# Run the gateway with the agent365 platform configured (loads the
# bridge in-process via the entry-point-discovered plugin):
hermes gateway run --profile <slug>
hermes a365 status <slug>                                              # final verification
```

### Re-target an existing OpenClaw-on-A365 agent at Hermes

The blueprint stays in place; only the runtime endpoint and per-agent
config change:

```
hermes a365 instance create <slug> --owner <email> --owner-aad-id <oid> --apply
hermes a365 publish --agent-name "<Existing Display Name>" --apply
# Operator re-uploads the zip; the activity bridge then takes over the
# BF subscription URL the previous runtime was using.
```

### Decommission an agent cleanly

```
hermes a365 cleanup --agent-name "<Display Name>"                              # plan
hermes a365 cleanup --agent-name "<Display Name>" --apply --confirm="<Display Name>"
```

`--kinds=instance,blueprint` skips Azure when the App Service was
provisioned out-of-band. Tenant-wide infrastructure (Frontier Preview
enrollment, the custom `Agent 365 CLI` client app, license) is never
touched.

---

Subcommand implementations live under `src/hermes_a365/`; each is a
thin CLI over a planner + applier pair parameterised by a `Mutator`
protocol so the apply path is unit-testable without the live A365 CLI.
Packaged Jinja templates resolve via `importlib.resources` against
`hermes_a365._data.templates`; dated reference snapshots live under
`references/`. For per-subcommand flags see `hermes a365 <verb> --help`;
the original v0.1 design draft is archived at
`docs/historical/SPEC-v0.1-draft.md`.
