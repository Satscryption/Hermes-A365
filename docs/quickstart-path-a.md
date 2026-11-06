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
