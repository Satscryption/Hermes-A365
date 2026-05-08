# Hermes-A365

Integrate Hermes agents into the Microsoft 365 ecosystem using
**Microsoft Agent 365** (A365), the governance / identity / observability
control plane that GA'd 2026-05-01.

## Status

**v0.1.0** — first operator-targeted tag. Validated end-to-end against
a Frontier-Preview-enrolled M365 tenant on Microsoft Teams 1:1 chat
(rounds 3 → 5, last walked 2026-05-06 against CLI **1.1.171**). The
secret-null regression-recovery path was re-walked round-6 against
**1.1.174** (2026-05-07) — full end-to-end on 1.1.174 is not yet
re-walked. **624 tests passing, ruff clean.** See [CHANGELOG.md](CHANGELOG.md)
for the full release notes.

## What works today

Lift of the per-surface matrix from
[`references/m365-surface-coverage.md`](references/m365-surface-coverage.md).
Legend: ✅ validated end-to-end · 🟢 architecturally covered, walk
pending · 🟡 needs new code · 🔴 different runtime; would be a
separate plugin · ⚪ out of scope.

| Surface | Status | Gating |
|---|---|---|
| **Microsoft Teams 1:1 chat** | ✅ | round-5 §9d walkthrough 2026-05-06 |
| Teams group chat | 🟢 | adapter maps `chat_type=group`; live walk #17 |
| Teams team channels (incl. threading) | 🟢 | minor `thread_id` extension; live walk #17 |
| Mobile Teams | 🟢 | identical wire shape; not separately walked |
| M365 Copilot Chat (standalone web app) | 🟡 | needs **#3 streaming** — Copilot Chat enforces a non-streaming reply timeout |
| Outlook compose-action (`task/fetch` / `task/submit`) | 🟡 | needs **#18 invoke handlers** |
| Teams compose extensions / Microsoft Search invokes | 🟡 | needs **#18 invoke handlers** |
| Cron / proactive sends (any surface) | 🟡 | needs **#4 proactive** — `ConversationRef` registry already shipped |
| Direct Line / web-chat / SharePoint embedded | 🟡 | bypasses A365 user-FIC; would need a separate auth path |
| Slack / Telegram / WhatsApp / Twilio / Line / Kik / GroupMe | 🟢 | external-channel `chat_type` mapping; not a primary scope |
| Word / Excel / PowerPoint Copilot side-panel (declarative) | 🔴 | declarative agents are a different runtime — Microsoft hosts the orchestrator |
| Office Add-ins / Loop components / OneNote agent | 🔴 | different SDKs; would be separate complementary packages |

## Known limitations

`v0.1.0` ships the operator wrapper, the read path, and the Bot
Framework activity bridge that backs the Hermes `agent365` gateway
platform. Several surfaces and operator ergonomics are explicitly
**not** in this tag:

- **M365 Copilot Chat streaming** is not implemented
  ([#3](https://github.com/satscryption/Hermes-A365/issues/3)).
  `Agent365Adapter.edit_message` is a no-op and `REQUIRES_EDIT_FINALIZE`
  is unset. Copilot Chat surface (#16) is gated on this.
- **Proactive replies for >10 s agent thinking** are not implemented
  ([#4](https://github.com/satscryption/Hermes-A365/issues/4)). `send()`
  still requires a cached inbound; cron-driven sends do not work yet.
- **`hermes gateway setup` wizard** is not yet shipped
  ([#13](https://github.com/satscryption/Hermes-A365/issues/13)).
  Operators must hand-edit `~/.hermes/config.yaml` and `~/.hermes/.env`
  per the [Operator setup](#operator-setup) section below.
- **Invoke activities** (Outlook compose-action, Teams compose
  extensions, search, signin/verifyState) are tracked under
  [#18](https://github.com/satscryption/Hermes-A365/issues/18); umbrella
  not yet implemented.
- **Plaintext on-disk secret on macOS / Linux.** DPAPI is Windows-only;
  on macOS / Linux the GA CLI writes the agent blueprint client secret
  to `a365.generated.config.json` in plaintext. See [Security model](#security-model).
- **End-to-end re-walk on CLI 1.1.174** has not yet been done. Round-6
  validated only the `register --apply` regression-recovery flow against
  1.1.174; round-5 (1.1.171) is the last full E2E walkthrough.
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
  `register --apply`, and the keychain shim in `scripts/keychain.py`
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
├── scripts/                 # One module per subcommand + shared helpers
│   ├── _common.py               # parse_env, slugify, safe_run, jinja_env, deep_diff
│   ├── a365_config.py           # a365.config.json round-trip
│   ├── activity_bridge.py       # verify + serve + update-endpoint (standalone)
│   ├── cleanup.py
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
│   └── status.py
├── plugins/agent365/        # Hermes gateway platform plugin
│   ├── plugin.yaml              # Manifest (loader globs lowercase)
│   ├── __init__.py              # register(ctx): platform + CLI subcommand
│   ├── adapter.py               # Agent365Adapter(BasePlatformAdapter)
│   ├── cli.py                   # `hermes a365 <verb>` argparse tree
│   ├── conversations.py         # ConversationRef + ConversationRegistry
│   └── README.md
├── templates/
│   ├── blueprint.json.j2
│   ├── consent-url.txt.j2
│   ├── instance.env.j2
│   └── adaptive-cards/          # greeting / confirmation / error
└── tests/                   # 624 tests (pytest + ruff clean)
    ├── conftest.py
    ├── golden/
    └── test_*.py
```

## Install

`hermes-a365` is a [virtual project](https://docs.astral.sh/uv/concepts/projects/init/#applications-vs-libraries)
in `pyproject.toml` (`[tool.uv] package = false`) — it's distributed
as a **clone-and-`uv sync`** package, not via PyPI. You'll need it
checked out alongside (or symlinked into) a working Hermes harness.

```bash
git clone https://github.com/satscryption/Hermes-A365.git
cd Hermes-A365
uv sync --extra bridge      # `bridge` extras are needed for `activity-bridge serve`
                            # and the Hermes plugin runtime; omit for setup-only ops
```

Once Hermes is on your machine ([`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)),
the [Operator setup](#operator-setup) section below shows how to make
`hermes a365 <verb>` and the `agent365` gateway platform available
inside the harness.

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

# 6. Package the AI Teammate manifest zip for admin-centre upload
hermes a365 publish --agent-name "Inbox Helper" --aiteammate --apply

# 7. Operator: in M365 Admin Centre → Agents → All agents → Upload
#    custom agent, upload the zip emitted by step 6, then activate
#    the agent for each target user under Agent 365 admin centre.

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

> **Running the scripts directly.** Every `hermes a365 <verb>`
> resolves to `scripts/<verb>.py`. For development, `uv run python
> scripts/<verb>.py [...]` is equivalent and may be more convenient
> when iterating without a configured Hermes harness.

## Operator setup

Until `hermes gateway setup`'s wizard ships
([#13](https://github.com/satscryption/Hermes-A365/issues/13)), the
`agent365` plugin needs three manual steps before `hermes gateway
run` will load it:

**1. Symlink the plugin into Hermes's plugin tree.** A copy will not
work — the bridge helpers under `<repo>/scripts/` are imported via
the symlink's resolved path. (Vendoring `scripts/` into the plugin is
queued for whenever this ships standalone.)

```bash
ln -sfn "$PWD/plugins/agent365" ~/.hermes/plugins/agent365
```

**2. Configure the gateway in `~/.hermes/config.yaml`.** Add the
plugin to `plugins.enabled` and a `gateway.platforms.agent365` block
that points at the per-agent `.env` `instance create` writes:

```yaml
plugins:
  enabled:
    - agent365

gateway:
  platforms:
    agent365:
      enabled: true
      extra:
        slug: inbox-helper
        port: 3978
        host: 127.0.0.1
        # Path the adapter reads to pick up the blueprint client secret
        # when neither the env var nor a keychain entry is available.
        generated_config_path: /absolute/path/to/Hermes-A365/a365.generated.config.json
```

**3. Make the runtime env vars visible to the gateway.** Source the
per-agent `.env` into the gateway's process shell so the adapter
inherits `A365_TENANT_ID`, `A365_APP_ID`, and `AA_INSTANCE_ID`. The
per-agent `.env` does **not** carry the secret, so export
`A365_BLUEPRINT_CLIENT_SECRET` separately:

```bash
set -a; . ~/.hermes/agents/inbox-helper/.env; set +a
export A365_BLUEPRINT_CLIENT_SECRET="$(cat ~/.hermes-secrets/inbox-helper.txt)"
# For testing, allow all signed-in users to reach the agent. Real
# operators should set A365_ALLOWED_USERS=<csv-of-emails> instead.
export A365_ALLOW_ALL_USERS=true
```

After all three steps, `hermes gateway run` should load `agent365`
during plugin discovery and surface it in `hermes a365 status
<slug>`'s `activity_bridge` row as `ok`.

## Subcommand reference

For exhaustive flags on any verb, run `hermes a365 <verb> --help`
(or `uv run python scripts/<verb>.py --help` outside a Hermes
harness). The shape:

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
hermes a365 publish --agent-name "<display>" [--aiteammate] [--apply]
hermes a365 cleanup --agent-name "<display>" [--slug <slug>] [--kinds=...] \
    [--purge-orphans] [--orphan-instance-id <guid>] --apply --confirm "<display>"

# === Activity bridge ===
hermes a365 activity-bridge verify --slug <slug>
hermes a365 activity-bridge serve --slug <slug> --port 3978
hermes a365 activity-bridge update-endpoint --agent-name "<display>" \
    --url <https://...> [--apply]
```

The internal helpers under `scripts/` (`emit_card.py`, `keychain.py`,
`reconcile_app.py`, `reconcile_blueprint.py`, `render_instance_env.py`,
`hermes_responder.py`) are not surfaced as `hermes a365 <verb>`
subcommands; they're libraries the orchestrators import. Run them via
`uv run python scripts/<x>.py` if you need to.

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
  spanning 1.1.171 → 1.1.174). Wrapper-side coverage shipped in
  slice 19s — see closure of [#14](../../issues/14) below.
- **[Hermes#20133](https://github.com/NousResearch/hermes-agent/issues/20133)** —
  upstream proposal to add `hermes-a365` as an official optional
  skill. Filed 2026-05-05. Reframed in slice 19l after the SPEC §10
  Q1 contract turned out to already exist in the harness; awaiting
  NousResearch guidance on naming + placement.

Open issues in this repo (run `gh issue list` for current state):

**Active build tracks:**

- **[#3](../../issues/3)** — Activity bridge streaming responses.
  **Hard prerequisite for #16** (M365 Copilot Chat surface validation
  per slice 19u) — Copilot Chat enforces a ~15s non-streaming reply
  timeout.
- **[#4](../../issues/4)** — Proactive long-running reply pattern.
  Surface-agnostic. Slice 19o registry (`ConversationRef` +
  `conversations.json`) is the **already-shipped** prerequisite;
  what's missing is the Hermes-side trigger.

**Surface-validation walkthroughs:**

- **[#16](../../issues/16)** — Slice 19u: validate M365 Copilot Chat
  surface (gates on #3).
- **[#17](../../issues/17)** — Slice 19v: validate Teams group +
  channel surfaces (architecturally covered, just needs a live walk).

**Adapter quality / operator UX:**

- **[#13](../../issues/13)** — Slice 19r: `interactive_setup()` for
  `hermes gateway setup` wizard. Surface-agnostic.
- **[#18](../../issues/18)** — Slice 19w: handle invoke activities
  (BF wire-protocol). Foundation slices 19w-a (typed dispatch +
  `InvokeContext` + response builders) and 19w-b (generalised
  `TokenFactory`) land first; per-name children 19w-c..g handle
  `task/{fetch,submit}` + `adaptiveCard/action`,
  `composeExtension/*`, `signin/{verifyState,tokenExchange}`,
  `search` + `searchMessageExtension/query`, and invoke-aware
  idempotency replay independently after that. Work IQ V2 amplifier
  work (search-invoke fast-path, auto-grounding, V2 token bootstrap)
  split out to [#21](../../issues/21). Supersedes the older #5.

**Deferred (pending operator demand):**

These are architecturally-sound future moves that we will not pick up
until a concrete operator pain point surfaces — designing them in a
vacuum risks getting the API surface wrong. Each issue body lists the
explicit triggers that would re-prioritise it.

- **[#19](../../issues/19)** — Pluggable secrets provider. Replace
  `scripts/keychain.py`'s OS-keychain shim with a `SecretsProvider`
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

- ~~#14~~ — GA CLI client-secret persistence regression. **Closed
  2026-05-07** after slice 19s shipped layer 1 (detection +
  `--auto-recover-secret` flag) and round-6 walkthrough validated
  end-to-end against CLI 1.1.174. Layer 2 filed upstream as
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
