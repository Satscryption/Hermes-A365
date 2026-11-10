# Path B quickstart (Custom Engine Agent)

Path B covers **Custom Engine Agent** surfacing in **M365 Copilot Chat** (and related Copilot fabric entry points) via Azure Bot Service — not the Path A AI Teammate admin-centre upload/activation flow. This document is the complete Path B quickstart (**C2.1 and C2.2**): shared blueprint and consent, a **separate non-agentic Bot Framework identity**, gateway and tunnel, Bot Service provisioning, `publish --copilot-chat`, MAC upload, and Copilot Chat reply verification. Path B does **not** require Path A `publish --aiteammate` or admin-centre activation. The full numbered runbook remains [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11**. Product contract (CLI, env, HTTP): [`docs/SURFACE.md`](SURFACE.md).

Path B does **not** ask you to complete Path A publish or activation; it **does** use the same agentic **blueprint** Entra app from `register --m365` as Path A's messaging stack. See [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.0** for the canonical shape.

## Prerequisites and operator inputs

Supported Python, OS (macOS/Linux; Windows is not supported for v1 end-to-end flows), Hermes gateway floors, and Microsoft tooling pins are in [`references/v1-compatibility-matrix.md`](../references/v1-compatibility-matrix.md) (see **Python**, **Host operating systems**, **Hermes Agent**, **Microsoft operator tooling**). Combined-environment install: [`references/hermes-runtime-compatibility.md`](../references/hermes-runtime-compatibility.md).

**M365 / Entra (same as Path A walks):**

- An M365 tenant where you hold **Global Administrator** or **Agent Administrator**, enrolled in **Frontier Preview**, with an appropriate Agent 365 license — SKU notes in [`references/license-cost-table.md`](../references/license-cost-table.md) and [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **What you need before starting**.
- `a365` (GA `Microsoft.Agents.A365.DevTools.Cli`), `az` (signed in to the target tenant), `pwsh` 7+, and a supported OS keychain backend — summarized in [`SKILL.md`](../SKILL.md) → **Prerequisites**.
- An existing tenant Entra application with display name exactly **`Agent 365 CLI`** — [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **What you need before starting**; [`SKILL.md`](../SKILL.md) → **Prerequisites**. Doctor verifies discoverability via `az`; it does **not** create this app.
- A Hermes install with `hermes-a365[bridge]` in the Hermes venv so `agent365` loads via `hermes_agent.plugins` — [`README.md`](../README.md) → **Install** (gateway plugin path).

**Path B additive (Azure):**

- An **Azure subscription** in the **same Entra tenant** as the M365 tenant (F0 Bot Service SKU is sufficient for registration-only shapes).
- **Contributor** (or higher) on the subscription or the resource group you plan to use — a **working assumption**; the minimum-sufficient Azure role is **not confirmed** ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **11.2 — Prerequisites**).
- **`Microsoft.BotService` resource provider registered** on that subscription (`az provider register --namespace Microsoft.BotService --wait`) — probe details in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **11.2 — Prerequisites (additive to Path A)**.

Define placeholders before you run commands (align with the runbook tables in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **What you need before starting** and **§11.2**):

| Placeholder | Example | Role |
| --- | --- | --- |
| `<tenant-id>` | `<guid>` | Entra tenant id (pin for `register --apply`) |
| `<display-name>` | `<short agent name>` | `hermes a365 register --agent-name` (CLI derives Identity/Blueprint display names) |
| `<slug>` | `<lowercase-slug>` | Local agent directory under `~/.hermes/agents/<slug>/` |
| `<owner-email>` | `<user@example.com>` | Test user / instance owner |
| `<owner-aad-id>` | `<guid>` | That user's Entra object id |
| `<subscription-id>` | `<guid>` | Azure subscription for this preflight (`az account set`) and Bot Service in C2.2 |
| `<azure-rg>` | `<rg-name>` | Resource group name (C2.2) |
| `<azure-bot-name>` | `<bot-name>` | Azure Bot resource name (4–42 chars; see runbook **§11.2**) |
| `<bf-app-id>` | `<guid>` | Separate non-agentic Entra app for Bot Framework S2S |
| `<tunnel-url>` | `https://<tunnel-host>` | Public HTTPS base for `/api/messages` (record before `bot-service create`) |

## Gateway mode, Hermes floors, and profile ownership

Hermes gateway reads `gateway.multiplex_profiles` from Hermes config (see [`references/v1-compatibility-matrix.md`](../references/v1-compatibility-matrix.md) → **Hermes Agent (gateway plugin)**):

- **`gateway.multiplex_profiles: false` is the single-agent default.** A single-agent operator does **not** need to enable multiplex.
- **`gateway.multiplex_profiles: true`** turns on multiplex mode. Its minimum Hermes versions are calendar tag **`v2026.6.19`** and Hermes Agent **`0.17.0`**. Keep this separate from the **plugin-only** baseline **`v2026.4.30` / `0.12.0`** on the same matrix table.
- **Hermes owns** the profile **runtime**, **config**, **skills**, **memory**, and **secrets**. The Hermes-A365 bridge consumes the **selected** Hermes profile; profile ownership is **not** transferred to Hermes-A365.

For single-agent Path B, stay on the default: **do not** set multiplex to `true` unless you are deliberately running multiplex mode and meet the multiplex floor above.

## Install and preflight

1. **Install the package** into the active Hermes Python environment ([`README.md`](../README.md) → **Install** — `pipx` for standalone CLI, or `uv pip install 'hermes-a365[bridge]'` for the gateway plugin).
2. **Confirm discovery:** `hermes plugins list` should show `agent365` ([`SKILL.md`](../SKILL.md) → **Prerequisites**).
3. **Seed operator config:** copy `a365.config.json.example` to `a365.config.json` in your working directory (`A365Config` field summary: [`docs/SURFACE.md`](SURFACE.md)).
4. **Optional for registration only:** you may seed `A365_TENANT_ID=<tenant-id>` in `~/.hermes/.env` before first apply because `register` below passes `--tenant-id` ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **0. Local bootstrap**). That optionality does **not** make the parent `~/.hermes/.env` optional for `hermes a365 consent` or `instance create` — both required parent keys are documented before consent.
5. Run **`hermes a365 doctor --human`** — exit `0` means OK, `1` means warnings, `2` means errors. The installed `a365` CLI probe currently always warns, so expect exit `1` when no errors are present; resolve errors before continuing ([`SKILL.md`](../SKILL.md) → **`hermes a365 doctor`**).
6. **`az account set --subscription <subscription-id>`** and confirm tenant alignment: `az account show --query "{tenantId:tenantId, name:name}" -o table` ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.2**).
7. Run the upstream prereq check: `yes y | a365 setup requirements` — pass criteria in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **1. doctor**.

This preflight is written so the **single-agent** path works with **`gateway.multiplex_profiles` left at the default (`false`)** — you do not need multiplex for the steps above.

## Register shared blueprint and grant consent

Path B shares Path A's **agentic blueprint** registration: orchestrate `a365 setup blueprint` plus MCP/Bot permission steps ([`SKILL.md`](../SKILL.md) → **`hermes a365 register`**; runbook **§3** in [`references/live-tenant-test.md`](../references/live-tenant-test.md)).

**Plan (dry-run):**

```bash
hermes a365 register \
  --agent-name "<display-name>" \
  --tenant-id "<tenant-id>" \
  --m365
```

**Blueprint client secret (required):** After successful `a365 setup blueprint`, copy `agentBlueprintClientSecret` into `A365_BLUEPRINT_CLIENT_SECRET` in the selected profile's `~/.hermes/.env` **before** `setup permissions mcp` / `bot` (D24). Follow [`README.md`](../README.md) → **Security model** (`README.md:228–236`) (through the `setup permissions` clobber note) and [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **3. register** (through the copy-before-permissions warning).

`register --apply` runs blueprint and permission steps together with **no documented pause** for that intervening copy ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → composite apply). On a fresh composite run, if the generated credential is missing or empty, complete the documented recovery, then copy the **nonempty** resulting value into the profile env **before continuing** — a recovery flag, generated-file presence, or keychain-only storage does **not** satisfy the profile-env handoff. Verify saved `A365_BLUEPRINT_CLIENT_SECRET` at the checkpoint below (including after consent-deferred registration resumes). See [`README.md`](../README.md) → **Security model** (`README.md:228–236`) and runbook **§3**.

**Apply:** pin the tenant before `--apply`; forward `--m365`; keep `--auto-recover-secret` on live tenants while the GA CLI secret-persistence gap is open ([`SKILL.md`](../SKILL.md) → **`hermes a365 register`**).

```bash
hermes a365 register \
  --agent-name "<display-name>" \
  --tenant-id "<tenant-id>" \
  --m365 \
  --auto-recover-secret \
  --apply
```

## Parent `~/.hermes/.env` before consent and instance creation

Before `hermes a365 consent` or `instance create`, the selected profile's parent `~/.hermes/.env` must include nonempty `A365_TENANT_ID` (the same tenant pin used for registration) and `A365_APP_ID` copied from **`agentBlueprintId`** in `a365.generated.config.json` — **not** the separate BF app id. Set file mode `0600`. Registration writes its ownership anchor under `agents/<slug>/.env`, not in this parent file ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **0. Local bootstrap**).

```bash
# append or update ~/.hermes/.env (chmod 600)
echo "A365_TENANT_ID=<tenant-id>" >> ~/.hermes/.env
echo "A365_APP_ID=<agentBlueprintId-from-generated-config>" >> ~/.hermes/.env
chmod 600 ~/.hermes/.env
```

Complete this checkpoint **before** consent below; keep both parent identifiers and the blueprint secret above available **before** the BF steps and `instance create`.

If `register` reports **deferred admin consent**, run:

```bash
hermes a365 consent "<display-name>"
```

…or `hermes a365 consent "<display-name>" --no-open` ([`SKILL.md`](../SKILL.md) → **`hermes a365 consent`**; runbook **§4**).

**Checkpoint:** `register --apply` completes the blueprint + permission plan, `a365.generated.config.json` exists (gitignored), `A365_BLUEPRINT_CLIENT_SECRET` is saved in the profile `~/.hermes/.env`, and `consent` exits `0` once scopes are granted — checkboxes in runbook **§3** and **§4**. You have **not** run Path A `publish --aiteammate` or admin-centre activation; those are out of scope for Path B.

## Register Path B Bot Framework identity and local instance

The blueprint Entra app is **agentic** and cannot mint Bot Framework app-only tokens (`AADSTS82001`). Path B requires a **separate, non-agentic** Entra app as the bot identity (`A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET`). Full rationale and portal steps: [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.2.5**.

**Operator summary:**

1. **Entra portal → App registrations → New registration** — single-tenant service identity (no redirect URI). Example display name: `<display-name> Path B Identity`.
2. **Certificates & secrets → New client secret** — copy the secret **value** (placeholder `<secret-value>` below) into `~/.hermes/.env`:

   ```bash
   echo "A365_BF_APP_ID=<bf-app-id>" >> ~/.hermes/.env
   echo "A365_BF_CLIENT_SECRET=<secret-value>" >> ~/.hermes/.env
   chmod 600 ~/.hermes/.env
   ```

3. **API permissions** — grant **application** permission **`Bot.Connector`** (resource `8d2d3342-cf29-4959-9577-0e0eafbd16bc`) and **admin consent** ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.2.5** step 3).
4. **Propagate to the per-agent env** with `instance create` (local files only; does not create the cloud agentic user):

   ```bash
   hermes a365 instance create "<slug>" \
     --owner "<owner-email>" \
     --owner-aad-id "<owner-aad-id>" \
     --apply
   ```

   **Expected:** `~/.hermes/agents/<slug>/.env` includes `AA_INSTANCE_ID` and carries `A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET` from operator `~/.hermes/.env` ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **5. instance**, **§11.2.5** step 4).

5. **Configure gateway sender policy** before C2.2 exposes `/api/messages`: run **`hermes gateway setup --platform agent365`**, then set `A365_ALLOW_ALL_USERS=true` or `A365_ALLOWED_USERS=<csv>` in `~/.hermes/.env` ([`README.md`](../README.md) → **Operator setup**). Leave **`gateway.multiplex_profiles` at the default (`false`)** for single-agent Path B.

**C2.1 exit criteria:** blueprint + consent green; BF app registered with `Bot.Connector` consent; `A365_BLUEPRINT_CLIENT_SECRET` and both parent identifiers in profile env; `A365_BF_*` in operator and per-agent env; instance `.env` present; doctor shows no errors (warnings reviewed).

## Configure gateway and establish tunnel (C2.2)

Tunnel-first ordering is frozen: record a working **public HTTPS** URL for `/api/messages` **before** `bot-service create`. Reuse the same mode/floor/ownership rules as [Gateway mode, Hermes floors, and profile ownership](#gateway-mode-hermes-floors-and-profile-ownership). HTTPS and reachability constraints: [`references/exposing-the-bot-endpoint.md`](../references/exposing-the-bot-endpoint.md) → **What Microsoft requires** and **Options** (quick-tunnel examples in the runbook).

**Typical layout (two terminals):**

1. **Gateway —** load profile env, then start the gateway ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§9d.3**):

   ```bash
   set -a
   . ~/.hermes/.env
   . ~/.hermes/agents/<slug>/.env
   set +a
   hermes gateway run
   # or: hermes gateway run --profile <slug>  # if refused, see runbook §9d.3 (gateway.standalone)
   ```

   **Sanity:** `hermes gateway status` shows `agent365` connected; `curl -fsS http://127.0.0.1:3978/healthz` returns `"ok": true`.

2. **Tunnel —** publish `http://localhost:3978` to a public URL and note the base as `<tunnel-url>` (trailing path is `/api/messages`). Quick-tunnel example in [`references/exposing-the-bot-endpoint.md`](../references/exposing-the-bot-endpoint.md) → **Cloudflare quick tunnel**.

3. **Checkpoint:** you have a stable-enough URL for this session, gateway health is green, and you will pass **`https://<tunnel-host>/api/messages`** (or equivalent) to `bot-service create --endpoint` next. Do **not** run `bot-service create` until this URL is recorded.

## Provision Bot Service using the established endpoint (C2.2)

`bot-service create` requires **`--endpoint`** (wrapper appends `/api/messages` when omitted — see `src/hermes_a365/bot_service.py` parser). It binds the **BF app** (`<bf-app-id>` / `A365_BF_APP_ID`), not the blueprint app id.

```bash
hermes a365 bot-service create \
  --agent-name "<display-name>" \
  --resource-group "<azure-rg>" \
  --endpoint "<tunnel-url>/api/messages" \
  --tenant-id "<tenant-id>" \
  --subscription-id "<subscription-id>" \
  --appid "<bf-app-id>" \
  --bot-name "<azure-bot-name>" \
  --apply
```

Omitted `--appid`, `--tenant-id`, or `--subscription-id` are read from `~/.hermes/.env` and `az` context when set. The wrapper registers `Microsoft.BotService`, creates or reconciles the bot resource, enables the Microsoft Teams channel, and applies publishing terms — detail in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.3–§11.4**.

**Verify:**

```bash
hermes a365 bot-service verify --agent-name "<display-name>"
```

**Expected:** `properties.endpoint` matches your tunnel URL; `properties.msaAppId` is `<bf-app-id>`. Path A operators may also run `activity-bridge update-endpoint` for the MCP Platform messaging endpoint; that step is **optional for Path-B-only** Copilot Chat surfacing and is independent of ARM registration ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.0**, **§11.5**).

## Publish CEA package (C2.2)

Emit the Custom Engine Agent zip with **`--copilot-chat` only** (no `--aiteammate`) and **`--bot-id <bf-app-id>`** so `bots[]` references the Bot Service identity:

```bash
hermes a365 publish \
  --agent-name "<display-name>" \
  --tenant-id "<tenant-id>" \
  --copilot-chat \
  --bot-id "<bf-app-id>" \
  --apply
```

**Expected:** Path-B-only output at `<cwd>/manifest/manifest.zip`. For MAC **Update in store** later, keep a stable catalog `manifest.id`; reserve `--manifest-id auto` for combined Path A+B publishes. Version bump rules: [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.0**, **§11.6**.

## Upload in MAC and verify Copilot Chat (C2.2)

**Upload (operator UI):** [Microsoft Admin Centre](https://admin.microsoft.com) → **Agents** → **Upload custom agent** → select `manifest.zip` from the publish step. First test: scope **Just me** — walkthrough in [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.7**. **Re-uploads:** open the agent → **Update in store** (no assignment step on update).

**Verify Copilot Chat (UI evidence):** after propagation (~5–15 min), open [M365 Copilot Chat](https://m365.cloud.microsoft). Under **Agents**, find the agent under **Available to add** → **Add**, then send a **distinctive test prompt** and confirm a **visible assistant reply** in the thread. Cowork `@` mentions may show manifest text **without** calling the bot — use Copilot Chat for end-to-end routing ([`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.8**). Gateway logs (`inbound path=B`, `Agent365Adapter.handle_message`) are diagnostics, not a substitute for UI reply evidence.

## Endpoint changes; stop or clean up (C2.2)

**When the tunnel URL changes** (common with quick tunnels), update the Bot Service ARM endpoint; optionally update the MCP Platform messaging endpoint if you use Path A's `update-endpoint` path:

```bash
hermes a365 bot-service update-endpoint \
  --agent-name "<display-name>" \
  --url "<new-tunnel-url>/api/messages" \
  --apply

# Optional — MCP Platform (Path A messaging registration), not required for Path-B-only:
hermes a365 activity-bridge update-endpoint \
  --agent-name "<display-name>" \
  --url "<new-tunnel-url>/api/messages" \
  --apply
```

**Stop runtime:** stop the gateway process and tunnel in the terminals you started above.

**Path B Azure cleanup (leaves blueprint for reuse):** read dry-run output first — [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§11.9**:

```bash
hermes a365 bot-service cleanup \
  --agent-name "<display-name>" \
  --apply \
  --confirm "<display-name>"
```

**Sidecar / migration contract (read-only):** [`docs/SURFACE.md`](SURFACE.md) → **Sidecars** (D6). Full-tenant teardown with blueprint cleanup gates remains [`references/live-tenant-test.md`](../references/live-tenant-test.md) → **§10**.
