---
name: hermes-a365
description: Use when registering, deploying, or operating a Hermes-driven agent under Microsoft Agent 365 governance — covers Entra app registration, agent blueprints, MCP-mediated M365 data access, Bot Framework activity bridging, OpenTelemetry, and Teams/Outlook channel deployment.
version: 0.1.0
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

Use this skill when the operator has a Microsoft 365 tenant and wants
their Hermes agent governed by A365; skip it for generic Graph access or
Bot Framework deployments outside A365.

## When to Use

Use when the user wants to:

- Stand up a brand-new A365-governed Hermes agent on a clean Microsoft
  tenant.
- Deploy an existing Hermes agent to Teams, Outlook, or M365 Copilot.
- Toggle which Work IQ MCP tools (`mail`, `calendar`, `sharepoint`,
  `teams`, `tasks`, `people`) the agent can call.
- Rotate user-FIC credentials before they expire (default: 90 days).
- Verify telemetry, channel state, or license posture.
- Tear down an agent without disturbing tenant-wide infrastructure.
- Migrate an OpenClaw-on-A365 deployment to Hermes (preserves the
  existing blueprint via `instance create --reuse-blueprint`).

Don't use when:

- Generic Microsoft Graph access is the goal — use a Graph-only skill.
- Deploying a Bot Framework bot **outside** A365 governance.
- Setting up OpenAI Agents SDK or another framework end-to-end — A365
  governs the runtime; pick the appropriate framework skill for the
  agent itself.

## Prerequisites

- A Microsoft 365 tenant where the operator has **Global Administrator**
  or **Agent Administrator** role.
- The A365 CLI on PATH. Either variant works:
  - `a365` .NET (Microsoft package feed), ≥ 1.0.0.
  - `atk` npm (`@microsoft/agent-365-cli`), ≥ 1.0.0.
  Both ship as `a365` on PATH; the doctor (§Verification) detects which.
- `az` CLI ≥ 2.55.0 for Entra reads.
- An OS keychain backend: macOS Security framework or Linux libsecret
  (`secret-tool`). Windows is not yet supported.
- A tenant license: either the **Agent 365 add-on** ($15/user/month) or
  **Microsoft 365 E7** ($99/user/month). The skill never purchases — it
  recommends; see [`references/license-cost-table.md`](references/license-cost-table.md).
- A Hermes harness with this skill available under
  `optional-skills/cloud-platforms/hermes-a365/`.

## Core procedures

Every state-mutating subcommand defaults to **dry-run**. Pass `--apply`
to execute. Repeated invocation converges to the same state — every plan
is idempotent and reconciles against `a365 query-entra` output.

### `hermes a365 doctor`

Read-only environment probe. Exits 0 = ok, 1 = warn, 2 = error. Probes:
A365 CLI variant + version, `az` CLI, network reachability
(`login.microsoftonline.com`, `graph.microsoft.com`, tenant A365 host),
keychain backend, `~/.hermes/.env` parseability. Run this first on any
new machine.

### `hermes a365 license --users <n> --agents <n> --plan <E3|E5|...>`

Read-only. Recommends the A365 add-on or E7 based on the decision matrix
in [`references/license-cost-table.md`](references/license-cost-table.md).
Records the chosen model in `~/.hermes/.env` as `A365_LICENSE_MODEL`.

### `hermes a365 register --app-name <name> --tenant-id <tenant>`

Composite plan: T1 first-party app + T2 confidential client + user-FIC
configuration. Each app is reconciled by display name; tier mismatch on
the same name aborts (refuses to silently mutate). State machine:

1. Plan: `query-entra --by-name` for both names → reconcile against
   desired (`reconcile_app`).
2. Apply (`--apply`): create or patch each app via `a365 setup app`,
   configure user-FIC, store the T2 client secret in the OS keychain,
   atomically write `A365_TENANT_ID` and `A365_APP_ID` to
   `~/.hermes/.env`.

Failure modes:

- `AADSTS500011` (license not propagated): retried up to 3× with 30 s
  backoff (configurable; mockable in tests via `sleep_fn`).
- `AADSTS90094` (admin consent required): surfaced as
  "deferred — run `hermes a365 consent`" rather than failing the run.
  The apps are created either way.
- Tier mismatch on existing app: aborts with explicit reason. Operator
  must rename or remove the existing app.

The T2 client secret is written **only** to the keychain
(`hermes-a365.<tenant>.<appId>`) — never to disk.

### `hermes a365 consent`

Renders the admin-consent URL from `templates/consent-url.txt.j2`, opens
it in the default browser (unless `--no-open`), then polls
`query-entra --consent-status` every 5 s up to a 5 min timeout. Idempotent;
re-running after grant is a no-op.

### `hermes a365 blueprint create <slug> [--description --purpose --workiq …]`

Renders `templates/blueprint.json.j2`, queries
`query-entra --blueprint=<slug>`, computes a deep diff via
`reconcile_blueprint`, and either creates / patches / no-ops. Server-
assigned fields (`blueprintId`, `lastPatched`, `etag`, …) are stripped
from the actual payload before diffing so a freshly-registered blueprint
reads as a clean noop on the second run. Apply writes the rendered JSON
to a tmp file, hands it to `a365 setup blueprint --file=…`, and caches a
copy at `~/.hermes/agents/<slug>/blueprint.json`.

Slug mismatches (renaming a blueprint) **abort** — rename requires
cleanup-then-recreate.

Property allowlist: see [`references/entra-blueprint-properties.md`](references/entra-blueprint-properties.md).

### `hermes a365 instance create <slug> --owner --owner-aad-id [...]`

Per-agent runtime config. Inherits `A365_APP_ID`, `A365_TENANT_ID`,
`A365_CLI_VARIANT`, and `HERMES_OTLP_ENDPOINT` from
`~/.hermes/.env`. An existing `AA_INSTANCE_ID` in
`~/.hermes/agents/<slug>/.env` is preserved across runs (idempotency);
business-hours fields from a prior run are also preserved unless
explicitly overridden. Plan distinguishes `create`, `create-cloud-only`
(local id exists but cloud is missing), and `noop`. The
`A365_APP_PASSWORD` is **never** written to this file — runtime consumers
fetch it from the OS keychain on demand.

### `hermes a365 deploy <slug> --channels=<list>`

Idempotent set reconciliation across `teams` / `outlook` / `m365copilot`.
Reads the agent's currently-bound channels from
`query-entra --instance` (state `ok` = bound), computes the diff, hands
the desired absolute set to `a365 deploy --channels=<list>`. A365
reconciles additions and removals server-side. Empty list = unbind all.
Same set = noop, no mutator call.

### `hermes a365 workiq <slug> --enable|--disable|--set=<list>`

Toggles Work IQ MCP exposure on the cached blueprint. Reads
`~/.hermes/agents/<slug>/blueprint.json`, reconstitutes
`BlueprintInputs`, applies the change, and delegates to
`blueprint create`'s reconcile pipeline. `--set` is mutually exclusive
with `--enable`/`--disable`. Unknown tool names are rejected up-front
against the allowlist in
[`references/entra-blueprint-properties.md`](references/entra-blueprint-properties.md).

### `hermes a365 telemetry <slug>`

Read-only. Three checks: `HERMES_OTLP_ENDPOINT` set in agent .env,
`AA_INSTANCE_ID` recorded, last span seen via `query-entra --telemetry`.
JSON output by default; `--human` for a markdown table. Span injection
itself is the activity bridge's job (see below); this command only
verifies the pipeline. Schema:
[`references/opentelemetry-config.md`](references/opentelemetry-config.md).

### `hermes a365 fic rotate`

Re-issues the user-FIC backing the T2 confidential client and refreshes
the OS keychain entry. Surfaces an explicit reminder to restart the
activity bridge so it picks up the new credential. Fails-clean if the
parent .env is missing or incomplete.

### `hermes a365 status [<slug>]`

Aggregates nine components into a single report — license, T1/T2 apps,
blueprint, instance, channels, activity bridge, telemetry, FIC. Exit
codes: `0` ok, `1` partial, `2` broken, `3` skill not yet bootstrapped.
The cloud components gracefully degrade to `skipped` when the A365 CLI
isn't installed.

### `hermes a365 cleanup <slug> --confirm=<slug> --apply`

Per-agent destructive teardown in order `deployment → instance →
blueprint`. Apps (T1/T2) are **not** touched — they're tenant-wide
infrastructure shared across agents. `--confirm` must be the literal
agent slug. The plan is always printed (even without `--apply`) so the
operator can audit before mutating. Defensive: missing state turns into
a recorded skip rather than an error. Local artefacts
(`.env`, `blueprint.json`, empty agent dir) are removed only after the
cloud steps succeed.

### `hermes a365 activity-bridge`

**Status: TODO.** Blocked on the Hermes IPC contract (§10 Q1 of the
design spec). When unblocked the bridge will run as a long-lived adapter
that authenticates as the T2 confidential client (pulling the secret
from the keychain), subscribes to BF activities for the instance, and
routes `message` activities to the local Hermes agent + `invoke`
activities to the Adaptive Card builder under `templates/adaptive-cards/`.
The forward-looking activity-shape catalogue is at
[`references/activity-protocol-shapes.md`](references/activity-protocol-shapes.md).

## Conflict resolution

| Conflict | Behaviour |
|---|---|
| App with same display name but different tier | `register` aborts with explicit reason; rename or remove the existing app first. |
| Blueprint with same slug but mismatching identity | `blueprint create` aborts; rename via cleanup-then-recreate. |
| Instance already registered, local AA_INSTANCE_ID empty | `instance create` reuses the existing id (preserves the cloud state). |
| Channel set already converged | `deploy` is a no-op — no mutator call. |
| Cleanup target has no recorded state | The corresponding step is skipped, not errored. |
| License missing or insufficient | `register` retries `AADSTS500011` with backoff; on exhaustion the operator runs `hermes a365 license` and re-tries. |

## Common pitfalls

1. **Delegated permissions, not application permissions.** A365
   explicitly requires delegated permissions. Pasting an application-
   permission consent URL silently breaks at runtime.
2. **CLI variant collision.** `atk` (npm) and `a365` (.NET) both ship as
   `a365` on PATH on some systems. The doctor detects which is active
   and the parent .env records `A365_CLI_VARIANT` for downstream
   consumers.
3. **T1 vs T2 apps.** First-party (T1) apps cannot be modified after
   creation in some tenants. The skill creates both: T1 for sign-in, T2
   (confidential client with FIC) for runtime. The T2 client secret
   lives in the OS keychain only.
4. **Blueprint rename ≠ re-registration.** Renaming a blueprint (changing
   `agentIdentity.slug`) requires `cleanup` then `blueprint create`. The
   reconciler aborts on slug mismatch.
5. **License propagation lag.** A365 license assignment can lag 5–30 min
   after purchase. `register` retries `AADSTS500011` 3× with 30 s
   backoff; if you're outside that window, `hermes a365 doctor` is the
   first port of call.
6. **`AA_INSTANCE_ID` reuse across re-runs.** `instance create` preserves
   the existing id. Don't manually edit the per-agent .env to "reset" it
   without first running `cleanup` — the cloud instance will linger.
7. **Per-agent secret never on disk.** `A365_APP_PASSWORD` is the keychain
   entry name, not a file. Treat it as never-disk-resident.

## Verification checklist

- [ ] `hermes a365 doctor` exits 0.
- [ ] `hermes a365 status <slug>` shows: license ok, app(T1)/app(T2) ok,
      consent granted, blueprint ok, instance ok, channels ok for the
      configured set, telemetry ok (last span within 5 min of any
      activity), FIC ok (expiry > 7 days).
- [ ] Test message in Teams returns an Adaptive Card from the agent.
- [ ] OTLP trace visible in the A365 admin centre for the test message.
- [ ] `hermes a365 fic rotate --apply` succeeds and the agent stays
      connected after the activity bridge restart.
- [ ] `hermes a365 cleanup <slug>` (dry-run) lists exactly the resources
      `status` reported.

## One-shot recipes

### Bootstrap a single agent on a clean tenant

```
hermes a365 doctor                                                # health check
hermes a365 license --users <n> --agents <n> --plan E5            # decide license
hermes a365 register --app-name "<name>" --tenant-id <tenant>     # plan
hermes a365 register --app-name "<name>" --tenant-id <tenant> --apply
hermes a365 consent                                               # in-browser grant
hermes a365 blueprint create <slug> --description <…> --purpose <…> --workiq mail,calendar --apply
hermes a365 instance create <slug> --owner <email> --owner-aad-id <oid> --apply
hermes a365 deploy <slug> --channels=teams --apply
# When activity-bridge ships:
# hermes a365 activity-bridge start <slug> --detach
hermes a365 status <slug>                                         # final verification
```

### Re-target an existing OpenClaw-on-A365 agent at Hermes

The blueprint stays in place; only the runtime endpoint changes. Run:

```
hermes a365 instance create <slug> --owner <email> --owner-aad-id <oid> \
    --reuse-blueprint=<existing-slug> --apply
hermes a365 deploy <slug> --channels=<existing-channels> --apply
```

The activity bridge then takes over the BF subscription URL the previous
runtime was using.

### Rotate user-FIC ahead of expiry

```
hermes a365 status            # check fic.expires
hermes a365 fic rotate        # plan
hermes a365 fic rotate --apply
# restart the activity bridge so it picks up the new credential
```

### Decommission an agent cleanly

```
hermes a365 cleanup <slug>                              # plan
hermes a365 cleanup <slug> --apply --confirm=<slug>     # destructive
```

Apps remain — they're shared with other agents.

---

Subcommand implementations live under `scripts/`; each is a thin CLI
over a planner + applier pair parameterised by a `Mutator` protocol so
the apply path is unit-testable without the live A365 CLI. Templates
under `templates/`, dated reference snapshots under `references/`.
Canonical design and full per-subcommand examples are in `SPEC.md`.
