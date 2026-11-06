# Path A quickstart (AI Teammate) — round C1.1

Path A covers **AI Teammate**: an M365 agentic directory identity and Teams 1:1 chat (`manifestVersion: devPreview` from GA `a365 publish` — see [`references/v1-compatibility-matrix.md`](../references/v1-compatibility-matrix.md)). This document is **round C1.1** only: prerequisites, install/preflight, blueprint registration, and admin consent. Later steps (local instance, publish, tunnel, Teams verification) are **C1.2**; the full numbered runbook remains [`references/live-tenant-test.md`](../references/live-tenant-test.md). Hermes product contract (CLI, env, HTTP): [`docs/SURFACE.md`](SURFACE.md).

## Prerequisites and operator inputs

Supported Python, OS (macOS/Linux; Windows is not supported for v1 end-to-end flows), Hermes gateway floors, and Microsoft tooling pins are in [`references/v1-compatibility-matrix.md`](../references/v1-compatibility-matrix.md) (see **Python**, **Host operating systems**, **Hermes Agent**, **Microsoft operator tooling**). Combined-environment install details: [`references/hermes-runtime-compatibility.md`](../references/hermes-runtime-compatibility.md).

You also need:

- An M365 tenant where you hold **Global Administrator** or **Agent Administrator**, enrolled in **Frontier Preview**, with an appropriate Agent 365 license — see [`references/license-cost-table.md`](../references/license-cost-table.md) for SKU names.
- `a365` (GA `Microsoft.Agents.A365.DevTools.Cli`), `az` (signed in to the target tenant), `pwsh` 7+, and a supported OS keychain backend — summarized in [`SKILL.md`](../SKILL.md) → **Prerequisites** and in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **What you need before starting**.
- A Hermes install with `hermes-a365[bridge]` in the Hermes venv so `agent365` loads via `hermes_agent.plugins` — see [`README.md`](../README.md) → **Install** (gateway plugin path).

Define these placeholders before you run commands (same names as the runbook table in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **What you need before starting**):

| Placeholder | Example | Role |
| --- | --- | --- |
| `<tenant-id>` | `<guid>` | Entra tenant id (pin for `register --apply`) |
| `<display-name>` | `<short agent name>` | `hermes a365 register --agent-name` (CLI derives Identity/Blueprint display names) |
| `<slug>` | `<lowercase-slug>` | Local agent directory under `~/.hermes/agents/<slug>/` |

## Gateway mode, Hermes floors, and profile ownership

Hermes gateway reads `gateway.multiplex_profiles` from Hermes config (see [`references/v1-compatibility-matrix.md`](../references/v1-compatibility-matrix.md) → **Hermes Agent (gateway plugin)**):

- **`gateway.multiplex_profiles: false` is the single-agent default.** A single-agent operator does **not** need to enable multiplex.
- **`gateway.multiplex_profiles: true`** turns on multiplex mode. Its minimum Hermes versions are calendar tag **`v2026.6.19`** and Hermes Agent **`0.17.0`**. Keep this separate from the **plugin-only** baseline **`v2026.4.30` / `0.12.0`** on the same matrix table.
- **Hermes owns** the profile **runtime**, **config**, **skills**, **memory**, and **secrets**. The Hermes-A365 bridge consumes the **selected** Hermes profile; profile ownership is **not** transferred to Hermes-A365.

For single-agent Path A, stay on the default: **do not** set multiplex to `true` unless you are deliberately running multiplex mode and meet the multiplex floor above.

## Install and preflight

1. **Install the package** into the active Hermes Python environment (see [`README.md`](../README.md) → **Install** — `pipx` for standalone CLI, or `uv pip install 'hermes-a365[bridge]'` for the gateway plugin).
2. **Confirm discovery:** `hermes plugins list` should show `agent365` (entry-point discovery — [`SKILL.md`](../SKILL.md) → **Prerequisites**).
3. **Seed operator config:** copy `a365.config.json.example` to `a365.config.json` in your working directory (`a365.config.json` fields are summarized in [`docs/SURFACE.md`](SURFACE.md) → **`A365Config` fields**).
4. **Optional operator env shell:** create or edit `~/.hermes/.env` with `A365_TENANT_ID=<tenant-id>` before first apply (bootstrap pattern in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **0. Local bootstrap**).
5. Run **`hermes a365 doctor --human`** — expect exit `0` (probes described in [`SKILL.md`](../SKILL.md) → **`hermes a365 doctor`**).
6. Run **`hermes a365 license --users <n> --agents <n> --plan <SKU>`** if you want a read-only license recommendation (`A365_LICENSE_MODEL` — [`SKILL.md`](../SKILL.md) → **`hermes a365 license`**).
7. Run the upstream prereq check (installs PS modules / fixes client-app wiring as needed): `yes y | a365 setup requirements` — pass criteria in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **1. doctor**.

This preflight is written so the **single-agent** path works with **`gateway.multiplex_profiles` left at the default (`false`)** — you do not need multiplex for the steps above.

## Register blueprint and grant consent

Registration orchestrates `a365 setup blueprint`, then MCP and Bot permission steps — behaviour and flags in [`SKILL.md`](../SKILL.md) → **`hermes a365 register`** (see also [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **3. register**).

**Plan (dry-run):**

```bash
hermes a365 register \
  --agent-name "<display-name>" \
  --tenant-id "<tenant-id>" \
  --m365
```

**Apply:** `--tenant-id` (or `A365_TENANT_ID`) is required before `--apply`; the wrapper checks `az account show` against that pin only ([`SKILL.md`](../SKILL.md) → **`hermes a365 register`**). Forward `--m365` to blueprint setup; keep `--auto-recover-secret` on live tenants while the GA CLI secret-persistence gap is open — [`SKILL.md`](../SKILL.md) → **`hermes a365 register`**.

```bash
hermes a365 register \
  --agent-name "<display-name>" \
  --tenant-id "<tenant-id>" \
  --m365 \
  --auto-recover-secret \
  --apply
```

After `setup blueprint`, the CLI writes `a365.generated.config.json` (gitignored) with the blueprint **app id** and **client credential** material, and updates `a365.config.json` with derived display names. **Before** the MCP / Bot permission steps run, copy the fresh blueprint client credential into `A365_BLUEPRINT_CLIENT_SECRET` in `~/.hermes/.env` if your gateway workflow needs it — ordering and handoff in [`SKILL.md`](../SKILL.md) → **`hermes a365 register`** and checklist bullets in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **3. register**.

If `register` reports **deferred admin consent** (`AADSTS90094` path), run:

```bash
hermes a365 consent "<display-name>"
```

…or poll without opening a browser (`--no-open`) — see [`SKILL.md`](../SKILL.md) → **`hermes a365 consent`** and [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **4. consent**.

**C1.1 exit criteria:** `register --apply` completes the blueprint + permission plan, `a365.generated.config.json` exists (and is gitignored), and `consent` exits `0` once scopes show as granted — detailed checkboxes in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **3. register** and **4. consent**. **Next:** instance/runtime, publish, tunnel, and Teams verification are **C1.2** (same runbook file, later sections).

Round **C1.2** continues this file below with those steps.

## Create local instance and configure runtime (C1.2)

`instance create` is **local file generation only** — it writes `~/.hermes/agents/<slug>/.env` with slug, owner metadata, and inherited tenant/app ids; it does **not** create the cloud agentic user (that comes from AI Teammate upload/activation). Behaviour and pitfalls: [`SKILL.md`](../SKILL.md) → **`hermes a365 instance create`**, checklist: [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **5. instance**.

**Placeholders (add to your table):** `<owner-email>` (M365 user who will own the instance) and `<owner-aad-id>` (that user's Entra object id).

```bash
hermes a365 instance create <slug> \
  --owner "<owner-email>" \
  --owner-aad-id "<owner-aad-id>" \
  --apply
```

**Expected:** `~/.hermes/agents/<slug>/.env` exists with `AA_INSTANCE_ID`; blueprint **client secret** is **not** in that file — see the grep check in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **5. instance**.

Configure the gateway plugin and sender policy next (wizard + env):

1. Run **`hermes gateway setup --platform agent365`** — patches `~/.hermes/.env` and `~/.hermes/config.yaml` (`gateway.platforms.agent365`), see [`README.md`](../README.md) → **Operator setup**.
2. In **`~/.hermes/.env`**, set either `A365_ALLOW_ALL_USERS=true` (dev) or `A365_ALLOWED_USERS=<csv>` (restricted), matching [`README.md`](../README.md) → **Operator setup** — the profile `.env` is the sender allowlist for the gateway.
3. Start from the default single-agent posture: leave **`gateway.multiplex_profiles` unset / `false`** unless you are deliberately on multiplex (floors in [Gateway mode, Hermes floors, and profile ownership](#gateway-mode-hermes-floors-and-profile-ownership) and [`references/v1-compatibility-matrix.md`](../references/v1-compatibility-matrix.md)).

**C1.2 checkpoint:** wizard completes, `hermes a365 doctor --human` still exits `0`, and `hermes a365 activity-bridge verify --slug <slug>` is ready to run before you expose `/api/messages` (verify contract: [`docs/SURFACE.md`](SURFACE.md) → **CLI** / adapter docs).

## Publish AI Teammate package and activate in admin centre (C1.2)

Path A needs **`--aiteammate`** so `publish` emits the **AI Teammate** zip for M365 Admin Centre upload — not blueprint-only Graph registration (wrapper behaviour: [`SKILL.md`](../SKILL.md) → **`hermes a365 publish`**).

```bash
hermes a365 publish \
  --agent-name "<display-name>" \
  --tenant-id "<tenant-id>" \
  --aiteammate \
  --apply
```

**Expected:** the command prints a **manifest zip path**; the file exists on disk — acceptance bullets in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **6. publish**.

**Operator upload and activation** (UI steps, not CLI): sign in to **M365 Admin Centre**, upload the zip under the Agents / Integrated apps flow, wait for propagation, then **activate** the agent for target users so the agentic Entra user binds — ordered checklist in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **7. Operator step — Admin Centre (AI Teammate flow only)**.

> **Ordering note:** if `publish --apply` runs in the same session as `register --apply`, re-check `agentBlueprintClientSecret` in `a365.generated.config.json` — GA `a365 publish` can clobber local fields (warning in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **6. publish**).

## Start gateway, tunnel, and messaging endpoint (C1.2)

Microsoft delivers inbound activities to whatever **public HTTPS URL** you register for `/api/messages`. Pick any tunnel or reverse proxy that meets the HTTPS / reachability constraints in [`references/exposing-the-bot-endpoint.md`](../references/exposing-the-bot-endpoint.md) → **What Microsoft requires** (concrete options in the same file).

**Typical layout (two terminals):**

1. **Gateway —** load profile env, then run the gateway (health + adapter connect pattern in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **9d.3 — Start the Hermes gateway**):

   ```bash
   set -a
   . ~/.hermes/.env
   . ~/.hermes/agents/<slug>/.env
   set +a
   hermes gateway run
   # or: hermes gateway run --profile <slug>  # see runbook §9d.3 if the profile is refused
   ```

   **Sanity:** `curl -fsS http://127.0.0.1:3978/healthz` returns JSON with `"ok": true` (same section).

2. **Tunnel —** publish `http://localhost:3978` (plugin port / `extra.port`) to a public `https://<tunnel-host>/…` URL using your chosen tooling — examples start at [`references/exposing-the-bot-endpoint.md`](../references/exposing-the-bot-endpoint.md) → **Options**.

3. **Messaging endpoint —** point MCP Platform at the tunnel URL (wrapper drives `a365 setup blueprint --m365 --update-endpoint`):

   ```bash
   hermes a365 activity-bridge update-endpoint \
     --agent-name "<display-name>" \
     --url "https://<tunnel-host>/api/messages" \
     --apply
   ```

**Expected:** `update-endpoint --apply` completes; the gateway log shows the adapter **connected** and `/api/messages` receives traffic when Teams delivers an activity — extended gates in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **9d.4** and **9d.5**.

## Verify Teams 1:1 reply (C1.2)

In **Microsoft Teams**, open a **1:1 chat** with the agent (search for `<display-name>`), send a **distinctive test prompt**, and confirm a **visible assistant reply in the thread**.

- Treat **UI evidence** (message bubble content) as the primary pass/fail signal.
- Gateway / Hermes logs (for example `Agent365Adapter.handle_message` lines) are **diagnostics** — useful, but not a substitute for seeing the reply in Teams — see acceptance bullets in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **9d.5 — Drive a Teams turn through the agent loop**.

## Stop, optional cleanup, further reading (C1.2)

**Stop runtime only (leave tenant registrations for another pass):** [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **9d.7 — Tear down** (stop gateway, stop tunnel, optional local artefact cleanup).

**Full tenant cleanup (includes `cleanup` plans and confirmation gates):** [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **10. cleanup** — read the dry-run plan before **`--apply`**.

**Migration / support contract pointer (hand-edited surfaces):** [`docs/SURFACE.md`](SURFACE.md) → **Migrations** (D6 additive auto-stamps; read-only in this round).
