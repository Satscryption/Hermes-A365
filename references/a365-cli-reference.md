# A365 CLI reference

Snapshot date: 2026-05-05 (verified against installed CLI v1.1.171);
issue #35 addendum on 2026-05-15 re-checks the #408
secret-persistence regression against CLI 1.1.181.

The CLI is **`Microsoft.Agents.A365.DevTools.Cli`** (binary name `a365`),
distributed as a .NET tool from NuGet:

```
dotnet tool install -g Microsoft.Agents.A365.DevTools.Cli --prerelease
```

Verified GA version: **1.1.171** (`a365 --version` output:
`1.1.171+11c378141d`). Latest live-verified affected build for macOS /
Linux setup flows: **1.1.181**. Versions 1.1.171, 1.1.174, and 1.1.181
reproduce Microsoft#408 (`agentBlueprintClientSecret` persists as
`null` after a successful `setup blueprint`); wrapper recovery remains
available via `register --auto-recover-secret`.

macOS dotnet-host gotcha: `brew install dotnet` doesn't set
`DOTNET_ROOT`, so the freshly-installed `a365` errors with
"You must install .NET to run this application." Fix by exporting
`DOTNET_ROOT="$(brew --prefix dotnet)/libexec"` (and adding
`~/.dotnet/tools` to `PATH` so the binary resolves).

- Source: <https://github.com/microsoft/Agent365-devTools>
- NuGet: <https://www.nuget.org/packages/Microsoft.Agents.A365.DevTools.Cli>
- Docs: <https://learn.microsoft.com/en-us/microsoft-agent-365/developer/agent-365-cli>

Verified version: **1.1.171** (commit `11c378141d`). Latest
live-verified affected version for the secret-persistence regression:
**1.1.181**. No fixed-version floor is currently live-verified.

There is **no npm variant**. What lives on npm under the
`@microsoft/agents-a365-*` namespace is the **runtime SDK**, not a CLI:
`@microsoft/agents-a365-runtime`, `-tooling`, `-notifications`, plus
framework extensions for OpenAI / LangChain / Claude. The .NET CLI is the
single CLI-shaped surface Microsoft publishes.

---

## ⚠️ Spec drift discovered 2026-05-04

The v0.1 design draft (now at
[`docs/historical/SPEC-v0.1-draft.md`](../docs/historical/SPEC-v0.1-draft.md),
drafted 2026-05-03 from public documentation that pre-dated the GA
release) assumed a CLI command surface that **does not match the GA
reality**. Major divergences:

| What the v0.1 draft / scripts/ assumed | What the CLI actually exposes |
|---|---|
| Two CLI variants: `a365` (.NET) + `atk` (npm) | One CLI: `a365` (.NET only). No npm equivalent. |
| `setup app --tier=1`, `setup app --tier=2` | No tier split. `setup blueprint` creates a single Entra app for the blueprint; the agent **identity** is auto-created server-side. |
| `setup blueprint --file=<JSON>` | `setup blueprint` reads from `a365.config.json` / `ToolingManifest.json` (or `--agent-name` for config-less mode), **not** an arbitrary JSON file. |
| `fic configure --app=<id>` | No `fic` subcommand. FIC is configured implicitly during `setup permissions {mcp,bot}`. |
| `fic rotate --app=<id>` | No CLI command for rotation. Likely SDK-only or admin-portal-only. |
| `create-instance --blueprint=<slug> --instance=<id>` | No such command. Instance lifecycle is part of `setup all` / `publish`. |
| `deploy --instance=<id> --channels=teams,outlook,m365copilot` | No standalone `deploy` command at the top level. Channel deployment is **out of CLI scope** — the operator runs `publish` to produce a package and uploads it to the M365 Admin Center manually. The `--m365` flag at `setup blueprint` time registers the messaging endpoint via MCP Platform. |
| `cleanup deployment --instance=<id>`, `cleanup app --app=<id>` | No `deployment` or `app` cleanup kinds. Real subs: `cleanup blueprint`, `cleanup instance`, `cleanup azure`. Bare `cleanup` removes ALL agent resources (blueprint + instance + Azure) in one shot. |
| `query-entra --by-name`, `--by-app-id`, `--license`, `--telemetry`, `--instance-channel`, `--scopes`, `--fic`, `--blueprint`, `--instance`, `--consent-status` | None of these flags exist. The full `query-entra` surface is two subcommands: `blueprint-scopes` and `instance-scopes`. |
| Operator runs the skill, the skill registers the Entra apps for them | **Reverse:** operator must pre-register a custom Entra client app named "Agent 365 CLI" (with delegated Graph permissions and admin consent) **before** the CLI works. The CLI then runs *as* that app. See [Custom Client App Registration](https://learn.microsoft.com/microsoft-agent-365/developer/custom-client-app-registration). |

Implication: the v0.1 `Mutator` protocol and most of the v0.1 planner /
applier scripts targeted subcommands that don't exist. The v0.2 rebuild
shipped in this repo drives the real CLI; see
[`docs/historical/SPEC-v0.1-draft.md`](../docs/historical/SPEC-v0.1-draft.md)
`## 14.1 Risks` row "Hermes harness API drift" for the change-management
posture that anticipated this scenario.

---

## Real top-level commands (verified 2026-05-04, v1.1.171)

| Command | Purpose |
|---|---|
| `setup` | Bootstrap (granular per-step or `setup all`). |
| `publish` | Update manifest IDs, package the manifest for upload to the M365 Admin Center. |
| `query-entra` | Read scopes / consent status for blueprint or instance. |
| `cleanup` | Tear down blueprint / instance / Azure resources. |
| `develop` | Manage MCP tool servers in the local agent dev workflow. |
| `develop-mcp` | Manage MCP servers in Dataverse environments. |

There is **no** standalone `deploy`, `register`, `create-instance`, or
`fic` command at the top level despite README mentions in some Microsoft
docs.

## `setup` subcommands

| Subcommand | Purpose | Min permissions |
|---|---|---|
| `setup requirements` | Validate prerequisites (PowerShell 7+, Frontier Preview enrollment, custom client app, Azure context). Read-only. | Sign-in only. |
| `setup blueprint` | Create the agent blueprint — Entra app registration tied to the blueprint identity. | Agent ID Developer role. |
| `setup permissions mcp` | Configure MCP server OAuth2 grants + inheritable permissions. | Global Administrator. |
| `setup permissions bot` | Configure Messaging Bot API OAuth2 grants. | Global Administrator. |
| `setup permissions custom` | Configure custom resource OAuth2 grants. | Global Administrator. |
| `setup permissions copilotstudio` | Configure Power Platform CopilotStudio.Copilots.Invoke. | Global Administrator. |
| `setup all` | Run blueprint + permissions + endpoint in one shot. | Global Administrator (covers all sub-steps). |

Common flags across `setup *`:

- `-n / --agent-name <name>` — base name; derives `<name> Identity`,
  `<name> Blueprint`. Auto-detects tenant from `az account show`.
- `--tenant-id <tenant>` — override auto-detection.
- `--dry-run` — show what would happen without executing.
- `-v / --verbose` — verbose logging.
- `--m365` — treat the agent as an M365 agent (registers messaging
  endpoint via MCP Platform). Default off.
- `--skip-requirements` — skip prerequisites validation (use with care).

`setup all` also accepts `--aiteammate` (AI Teammate vs blueprint-only)
and `--authmode <obo|s2s|both>` (auth pattern for the agent identity).

## `query-entra` subcommands

| Subcommand | Purpose |
|---|---|
| `query-entra blueprint-scopes` | List configured scopes + consent status for the agent blueprint. |
| `query-entra instance-scopes` | List configured scopes + consent status for the agent instance. |

Common flags: `-n / --agent-name`, `--tenant-id`, `-v / --verbose`.

## `cleanup` subcommands

| Subcommand | Purpose |
|---|---|
| `cleanup` (bare) | Remove ALL: blueprint, instance, Azure resources. |
| `cleanup blueprint` | Remove Entra ID blueprint application + service principal. |
| `cleanup instance` | Remove agent instance identity + user from Entra ID. |
| `cleanup azure` | Remove Azure resources (App Service, App Service Plan). |

Common flags: `-n / --agent-name`, `--tenant-id`, `--dry-run`,
`-v / --verbose`, `-y / --yes` (skip confirmation).

`cleanup blueprint` extras: `--endpoint-only` (drop messaging endpoint
only), `--m365` (clear endpoint from Teams Graph via MCP Platform —
only meaningful with `--endpoint-only`).

## `publish`

Updates manifest IDs and produces a package zip the operator uploads to
the M365 Admin Center. Flags: `-n / --agent-name`, `--tenant-id`,
`--dry-run`, `--aiteammate`, `--use-blueprint` (blueprint-based non-DW
flow), `-v / --verbose`.

## `develop` subcommands (MCP tool servers, local dev)

`list-available`, `list-configured`, `add-mcp-servers <list>`,
`remove-mcp-servers <list>`, `get-token`, `add-permissions`,
`mts` / `start-mock-tooling-server`.

## `develop-mcp` subcommands (Dataverse-hosted MCP)

`list-environments`, `list-servers`, `publish`, `unpublish`, `approve`,
`block`, `package-mcp-server`, `register-external-mcp-server`.

---

## Hard prerequisites the CLI checks for (verified via `setup requirements`)

1. **PowerShell 7+ on PATH** as `pwsh`. The CLI shells out for several
   operations. macOS install: `brew install powershell` (the formula).
   The older `brew install --cask powershell` recipe is deprecated as
   of 2026-05 — the cask was renamed to `powershell@preview` and
   marked as failing macOS Gatekeeper; use the formula instead.
2. **Tenant enrolled in the Microsoft Frontier Preview Program.** The
   CLI cannot verify automatically and only warns; setup will fail at
   later steps if not enrolled. Enrollment URL:
   <https://adoption.microsoft.com/copilot/frontier-program/>
3. **Custom Entra client app** named `Agent 365 CLI` (or whatever the
   operator names it) with delegated Microsoft Graph permissions and
   admin-consent granted. The CLI runs *as* this app via interactive
   delegated auth (browser or device-code on platforms without WAM).
4. **`az login`** with an account that has at least Agent ID Developer
   role; Global Administrator is needed for the `setup permissions *`
   steps and for `setup all`.
5. **Azure subscription** with Contributor role for infrastructure
   provisioning steps in `setup all`.

## Auth model

Interactive delegated auth via MSAL. On macOS (verified on macOS 26.4.1
during this snapshot), browser-based WAM auth is **not supported** —
the CLI falls back to device-code flow and prints a code + URL the
operator enters at <https://login.microsoft.com/device>.

Token cache: persistent MSAL cache at
`~/Library/Application Support/Microsoft.Agents.A365.DevTools.Cli/`.
On macOS the persistent cache via Keychain currently fails to register
("Failed to register persistent token cache"); the CLI warns that
"authentication prompts may be repeated."

## Config

The CLI prefers an `a365.config.json` next to the operator's working
directory (and `ToolingManifest.json` for MCP server bindings). When
omitted, `--agent-name` is the universal handle; tenant is detected via
`az account show`.

## What this skill currently doesn't drive

Given the divergence above, the v0.1 skill scripts in this repo do not
yet map to the real CLI. The Mutator protocol needs to be redesigned
around the actual command set. Until that lands, treat the skill as a
**design artefact + planner architecture** that needs the apply-side
re-implemented; the unit tests, reconcilers, status report, doctor,
and local artefact handling all survive the redesign.

Live SKU naming spotted in the user's tenant during the same probe:
`MICROSOFT_AGENT_365_TIER_3` — the SKU part number, distinct from the
"Agent 365 add-on" / "M365 E7" labels we recorded in
[`license-cost-table.md`](license-cost-table.md). That file should be
updated to use the real SKU part numbers.
