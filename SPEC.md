# `hermes-a365` — Skill Specification

**Status:** Draft v1 — 2026-05-03
**Author:** Sadiq Jaffer (drafted with Claude)
**Target:** Hermes Agent harness (`~/.hermes/hermes-agent/`)
**Source repo:** <https://github.com/satscryption/Hermes-A365>
**Replaces / parallels:** the existing OpenClaw integration with Microsoft Agent 365 (the `SidU/openclaw-a365` Bot Framework channel plugin and surrounding tooling)

> **Repo split note.** This spec, the reference material under `references/`, the prototype scripts under `scripts/`, and the templates under `templates/` are developed in the standalone repo `satscryption/Hermes-A365` so design and iteration can move at their own cadence. The final `SKILL.md` is **contributed upstream** to `hermes-agent/optional-skills/cloud-platforms/hermes-a365/SKILL.md` (see §3.1) and pulls in the artefacts from this repo at upstream-contribution time. Until that happens, this repo is the authoritative working tree.

---

## 1. Background

**Microsoft Agent 365 ("A365")** went GA 2026-05-01 as Microsoft's governance/identity/observability control plane for AI agents. It is **not** an agent framework — it bolts onto an existing agent stack (Microsoft Agent Framework, Microsoft 365 Agents SDK, OpenAI Agents SDK, OpenClaw, Claude Code SDK, etc.) and provides:

- Entra-backed agent identity (delegated, not application, permissions)
- Tenant licensing (`$15/user/mo` add-on, or M365 E7 `$99/user/mo`)
- Agent blueprints registered via `a365 setup blueprint`
- MCP-mediated access to Microsoft 365 data (Mail, Calendar, SharePoint, Teams) — "Work IQ tools"
- Bot Framework Activity protocol for notifications and adaptive-card invokes
- OpenTelemetry observability with admin-center surface
- Teams / Outlook / Microsoft 365 Copilot channel adapters

A typical OpenClaw-on-A365 deployment wires this together with:
- **`SidU/openclaw-a365`** — Bot Framework channel plugin bridging the OpenClaw runtime to A365 activities
- A bootstrap procedure for Entra app registration, admin consent, and license decision (per the public Microsoft Learn registration guide)
- Per-agent runtime config (env-driven) with `A365_APP_ID`, `A365_APP_PASSWORD`, `A365_TENANT_ID`, `OWNER`, `OWNER_AAD_ID`, `AGENT_IDENTITY`, `AA_INSTANCE_ID`

**The Hermes equivalent** must reproduce that integration surface for agents driven by the Hermes harness, expressed as a single optional-skill the user can invoke to bootstrap, deploy, and operate an A365-registered Hermes agent.

> Public references for everything below:
> - Microsoft Learn — Agent 365 SDK and CLI: <https://learn.microsoft.com/en-us/microsoft-agent-365/developer/>
> - Microsoft Learn — Agent 365 CLI reference: <https://learn.microsoft.com/en-us/microsoft-agent-365/developer/agent-365-cli>
> - Microsoft Learn — Custom client app registration: <https://learn.microsoft.com/en-us/microsoft-agent-365/developer/custom-client-app-registration>
> - Microsoft Agent 365 GA announcement (2026-05-01): <https://www.microsoft.com/en-us/security/blog/2026/05/01/microsoft-agent-365-now-generally-available-expands-capabilities-and-integrations/>
> - Bot Framework Activity protocol: <https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-activities>
> - Adaptive Cards (v1.6): <https://adaptivecards.io/>
> - `SidU/openclaw-a365` Bot Framework channel plugin: <https://github.com/SidU/openclaw-a365>
> - Hermes skill format: see the Hermes Agent harness' built-in skill-authoring guide (bundled with the harness)

---

## 2. Goals & non-goals

### 2.1 Goals

1. Provide a single Hermes optional-skill, **`hermes-a365`**, that walks a user from "fresh tenant" to "A365-governed Hermes agent answering in Teams/Outlook" without leaving the harness.
2. Cover every capability A365 exposes that the OpenClaw integration uses: licensing, Entra registration, blueprint, identity, MCP/Work IQ, Activity protocol, OpenTelemetry, channel adapters.
3. Be safe-to-rerun and dry-run-by-default — every state-mutating subcommand should plan first, mutate only on explicit `--apply`.
4. Encode all Microsoft state changes (tenant license, app registration, blueprint, deployment) as **idempotent steps** with explicit reconciliation against current Microsoft state, not blind re-application.
5. Plug into Hermes' existing config (`~/.hermes/config.yaml`, `~/.hermes/.env`) and skill conventions — no parallel config tree.

### 2.2 Non-goals

1. **Not** a reimplementation of the Microsoft Agent Framework. We orchestrate the official `a365` CLI and Microsoft SDKs; we don't replace them.
2. **Not** a model gateway. OpenClaw used its own gateway as a model proxy in per-agent containers — Hermes uses Hermes-native model config; this skill does not duplicate that.
3. **Not** a Teams/Outlook UI builder. Adaptive Cards are emitted; design tooling is out of scope.
4. **No** automatic license purchase. The skill identifies, recommends, and stages — but a tenant admin still clicks "buy" in admin center.
5. **No** secret exfiltration. Client secrets and FIC tokens stay in OS keychain or the user-designated secret store; the skill never writes them to repo-tracked files.

### 2.3 Mapping: A365-for-OpenClaw → Hermes equivalent

| Capability | Origin in OpenClaw integration | Hermes equivalent (this skill) |
|---|---|---|
| Tenant license decision | Microsoft Learn — A365 licensing guidance | `hermes a365 license` — recommends model, never purchases |
| Entra app registration (T1/T2/user-FIC) | Microsoft Learn — Custom client app registration | `hermes a365 register` — drives `a365 query-entra`, `a365 setup app` |
| Admin consent | Microsoft Learn — Admin consent flow | `hermes a365 consent` — emits the consent URL, polls `query-entra` for grant |
| Agent blueprint | A365 CLI `setup blueprint` | `hermes a365 blueprint` — generates JSON from template, runs `a365 setup blueprint` |
| Per-agent runtime env | A365 SDK runtime config (env-driven) | `hermes a365 instance create` — writes `~/.hermes/agents/<slug>/.env` |
| Model-backend abstraction | OpenClaw Gateway as model proxy via `openclaw.json` providers | **Dropped.** Hermes uses its own model config; A365 doesn't care which model backend the agent uses. |
| Bot Framework Activity bridge | `SidU/openclaw-a365` plugin | `hermes a365 activity-bridge` — runs the Hermes-side activity adapter |
| Adaptive Cards (`invoke` activity) | Bot Framework Activity protocol — `invoke` shape | Templates in `templates/adaptive-cards/`; helper `scripts/emit_card.py` |
| Work IQ MCP servers | A365 admin center + per-agent toggle | `hermes a365 workiq` — toggles MCP exposure per blueprint |
| OpenTelemetry export | A365 SDK auto-instrumentation | `hermes a365 telemetry` — verifies OTLP endpoint + sampling |
| Teams / Outlook / M365 Copilot channels | A365 CLI `a365 deploy` | `hermes a365 deploy` — wraps `a365 deploy --channels=...` |
| Federated identity (user-FIC, T2) | A365 CLI `a365 fic` | `hermes a365 fic rotate` — driven by `a365 fic` |
| Cleanup | A365 CLI `a365 cleanup` | `hermes a365 cleanup` — destructive, requires `--confirm` |

---

## 3. Skill placement & metadata

### 3.1 Install path

**In-repo, optional-skill, new category `cloud-platforms`:**

```
~/.hermes/hermes-agent/optional-skills/cloud-platforms/hermes-a365/
```

Rationale:
- It ships with the Hermes harness as an opt-in optional-skill, so a fresh Hermes install can adopt it without a separate distribution channel.
- `cloud-platforms/` is a new top-level optional-skill category. Don't invent categories casually, but A365/AWS/GCP/Azure agent integrations don't fit any existing Hermes optional-skill category (`blockchain`, `communication`, `health`, `migration`, `security`, `web-development`, `mlops`). Document the new category in the optional-skills index (if one exists) at the same time.
- **Not** a default-loaded skill. It's heavy, opinionated, and only relevant to users with Microsoft tenants.

### 3.2 Frontmatter (validator-compliant)

```yaml
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
```

**Validator notes:**
- Name `hermes-a365` is 11 chars (≤64).
- Description above is ~360 chars (≤1024).
- Frontmatter is a YAML mapping starting at byte 0.
- Whole SKILL.md should target 8-15k chars; capability detail beyond that lives in `references/`.

### 3.3 CLI surface

A single dispatch entry, `hermes a365 <subcommand>`, exposed via the harness' standard skill→CLI bridge. Subcommands:

| Subcommand | Purpose |
|---|---|
| `hermes a365 doctor` | Read-only environment check: `a365` CLI present, `atk` npm vs `.NET` variant detected, `az` CLI available, network reachable, current tenant, current license posture |
| `hermes a365 license` | Recommend license model based on agent count + user count; emits a markdown comparison; never purchases |
| `hermes a365 register [--app-name --tenant]` | Idempotent Entra T1 + T2 registration; reconciles against `a365 query-entra` |
| `hermes a365 consent` | Emits admin-consent URL, polls until grant detected |
| `hermes a365 blueprint create <agent-slug>` | Renders blueprint JSON from template, `a365 setup blueprint` |
| `hermes a365 instance create <agent-slug> [--owner --owner-aad-id]` | Writes `~/.hermes/agents/<slug>/.env`, registers agent instance, sets up FIC |
| `hermes a365 deploy <agent-slug> [--channels=teams,outlook,m365copilot]` | Wraps `a365 deploy` |
| `hermes a365 activity-bridge start <agent-slug>` | Runs the Hermes-side Bot Framework Activity bridge as a foreground or detached process |
| `hermes a365 workiq <agent-slug> [--enable mail,calendar,...]` | Toggles MCP-mediated Work IQ tools |
| `hermes a365 telemetry verify <agent-slug>` | Confirms OTLP endpoint, sampling, last span seen |
| `hermes a365 fic rotate <agent-slug>` | User-FIC token rotation |
| `hermes a365 status [<agent-slug>]` | All-up status: license, app, blueprint, instance, deployment, last activity |
| `hermes a365 cleanup <agent-slug> --confirm` | Destructive: deletes blueprint, instance, app — never tenant licenses |

**Default posture:** every state-mutating subcommand defaults to `--dry-run` unless `--apply` is passed.

---

## 4. SKILL.md body structure

Hermes peer skills follow a predictable shape. Use this skeleton:

```
# Hermes A365

## Overview
2-3 sentences: what A365 is, what this skill does, who should use it.

## When to Use
- Bulleted triggers: "User has a Microsoft 365 tenant and wants their Hermes
  agent to appear in Teams as a first-class A365 agent"
- "User is migrating an OpenClaw-on-A365 deployment to Hermes"
- "User needs to rotate FIC tokens or refresh blueprint"

Don't use for:
- Generic Microsoft Graph access (use `hermes-msgraph` instead, when it exists)
- Bot Framework deployments outside A365 governance

## Prerequisites
Bulleted: Microsoft 365 tenant with Global Admin or Agent Admin role, `a365`
CLI installed (and chosen variant — `atk` npm or `a365` .NET — recorded), `az`
CLI for Entra interactions, OS keychain for secret storage.

## Core procedures
One subsection per CLI subcommand from §3.3, each laid out as:
  - Goal (one sentence)
  - Inputs the skill collects via `clarify`
  - State-machine diagram (text-only) showing dry-run → confirm → apply → verify
  - Idempotency rules
  - Failure modes and remediation

## Conflict resolution
Apply standard reconciliation:
  - Resource exists with same name but different config → reconcile/overwrite/abort
  - Resource exists owned by another agent → abort with pointer
  - License insufficient → halt and surface admin-center URL

## Common pitfalls
Numbered list, drawn from operator experience and Microsoft Learn:
  1. Delegated permissions, not application permissions — A365 explicitly requires
     this. Pasting an application-permission consent URL silently breaks at runtime.
  2. CLI binary collision: `atk` (npm) and `a365` (.NET) both ship as `a365` on
     PATH on some systems — record which one was used in `~/.hermes/agents/<slug>/.env`
     as `A365_CLI_VARIANT`.
  3. T1 vs T2 — first-party Entra apps cannot be modified after creation in some
     tenants; defer to T2 (confidential client) when in doubt.
  4. Blueprint rename != re-registration; renaming requires cleanup-then-recreate.
  5. License changes propagate asynchronously — verify with `a365 query-entra
     --license` before retrying registration on a "license missing" error.

## Verification checklist
- [ ] `hermes a365 doctor` exits 0
- [ ] `hermes a365 status <agent>` shows license=ok, app=registered, consent=granted,
      blueprint=registered, instance=deployed, channels=teams[+outlook|+m365copilot],
      telemetry=heartbeat-within-5min
- [ ] Test message in Teams returns Adaptive Card from agent
- [ ] OTLP trace visible in admin center for the test message
- [ ] `hermes a365 fic rotate` succeeds and agent stays connected

## One-shot recipes
- "Bootstrap a single agent from a clean tenant" — calls register → consent →
  blueprint → instance → deploy in sequence with paired verification gates.
- "Re-target an existing OpenClaw-on-A365 agent at Hermes" — port any Hermes-side
  state separately, then call `hermes a365 instance create --reuse-blueprint=<existing>`
  to pivot the existing blueprint to the new Hermes runtime without re-registering
  the Entra app.
```

---

## 5. File layout

```
optional-skills/cloud-platforms/hermes-a365/
├── SKILL.md
├── references/
│   ├── a365-cli-reference.md            # Mirrors learn.microsoft.com/.../agent-365-cli
│   ├── entra-blueprint-properties.md    # Property reference, since MS publishes it as prose
│   ├── activity-protocol-shapes.md      # message + invoke (adaptiveCard/action) shapes
│   ├── work-iq-tools.md                 # MCP server inventory (Mail, Calendar, etc.)
│   ├── license-comparison.md            # $15 add-on vs E7 $99
│   ├── opentelemetry-config.md          # OTLP endpoint, span schema
│   └── error-codes.md                   # AADSTS, A365-specific, BotFramework-specific
├── scripts/
│   ├── doctor.py                        # Env probe; emits JSON to stdout
│   ├── render_blueprint.py              # Template → blueprint JSON
│   ├── render_instance_env.py           # Template → ~/.hermes/agents/<slug>/.env
│   ├── activity_bridge.py               # Hermes-side adapter for BF activities
│   ├── emit_card.py                     # Adaptive Card payload builder
│   ├── reconcile_app.py                 # Diffs desired Entra app state vs actual
│   ├── reconcile_blueprint.py           # Same for blueprint
│   ├── status.py                        # Aggregates state for `hermes a365 status`
│   └── secrets.py                       # OS-keychain wrapper (macOS Keychain, Linux Secret Service)
├── templates/
│   ├── blueprint.json.j2                # Agent blueprint
│   ├── instance.env.j2                  # Per-agent .env
│   ├── adaptive-cards/
│   │   ├── greeting.json.j2
│   │   ├── confirmation.json.j2
│   │   └── error.json.j2
│   └── consent-url.txt.j2               # Pre-filled admin consent URL
└── assets/
    └── (none required at v0.1)
```

**Constraints from the Hermes validator:** subdir allowlist is `references/`, `scripts/`, `templates/`, `assets/`. No other top-level subdirs. Keep individual files small; the validator caps SKILL.md itself at 100k chars but does not cap reference files — still, keep references readable (≤ ~20k each).

**Why scripts/ is allowed to be substantial here:** Hermes peer skills are usually procedure-only, but state-mutating CLI orchestration against a remote tenant *needs* idempotency code we don't want to re-derive in markdown on each invocation.

---

## 6. Capability coverage (detailed)

### 6.1 Tenant license decision (`hermes a365 license`)

- **Inputs:** estimated number of agents, estimated number of users-receiving-agent-output, current M365 plan.
- **Decision rule:**
  - If users < 25 OR M365 plan < E5 → recommend `$15/user/mo Agent 365 add-on`.
  - If users ≥ 25 AND want bundled Copilot+Defender+Purview → recommend `M365 E7 ($99/user/mo)`.
  - Surface that license model is recorded as `A365_LICENSE_MODEL=per_agent|e7` in `~/.hermes/.env`.
- **Output:** markdown comparison table, link to admin-center purchase URL, **no purchase action**.
- **Idempotency:** read-only.

### 6.2 Entra app registration (`hermes a365 register`)

- **State machine:**
  1. `a365 query-entra --by-name <app-name>` — does T1 first-party app exist?
     - If yes, capture `appId`, mark T1=present.
     - If no, run `a365 setup app --tier=1 --name=<app-name>`.
  2. Same for T2 confidential client app: `a365 setup app --tier=2 --name=<app-name>-conf`.
  3. Configure user-FIC: `a365 fic configure --app=<T2-appId>`.
- **Required env after success** (written to `~/.hermes/.env`):
  - `A365_APP_ID` (T2)
  - `A365_APP_PASSWORD` (T2 secret — **only into OS keychain, never repo file**)
  - `A365_TENANT_ID`
- **Idempotency:** name-based reconciliation. Name collision in another tenant scope = abort with explicit message.
- **Failure modes:**
  - `AADSTS90094` (admin consent required) → defer to §6.3.
  - `AADSTS500011` (resource principal not found in tenant) → license check (§6.1) hasn't propagated; back off 30s and retry up to 3x.

### 6.3 Admin consent (`hermes a365 consent`)

- Render consent URL via `templates/consent-url.txt.j2` filled with `A365_APP_ID` and tenant ID.
- Open in default browser unless `--no-open`.
- Poll `a365 query-entra --consent-status --app=<id>` every 5 s until granted or 5-minute timeout.
- Idempotency: re-running after grant succeeds is a no-op.

### 6.4 Agent blueprint (`hermes a365 blueprint create`)

- Inputs: `agent-slug`, `description`, `purpose`, `functions[]`, `app-roles[]`, `optional-claims[]`, `dlp-policy`, `external-access-policy`, `logging-policy`.
- Render with `templates/blueprint.json.j2`. Microsoft does not publish a JSON Schema for blueprints — properties listed in prose at <https://learn.microsoft.com/en-us/microsoft-agent-365/developer/registration>. Author by example; surface unknown properties as warnings via `references/entra-blueprint-properties.md`.
- Run `a365 setup blueprint --file=<path>`.
- Verify with `a365 query-entra --blueprint=<slug>`.
- Idempotency: diff actual blueprint vs rendered desired; only PATCH delta. Renames require cleanup (see §6.13).

### 6.5 Per-agent runtime config (`hermes a365 instance create`)

- Inputs: `agent-slug`, `owner`, `owner-aad-id`.
- Writes `~/.hermes/agents/<slug>/.env` from `templates/instance.env.j2` containing:
  ```
  AGENT_IDENTITY=<slug>
  OWNER=<owner>
  OWNER_AAD_ID=<owner-aad-id>
  A365_APP_ID=<from ~/.hermes/.env>
  A365_TENANT_ID=<from ~/.hermes/.env>
  AA_INSTANCE_ID=<generated UUID>
  A365_CLI_VARIANT=<atk-npm|a365-dotnet>
  HERMES_OTLP_ENDPOINT=<inherited from A365>
  BUSINESS_HOURS_TZ=<optional>
  BUSINESS_HOURS_START=<optional>
  BUSINESS_HOURS_END=<optional>
  ```
- Calls `a365 create-instance --blueprint=<slug> --instance=<AA_INSTANCE_ID>`.
- Idempotency: existing `AA_INSTANCE_ID` is preserved; only missing fields are filled.
- **Secrets policy:** `A365_APP_PASSWORD` is *not* written to this file. The activity bridge (§6.7) and any other consumer pulls it from OS keychain on demand.

### 6.6 Work IQ MCP exposure (`hermes a365 workiq`)

- Toggle which MCP-mediated M365 data sources the blueprint can call: `mail`, `calendar`, `sharepoint`, `teams`, `tasks`, `people`. Each maps to an A365-managed MCP server.
- Stored in blueprint; changes go through `hermes a365 blueprint create` reconciliation.
- Verify by listing exposed tools in admin center: surface link from skill output.
- **No** local MCP server is run — A365 manages them. This subcommand is config-only.

### 6.7 Activity bridge (`hermes a365 activity-bridge`)

This is the analogue of the `SidU/openclaw-a365` Bot Framework channel plugin.

- **Process:** runs as a long-lived adapter that:
  1. Authenticates as the T2 confidential client (pulls secret from OS keychain).
  2. Subscribes to BF activities via the URL from `a365 query-entra --instance-channel`.
  3. For each `message` activity → routes to local Hermes agent at `~/.hermes/agents/<slug>/`, captures response, posts back as `message` activity.
  4. For each `invoke` activity (`adaptiveCard/action`) → renders an Adaptive Card response from `templates/adaptive-cards/`.
- **Lifecycle:**
  - `start` — foreground or `--detach` (writes PID to `~/.hermes/agents/<slug>/bridge.pid`).
  - `stop` — SIGTERM via PID file.
  - `status` — alive + last activity timestamp.
- **Logging:** structured JSON to `~/.hermes/agents/<slug>/bridge.log`. Spans exported via OTLP.
- **Hermes-runtime contract:** the bridge invokes the Hermes agent through whatever local-call mechanism Hermes already exposes (TBD in implementation — the agent harness already has a request/response surface; reuse it rather than re-implementing).

### 6.8 OpenTelemetry (`hermes a365 telemetry`)

- A365 auto-instruments registered agents: spans, metrics, and a small canonical event vocabulary (agent.received, agent.responded, agent.tool_invoked, agent.error).
- This skill's job:
  - Confirm `HERMES_OTLP_ENDPOINT` is set in the per-agent .env.
  - Inject Hermes' own spans into the same trace context.
  - Verify last span seen via `a365 query-entra --telemetry --instance=<id>`.
- Span schema doc: `references/opentelemetry-config.md`.

### 6.9 Channel deployment (`hermes a365 deploy`)

- Wraps `a365 deploy --instance=<id> --channels=<list>`.
- Channels supported: `teams`, `outlook`, `m365copilot`.
- Per-channel verification:
  - Teams — bot installable in chat? Returns deep-link.
  - Outlook — agent appears in compose pane?
  - M365 Copilot — agent appears in agent picker?
- Idempotent: re-deploying with the same channel set is a no-op.

### 6.10 Federated identity rotation (`hermes a365 fic rotate`)

- Runs `a365 fic rotate --app=<T2-appId>`.
- Re-issues user-FIC token, updates OS keychain.
- Restart of activity bridge is required after rotation — the subcommand prompts/triggers it.
- Schedule note: A365 user-FICs expire on a tenant-configured cadence (default 90 days). Surface the next-rotation date in `hermes a365 status`.

### 6.11 Status (`hermes a365 status`)

Aggregates everything via `scripts/status.py`. Output (markdown table):

```
Component        State      Detail
-----------      -----      ------
license          ok         per_agent ($15/user/mo), 12 of 25 seats used
app (T1)         ok         appId=...
app (T2)         ok         appId=..., consent=granted 2026-04-30
blueprint        ok         <slug>, last patched 2026-05-02
instance         ok         AA_INSTANCE_ID=...
channels         partial    teams=ok outlook=ok m365copilot=missing
activity-bridge  ok         pid=12345, last activity 2026-05-03 14:22 UTC
telemetry        ok         last span 2026-05-03 14:22 UTC, sampler=parent_based
fic              warn       expires 2026-05-15 (12 days)
```

Exit codes: `0` all-ok, `1` partial, `2` broken, `3` skill not yet bootstrapped.

### 6.12 Doctor (`hermes a365 doctor`)

Read-only, fast. Checks:
- `a365` CLI present, variant detected, version captured.
- `az` CLI present, `az account show` succeeds.
- Network reachable: `login.microsoftonline.com`, `graph.microsoft.com`, `<tenant>.api.agent365.microsoft.com`.
- OS keychain backend available.
- `~/.hermes/.env` and `~/.hermes/config.yaml` parseable.
- Hermes harness version (`hermes --version`) within supported range.

Pure diagnostic; never mutates.

### 6.13 Cleanup (`hermes a365 cleanup`)

- Order matters: deployment → instance → blueprint → app (T2) → app (T1).
- Each step requires the corresponding A365 CLI cleanup subcommand.
- `--confirm` required and must include the agent-slug literal: `hermes a365 cleanup my-agent --confirm=my-agent`.
- Tenant license is **never** touched by this skill.
- Local files: optionally archived to `~/.hermes/archive/a365/<slug>/<timestamp>/`. On by default; `--no-archive` to skip.

---

## 7. Configuration model

### 7.1 Files this skill writes

| File | Owner | Contents | Repo-tracked? |
|---|---|---|---|
| `~/.hermes/.env` | merged | `A365_TENANT_ID`, `A365_APP_ID` (T2), `A365_LICENSE_MODEL`, `A365_CLI_VARIANT` | No |
| OS keychain entry `hermes-a365.<tenant>.<appId>` | this skill | T2 client secret | No (keychain) |
| `~/.hermes/agents/<slug>/.env` | this skill | per-agent vars (§6.5) | No |
| `~/.hermes/agents/<slug>/blueprint.json` | this skill | last-rendered blueprint, for diffing | Optional |
| `~/.hermes/agents/<slug>/bridge.pid` | activity bridge | PID for foreground bridge | No |
| `~/.hermes/agents/<slug>/bridge.log` | activity bridge | structured JSON logs | No |
| `~/.hermes/config.yaml` | merged | adds `agents.<slug>.runtime: a365` and `agents.<slug>.bridge: hermes-a365` | No (user-local) |

### 7.2 Files this skill reads

- `~/.hermes/config.yaml` for global tenant defaults.
- `~/.hermes/agents/<slug>/.env` for per-agent state.
- OS keychain for secrets.

### 7.3 Files this skill never touches

- `~/.openclaw/*` (out of scope; OpenClaw configuration migration is a separate concern handled outside this skill).
- Repo-tracked code or skills.
- Tenant-wide M365 settings outside the registered app(s).

---

## 8. Lifecycle / state machine

```
                 ┌───────────────┐
                 │  uninstalled  │
                 └──────┬────────┘
                        │ doctor
                        ▼
                 ┌───────────────┐
                 │   diagnosed   │
                 └──────┬────────┘
                        │ register + consent
                        ▼
                 ┌───────────────┐
                 │  app-ready    │
                 └──────┬────────┘
                        │ blueprint create
                        ▼
                 ┌───────────────┐
                 │ blueprinted   │
                 └──────┬────────┘
                        │ instance create
                        ▼
                 ┌───────────────┐
                 │ instantiated  │
                 └──────┬────────┘
                        │ deploy
                        ▼
                 ┌───────────────┐
                 │   deployed    │
                 └──────┬────────┘
                        │ activity-bridge start
                        ▼
                 ┌───────────────┐
                 │   serving     │◄──┐
                 └──────┬────────┘   │ fic rotate, blueprint patch,
                        │ cleanup    │ workiq toggle, channel change
                        ▼            │ (loops back to serving)
                 ┌───────────────┐   │
                 │   archived    │───┘
                 └───────────────┘
```

State is derived (not stored) by `scripts/status.py` — never persist a state value the live system can be queried for.

---

## 9. Verification & acceptance criteria

The skill is "done" when **all** of the following hold:

1. `hermes a365 doctor` exits 0 on a tenant-admin's macOS Sequoia or Ubuntu 24.04 with stock `a365` and `az` CLIs.
2. From a clean tenant, `hermes a365 register && hermes a365 consent && hermes a365 blueprint create test && hermes a365 instance create test --owner=<me> --owner-aad-id=<my-aad-id> && hermes a365 deploy test --channels=teams && hermes a365 activity-bridge start test --detach` succeeds end-to-end without manual intervention beyond the consent click.
3. A test message in Teams to the agent returns an Adaptive Card response generated by the Hermes harness.
4. `hermes a365 status test` shows all components green.
5. `hermes a365 telemetry verify test` shows a span with trace-id matching the test message within 60 s.
6. `hermes a365 fic rotate test` rotates without disconnection (bridge auto-restarts).
7. `hermes a365 cleanup test --confirm=test` removes deployment, instance, blueprint, and T2 app, and archives local files.
8. **Idempotency:** re-running any state-mutating subcommand after success exits 0 with a "no-op" message — no Microsoft API mutations issued.
9. **Dry-run:** every state-mutating subcommand without `--apply` prints the planned change set and makes zero mutations.
10. **Validator:** `skill_view hermes-a365` shows it loaded, frontmatter parsed, body within length cap.

---

## 10. Open questions (for follow-up before implementation)

1. **Hermes runtime contract for the activity bridge.** The bridge needs to invoke a Hermes agent and capture its response. What is the existing Hermes IPC surface (CLI subprocess, local socket, in-process API)? Resolve by inspecting the harness' bundled CLI and the autonomous-agent reference skill before implementation.
2. **Multi-tenant.** Is one Hermes install ever pointed at more than one Microsoft tenant? If yes, `~/.hermes/.env` is wrong — we need `~/.hermes/tenants/<tenant-id>/.env`. v0.1 assumes single-tenant; promote to multi-tenant in v0.2 if needed.
3. **OS-keychain abstraction.** macOS Keychain via `security`, Linux via `secret-tool` (libsecret). Windows is out of scope for v0.1 — confirm.
4. **Model-vendor neutrality.** A365 does not care which model backend the agent uses, and this skill must not couple to one. Confirm the activity bridge stays vendor-agnostic — it talks to Hermes; Hermes talks to whatever model is configured.
5. **MCP server for `hermes-a365` itself.** Should `hermes-a365` *expose* an MCP server (so other agents can drive A365 via MCP) in addition to driving A365 itself? Out of scope for v0.1; revisit in v0.2.
6. **Adaptive-card renderer choice.** Microsoft's `adaptivecards.io` reference renderers vs a Hermes-native renderer. v0.1 uses the MS-supplied renderer via the bridge; templates ship Adaptive Card v1.6 schema.
7. **`atk` vs `a365` CLI variant detection.** Both ship as `a365` on PATH. The doctor needs a reliable disambiguation — likely `a365 --version` plus a binary-path check.

---

## 11. Test plan

### 11.1 Unit (in skill repo)

- `scripts/render_blueprint.py` — golden-file tests: input args → expected JSON.
- `scripts/render_instance_env.py` — golden-file tests: input args → expected `.env`.
- `scripts/reconcile_app.py`, `reconcile_blueprint.py` — diff tests with mocked `a365 query-entra` JSON.
- `scripts/status.py` — exit-code tests against staged JSON fixtures for each state.

### 11.2 Integration (against a Microsoft test tenant)

A separate `optional-skills/cloud-platforms/hermes-a365/tests/integration/` tree (or a sibling repo) runs the §9 acceptance scenarios against a sandbox tenant. Gated on `A365_TEST_TENANT_ID` env var; never runs in CI without it.

### 11.3 Manual

- Re-target recipe: take an existing OpenClaw-on-A365 agent, port any Hermes-side state separately, then run `hermes a365 instance create --reuse-blueprint=<existing>`. Verify the existing blueprint serves the new Hermes runtime without re-registering the Entra app.
- FIC expiry: artificially set `--fic-ttl=60s` (if A365 supports it; otherwise wait), observe that `hermes a365 status` warns at T-7d and errors at T-0.

---

## 12. Conventions

- **Idempotent and dry-run-first.** Every state-mutating subcommand plans first; mutation requires explicit `--apply`. Re-running a successful subcommand is a no-op.
- **Conflict resolution surfaces, never guesses.** When desired and actual state diverge, the skill presents the diff and the resolution choices (reconcile / overwrite / abort) — it does not silently overwrite.
- **Archives at `~/.hermes/archive/a365/<slug>/<timestamp>/`** for any local files removed by `cleanup` (unless `--no-archive`).
- **Validator-of-record:** the Hermes harness' bundled skill-authoring guide. Lint `SKILL.md` against it before contributing upstream.
- **Stack-agnostic boundary.** The skill talks to A365 via the official `a365` CLI and to Hermes via the harness — nothing else. No dependency on or knowledge of the other agent stacks A365 supports (Microsoft Agent Framework, OpenAI Agents SDK, Microsoft 365 Agents SDK).
- **Vendor-agnostic.** The activity bridge does not encode which model provider the Hermes agent uses; that's Hermes' concern.

---

## 13. Versioning & rollout

- **v0.1.0** — single-tenant, single-agent, channels limited to Teams + Outlook + M365 Copilot. Macos + Linux only. Behind an explicit `hermes skills install hermes-a365 --acknowledge-experimental` flag.
- **v0.2.0** — multi-tenant, Windows host support, MCP-server export.
- **v1.0.0** — drop the experimental flag once §9 acceptance is green for ≥30 days on the user's reference tenant.

---

## 14. Out-of-scope (explicit)

- The `Microsoft Agent Framework` (Semantic Kernel + AutoGen merger). Distinct product, distinct skill if/when needed.
- Tenant-wide license purchasing automation.
- Adaptive Card design tooling.
- Bot Framework deployments outside A365 (e.g., direct to Azure Bot Service).
- Power Platform connectors.
- Microsoft Graph access *outside* what A365 mediates — a separate `hermes-msgraph` skill should cover that.
- Anything that mutates other users' agents in the tenant.

---

*End of spec v1 draft. Resolve §10 open questions, then proceed to implementation.*
