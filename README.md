# Hermes-A365

Integrate Hermes agents into the Microsoft 365 ecosystem using
**Microsoft Agent 365** (A365), the governance / identity / observability
control plane that GA'd 2026-05-01.

## Hermes-A365 vs sibling Hermes Teams plugins

Hermes ships its own **classic-Bot-Framework Microsoft Teams adapter**
(`plugins/platforms/teams/adapter.py`, shipped v2026.4.30; in-flight
work in [hermes-agent#10037](https://github.com/NousResearch/hermes-agent/pull/10037)
and [hermes-agent#13767](https://github.com/NousResearch/hermes-agent/pull/13767)).
**That is the right tool when you want Hermes as a generic Teams chat
bot** — DM, channels, group chats, threading, file attachments.
Setup: Azure App Registration + client secret / certificate / Managed
Identity + Teams app manifest with `bots[]`. No M365 tenant-directory
identity, no Copilot Chat surfacing.

**Hermes-A365 covers what classic Teams bots structurally can't:**

| Path | Surfaces it lights up | Operator prerequisites | Status |
|---|---|---|---|
| **A — AI Teammate** (M365 agentic user) | Hermes appears as a first-class agentic identity in your M365 tenant directory + "Built for your org" picker + M365 People search + agentic-user audit trails. Teams 1:1 chat with M365-native identity. | M365 tenant + Frontier Preview Program + Tier 3 license. **No Azure subscription.** | ✅ Validated round-8 end-to-end 2026-05-11 with streaming (v0.3.0) |
| **B — Custom Engine Agent** (Azure Bot Service + 1.21 manifest) | M365 Copilot Chat agents picker + side-panels in Word/Excel/PowerPoint/Outlook + Copilot-fabric search. Reaches classic Teams surfaces too as a side effect, but the sibling Teams adapter is the cleaner tool for those. | Path A's prerequisites **+ Azure subscription** + a **separate non-agentic Entra app** (`A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET`) for Bot Service S2S — the blueprint/agentic app can't mint BF tokens (`AADSTS82001`). | ✅ GA since v0.6.0 (2026-05-18); live Copilot Chat round-trip validated. Provision via the `bot-service` wrapper family (slice 20). |

Both paths share the same blueprint Entra app, service principal, and
bot endpoint, so an operator with both prerequisites can run both
surfaces from one Hermes-A365 install. They are not mutually
exclusive — and they are NOT in overlap with the sibling Teams
adapter.

**When to pick what:**

- *"Hermes is a chat bot in Teams (DM / channels / group / file
  uploads)"* → sibling Hermes Teams adapter (classic BF).
- *"Hermes is a first-class agentic user in our M365 tenant directory"* →
  Hermes-A365 Path A (AI Teammate).
- *"Hermes surfaces in M365 Copilot Chat and Copilot side-panels"* →
  Hermes-A365 Path B (Custom Engine Agent). Requires Azure
  subscription.
- *"All of the above"* → install both the sibling Teams adapter
  AND Hermes-A365; configure each for its lane.

See [`references/m365-surface-coverage.md`](references/m365-surface-coverage.md)
for the full surface-by-surface matrix and the architectural
reasoning behind the split.

## Status

**v0.7.2** (released 2026-05-29). Both paths are GA — Path A
(AI Teammate) and Path B (Copilot Chat) are live-validated
end-to-end against the satscryption tenant.

- **v0.7.2** (2026-05-29) — Copilot Chat reply-quality: non-personal
  turns coalesce into one rendered bubble (Copilot Chat doesn't render
  BF streaming), the duplicated agent-name lines are gone, and a
  stale-stream liveness guard protects the streaming path (closes
  [#54](https://github.com/satscryption/Hermes-A365/issues/54) /
  [#55](https://github.com/satscryption/Hermes-A365/issues/55) /
  [#62](https://github.com/satscryption/Hermes-A365/issues/62) /
  [#26](https://github.com/satscryption/Hermes-A365/issues/26) /
  [#38](https://github.com/satscryption/Hermes-A365/issues/38)).
- **v0.7.0 / v0.7.1** (2026-05-26) — the Path B `bot-service` wrapper
  family GA (`create` / `verify` / `update-endpoint` / `cleanup`,
  slice 20) plus operator-visible polish and docs corrections.
- **v0.6.0** (2026-05-18) — **Path B Copilot Chat GA**: inbound
  BF-shaped JWT validation ([#34](https://github.com/satscryption/Hermes-A365/issues/34)),
  a separate non-agentic Bot Framework app
  ([#36](https://github.com/satscryption/Hermes-A365/issues/36)), and
  the end-to-end live walk ([#16](https://github.com/satscryption/Hermes-A365/issues/16)).
  Closes the headline value-prop gap.
- **v0.5.0 / v0.5.1** (2026-05-13) — Path A proactive long-running
  reply pattern ([#4](https://github.com/satscryption/Hermes-A365/issues/4) /
  [#27](https://github.com/satscryption/Hermes-A365/issues/27)).
- **v0.3.0** (2026-05-11) — BF streaming-response protocol
  ([#3](https://github.com/satscryption/Hermes-A365/issues/3)).

**876 tests passing, ruff clean.** See [CHANGELOG.md](CHANGELOG.md)
for the full per-release notes.

Path A (AI Teammate) validated end-to-end against the satscryption
M365 tenant rounds 3 → 8, including full BF streaming protocol
round-trip on 2026-05-11 and the v0.5.0 proactive soak on 2026-05-13.

Path B (Custom Engine Agent) is GA since v0.6.0 — the live Copilot
Chat round-trip is validated, provisioned via the `bot-service`
wrapper family against a separate non-agentic Entra app. See the §11
runbook in [`references/live-tenant-test.md`](references/live-tenant-test.md).

## What works today

Lift of the per-surface matrix from
[`references/m365-surface-coverage.md`](references/m365-surface-coverage.md).
Legend: ✅ shipped + validated · 🟡 shipped, validation deferred ·
🔵 sibling-plugin lane · 🔴 out of scope · ⚪ non-surface.

| Surface | Best Hermes-stack path | Hermes-A365 coverage |
|---|---|---|
| **Teams 1:1 chat with M365 agentic identity** | Hermes-A365 Path A | ✅ round-8 E2E + streaming 2026-05-11 |
| Teams 1:1 chat (generic chat bot, no M365 identity) | Sibling Teams adapter | 🔵 use sibling |
| Teams group chat / channel / meetings | Sibling Teams adapter | 🔵 use sibling |
| Teams file attachments (image, PDF, DOCX, …) | Sibling Teams adapter (PR #13767) | 🔵 use sibling |
| **M365 Copilot Chat (standalone)** | Hermes-A365 Path B | ✅ GA since v0.6.0; provisioned via `bot-service` |
| Word / Excel / PowerPoint Copilot side-panels (Hermes as Copilot agent) | Hermes-A365 Path B | ✅ same Custom Engine Agent registration |
| Outlook — Copilot Chat side-panel inside Outlook | Hermes-A365 Path B | ✅ same |
| Microsoft Search invocation | Hermes-A365 Path B + #18 | 🟡 Path B GA; invoke handlers pending (#18) |
| Outlook compose-action (`task/fetch` / `task/submit`) | Path B (Copilot fabric) or sibling adapter | 🟡 / 🔵 |
| Teams compose extensions (`composeExtension/*`) | Sibling Teams adapter | 🔵 use sibling |
| Cron / proactive sends on M365 surfaces (Path A) | Hermes-A365 | ✅ shipped in v0.5.0 / v0.5.1 (slices 19x-a..e, #4 closed) |
| Word / Excel / PowerPoint as declarative Copilot agents | Separate skill | 🔴 different runtime |
| Office Add-ins / Loop / OneNote | Separate skills | 🔴 different SDKs |
| Web chat / Direct Line / SharePoint Embedded | Separate Direct Line skill | 🔴 bypasses M365 |
| Slack / Telegram / WhatsApp / etc. | Use Hermes' respective platform adapters | 🔵 use the dedicated adapter |

## Known limitations

v0.7.2 ships both paths GA — Path A (AI Teammate) and Path B
(Copilot Chat via the `bot-service` wrapper family), the BF activity
bridge + streaming protocol (with non-personal reply coalescing), the
hardened setup wizard (with XDG-symlink auto-repair), and the Path A
proactive long-running reply pattern. Outstanding gaps:

- **Path B (Copilot Chat) requires Azure + a separate Entra app.** The
  Custom Engine Agent surfaces in Copilot Chat via an Azure Bot Service
  registration, provisioned with `hermes a365 bot-service create
  --apply` (slice 20). It needs a **separate non-agentic Entra app**
  (`A365_BF_APP_ID` / `A365_BF_CLIENT_SECRET`) for Bot Framework S2S —
  the blueprint/agentic app can't mint BF tokens (`AADSTS82001`). This
  is a setup requirement, not a gap: Path B is GA and live-validated
  since v0.6.0. See the §11 runbook in
  [`references/live-tenant-test.md`](references/live-tenant-test.md).
- **Path B proactive sends** — Path B *replies* (responding to an
  inbound Copilot Chat message) are GA; agent-*initiated* proactive
  sends on a Path B target are not yet a validated path. The outbound
  token chain for Path B is BF S2S (`Bot.Connector` audience) vs
  Path A's agentic three-stage user-FIC.
- **Invoke activities (Path B)** — Outlook compose-action
  (`task/fetch` / `task/submit`), Microsoft Search invocation, and
  OAuth invoke (`signin/verifyState`) for tools inside Copilot Chat
  are tracked under [#18](https://github.com/satscryption/Hermes-A365/issues/18);
  umbrella not yet implemented. Teams compose-extension invokes
  (`composeExtension/*`) are sibling-plugin lane, not Hermes-A365's.
- **Plaintext on-disk secret on macOS / Linux.** DPAPI is Windows-only;
  on macOS / Linux the GA CLI writes the agent blueprint client secret
  to `a365.generated.config.json` in plaintext. See [Security model](#security-model).
- **macOS 26 device-code prompt volume.** On macOS 26.x the GA `a365`
  CLI falls back to device-code per Entra-side mutation, so `register
  --apply --m365` hits ~10–12 prompts instead of 1–2. Documented in
  `references/live-tenant-test.md` §3.
- **AI Teammate-flow `agentRegistry` entries cannot be deleted by
  operators** (only "blocked" via the M365 Admin Centre). Microsoft
  platform limitation, not a wrapper bug.
- **Pluggable secrets / activity-bridge library split / Work IQ V2
  amplifiers** — tracked as deferred (#19, #20, #21); architecturally
  sound, picked up when concrete operator demand surfaces.

Three issues filed upstream during the validation walkthroughs:
[microsoft/Agent365-devTools#402](https://github.com/microsoft/Agent365-devTools/issues/402)
(cosmetic logging — fixed in 1.1.174),
[microsoft/Agent365-devTools#408](https://github.com/microsoft/Agent365-devTools/issues/408)
(`agentBlueprintClientSecret` null-on-disk regression — wrapper-side
detection + `--auto-recover-secret` ships in this release), and
[NousResearch/hermes-agent#20133](https://github.com/NousResearch/hermes-agent/issues/20133)
(upstream skill-contribution check-in).

## Security model

**The agent blueprint client secret is the most sensitive artefact
this skill handles.** Where it lives + how to keep it that way:

- **Windows operators**: `a365 setup blueprint` writes the secret
  to `a365.generated.config.json` and protects it via DPAPI. The
  `agentBlueprintClientSecretProtected` flag in the file is `true`.
- **macOS / Linux operators**: DPAPI is Windows-only. The GA CLI
  writes the secret in plaintext (`agentBlueprintClientSecretProtected:
  false`). The wrapper tightens the file mode to `0600` after
  `register --apply`, and the keychain shim in `hermes_a365.keychain`
  mirrors the secret into the OS keychain (macOS Keychain or libsecret)
  when available.
- **Source control**: the `.gitignore` blocks `a365.config.json*`,
  `*.generated.config.json*`, `a365.config.backup-*.json`, and
  `a365.generated.config.backup-*.json` (and any operator-suffixed
  variants like `…json.r5-cleared`). Don't override these.
- **Per-agent `.env` at `~/.hermes/agents/<slug>/.env`** never carries
  the secret — only tenant id, app id, and runtime metadata. Source it
  into the gateway shell + export `A365_BLUEPRINT_CLIENT_SECRET`
  separately (see [Operator setup](#operator-setup)).
- **`microsoft/Agent365-devTools#408`** — the GA CLI sometimes drops
  the secret entirely (writes `agentBlueprintClientSecret: null` to
  disk despite reporting success). `register --apply --auto-recover-secret`
  detects this and runs `az ad app credential reset --append` to mint
  a fresh secret in-place. Use the flag whenever you're not on Windows.

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

This repo holds the **design artefacts** — references, scripts,
templates, the activity bridge, and the gateway plugin. The
upstream `SKILL.md` is contributed into the Hermes Agent harness
at `hermes-agent/optional-skills/cloud-platforms/hermes-a365/SKILL.md`,
pulling these artefacts in at contribution time. The original
v0.1 design draft is archived at
[`docs/historical/SPEC-v0.1-draft.md`](docs/historical/SPEC-v0.1-draft.md).

## Repo layout

```
.
├── README.md                # This file (current spec — what ships, how to run it)
├── CHANGELOG.md             # Per-tag highlights + known limitations
├── SKILL.md                 # Validator-compliant upstream contribution
├── LICENSE                  # MIT
├── pyproject.toml           # Python 3.11+; uv-managed; optional [bridge] extras
├── a365.config.json.example # Seed copy for new tenant setup
├── docs/
│   ├── historical/          # Archived design drafts (e.g. SPEC-v0.1-draft.md)
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
├── src/hermes_a365/         # The installed package
│   ├── __init__.py
│   ├── _common.py               # parse_env, slugify, safe_run, jinja_env, deep_diff
│   ├── a365_config.py           # a365.config.json round-trip
│   ├── activity_bridge.py       # verify + serve + update-endpoint (standalone)
│   ├── cleanup.py
│   ├── cli.py                   # `hermes-a365 <verb>` console entry point
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
│   ├── status.py
│   ├── plugin/                  # Hermes gateway platform plugin
│   │   ├── plugin.yaml              # Manifest (loader globs lowercase)
│   │   ├── __init__.py              # register(ctx): platform + CLI subcommand
│   │   ├── adapter.py               # Agent365Adapter(BasePlatformAdapter)
│   │   ├── cli.py                   # `hermes a365 <verb>` argparse tree
│   │   ├── conversations.py         # ConversationRef + ConversationRegistry
│   │   └── README.md
│   └── _data/                   # Packaged Jinja templates (importlib.resources)
│       └── templates/
│           ├── blueprint.json.j2
│           ├── consent-url.txt.j2
│           ├── instance.env.j2
│           └── adaptive-cards/      # greeting / confirmation / error
└── tests/                   # 876 tests (pytest + ruff clean)
    ├── conftest.py
    ├── golden/
    └── test_*.py
```

## Install

`hermes-a365` ships as a PyPI package. There are two install paths,
depending on what you want:

**Standalone CLI** — for operators who just need to drive `register` /
`cleanup` / `doctor` / `status` / `activity-bridge serve` outside a
Hermes harness:

```bash
pipx install 'hermes-a365[bridge]'   # `bridge` extras only needed for `activity-bridge serve`
hermes-a365 doctor --human
```

**Gateway plugin** — installs the package into the Hermes venv so the
plugin loader auto-discovers `agent365` via the
`hermes_agent.plugins` entry point. No `~/.hermes/plugins/agent365/`
directory required:

```bash
~/.hermes/hermes-agent/venv/bin/pip install 'hermes-a365[bridge]'
hermes plugins list                # `agent365` should appear with source=entrypoint
```

**Local development** (from a checkout):

```bash
git clone https://github.com/satscryption/Hermes-A365.git
cd Hermes-A365
uv sync --all-extras
uv run pytest
```

Once Hermes is on your machine ([`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent))
and the plugin install above is done, the [Operator setup](#operator-setup)
section below covers the two remaining manual config edits.

## Quick start

The canonical end-to-end walkthrough is
[`references/live-tenant-test.md`](references/live-tenant-test.md). At a
glance, against a Frontier-Preview-enrolled M365 tenant where you hold
Global Admin and a `MICROSOFT_AGENT_365_TIER_3` license:

> **Budget time before you start.** On macOS 26.x the GA `a365` CLI
> falls back to device-code per Entra mutation, so `register --apply
> --m365 --aiteammate` typically hits **10–12 device-code prompts** in
> a row (each on a fresh tab) before it returns. On Linux / Windows
> the prompt count is 1–2. If you can run the apply path from Linux,
> do.

```bash
# 0. Seed the per-tenant config from the example. The wrapper auto-fills
#    most fields at apply time; the example documents the shape.
cp a365.config.json.example a365.config.json

# 1. Pre-deploy diagnostic
hermes a365 doctor --human                                # exit 0/1/2

# 2. Decide a license model (read-only, never purchases)
hermes a365 license --users 12 --agents 3 --plan E5

# 3. Register the blueprint + MCP/Bot permissions.
#    --m365 routes Teams via MCP Platform; --aiteammate creates the
#    agentic Entra user. --auto-recover-secret patches the GA CLI's
#    macOS / Linux secret-null regression (Microsoft#408) in place.
hermes a365 register --agent-name "Inbox Helper" \
    --m365 --aiteammate --apply --auto-recover-secret

# 4. (Verify admin consent — usually granted automatically by setup blueprint;
#     poll explicitly if `register` reported a deferred consent step)
hermes a365 consent "Inbox Helper" --no-open

# 5. Per-agent runtime config (writes ~/.hermes/agents/<slug>/.env)
hermes a365 instance create inbox-helper \
    --owner sadiq@contoso.com --owner-aad-id <oid> --apply

# 6. Package the manifest zip for admin-centre upload.
#    --aiteammate alone:  AI Teammate manifest (Teams 1:1 "Built for your org");
#                         upload at M365 Admin Centre.
#    --copilot-chat alone: Custom Engine Agent manifest (M365 Copilot Chat
#                         agents picker); upload at Teams Admin Center.
#    --aiteammate --copilot-chat: both zips side-by-side (Copilot Chat zip
#                         lands at <original>.copilot-chat.zip).
hermes a365 publish --agent-name "Inbox Helper" --aiteammate --apply

# 7. Operator: in M365 Admin Centre → Agents → All agents → Upload
#    custom agent, upload the zip emitted by step 6, then activate
#    the agent for each target user under Agent 365 admin centre.
#    (For --copilot-chat zips, upload at Teams Admin Center →
#     Manage apps → Upload + assign per-user policy.)

# 8. Re-point the messaging endpoint at whatever public HTTPS URL
#    fronts your local port 3978. The skill is tunnel-agnostic —
#    cloudflared / devtunnels / ngrok / Azure App Service / custom
#    reverse-proxy all work. See references/exposing-the-bot-endpoint.md.
hermes a365 activity-bridge update-endpoint \
    --agent-name "Inbox Helper" \
    --url https://<your-public-host>/api/messages --apply

# 9a. Standalone bridge (debug / no Hermes harness involved)
HERMES_BRIDGE_WEBHOOK=https://my-responder/respond \
    hermes a365 activity-bridge serve --slug inbox-helper

# 9b. Hermes plugin path (production: agent loop runs in the gateway)
hermes gateway run --profile inbox-helper

# 10. Status sanity (any time)
hermes a365 status inbox-helper --human

# 11. Tear down
hermes a365 cleanup --agent-name "Inbox Helper" \
    --slug inbox-helper --apply --confirm "Inbox Helper"
```

> **Running the CLI standalone.** Every `hermes a365 <verb>` mirrors
> `hermes-a365 <verb>` exactly — same flags, same behaviour. The
> `hermes-a365` script comes with `pipx install hermes-a365` and is
> handy when iterating without a configured Hermes harness.

## Operator setup

After the gateway-plugin pip install above (`~/.hermes/hermes-agent/venv/bin/pip
install 'hermes-a365[bridge]'`), the plugin is auto-discovered via its
`hermes_agent.plugins` entry point. Run the setup wizard to wire the
platform into Hermes:

```bash
hermes gateway setup --platform agent365
```

The wizard (slice 19r in v0.2.0; hardened in v0.4.0 by 19r-bis +
19r-a-bis) prompts through the generated-config path, tenant id,
blueprint app id, slug, port, secret bootstrap, and allow-all
toggle. It patches `~/.hermes/.env` (env vars) and
`~/.hermes/config.yaml` (`plugins.enabled` +
`gateway.platforms.agent365` block) and is fully idempotent —
re-running detects existing values, offers update-vs-keep, and
surfaces drift (stale `A365_APP_ID`, orphan slugs, missing
`tenantId` / `clientAppId` in `~/a365.config.json`, unreachable
`generated_config_path`, **missing or wrong-target XDG symlink at
`~/.config/a365/a365.generated.config.json`**) with auto-fixers
where possible. The `config.yaml` write is skipped when the
agent365 stanza hasn't materially changed, keeping the file
git-reviewable across wizard re-runs.

After the wizard, source the per-agent .env into the gateway's process
shell so the adapter inherits the runtime config, then start the
gateway:

```bash
set -a; . ~/.hermes/agents/<slug>/.env; set +a
hermes gateway run
```

`hermes a365 status <slug>` should now show the `activity_bridge` row
as `ok`.

> **Hand-edit fallback.** If you need to script the setup non-interactively
> (CI seed scripts, configuration management), the resulting
> `~/.hermes/config.yaml` block is:
>
> ```yaml
> plugins:
>   enabled:
>     - agent365
> gateway:
>   platforms:
>     agent365:
>       enabled: true
>       extra:
>         slug: inbox-helper
>         port: 3978
>         host: 127.0.0.1
>         generated_config_path: /Users/<you>/a365.generated.config.json
> ```
>
> …paired with `A365_TENANT_ID`, `A365_APP_ID`, `A365_BLUEPRINT_CLIENT_SECRET`,
> and either `A365_ALLOW_ALL_USERS=true` (testing) or `A365_ALLOWED_USERS=<csv>`
> (production) in `~/.hermes/.env`.

## Subcommand reference

For exhaustive flags on any verb, run `hermes a365 <verb> --help`
(or `hermes-a365 <verb> --help` outside a Hermes harness). The shape:

```bash
# === Read-only diagnostics ===
hermes a365 doctor [--human|--no-network]
hermes a365 license --users <n> --agents <n> --plan E3|E5|E7 [--bundled-security]
hermes a365 status [<slug>] [--human]
hermes a365 activity-bridge verify --slug <slug> [--human]

# === Apply-path orchestrators ===
hermes a365 register --agent-name "<display>" [--m365] [--aiteammate] \
    [--no-endpoint] [--auto-recover-secret] [--apply]
hermes a365 consent "<agent-name>" [--no-open] [--timeout 60]
hermes a365 instance create <slug> --owner <email> --owner-aad-id <oid> [--apply]
hermes a365 publish --agent-name "<display>" [--aiteammate] [--copilot-chat] \
    [--bot-id <guid>] [--apply]
hermes a365 cleanup --agent-name "<display>" [--slug <slug>] [--kinds=...] \
    [--purge-orphans] [--orphan-instance-id <guid>] --apply --confirm "<display>"

# === Activity bridge ===
hermes a365 activity-bridge verify --slug <slug>
hermes a365 activity-bridge serve --slug <slug> --port 3978
hermes a365 activity-bridge update-endpoint --agent-name "<display>" \
    --url <https://...> [--apply]
```

The internal helpers (`emit_card`, `keychain`, `reconcile_app`,
`reconcile_blueprint`, `render_instance_env`, `hermes_responder`) are
not surfaced as `hermes a365 <verb>` subcommands; they're libraries
the orchestrators import. Run them as `python -m hermes_a365.<x>` if
you need to.

> **macOS note for the keychain shim.** First write to the login
> keychain pops a UI dialog. Click "Always Allow" to avoid further
> prompts. CI / headless contexts may fail with `rc=36 User
> interaction is not allowed` — `security unlock-keychain` first.

## Open work

External issues filed:

- **[Microsoft#402](https://github.com/microsoft/Agent365-devTools/issues/402)** —
  `setup permissions bot` cosmetic logging gap. Filed 2026-05-05;
  Microsoft replied same day confirming Observability-only S2S
  assignment is intended (the other two resources use delegated
  OAuth2 only) — three message/log fixes queued for the next CLI
  release. **Fixes shipped in 1.1.174 (verified 2026-05-07).**
  Resolution captured in
  [`references/live-tenant-test.md`](references/live-tenant-test.md)
  (bug #18).
- **[Microsoft#408](https://github.com/microsoft/Agent365-devTools/issues/408)** —
  `setup blueprint`: `agentBlueprintClientSecret` persists as `null`
  on macOS despite successful credential creation. Filed 2026-05-07
  after round-6 walkthrough confirmed the regression is still
  present in CLI 1.1.174 (reproduces 100% across rounds 3, 4, 5, 6
  spanning 1.1.171 → 1.1.174). Re-checked 2026-05-15 for issue #35:
  NuGet release notes first mention the intended fix in 1.1.178, but
  a live R9 registration still reproduced the null-on-disk state on
  1.1.181. Wrapper-side coverage shipped in slice 19s — see closure
  of [#14](../../issues/14) below.
- **[Hermes#20133](https://github.com/NousResearch/hermes-agent/issues/20133)** —
  upstream proposal to add `hermes-a365` as an official optional
  skill. Filed 2026-05-05. Reframed in slice 19l after the SPEC §10
  Q1 contract turned out to already exist in the harness; awaiting
  NousResearch guidance on naming + placement.

Open issues in this repo (run `gh issue list` for current state).
Issues are tagged with `priority:next` / `priority:ready` /
`priority:conditional` / `blocked` / `deferred-pending-demand`
labels — `gh issue list --label "priority:ready"` surfaces the
working set.

Both paths are GA; the open backlog is feature-extension and
internal cleanup, not blocked-on-Azure work. The open fronts are
invoke activities ([#18](../../issues/18)), the v0.7.3 / v0.7.4 polish
+ Hermes-core slices ([#48](../../issues/48) /
[#65](../../issues/65) / [#53](../../issues/53)), and the
deferred-pending-demand set ([#19](../../issues/19) /
[#20](../../issues/20) / [#21](../../issues/21)).

**Active feature work:**

- **[#18](../../issues/18)** — Slice 19w: handle invoke activities
  (BF wire-protocol). Foundation slices 19w-a (typed dispatch +
  `InvokeContext` + response builders) and 19w-b (generalised
  `TokenFactory`) land first; per-name children 19w-c..g handle
  Path B-relevant invokes (`task/{fetch,submit}` via Copilot
  side-panel, `signin/verifyState`, `search`,
  `searchMessageExtension/query`) and invoke-aware idempotency
  replay. Compose-extension invokes (`composeExtension/*`) moved
  to sibling-Teams-adapter lane under the 2026-05-12 reframe.
  Work IQ V2 amplifier work split out
  to [#21](../../issues/21). Supersedes the older #5.

**Deferred (pending operator demand):**

These are architecturally-sound future moves that we will not pick up
until a concrete operator pain point surfaces — designing them in a
vacuum risks getting the API surface wrong. Each issue body lists the
explicit triggers that would re-prioritise it.

- **[#19](../../issues/19)** — Pluggable secrets provider. Replace
  `hermes_a365.keychain`'s OS-keychain shim with a `SecretsProvider`
  interface so operators can plug Vault / AWS Secrets Manager /
  Azure Key Vault / 1Password / etc. behind it. Defer until the
  first non-OS-keychain ask, or until Hermes ships its own
  abstraction we should consume rather than parallel.
- **[#20](../../issues/20)** — Split `activity-bridge` into BF-wire
  library + reference runtimes. The standalone `serve` and the
  `Agent365Adapter` plugin are already thin wrappers around mostly
  library-shaped logic (`_activity_to_event`, JWT validator,
  idempotency cache, FIC chain). Defer the formal split until a
  third runtime (e.g., embed in operator's FastAPI app, serverless
  function) is concretely needed.
- **[#21](../../issues/21)** — Work IQ V2 → invoke amplifiers. Once
  #18's `InvokeContext` + `TokenFactory` are in place, six BF invoke
  names (`composeExtension/{query,queryLink,anonymousQueryLink}`,
  `search`, `searchMessageExtension/query`, `task/fetch` grounding,
  `signin/verifyState` bootstrap) can be answered by Work IQ V2 MCP
  `tools/call` directly, bypassing the agent loop. Defer until a
  tenant with V2 per-workload-app consent asks us to back
  compose-extension search, or until 19w-g telemetry shows
  search-shaped invokes dominate (>40%) and the LLM-loop fallback
  cost justifies the build. Sibling of #18; depends on #18
  foundation slices (19w-a + 19w-b).

**Recent closures:**

- ~~#54~~ / ~~#55~~ / ~~#62~~ / ~~#26~~ / ~~#38~~ — **v0.7.2**
  Copilot Chat reply-quality (coalesce non-streaming replies into one
  bubble, drop the duplicate agent-name line, stale-stream liveness
  guard, `publish --manifest-id auto`, non-2xx reply-POST failures).
- ~~#47~~ / ~~#51~~ — **v0.7.1** Path B operator polish + docs
  corrections.
- bot-service wrapper family (`create` / `verify` / `update-endpoint`
  / `cleanup`) — **v0.7.0** (slice 20).
- ~~#16~~ / ~~#34~~ / ~~#36~~ — **v0.6.0 Path B Copilot Chat GA**: the
  end-to-end live walk (#16), inbound BF-shaped JWT validation (#34),
  and the separate non-agentic Bot Framework app for BF S2S (#36).
- ~~#27~~ — `send()` proactive fall-through unreachable in
  production. **Closed 2026-05-13** in v0.5.1 (slice 19x-e).
  Surfaced during the v0.5.0 soak: the registry persists `raw`
  to disk, so `_cached_inbound_for` returned the persisted value
  on every gateway restart and `send()` never fell through to
  `_send_proactive`. Fixed by gating on a per-lifetime
  `set[str]` of chat_ids the `/api/messages` route has captured
  this gateway boot.
- ~~#4~~ — Activity bridge proactive long-running reply pattern.
  **Closed 2026-05-13** in v0.5.0 (slices 19x-a..d). `send()`
  falls through to `_send_proactive` when this lifetime hasn't
  captured an inbound for the chat; POSTs to
  `<serviceUrl>/v3/conversations/<conv_id>/activities`
  (`sendToConversation`, no `replyToId`). Mints the agentic
  three-stage user-FIC chain against a synthetic activity-shape.
  `ConversationRegistry.prune_old_entries` mirrors Hermes'
  `SessionStore.prune_old_entries`; `pin` / `unpin` /
  `mark_used` explicit mutators. Path B proactive (agent-initiated
  sends on a Path B target) is not yet a validated path.
- ~~#25~~ — Setup wizard XDG symlink gap. **Closed 2026-05-12**
  in v0.4.0 (slice 19r-bis). Wizard now creates / repairs a
  symlink at `~/.config/a365/a365.generated.config.json` pointing
  at the operator's `A365_GENERATED_CONFIG_PATH`. Drift check
  surfaces `xdg_symlink_missing` / `xdg_symlink_wrong_target`
  with auto-fixer.
- ~~#24~~ — Custom Engine Agent publish path for Copilot Chat
  surface. **Closed 2026-05-12** in v0.4.0 (slice 19u-a).
  `hermes a365 publish --copilot-chat` emits a 1.21 manifest;
  optional `--bot-id` overrides; combine with `--aiteammate` for
  side-by-side zips. Live Copilot Chat surfacing shipped in v0.6.0
  ([#16](../../issues/16) / #34 / #36), provisioned via the
  `bot-service` wrapper family.
- ~~#22~~ — Setup wizard polish (slug + YAML diff noise).
  **Closed 2026-05-12** in v0.4.0 (slice 19r-a-bis). Slug
  prompt uses `prompt_choice` when >1 agent dirs;
  `~/.hermes/config.yaml` write skipped when stanza unchanged.
- ~~#17~~ — Slice 19v: Teams group + channel walkthrough.
  **Closed 2026-05-12** as superseded by the M365-ecosystem
  reframe. Teams group / channel surfaces are
  sibling-plugin-lane (classic Bot Framework via Hermes'
  `plugins/platforms/teams/`); Hermes-A365 doesn't own this
  surface.
- ~~#13~~ — Slice 19r: `interactive_setup()` for
  `hermes gateway setup` wizard. **Closed 2026-05-11** in v0.2.0.
  Hardened further in v0.4.0 (slice 19r-bis + 19r-a-bis, closing
  #25 + #22).
- ~~#3~~ — Activity bridge streaming responses. **Closed
  2026-05-11** in v0.3.0 (slices 19s + 19s-bis). Validated
  end-to-end in round-8 Teams 1:1 walkthrough.
- ~~#14~~ — GA CLI client-secret persistence regression. **Closed
  2026-05-07** after slice 19s shipped layer 1 (detection +
  `--auto-recover-secret` flag) and round-6 walkthrough validated
  end-to-end against CLI 1.1.174. Issue #35 re-check on 2026-05-15
  reproduced the upstream persistence gap on CLI 1.1.181, so layer 1
  remains required for live setup. Layer 2 filed upstream as
  [Microsoft#408](https://github.com/microsoft/Agent365-devTools/issues/408).
  Live-found bug fixed during validation: `_run_streaming` (slice
  18j) merges stderr into stdout, so `az -o json` output begins
  with a credential-protection `WARNING:` line that broke the
  initial `json.loads` parser; fixed via `_extract_first_json_object`
  using `JSONDecoder.raw_decode` from the first `{`.
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
- **2026-05-07** — README narrows
  [#18](https://github.com/satscryption/Hermes-A365/issues/18) scope
  to BF wire-protocol foundation + per-name handlers, splits Work IQ
  V2 amplifier work to new
  [#21](https://github.com/satscryption/Hermes-A365/issues/21).
  Slice 19s ships layer 1 of
  [#14](https://github.com/satscryption/Hermes-A365/issues/14) —
  detection + `--auto-recover-secret` for the GA CLI's
  `agentBlueprintClientSecret` persistence regression. Round-6
  validation walkthrough against CLI 1.1.174 confirms the regression
  is still present (filed upstream as
  [Microsoft#408](https://github.com/microsoft/Agent365-devTools/issues/408))
  and validates layer 1 end-to-end (one live-found JSON-parser bug
  fixed in commit `4b1a2e8`). #14 closed.

## License

MIT — see [`LICENSE`](LICENSE).
