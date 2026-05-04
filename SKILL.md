---
name: hermes-a365
description: Use when registering or operating a Hermes-driven agent under Microsoft Agent 365 governance — covers `a365 setup blueprint`, `setup permissions {mcp,bot}`, per-agent runtime config, manifest packaging via `a365 publish`, environment doctor, status reporting against `query-entra`, and destructive cleanup.
version: 0.2.0
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
    related_skills:
      - hermes-agent-skill-authoring
---

# Hermes A365

## Overview

Microsoft Agent 365 (A365, GA 2026-05-01) is a governance / identity /
observability control plane for AI agents that bolts on top of any agent
stack and adds Entra-backed identity, tenant licensing, agent blueprints,
MCP-mediated Microsoft 365 data access ("Work IQ"), Bot Framework activity
bridging, OpenTelemetry, and channel adapters for Teams / Outlook /
Microsoft 365 Copilot. This skill drives those capabilities from inside
the Hermes harness so a Hermes agent can appear as a first-class A365
agent without re-implementing any of the governance surface.

The v0.2 surface is built directly on the GA `Microsoft.Agents.A365.DevTools.Cli`
verbs documented in [`references/a365-cli-reference.md`](references/a365-cli-reference.md).
The skill composes them into idempotent plan/apply flows and fills the
gaps (license decision, admin consent grant, runtime `.env` generation).

## When to Use

Use when the user wants to:

- Stand up a brand-new A365-governed Hermes agent on a clean Microsoft
  tenant.
- Package an existing Hermes agent's manifest for upload to the M365
  Admin Centre (channel deployment is operator-side in v0.2).
- Verify environment / config / scope posture before or after a change.
- Tear down an agent's Azure App Service, instance identity, and
  blueprint app cleanly.
- Migrate an OpenClaw-on-A365 deployment to Hermes (the existing
  blueprint stays; only the runtime endpoint changes).

Don't use when:

- Generic Microsoft Graph access is the goal — use a Graph-only skill.
- Deploying a Bot Framework bot **outside** A365 governance.
- Setting up OpenAI Agents SDK or another framework end-to-end — A365
  governs the runtime; pick the appropriate framework skill for the
  agent itself.

## Prerequisites

- A Microsoft 365 tenant where the operator has **Global Administrator**
  or **Agent Administrator** role and is enrolled in Microsoft's
  Frontier Preview Program.
- The A365 CLI on PATH: `Microsoft.Agents.A365.DevTools.Cli` (.NET tool,
  ships as `a365`), ≥ 1.0.0. The npm `atk` variant referenced in v0.1
  drafts did not ship at GA; only the .NET tool is supported.
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
- A Hermes harness with this skill available under
  `optional-skills/cloud-platforms/hermes-a365/`.

## Core procedures

Every state-mutating subcommand defaults to **dry-run**. Pass `--apply`
to execute. Repeated invocation converges to the same state — the
underlying `a365` verbs are themselves idempotent and reconcile against
the live tenant.

### `hermes a365 doctor`

Read-only environment probe. Exits 0 = ok, 1 = warn, 2 = error. Probes:
`a365` CLI present + version, `az` CLI signed in, `pwsh` on PATH, the
custom `Agent 365 CLI` Entra app discoverable in the current tenant,
network reachability (`login.microsoftonline.com`,
`graph.microsoft.com`, tenant A365 host), keychain backend,
`~/.hermes/.env` parseability, Hermes harness version. Frontier Preview
enrollment is not auto-verifiable; doctor mentions it in prose. Run this
first on any new machine, and again after any tenant-side change.

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
unless explicitly overridden. The blueprint's confidential-client
secret is never written here — runtime consumers fetch it from the OS
keychain (`hermes-a365.<tenant>.<appId>`).

### `hermes a365 publish --agent-name <name> [--aiteammate] [--use-blueprint] [--tenant-id <id>]`

Wraps `a365 publish` to package the agent manifest into the zip the
operator uploads to the M365 Admin Centre. Channel deployment in v0.2
is **operator-side**: the admin signs in to the centre, uploads the
zip, and approves the agent for users in the desired DLP scope. The
wrapper surfaces the resulting package path plus an admin-centre URL
hint.

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
  AND `bridge.pid` exists). Activity bridge upstream work is still
  TODO; the probe stays so the future bridge can be liveness-checked.

Components dropped vs v0.1 (no CLI surface): license, per-tier app,
channels, telemetry, FIC. Exit codes: `0` ok, `1` partial, `2` broken,
`3` skill not yet bootstrapped.

### `hermes a365 cleanup --agent-name <name> [--kinds=...] --confirm=<name> --apply`

Destructive teardown. Drives the three real granular subcommands in
safe → unsafe order:

1. `a365 cleanup azure`     — Azure App Service + App Service Plan
2. `a365 cleanup instance`  — agent instance identity + user
3. `a365 cleanup blueprint` — Entra blueprint app + service principal

Tearing the App Service down first stops the runtime before the Entra
identity is revoked, then the blueprint last. After all cloud steps
succeed, local artefacts under `~/.hermes/agents/<slug>/` are removed.
`--kinds` accepts a comma-separated subset (e.g. `--kinds=instance,
blueprint` to skip Azure when the App Service was provisioned out-of-
band). `--confirm` must equal `--agent-name`. The plan is always printed
(even without `--apply`) so the operator can audit before mutating.

### `hermes a365 activity-bridge`

**Status: TODO**, blocked on the Hermes IPC contract (§10 Q1). When
unblocked the bridge will authenticate as the blueprint's confidential
client (secret from the keychain), subscribe to BF activities for the
instance, and route `message` activities to the local Hermes agent plus
`invoke` activities to the Adaptive Card builder under
`templates/adaptive-cards/`. Activity-shape catalogue:
[`references/activity-protocol-shapes.md`](references/activity-protocol-shapes.md).

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

1. **Channel deployment is operator-side.** v0.2 has no `deploy` verb.
   `publish` produces the zip; the M365 admin uploads and approves it.
   Anything that targeted a `hermes a365 deploy` command in older docs
   is gone.
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
7. **Per-agent secret never on disk.** The blueprint's confidential-
   client secret lives in the OS keychain only
   (`hermes-a365.<tenant>.<appId>`). The runtime .env never contains it.

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
hermes a365 register --agent-name "<Display Name>" --apply
hermes a365 consent                                                    # in-browser grant
hermes a365 instance create <slug> --owner <email> --owner-aad-id <oid> --apply
hermes a365 publish --agent-name "<Display Name>" --apply              # produce zip
# Operator: upload the zip in the M365 Admin Centre and approve for users.
# When activity-bridge ships:
# hermes a365 activity-bridge start <slug> --detach
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

Subcommand implementations live under `scripts/`; each is a thin CLI
over a planner + applier pair parameterised by a `Mutator` protocol so
the apply path is unit-testable without the live A365 CLI. Templates
under `templates/`, dated reference snapshots under `references/`.
Canonical design and full per-subcommand examples are in `SPEC.md`.
