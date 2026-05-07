# Live tenant integration test — Hermes-A365 v0.2

End-to-end runbook for verifying the v0.2 skill against a real Microsoft
Agent 365 tenant. Walk top-to-bottom on first run; expect ~30–45 minutes
including the M365 Admin Centre approval step.

**Snapshot:** 2026-05-05 (first live walkthrough completed; Slice 18i).
Pinned against `e8c4282` (Slice 18g) plus the fixes in Slice 18i.

> **Walkthrough notes (2026-05-05):** The first end-to-end run on a real
> tenant surfaced a number of wrapper bugs and CLI realities that diverge
> from the original runbook draft. Inline ⚠️ callouts below capture them;
> the open-bug summary at the end lists fixes queued for slices 18j+.
> If you hit something the runbook doesn't predict, that's a high-signal
> finding — log it.

## What you need before starting

- A Microsoft 365 tenant where you hold **Global Administrator** or
  **Agent Administrator**, enrolled in Microsoft's **Frontier Preview
  Program** (Agent 365 is gated on this; status visible in the M365
  Admin Centre under Settings → Org settings → Agent 365).
- An A365 license assigned to your test user account. The actual GA
  SKU name in `subscribedSkus` is **`MICROSOFT_AGENT_365_TIER_3`**
  (not "Agent 365 add-on" or "E7" — those names appear in marketing
  but never in Graph). If you've already got an Office productivity
  SKU on the user (e.g. `BUSINESS_PREMIUM_AND_MICROSOFT_365_COPILOT_FOR_BUSINESS`),
  Tier 3 will collide on `OFFICESUBSCRIPTION` ↔ `OFFICE_BUSINESS` —
  assign Tier 3 with `OFFICESUBSCRIPTION` (skuId `43de0ff5-c92c-492b-9116-175376d08c38`)
  in `disabledPlans` so the user keeps Office from the existing SKU.
- The custom Entra client app **with display name exactly `Agent 365 CLI`**
  registered in the tenant. The CLI hard-codes this name. ⚠️ Our doctor
  hard-codes the same default (`probe_custom_client_app`); if your
  operator named the app differently, rename in Entra rather than
  registering a duplicate.
- Local prereqs: `a365` CLI ≥ 1.0.0 (verified GA: 1.1.171), `az` CLI
  ≥ 2.55.0 signed in to the target tenant (`az login --tenant <tenant>`),
  `pwsh` 7+ on PATH (install via `brew install powershell` — the cask
  variant is deprecated), an OS keychain backend (macOS Security or
  Linux libsecret), and `dotnet` 10+ runtime if `a365` was installed via
  `dotnet tool install -g`. On macOS you also need
  `DOTNET_ROOT=$(brew --prefix dotnet)/libexec` exported and
  `~/.dotnet/tools` on PATH.
- A test mailbox / Teams account in the same tenant to drive the test
  message at the end.

Record these once, used throughout:

| Var | Example | Meaning |
|---|---|---|
| `<tenant>` | `contoso.onmicrosoft.com` | M365 tenant domain |
| `<tenant-id>` | `<guid>` | Entra tenant id |
| `<display-name>` | `Hermes Inbox Helper` | CLI `--agent-name`; the CLI derives `<display-name> Identity` and `<display-name> Blueprint` from this |
| `<slug>` | `inbox-helper` | local slug used for `~/.hermes/agents/<slug>/` |
| `<owner-email>` | `sadiq@contoso.com` | test user / agent owner |
| `<owner-aad-id>` | `<guid>` | their Entra object id (`az ad signed-in-user show --query id -o tsv`) |

## 0. Local bootstrap

Create `~/.hermes/.env` if it doesn't exist:

```
A365_TENANT_ID=<tenant-id>
A365_APP_ID=                                         # fill in manually after register / setup blueprint
HERMES_OTLP_ENDPOINT=https://<tenant>.otel.agent365.microsoft.com
```

⚠️ The earlier draft of this runbook claimed `register` populates
`A365_APP_ID` and `license` populates `A365_LICENSE_MODEL`. Neither
wrapper writes to this file in v0.2 — you fill the keys in by hand
after blueprint creation prints the appId. (Bug queued for slice
18j.)

In the repo root, you'll also want a working `a365.config.json` —
register populates derived display names there on `--apply`:

```bash
cd /Users/sadiqjaffer/satscryption/Hermes-A365
echo '{}' > a365.config.json
```

Throughout this runbook, every `uv run` command is run from the repo
root.

## 1. doctor — environment readiness

```bash
uv run python scripts/doctor.py --human
echo "exit=$?"
```

**Pass criterion:** `exit=0`. Every probe should show `ok`. Common
amber paths:

- `pwsh` missing → `brew install powershell` (the cask variant
  `--cask powershell` is deprecated as of 2026-05; use the formula).
- `Agent 365 CLI` client app not discoverable → either register it per
  Microsoft's docs, or rename your existing operator-managed app's
  display name to `Agent 365 CLI` (the appId stays stable). The WARN
  message now reports "no Entra app named …" precisely (slice 18m).
  Operators who can't rename can set `A365_CLIENT_APP_NAME=<their-name>`
  (slice 18r) — but the underlying `a365` CLI still hard-codes the
  default, so that override only quiets our wrapper, not the real
  CLI's own lookup.
- Network probe failing → corporate proxy. Doctor honours `HTTPS_PROXY`.
- `~/.hermes/.env` missing → step 0 above.

After doctor, also run the CLI's authoritative check:

```bash
yes y | a365 setup requirements
```

This auto-installs missing PowerShell modules
(`Microsoft.Graph.Authentication`, `Microsoft.Graph.Applications`)
and, if your `Agent 365 CLI` client app is missing the Agent 365
permissions / redirect URIs / public-client-flow flag, prompts to
add them (~7 permissions, 2 redirect URIs, public client flag). The
`yes y |` answers the confirmation non-interactively. Expect a
device-code prompt on first run for cached-token bootstrap.

- [ ] `doctor --human` exits 0 against the live tenant.
- [ ] `a365 setup requirements` reports `Requirements: 2 passed,
      1 warnings, 0 failed` (the warn is Frontier Preview, which is
      not auto-verifiable).

## 2. license — recommendation only (no purchase)

```bash
uv run python scripts/license.py --users 5 --agents 1 --plan E5
```

**Pass criterion:** prints a recommendation and exits 0. The
recommendation now names the actual `subscribedSkus` partNumber
(`MICROSOFT_AGENT_365_TIER_3` for the add-on; `MICROSOFT_365_E7`
for the bundle) so it lines up with what `az rest --url
.../subscribedSkus` shows in the tenant. License is read-only and
never writes to `~/.hermes/.env`.

- [ ] `license` recommendation rendered with the partNumber visible.

## 3. register — `setup blueprint` + `setup permissions {mcp,bot}`

The v0.2 `register.py` wrapper drives the three apply steps
end-to-end. Slice 18j replaced `subprocess.run(capture_output=True)`
with `_run_streaming` (line-buffered Popen + `select.select`
deadline + stderr→stdout merge), so device-code prompts surface in
real time. Use the wrapper directly:

```bash
uv run python scripts/register.py \
    --agent-name "<display-name>" \
    --m365 \
    --apply
```

(Drop `--m365` for blueprint-only flow without messaging endpoint
registration. Add `--tenant-id <id>` to skip the `az account show`
auto-detect.)

Behind the flag: `register --apply` runs `a365 setup blueprint
[--m365]`, then `setup permissions mcp`, then `setup permissions bot`,
in order. Each step may emit its own device-code prompt — see the
"device-code volume" caveat below. The wrapper streams every prompt
to stdout the moment the CLI prints it.

**Use `--auto-recover-secret` to auto-handle the
`agentBlueprintClientSecret` persistence regression** (see "Failure
modes" below): when set, after a successful apply the wrapper detects
the broken state and runs `az ad app credential reset --append` +
patches the generated config + tightens to mode `0600`. Off by
default; without the flag the wrapper prints a paste-ready recovery
hint and exits 0.

```bash
uv run python scripts/register.py \
    --agent-name "<display-name>" \
    --m365 \
    --auto-recover-secret \
    --apply
```

After `setup blueprint`, `a365.config.json` (operator config) gains
the derived `<display-name> Blueprint` / `<display-name> Identity`
names, and **`a365.generated.config.json`** (gitignored) gains the
blueprint appId, SP id, and the **client secret in plaintext** (DPAPI
is Windows-only). Treat that file as keychain-equivalent sensitivity.

Failure modes to watch:

- **`agentBlueprintClientSecret: null` on disk despite "Client secret
  created successfully!"** — GA CLI persistence regression on
  macOS / Linux. Reproduces 100% across rounds 3–6 (CLI 1.1.171
  through 1.1.174). The credential really is minted on the Entra app
  side; only the local persistence is broken. Filed upstream as
  [microsoft/Agent365-devTools#408](https://github.com/microsoft/Agent365-devTools/issues/408).
  The wrapper's layer-1 detection (slice 19s) catches this and
  surfaces a paste-ready recovery line; pass `--auto-recover-secret`
  to fix it inline. If the warning fires post-apply, the on-disk
  secret is null and downstream commands (`update-endpoint`,
  bridge runtime) won't work without recovery.
- **Device-code volume on macOS 26** — `Failed to register persistent
  token cache. Authentication prompts may be repeated.` and `Browser
  authentication is not supported on this platform: macOS 26.4.1`
  combine to give one device-code prompt per Entra-side mutation
  (~10–12 prompts per `register --apply`). On Windows / Linux the
  persistent MSAL cache holds and you get 1–2 prompts total. Not
  yet filed upstream as a separate issue.
- **AADSTS500011** (license not yet propagated) — wait 5–30 min after
  assigning Tier 3 and re-run. The wrapper retries this code 3× with
  30 s backoff automatically.
- **`pwsh` not found** — `a365 setup` errors out citing
  `setup requirements`. Fix the prereq and re-run.
- **"Admin consent has not been granted... non-admin user"** during
  `setup permissions bot` — cosmetic CLI message that fires even when
  you ARE Global Admin. Microsoft confirmed
  ([microsoft/Agent365-devTools#402](https://github.com/microsoft/Agent365-devTools/issues/402),
  2026-05-05) the line is misleading: it triggers on a
  consent-not-yet-granted state for `AppRoleAssignment.ReadWrite.All`,
  not on a role check, and the PowerShell fallback acquires the token
  interactively a moment later. **Fixes shipped in 1.1.174** —
  message rephrased to "An administrator must grant tenant-wide
  consent to proceed". If the run still exits 0, the operation
  completed correctly. The `appRoleAssignments` post-run query will
  show only `Observability API` — that is also intended (Messaging
  Bot API and Power Platform API use OAuth2 delegated grants only,
  not S2S).

- [ ] `register --apply` exits 0 (drives `setup blueprint` →
      `setup permissions mcp` → `setup permissions bot` in order).
- [ ] Each step shows `[apply] <step>: <description> — done` in the
      wrapper's summary block.
- [ ] `a365.generated.config.json` exists and is **gitignored** (verify
      with `git check-ignore -v a365.generated.config.json`).
- [ ] `agentBlueprintClientSecret` is populated in
      `a365.generated.config.json`. If null, the wrapper's layer-1
      `[warn]` line should have fired pointing at Microsoft#408 with
      a paste-ready recovery. Re-run with `--auto-recover-secret` or
      paste the suggested `az ad app credential reset --append`
      command, then patch the field manually.

## 4. consent — admin grant

In v0.2, **admin consent for the blueprint app is already granted by
`setup blueprint`** (the second device-code flow opens an admin-consent
URL the operator approves). `consent.py` is now a thin verifier
(slice 18k):

```bash
uv run python scripts/consent.py "<display-name>" --no-open --interval 5 --timeout 30
```

It polls `a365 query-entra blueprint-scopes` and exits 0 once the
classifier sees a "consented / granted / ok" hint. To re-grant after
revocation, drop `--no-open` so it opens the admin-consent URL in a
browser first.

For belt-and-braces verification, query Graph directly:

```bash
SP_ID=$(az ad sp list --display-name "<display-name> Blueprint" --query "[0].id" -o tsv)
az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/oauth2PermissionGrants" \
    --query "value[].{resource:resourceId, scope:scope, consentType:consentType}" -o json
```

You should see `consentType: AllPrincipals` grants across Microsoft
Graph (Mail/Chat/Files/Sites/etc.), Connectivity API, Agent 365
Tools, Agent 365 Observability, and MCP Servers Metadata API.

- [ ] `consent.py "<display-name>"` exits 0 (or skip if you've
      verified directly via Graph).
- [ ] Direct Graph query confirms `AllPrincipals` grants for the
      blueprint SP across the resources above.

## 5. instance create — local runtime .env

Dry-run:

```bash
uv run python scripts/instance_create.py <slug> \
    --owner <owner-email> \
    --owner-aad-id <owner-aad-id>
```

Apply:

```bash
uv run python scripts/instance_create.py <slug> \
    --owner <owner-email> \
    --owner-aad-id <owner-aad-id> \
    --apply
```

This is purely local — no cloud calls. It writes
`~/.hermes/agents/<slug>/.env` with `AA_INSTANCE_ID` (preserved across
re-runs), owner metadata, and inherited `A365_APP_ID` /
`A365_TENANT_ID` / `HERMES_OTLP_ENDPOINT`.

Slice 18n cleaned up the rendered .env (no v0.1 `A365_CLI_VARIANT`)
and moved UUID generation to apply-time, so dry-run shows
`AA_INSTANCE_ID: (generated at apply)` rather than a value `--apply`
would discard.

- [ ] `~/.hermes/agents/<slug>/.env` exists, parseable, contains
      `AA_INSTANCE_ID`.
- [ ] Blueprint client secret is **not** in the file (verify with
      `grep -i secret ~/.hermes/agents/<slug>/.env` → no matches).

## 6. publish — register agent instance via Graph

⚠️ **GA reality differs from earlier docs.** For blueprint-only agents
(default — no `--aiteammate` flag), `a365 publish` does **not** create
a manifest zip. Instead it `POST`s to
`/beta/agentRegistry/agentInstances` to register the instance and
saves the resulting `agentInstanceId` into `a365.generated.config.json`.
The `--use-blueprint` flag in the GA help text (`a365 publish --help`)
documents this: "blueprint-based non-DW flow (calls Agent Instance
Graph API, no manifest)".

Manifest packaging only applies to AI Teammate agents (`--aiteammate`),
which our wrapper doesn't currently distinguish from blueprint-only
mode (queued for slice 18j).

```bash
a365 publish --agent-name "<display-name>" --tenant-id <tenant-id>
```

Expected output ends with `Agent instance registered: <guid>`.

- [ ] `Agent instance registered: <guid>` printed; `agentInstanceId`
      now populated in `a365.generated.config.json`.

## 7. Operator step — Admin Centre (AI Teammate flow only)

For **blueprint-only agents (default)**, this step is **N/A** — the
publish step in §6 already registered the instance via Graph. There's
no zip to upload.

For **AI Teammate agents** (when you ran `publish` with `--aiteammate`),
upload the zip:

1. Sign in to the M365 Admin Centre as Global Admin.
2. Settings → Integrated apps → Upload custom apps.
3. Upload the zip from step 6 and approve for the desired DLP scope.
4. Wait 1–5 min for propagation.

- [ ] (AI Teammate only) Zip uploaded and approved in the Admin Centre.
- [ ] (AI Teammate only) Agent visible in Teams app catalog for the
      test user.

## 8. End-to-end activity — test message

The activity bridge is still TODO (SPEC §10 Q1), so this step verifies
the **A365 governance plane** is wired up, not the Hermes runtime
roundtrip. Until activity-bridge ships, expect either:

- A `default` Microsoft response card (governance OK, no Hermes
  runtime), or
- An empty / loading card if the bot endpoint isn't yet bound — that's
  acceptable for this test.

Drive a test message:

1. In Teams, open a 1:1 chat with the agent (search for `<display-name>`).
2. Send a plain `hello`.
3. Open the M365 Admin Centre → Agent 365 → Telemetry within ~5 min.

- [ ] OTLP trace appears for the test message in the admin-centre
      telemetry view (or in your tenant's connected backend if you've
      configured one; the OTLP endpoint is in `~/.hermes/.env`).

When activity-bridge ships, this step gets a second checkbox: an
Adaptive Card response from the Hermes agent.

## 9. status — sanity check against `query-entra`

```bash
uv run python scripts/status.py <slug> --human
echo "exit=$?"
```

**Pass criterion:** all three cloud components report `ok`. The
overall report returns `partial` / exit 1 because `activity_bridge:
missing` (expected until the bridge ships).

You can now pass either the slug (`inbox-helper`) or the display name
(`"Hermes Inbox Helper"`) — slice 18l made `gather_local_config`
fall back to `slugify(agent_name)` if the literal-name dir doesn't
exist. Slice 18q sharpened the `_classify_scopes_output` heuristic
so the `blueprint_scopes` `detail` field surfaces real content
rather than the CLI's "Querying Entra ID for…" progress preamble.

- [ ] `local_config: ok`
- [ ] `blueprint_scopes: ok`
- [ ] `instance_scopes: ok`

## 9b. activity-bridge verify (slice 19a) — runtime config sanity

```bash
uv run python scripts/activity_bridge.py verify --slug <slug> --human
echo "exit=$?"
```

Ships in slice 19a as the diagnostic half of the bridge (the long-
running `serve` mode lands in 19b once the BF webhook contract is
documented). Five probes:

- `local_config` — `~/.hermes/agents/<slug>/.env` is parseable and
  carries the keys the runtime needs (`A365_TENANT_ID`,
  `A365_APP_ID`, `AA_INSTANCE_ID`).
- `generated_config` — `a365.generated.config.json` (in cwd) has
  the blueprint client secret + appId; warns if perms looser than
  0600 (slice 18i / 18x policy).
- `token_acquisition` — runs an actual `client_credentials` POST to
  AAD against the `Agent365Observability` resource — the only S2S
  role the GA CLI assigns by design (Microsoft confirmed at
  microsoft/Agent365-devTools#402; Messaging Bot / Power Platform
  use delegated OAuth2 only). On AADSTS7000218 (no role on resource)
  it warns rather than errors: the secret works, just the scope
  permission is missing — useful diagnostic, not a blocker.
- `reachability` — TCP probes against
  `login.microsoftonline.com` + `graph.microsoft.com`.
- `otlp_endpoint` — DNS lookup on the configured OTLP endpoint.

Exit codes match doctor: 0 = all ok, 1 = at least one warn, 2 = at
least one error. Run as a CI gate before deploying the bridge daemon
in 19b.

- [ ] `bridge verify` returns 0 (or 1 with only the documented
      AADSTS7000218 / OTLP-DNS warnings) against the fresh tenant.

## 9c. activity-bridge serve + reference responder (slices 19b + 19c + 19e) — Teams round-trip

Validates the full runtime path. Slice 19e (issue #6) replaced the
broken `client_credentials` outbound auth with the canonical A365
agentic three-stage `user_fic` chain — see
`scripts/activity_bridge.py::acquire_outbound_token` for the
implementation. This is the runtime walkthrough that round-2
couldn't reach.

⚠️ **CLI quirk caught in round-3 — `a365 publish` clobbers the
local secret.** If you run `register --apply` then `publish --apply`,
`a365.generated.config.json` will lose
`agentBlueprintClientSecret` (along with `botMsaAppId`, `botId`,
`messagingEndpoint`). Two ways to recover:

1. Re-run `update-endpoint --apply` to restore the bot identity from
   server state, then `az ad app credential reset --id
   <agentBlueprintId> --append` to mint a new secret. Patch the
   secret into `a365.generated.config.json` (the `agentBlueprintClientSecret`
   key) and `chmod 600` the file.
2. Or just `cleanup -y` and re-do register without ever calling
   publish — `update-endpoint --apply` registers an agent identity
   on its own.

Prerequisites:

- `botMsaAppId` populated in `a365.generated.config.json`. The
  default `setup blueprint` (without `--m365`) leaves it `null`. Run

  ```bash
  uv run python scripts/activity_bridge.py update-endpoint \
      --agent-name "<display-name>" \
      --url https://<tunnel>.trycloudflare.com/api/messages --apply
  ```

  This passes `--m365` under the hood (provisioning the bot
  identity + populating `botMsaAppId`) and pins the messaging
  endpoint to your tunnel.
- A way to expose `localhost:3978` to A365's connector as an
  HTTPS URL. The walkthrough below uses **Cloudflare quick
  tunnel** (`cloudflared`) for expedience — substitute any of the
  options in
  [`references/exposing-the-bot-endpoint.md`](exposing-the-bot-endpoint.md)
  for non-walkthrough deployments. The skill is tunnel-agnostic;
  `update-endpoint --apply` takes whatever URL you produce.

Stand up three processes (substitute your tunnel/proxy of choice
for the `cloudflared` line if you're not following the walkthrough
literally):

```bash
# 1. Tunnel — exposes the bridge port to A365's BF infra.
#    Quick tunnel (no account / no setup) shown here. For a
#    stable URL or production use, see references/exposing-the-bot-endpoint.md.
cloudflared tunnel --url http://localhost:3978 &
# Take the trycloudflare.com URL it prints.

# 2. Reference responder.
uv run python scripts/hermes_responder.py serve \
    --port 9090 --mode greeting --slug inbox-helper &

# 3. Bridge.
HERMES_BRIDGE_WEBHOOK=http://127.0.0.1:9090/respond \
    uv run python scripts/activity_bridge.py serve \
        --slug inbox-helper --port 3978 &
```

Send a test message in the Teams 1:1 chat with the agent.

- [ ] First Teams message from a fresh chat returns the greeting
      Adaptive Card.
- [ ] Subsequent messages return `You said: <text>`.
- [ ] `~/.hermes/agents/inbox-helper/responder.log` accumulates one
      JSON-line per turn.
- [ ] `~/.hermes/agents/inbox-helper/bridge.log` shows JWT-validated
      activity ingress + reply via `serviceUrl`.

If the responder returns 200 but Teams shows nothing, the bridge log
is the place to look — that's where outbound reply errors surface.

## 9d. activity-bridge via Hermes plugin (slices 19m + 19n + 19o) — full agent-loop round-trip

Validates the end-to-end runtime: the plugin loaded **inside the
Hermes harness**, an activity routing through `BasePlatformAdapter
.handle_message(event)`, the agent loop reasoning, and a reply
landing back via `Agent365Adapter.send()`. This is the round-N
acceptance gate for #1 (gateway-platform plugin path).

⚠️ **Prefer §9c first if you're debugging.** If §9d misbehaves,
drop down to §9c (bridge-only standalone) to bisect — that proves
the underlying A365 auth + JWT + serviceUrl plumbing without
Hermes in the path. Once §9c is green, the only remaining variable
in §9d is the harness wiring.

### Prerequisites

A complete checklist — three buckets, all required before you start
the actual walkthrough at §9d.1.

**Tenant + local toolchain** (one-time, same as the rest of this
runbook):

- [ ] Steps **§0 through §7** of this runbook all complete:
  doctor green, license recommendation rendered, blueprint
  registered (`register --apply`), admin consent granted, per-agent
  `.env` written by `instance create --apply`, `publish --aiteammate
  --apply` zip uploaded via Admin Centre and activated for your
  user. §1's `What you need before starting` callout at the top of
  this file lists the underlying prereqs (Frontier Preview, Tier 3
  license, `Agent 365 CLI` Entra app, `a365` CLI, `az` CLI, pwsh,
  dotnet, etc.). If any of these are red, §9d will fail in
  hard-to-diagnose ways.
- [ ] `cloudflared` installed (`brew install cloudflared`).

**Bridge runtime** (this is what gets ported under the plugin):

- [ ] Bridge extras installed: `uv sync --extra bridge` from the
  repo root (pulls in `fastapi`, `uvicorn[standard]`, `httpx`,
  `pyjwt[crypto]`).
- [ ] §9b (`activity_bridge verify`) and §9c (bridge-standalone
  Teams round-trip) both green at least once recently. §9d adds
  the harness on top of these — you don't want to be debugging
  three layers at once.

**Hermes harness** (the layer §9d adds):

- [ ] Hermes harness installed at `~/.hermes/hermes-agent/` per its
  standard install.
- [ ] `hermes` CLI on PATH (`hermes --version` reports a build).
- [ ] You can run `hermes gateway run` against the harness without
  any platform enabled and it stays up cleanly. (If the harness
  itself is broken, §9d won't help — fix that first.)
- [ ] These env vars **exported in the shell that runs `hermes
  gateway run`** (the gateway process reads its own environ; the
  per-agent `.env` at `~/.hermes/agents/<slug>/.env` is read by the
  bridge but not auto-sourced by Hermes):

  ```bash
  export A365_TENANT_ID="$(az account show --query tenantId -o tsv)"
  export A365_APP_ID=<blueprint-app-id>          # from a365.generated.config.json
  export A365_BLUEPRINT_CLIENT_SECRET=<secret>   # same file; or set extra.generated_config_path in config.yaml
  export AA_INSTANCE_ID=<instance-id>            # from a365.generated.config.json
  ```

  As a shortcut while iterating, source the per-agent .env into
  the gateway shell:

  ```bash
  set -a; source ~/.hermes/agents/inbox-helper/.env; set +a
  ```

  (the `set -a` flag exports every variable assigned in the file).
  Pair it with an explicit `export A365_BLUEPRINT_CLIENT_SECRET=…`
  since the agent .env doesn't carry the secret by design.

If any of these are still red, stop here and fix before §9d.1 —
the runbook below assumes they're all green.

### 9d.1 — Install the plugin into Hermes' plugin path

For development, symlink rather than copy so edits land immediately:

```bash
mkdir -p ~/.hermes/plugins
ln -sfn "$PWD/plugins/agent365" ~/.hermes/plugins/agent365
ls -l ~/.hermes/plugins/agent365   # confirm symlink resolves
```

The plugin auto-discovers `scripts/activity_bridge.py` via a
sys.path append at module load (relative to the symlink target),
so the bridge helpers (`validate_inbound_jwt`, `send_reply`,
`acquire_outbound_token`, the caches) are reused — not vendored.
Plain copies of the plugin directory without `scripts/` as a
sibling won't load; that's tracked as a follow-up
(vendor-the-bridge-into-the-plugin) for whenever this ships outside
a dev checkout.

### 9d.2 — Wire the platform into Hermes config.yaml

Add the platform block to `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    agent365:
      enabled: true
      extra:
        slug: inbox-helper
        port: 3978
        host: 127.0.0.1
        # blueprint_client_secret read from env or generated config:
        # generated_config_path: /Users/<you>/satscryption/Hermes-A365/a365.generated.config.json
```

The plugin reads `A365_TENANT_ID`, `A365_APP_ID`, and
`A365_BLUEPRINT_CLIENT_SECRET` from env (env wins over `extra`).
The wrapper's `register --apply` + `instance create --apply` flow
already populates `~/.hermes/.env` and the per-agent `.env` with
these. If the secret isn't in env, the plugin falls back to
reading `agentBlueprintClientSecret` from the generated config
path (defaulting to cwd, or the `extra.generated_config_path`
override).

### 9d.3 — Start the Hermes gateway

```bash
hermes gateway run
```

(Or whatever the canonical run command is in your harness install
— check `hermes --help`.) The gateway should:

1. Discover the plugin at `~/.hermes/plugins/agent365`.
2. Call `register(ctx)` → `ctx.register_platform(name="agent365", …)`.
3. Construct `Agent365Adapter(cfg)` via the registered factory.
4. Call `connect()` — uvicorn binds `127.0.0.1:3978`,
   `_mark_connected()` flips, gateway logs `agent365: connected`.
5. Load `~/.hermes/agents/inbox-helper/conversations.json` if it
   exists from prior runs (slice 19o).

Verify connectivity:

```bash
hermes gateway status
# expect: agent365 ✓ connected
curl -fsS http://127.0.0.1:3978/healthz
# expect: {"ok": true, "slug": "inbox-helper", ...}
```

⚠️ **If `hermes gateway status` shows `agent365: failed (config_error)`**,
the adapter's `_make_bridge_config()` couldn't resolve tenant/app/secret
from env or generated config. Inspect the gateway log for the exact
missing key. Most common cause: the gateway process inherited a
shell where `A365_BLUEPRINT_CLIENT_SECRET` isn't exported and cwd
isn't where `a365.generated.config.json` lives — fix by either
exporting the env var in the gateway's process, or setting
`extra.generated_config_path` in `config.yaml` to the absolute path.

### 9d.4 — Re-point messaging endpoint at the gateway tunnel

The bridge under §9c was bound to its own tunnel. Hermes' uvicorn
takes that role now. Re-run `update-endpoint` against whatever
public-reachable URL you point at the gateway port. The walkthrough
example uses Cloudflare quick-tunnel for expedience; see
[`references/exposing-the-bot-endpoint.md`](exposing-the-bot-endpoint.md)
for stable-URL alternatives.

```bash
# 1. Tunnel — quick-tunnel example. Substitute named-cloudflared,
#    devtunnels, ngrok, or your reverse proxy as appropriate.
cloudflared tunnel --url http://localhost:3978 &
# 2. Re-point.
uv run python scripts/activity_bridge.py update-endpoint \
    --agent-name "Hermes Inbox Helper" \
    --url https://<tunnel>.trycloudflare.com/api/messages --apply
```

The `update-endpoint` wrapper still drives `a365 setup blueprint
--m365 --update-endpoint <url>` and is gateway-agnostic — it just
tells MCP Platform where to deliver activities. Whether
`localhost:3978` is "the bridge" or "Hermes' uvicorn with the plugin
mounted" is invisible to MCP.

### 9d.5 — Drive a Teams turn through the agent loop

Send a message in the Teams 1:1 chat with **Hermes Inbox Helper**
(or whichever AI Teammate slug the activation step bound to your
user).

Acceptance gates — Hermes side:

- [ ] Hermes gateway log shows the inbound activity arriving on
      `agent365`: `Agent365Adapter.handle_message(...)` fires.
- [ ] The agent loop runs (look for tool-call lines if your
      `~/.hermes/skills/` set has any wired) and produces a reply.
- [ ] The reply hits Teams within ~10 s. (Longer-running turns
      need the proactive pattern — that's #4, not in scope here.)
- [ ] `~/.hermes/agents/inbox-helper/conversations.json` was
      written — open it and confirm the conversation id from your
      Teams thread is present with `last_inbound_activity_id`
      pointing at your most recent message.

Acceptance gates — A365 side (regression with §9c):

- [ ] No 401 / 403 in Hermes' uvicorn log on
      `POST /api/messages` — slice 19f's AAD-v2 validator still
      accepts Microsoft's tokens with the plugin in the path.
- [ ] Bridge dedupe (slice 19i) still short-circuits Microsoft's
      retry deliveries: send a duplicate by tapping send again
      quickly and confirm only one `handle_message` line for the
      retry.
- [ ] Outbound user-FIC chain still mints (slice 19e). The reply
      activity POST returns 2xx.

### 9d.6 — Restart durability check (slice 19o)

```bash
# Stop the gateway.
hermes gateway stop  # or kill the process
# Confirm the conversations file is on disk.
cat ~/.hermes/agents/inbox-helper/conversations.json | jq .
# Restart.
hermes gateway run
hermes gateway status   # agent365 ✓ connected
```

Send another Teams DM. The agent should reply on the same
conversation thread without you having to seed it again — the
registry hydrated the chat context on `__init__`. This is the
precondition that unblocks proactive long-running replies (#4).

- [ ] Across a gateway restart, a Teams DM still gets a reply (no
      "no cached inbound for chat_id" failure in the gateway log).
- [ ] `conversations.json` carries the same conversation id before
      and after the restart.

### 9d.7 — Tear down

For just the runtime (leave the tenant blueprint in place for the
next run):

```bash
hermes gateway stop
pkill -f "cloudflared tunnel"
rm -f ~/.hermes/agents/inbox-helper/bridge.pid
# Optional: rm ~/.hermes/agents/inbox-helper/conversations.json
# to clear chat memory.
```

For the full tenant cleanup, drop down to §10.

## 10. cleanup — leave the tenant clean

Slice 18l fixed the argv composition (bug #11) and the local-dir
slug resolution (bug #12), so the wrapper apply path now works
end-to-end.

Dry-run first to review the plan and verify the resolved local slug:

```bash
uv run python scripts/cleanup.py --agent-name "<display-name>" --tenant-id <tenant-id>
```

The plan output prints `local slug: <slug>` and renders each step as
`a365 cleanup -y <kind> --agent-name "<display-name>"` — confirm the
slug matches the directory you used at `instance create` time. If
they diverge, pass `--slug <your-slug>` to override.

Apply:

```bash
uv run python scripts/cleanup.py --agent-name "<display-name>" \
    --tenant-id <tenant-id> --apply --confirm "<display-name>"
```

The CLI deletes the blueprint Entra app + SP, removes the local
`a365.config.json` and `a365.generated.config.json` (after backing
both up to `*.backup-<timestamp>.json` in the same directory), and
emits no errors on absent resources (e.g. no Azure App Service in
your test setup is fine — it's a no-op). The wrapper then removes
the per-agent local artefacts under `~/.hermes/agents/<slug>/`.

⚠️ **Backup files contain the secret.** The
`a365.generated.config.backup-*.json` file the cleanup leaves behind
holds the same plaintext client secret as the original. Slice 18i
gitignored both backup patterns; slice 18x further `chmod 600`s every
`a365.{,generated.}config.backup-*.json` in cwd at the end of an
apply run so a stray multi-user-machine incident doesn't leak the
secret. If you've cloned to a fresh checkout, double-check
`git check-ignore -v a365.generated.config.backup-*.json` returns a
hit before running `git add`. Operators can `rm` the backups
manually once they've audited what cleanup did.

⚠️ **Orphan agentic users (slice 19g).** If the agent was published as
an AI Teammate (`publish --aiteammate --apply`) and activated for a
user, the GA CLI's `cleanup blueprint` step calls a Graph DELETE on a
non-existent `/beta/agentUsers/<id>` segment, logs the failure, and
leaves the per-user agentic Entra user orphaned.

⚠️ **Orphan agentRegistry instances (slice 19h).** Independently, the
GA CLI's `cleanup blueprint` deletes the blueprint Entra app + agent
identity SP but does **not** issue a Graph DELETE on the
`/beta/agentRegistry/agentInstances/<id>` registry record. The
wrapper snapshots `agentInstanceId` from `a365.generated.config.json`
*before* the CLI wipes it, so the orphan id is recoverable in the
end-of-run summary.

The wrapper now surfaces both orphan kinds with ready-to-paste
recovery lines and exits **1** if any orphan remains. Two ways to
handle:

- Re-run with `--purge-orphans`, which calls
  `az ad user delete --id <id>` for each orphan agentic user and
  `az rest --method DELETE --uri …/agentInstances/<id>` for each
  orphan registry entry after the CLI steps:
  ```bash
  uv run python scripts/cleanup.py --agent-name "<display-name>" \
      --tenant-id <tenant-id> --apply --confirm "<display-name>" \
      --purge-orphans
  ```
  The agentRegistry DELETE requires **`AgentInstance.ReadWrite.All`**
  on the calling app (NOT `AgentRegistry.ReadWrite.All` — that
  scope doesn't exist on Microsoft Graph). The az CLI's first-party
  app token doesn't carry it, so blueprint-only-flow orphans 403
  by default; on accounts that have granted it to the
  `Agent 365 CLI` app + run via MSAL device-code, the wrapper's
  DELETE works for blueprint-only-flow instances.

- **AI Teammate flow reality (re-confirmed across rounds 3, 4, 5 —
  stable across walkthroughs).** AI Teammate-flow instances
  (`originatingStore: "Microsoft Agent Store"` + `managedBy:
  9b975845-…`) **always 403 on Graph DELETE regardless of scope** —
  not even `AgentInstance.ReadWrite.All` granted to the
  `Agent 365 CLI` app clears them. Microsoft gates store-managed
  deletes behind a different authorization rule that isn't
  operator-exposed as a delegated scope at all. The
  `--orphan-instance-id` flag is therefore a no-op for AI-Teammate
  registrations; passing the GUID just produces a documented 403
  recovery-line in the wrapper output. **Don't waste time trying
  alternate scopes.**

  Canonical path for store-managed instances: **M365 Admin Centre
  → Agents → All agents → click the agent → Instance tab →
  select instance → Delete**. Per
  [Microsoft Learn: Manage agent instances](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-agent-instances).
  30-day soft-delete with audit retention.

  Then on the agent's main pane → **Block** (per
  [Microsoft Learn: agent-actions](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/agent-actions)).
  Microsoft doesn't expose Delete for custom-uploaded AI Teammates;
  blocking is as clean as the registry entry gets.

  Doing only Block leaves an inert orphan in
  `agentRegistry/agentInstances`. Doing both Instance Delete + Block
  drops the orphan to baseline.

- Or copy the recovery line(s) the wrapper prints and run by hand
  (only useful for blueprint-only-flow orphans).

- [ ] `cleanup --apply` exits 0 (or 1 with only documented orphans —
      re-run with `--purge-orphans` to make it 0).
- [ ] Blueprint app + service principal removed from Entra Portal.
- [ ] No `Orphaned agentic user:` or `orphaned agentRegistry instance:`
      entries remain in the wrapper's end-of-run summary.
- [ ] `~/.hermes/agents/<slug>/` removed locally.
- [ ] Tenant-wide infra (`Agent 365 CLI` client app, license, Frontier
      Preview enrollment) is **untouched** — verify in the Admin Centre.

## Roll-up

If every checkbox above is ticked, the v0.2 skill is verified
end-to-end against your tenant. Open issues observed during the run
(unexpected error codes, CLI behaviour mismatches with
`references/a365-cli-reference.md`, retry counts that needed bumping)
should land in the repo as a follow-up slice — these are the highest-
signal inputs we can get pre-activity-bridge.

If a step fails, **do not** skip ahead — most downstream steps depend on
the prior step's tenant state. Fix in place or run `cleanup` and start
over.

## Open wrapper bugs queued for slices 18j+

Captured during the 2026-05-05 walkthrough. Each is a discrete, small
fix; none requires architectural rework except the last.

| # | File / area | Symptom |
|---|---|---|
| 1 | `_common.py:48` `safe_run` | ~~Returns `None` for empty stdout+stderr success~~. **Fixed in slice 18m** — empty success now returns `""`; `None` reserved for real failure (timeout, OSError, non-zero exit). |
| 2 | `doctor.py probe_custom_client_app` | ~~Misleading "az not signed in?" on app-not-found.~~ **Fixed in slice 18m** as a downstream of #1 — the probe's branching was already correct, just fed the wrong contract. The "no Entra app named X" branch now triggers as intended. |
| 3 | `doctor.py probe_custom_client_app` | ~~Hard-codes `"Agent 365 CLI"`.~~ **Fixed in slice 18r** — set `A365_CLIENT_APP_NAME` in the environment to override the lookup. The probe still warns when the named app isn't found, with the canonical-name reminder. The underlying `a365` CLI itself still hard-codes the default; operators with a non-default app name need to either rename the Entra app or accept that `setup blueprint` won't find it until they do. |
| 4 | `license.py` reason text | ~~Renders nonsensical "users=N < 25 or plan=E5 < E5"~~. **Fixed in slice 18o** — only the predicate(s) that actually fired are reported, joined by " and " when both apply. |
| 5 | `license.py` / SKILL.md / runbook | ~~Earlier docs claimed `license` writes `A365_LICENSE_MODEL` to `~/.hermes/.env`.~~ **Doc-fixed in slices 18i + 18o** — runbook step 2 and `references/license-cost-table.md` no longer make the promise; license stays read-only as its docstring says. |
| 6 | `license.py` SKU naming | ~~Recommends "Agent 365 add-on" / "E7" without naming the actual `subscribedSkus` partNumber.~~ **Fixed in slice 18o** — both labels now include the partNumber (`MICROSOFT_AGENT_365_TIER_3` / `MICROSOFT_365_E7`); operators can grep `subscribedSkus` directly. |
| 7 | `register.py` / `cleanup.py` / `publish.py` rendered argv | ~~Multi-word agent names render unquoted; misleading on copy-paste.~~ **Fixed in slice 18p** — all three plan renderers use `shlex.join`, so `--agent-name 'Hermes Inbox Helper'` comes out shell-pasteable. |
| 8 | `consent.py` | ~~Calls `qs.query_consent(app_id=...)`, a method that doesn't exist on the v0.2 `QuerySource` protocol.~~ **Fixed in slice 18k** — polling now uses `query_blueprint_scopes` and shares the `_classify_scopes_output` heuristic with `status.py`. CLI takes a positional `agent_name` (omittable when `--print-url-only`). |
| 9 | `instance_create.py` | ~~Writes a leftover `A365_CLI_VARIANT` key (v0.1 artefact).~~ **Fixed in slice 18n** — field, template line, validation, CLI flag, and golden files all dropped. |
| 10 | `instance_create.py` | ~~Dry-run renders a fresh `AA_INSTANCE_ID` that `--apply` discards in favour of its own.~~ **Fixed in slice 18n** — UUID generation moved to apply; dry-run for new agents prints `(generated at apply)` instead of a misleading value. |
| 11 | `cleanup.py` wrapper | ~~Composes `--yes` on each subcommand.~~ Slice 18l moved `-y` to the parent verb (`a365 cleanup -y <kind>`), but the **2026-05-05 round-2 walkthrough caught the GA CLI ignoring `-y` on subcommands** — each `cleanup azure` / `instance` / `blueprint` still prompted "Continue with X cleanup? (y/N):" and exited rc=1 on empty stdin. **Fully fixed in slice 18w**: extended `Mutator.run` and `_run_streaming` with an optional `stdin_input` kwarg; cleanup pre-feeds `"y\n"` per step. The `-y` flag stays in the argv (documented intent + harmless redundancy). |
| 12 | `cleanup.py` / `status.py` | ~~Both look up local files using the literal `--agent-name` rather than the slug.~~ **Fixed in slice 18l** — `_common.slugify` derives the slug from the display name; `cleanup.py` adds a `--slug` override; `status.py` falls back to `slugify(agent_name)` if the literal-name dir doesn't exist. |
| 13 | `status.py` `blueprint_scopes` parser | ~~Reports the CLI's progress message in the `detail` field.~~ **Fixed in slice 18q** — `_meaningful_line` skips lines ending in `…`/`...` and lines starting with progress verbs (Querying / Checking / Resolving / Authenticating / Loading / `[DEBUG]` / `[INFO]`) before picking the representative line. Also tightened `_CONSENTED_HINTS` (dropped `"ok"`). The 2026-05-05 round-2 walkthrough caught the tightening was too aggressive: real GA `query-entra blueprint-scopes` doesn't print "consented" or "granted" — it prints "Successfully retrieved inheritable permissions from Graph API" + `Inheritable Scopes:` headers. **Slice 18v** added those literal strings to `_CONSENTED_HINTS` (verified against v1.1.171 output, regression-pinned). |
| 14 | `publish.py` wrapper | ~~Doesn't distinguish blueprint-only vs `--aiteammate` flow.~~ **Fixed in slice 18t** — plan output now prints `output:  Graph API instance registration (no zip)` vs `manifest zip for M365 Admin Centre upload`; result extracts the appropriate artefact (instance id vs zip path); post-apply messages branch (no admin-centre prompt for blueprint-only). |
| 15 | SKILL.md / runbook | ~~Claim "T2 client secret lives only in the keychain".~~ **Doc-fixed in slice 18s** — SKILL.md pitfall #7 now describes the macOS / Linux plaintext-on-disk reality and the gitignored backup-file risk. |
| 16 | `references/a365-cli-reference.md:144` | ~~`brew install --cask powershell` is deprecated.~~ **Doc-fixed in slice 18s** — references doc now says `brew install powershell` (the formula) and notes the cask was renamed to `powershell@preview` and flagged for Gatekeeper failures. Snapshot also gained the macOS `DOTNET_ROOT` gotcha that bit the walkthrough. |
| 17 | `mutator.py` (architectural) | `subprocess.run(capture_output=True)` blocks until completion, so device-code prompts and admin-consent flows from `a365 setup *` are invisible. Slice 18i bumped the timeout to 900 s as a stop-gap. The proper fix is line-streamed output via `Popen` with `stdout=PIPE` and a reader thread. |
| 18 | `setup permissions bot` interaction | **Resolved upstream — intended behaviour with cosmetic logging gap.** Filed with Microsoft as [microsoft/Agent365-devTools#402](https://github.com/microsoft/Agent365-devTools/issues/402); reply on 2026-05-05 from @sellakumaran clarifies: (a) the blueprint SP is **supposed to** receive only the `Agent365Observability` S2S app-role assignment — Messaging Bot API and Power Platform API are configured via delegated OAuth2 grants only, the misleading `Configuring S2S app role assignments...` header will be reworded; (b) the mid-run "non-admin user" message is a real bug but cosmetic — fires on `AppRoleAssignment.ReadWrite.All` consent state, not a role check, and the PowerShell fallback acquires the token interactively, so a run that exits 0 completed correctly; (c) the unconditional `Bot API permissions configured successfully` log will be gated on the actual S2S outcome. All three fixes ship in the next CLI release. **Do not** manually `POST /servicePrincipals/<sp>/appRoleAssignments` for Messaging Bot / Power Platform — that would grant privileges the system doesn't intend. Operator-side query (informational only): `az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals/<blueprint-sp-id>/appRoleAssignments" --query "value[].{resource:resourceDisplayName, role:appRoleId}" -o table` — expect exactly one row (`Agent365Observability`). |
