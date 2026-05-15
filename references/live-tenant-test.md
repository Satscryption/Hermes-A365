# Live tenant integration test — Hermes-A365 v0.5

End-to-end runbook for verifying Hermes-A365 (v0.5.1 at time of last
refresh) against a real Microsoft Agent 365 tenant. Walk top-to-bottom
on first run; expect ~30–45 minutes including the M365 Admin Centre
approval step (longer if the tenant is on macOS 26 — see §3's
device-code-volume failure mode).

**Snapshot:** 2026-05-15 (rounds 1–8 incorporated + slice 19u-a
walkthrough; §11 Path B drafted + Phase 2 walked 2026-05-14; #34
inbound shipped 2026-05-15; #33 outbound wrapper shipped 2026-05-15
but blocked on a separate-identity follow-up — see §11.10 finding
14). Tracks the current `main` branch; specific slices are
referenced inline where they matter to operator behaviour.

> ## Scope: Path A (AI Teammate) only
>
> Hermes-A365 has two M365-ecosystem paths (see
> [`references/m365-surface-coverage.md`](m365-surface-coverage.md)
> for the positioning). **This playbook covers Path A end-to-end:**
>
> - Register the blueprint Entra app + service principal.
> - Publish the AI Teammate manifest (`agenticUserTemplates`,
>   `manifestVersion: devPreview`) for M365 Admin Centre upload.
> - Activate the agentic user per-user, then validate Teams 1:1
>   round-trip (including BF streaming).
> - Cleanup back to a clean tenant.
>
> Path A requires only an M365 tenant, Frontier Preview enrollment,
> and a Tier 3 / E7 licence — **no Azure subscription**.
>
> **Path B (Custom Engine Agent + Azure Bot Service)** — for
> Copilot Chat agents picker and Word/Excel/PowerPoint/Outlook
> side-panel surfacing — has a **draft runbook in §11**, transcribed
> 2026-05-14 from Microsoft docs but **not yet walked end-to-end**
> against a live Azure subscription. Path B reuses Path A's
> blueprint Entra app + service principal as the bot identity, so
> §§0–6 still apply unchanged; §11 layers Azure Bot Service
> registration + Microsoft Teams channel enable + Custom Engine
> Agent manifest upload on top of that. Expect to find surprises on
> the first live walk (issue
> [#28](https://github.com/satscryption/Hermes-A365/issues/28)
> tracks the Phase 2 walkthrough + findings; when it lands green,
> [#16](https://github.com/satscryption/Hermes-A365/issues/16) closes
> as a side effect).
>
> Note: if you only want generic Teams chat (DM / channels / group
> / threading / file attachments) and don't need M365 directory
> identity or Copilot Chat reach, **use Hermes' sibling Teams
> adapter** at `plugins/platforms/teams/adapter.py` instead —
> classic Bot Framework, no A365 / agentic user setup needed.

> **Round history:** rounds 1–6 ran against this tenant between
> 2026-05-05 and 2026-05-07. Each round surfaced a discrete bug
> bundle that landed as slices 18i–19s. The runbook's ⚠️ callouts
> capture findings still active against current GA and fixed-upstream
> deltas. The [#408](https://github.com/microsoft/Agent365-devTools/issues/408)
> persistence regression in §3 reproduced 100% across CLI
> 1.1.171 → 1.1.174; Microsoft closed it in the next build line, and
> doctor now treats 1.1.178 as the fixed-version floor. The
> **[Wrapper-bug fix history](#wrapper-bug-fix-history-rounds-16)**
> section at the end summarises the wrapper-side fix history. If
> you hit something the runbook doesn't predict, that's a
> high-signal finding — log it.

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
- Local prereqs: `a365` CLI ≥ 1.1.178 recommended (verified affected:
  1.1.171, 1.1.174; doctor warns below the fixed-version floor), `az` CLI
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

By design, `register` and `license` don't write to `~/.hermes/.env`
— it's the operator's surface. After `register --apply`, copy the
new `agentBlueprintId` from `a365.generated.config.json` into
`A365_APP_ID` here. The per-agent `.env` (written by `instance
create --apply` to `~/.hermes/agents/<slug>/.env`) inherits these
values and adds `AA_INSTANCE_ID`.

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
hermes-a365 doctor --human
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
hermes-a365 license --users 5 --agents 1 --plan E5
```

**Pass criterion:** prints a recommendation and exits 0. The
recommendation now names the actual `subscribedSkus` partNumber
(`MICROSOFT_AGENT_365_TIER_3` for the add-on; `MICROSOFT_365_E7`
for the bundle) so it lines up with what `az rest --url
.../subscribedSkus` shows in the tenant. License is read-only and
never writes to `~/.hermes/.env`.

- [ ] `license` recommendation rendered with the partNumber visible.

## 3. register — `setup blueprint` + `setup permissions {mcp,bot}`

The `register.py` wrapper drives the three apply steps end-to-end.
Slice 18j replaced `subprocess.run(capture_output=True)`
with `_run_streaming` (line-buffered Popen + `select.select`
deadline + stderr→stdout merge), so device-code prompts surface in
real time. Use the wrapper directly:

```bash
hermes-a365 register \
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

**Use `--auto-recover-secret` to auto-handle the historical
`agentBlueprintClientSecret` persistence regression on older CLI
builds** (see "Failure modes" below): when set, after a successful
apply the wrapper detects the broken state and runs
`az ad app credential reset --append` + patches the generated config +
tightens to mode `0600`. Off by default; without the flag the wrapper
prints a paste-ready recovery hint and exits 0. On CLI ≥ 1.1.178 the
detector should not fire on a clean run; keep the flag as a safety net
only when doctor warns about an older CLI or you are deliberately
testing the recovery path.

```bash
hermes-a365 register \
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
  created successfully!"** — historical GA CLI persistence regression
  on macOS / Linux. Reproduces 100% across rounds 3–6 (CLI 1.1.171
  through 1.1.174); Microsoft closed
  [microsoft/Agent365-devTools#408](https://github.com/microsoft/Agent365-devTools/issues/408)
  in the next build line, and doctor warns below the fixed-version
  floor (1.1.178). The wrapper's layer-1 detection (slice 19s) remains
  as a safety net and surfaces a paste-ready recovery line; pass
  `--auto-recover-secret` to fix it inline on older builds. If the
  warning fires post-apply, the on-disk secret is null and downstream
  commands (`update-endpoint`, bridge runtime) won't work without
  recovery.
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
      `a365.generated.config.json`. On CLI ≥ 1.1.178 this should land
      populated without wrapper recovery. If null, the wrapper's
      layer-1 `[warn]` line should have fired pointing at Microsoft#408
      with a paste-ready recovery. Re-run with `--auto-recover-secret`
      or paste the suggested `az ad app credential reset --append`
      command, then patch the field manually.

## 4. consent — admin grant

**Admin consent for the blueprint app is already granted by
`setup blueprint`** (the second device-code flow opens an admin-consent
URL the operator approves). `consent.py` is a thin verifier
(slice 18k):

```bash
hermes-a365 consent "<display-name>" --no-open --interval 5 --timeout 30
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
hermes-a365 instance create <slug> \
    --owner <owner-email> \
    --owner-aad-id <owner-aad-id>
```

Apply:

```bash
hermes-a365 instance create <slug> \
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

`a365 publish` has two modes the wrapper exposes for Path A:

- **Blueprint-only (default, no `--aiteammate`)** — `POST`s to
  `/beta/agentRegistry/agentInstances` to register the instance and
  saves the resulting `agentInstanceId` into
  `a365.generated.config.json`. No manifest zip.
- **AI Teammate (`--aiteammate`)** — emits a manifest zip the operator
  uploads via M365 Admin Centre (see §7).

> **Path B note (v0.4.0, slice 19u-a, `#24` closed):**
> `hermes-a365 publish --copilot-chat` (optionally combined with
> `--aiteammate`) post-processes the emitted zip into a Custom
> Engine Agent manifest (`manifestVersion: "1.21"` + `bots[]` +
> `copilotAgents.customEngineAgents`) for Teams Admin Center
> upload + Copilot Chat surfacing. **Out of scope for this
> playbook** — Path B additionally needs Azure subscription +
> Azure Bot Service registration of the blueprint Entra app
> (`#16`). See the scope callout at the top of this file. The
> emitter itself is shipped and tested; the live walkthrough is
> deferred until Azure provisioning lands.

The wrapper distinguishes these (slice 18t): plan output prints
`output: Graph API instance registration (no zip)` vs `manifest zip
for M365 Admin Centre upload`; result extracts the appropriate
artefact (instance id vs zip path); post-apply messages branch (no
admin-centre prompt for blueprint-only).

Dry-run, then apply:

```bash
hermes-a365 publish --agent-name "<display-name>" --tenant-id <tenant-id>
hermes-a365 publish --agent-name "<display-name>" --tenant-id <tenant-id> --apply
```

Add `--aiteammate` for the AI Teammate flow.

⚠️ **`a365 publish` clobbers local secret + bot identity fields.**
Round-3 caught that running `publish --apply` after `register --apply`
nulls `agentBlueprintClientSecret` (along with `botMsaAppId`,
`botId`, `messagingEndpoint`) in `a365.generated.config.json`. The
underlying credential is unaffected on the Entra side. Recover by
either re-running `update-endpoint --apply` (to restore bot identity)
+ `az ad app credential reset --id <agentBlueprintId> --append` (to
mint a new secret); or by `cleanup -y` and re-doing `register`
without `publish` — `update-endpoint --apply` registers an agent
identity on its own. **Possibly the same root cause as
[microsoft/Agent365-devTools#408](https://github.com/microsoft/Agent365-devTools/issues/408)**
(post-`setup blueprint` persistence regression) — flagged in #408's
related-defects callout.

- [ ] Blueprint-only: `Agent instance registered: <guid>` printed;
      `agentInstanceId` now populated in `a365.generated.config.json`.
- [ ] AI Teammate: `manifest zip: <path>` printed; zip exists.
- [ ] If using publish in the same session as register: re-verify
      `agentBlueprintClientSecret` is still populated; recover per
      the warning above if not.

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

## 8. End-to-end activity — telemetry-only smoke test

This step verifies the **A365 governance plane** is wired up
(telemetry trace surfaces in admin-centre). The full Hermes runtime
round-trip lives in §9c (standalone bridge) and §9d (Hermes plugin
path); both shipped via slices 19a–19o and replace what was a TODO
in earlier drafts of this runbook.

Drive a test message:

1. In Teams, open a 1:1 chat with the agent (search for `<display-name>`).
2. Send a plain `hello`.
3. Open the M365 Admin Centre → Agent 365 → Telemetry within ~5 min.

Without §9c/§9d running, expect either a `default` Microsoft
response card (governance OK, no Hermes runtime) or an empty /
loading card if the bot endpoint isn't bound — both acceptable for
this step. With the bridge or plugin running, you'll get an actual
agent response; that's the §9c / §9d acceptance gate, not §8's.

- [ ] OTLP trace appears for the test message in the admin-centre
      telemetry view (or in your tenant's connected backend if you've
      configured one; the OTLP endpoint is in `~/.hermes/.env`).

## 9. status — sanity check against `query-entra`

```bash
hermes-a365 status <slug> --human
echo "exit=$?"
```

**Pass criterion:** all three cloud components report `ok`. The
overall report returns `partial` / exit 1 if `activity_bridge:
missing` (the probe checks for `bridge.pid` in
`~/.hermes/agents/<slug>/`; absent when neither §9c nor §9d is
currently running). Run §9b's `bridge verify` for runtime config
sanity, or §9c / §9d to actually start the bridge if you want a
green `activity_bridge` row.

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
hermes-a365 activity-bridge verify --slug <slug> --human
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
`hermes_a365.activity_bridge::acquire_outbound_token` for the
implementation. This is the runtime walkthrough that round-2
couldn't reach.

⚠️ **CLI quirk caught in round-3 — `a365 publish` clobbers the
local secret.** If you run `register --apply` then `publish --apply`,
`a365.generated.config.json` will lose
`agentBlueprintClientSecret` (along with `botMsaAppId`, `botId`,
`messagingEndpoint`). Two ways to recover:

1. Re-run `update-endpoint --apply` to restore the bot identity from
   server state, then run the `register --auto-recover-secret
   --apply` path (slice 19s) to detect and patch the missing secret
   automatically. The slice-19s recovery path was designed for the
   post-`setup blueprint` regression
   ([Microsoft#408](https://github.com/microsoft/Agent365-devTools/issues/408))
   but the post-`publish` clobber leaves the same null-on-disk
   shape, so the same flag fixes it. Manual fallback if you'd
   rather not re-run `register`: `az ad app credential reset --id
   <agentBlueprintId> --append`, then patch the resulting
   `.password` into `agentBlueprintClientSecret` and `chmod 600`.
2. Or just `cleanup -y` and re-do register without ever calling
   publish — `update-endpoint --apply` registers an agent identity
   on its own.

Prerequisites:

- `botMsaAppId` populated in `a365.generated.config.json`. The
  default `setup blueprint` (without `--m365`) leaves it `null`. Run

  ```bash
  hermes-a365 activity-bridge update-endpoint \
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
python -m hermes_a365.hermes_responder serve \
    --port 9090 --mode greeting --slug inbox-helper &

# 3. Bridge.
HERMES_BRIDGE_WEBHOOK=http://127.0.0.1:9090/respond \
    hermes-a365 activity-bridge serve \
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

### 9d.1 — Install the plugin into Hermes' venv

Install the package into the Hermes venv so the plugin loader
auto-discovers it via the `hermes_agent.plugins` entry point —
no `~/.hermes/plugins/agent365/` directory required:

```bash
~/.hermes/hermes-agent/venv/bin/pip install 'hermes-a365[bridge]'
hermes plugins list                # `agent365` should appear with source=entrypoint
```

For dev work against an unpublished checkout, use an editable install
into the same venv instead:

```bash
~/.hermes/hermes-agent/venv/bin/pip install -e ".[bridge]"
```

The plugin imports `hermes_a365.activity_bridge` directly — no
sys.path tricks, no symlinks. Edits to the package land immediately
under `pip install -e`; otherwise reinstall after every change.

### 9d.2 — Wire the platform via the setup wizard

```bash
hermes gateway setup --platform agent365
```

The wizard (slice 19r-a..b shipped in v0.2.0; 19r-bis hardening in
v0.4.0) walks the operator through:

- Path to `a365.generated.config.json` (default `~/a365.generated.config.json`).
- **XDG symlink** — the GA `a365` CLI reads
  `~/.config/a365/a365.generated.config.json` and does **not** honour
  `A365_GENERATED_CONFIG_PATH`. When the generated config lives
  elsewhere, the wizard creates/repairs a symlink at the XDG path
  pointing at it (slice 19r-bis, [#25](https://github.com/satscryption/Hermes-A365/issues/25)).
  Without this, `a365 publish` fails with `agentBlueprintId missing`.
- Tenant id (default from `az account show`).
- Blueprint Entra app id (default from the generated config; drift-warns
  if `~/.hermes/.env::A365_APP_ID` is stale).
- Agent slug (default to the single per-agent dir, or pick from the
  list when there are several; required-with-re-prompt when there are
  none — slice 19r-a-bis).
- Bridge port (default 3978).
- Client secret bootstrap (reads from generated config; flags
  Microsoft#408 if it's null).
- Allow-all toggle (testing) vs `A365_ALLOWED_USERS=<csv>` (production).

The wizard patches `~/.hermes/.env` (env vars) and `~/.hermes/config.yaml`
(`plugins.enabled` + `gateway.platforms.agent365` block). The
`config.yaml` write is now skipped when the stanza hasn't actually
changed (slice 19r-a-bis — was emitting ~270-line normalisation diffs
per run). Re-runnable: detects existing values and offers
update-vs-keep. A drift-detection pass runs first — surfaces stale
`A365_APP_ID`, orphan slugs, missing `tenantId`/`clientAppId` in
`~/a365.config.json`, unreachable `generated_config_path`, **and
missing/wrong-target XDG symlinks** — with auto-fixers where possible.

If you'd rather hand-edit (e.g. for one-off automation), the resulting
config.yaml block is:

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
        generated_config_path: /Users/<you>/a365.generated.config.json
```

…paired with `A365_TENANT_ID`, `A365_APP_ID`, `A365_BLUEPRINT_CLIENT_SECRET`,
and either `A365_ALLOW_ALL_USERS=true` or `A365_ALLOWED_USERS=<csv>`
in `~/.hermes/.env`.

**Hand-edit operators also need the XDG symlink** (the wizard handles
this automatically). If your generated config lives at
`~/a365.generated.config.json`:

```bash
mkdir -p ~/.config/a365
ln -s ~/a365.generated.config.json ~/.config/a365/a365.generated.config.json
```

### 9d.3 — Start the Hermes gateway

```bash
hermes gateway run
```

The gateway should:

1. Discover the plugin via the `hermes_agent.plugins` entry-point scan.
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
hermes-a365 activity-bridge update-endpoint \
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
- [ ] The reply hits Teams within ~10 s. Sustained turns >10 s
      surface progress via the BF streaming protocol (slices 19s +
      19s-bis, `#3` closed). Cron-driven / unsolicited outbound
      uses the proactive `sendToConversation` path (slices 19x-a..e,
      `#4` + `#27` closed in v0.5.1) — exercised separately under
      §9d.6 once the gateway has been restarted.
- [ ] For replies > ~2 s of agent thinking, the Teams bubble
      grows incrementally rather than appearing all at once — BF
      streaming via `Agent365Adapter.edit_message` is in the path
      (slices 19s + 19s-bis).
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
registry hydrated the chat context on `__init__`.

Then test the proactive path (`#4` + `#27` closed in v0.5.1). Without
sending an inbound first, trigger an outbound — either from Hermes
cron or by invoking `await adapter.send(chat_id, content)` directly.
The send-side gate keys on
`adapter._seen_inbounds_this_lifetime` (per-lifetime, non-persistent),
so a fresh-restarted gateway with no inbound yet routes through
`_send_proactive` → `sendToConversation` (no stale `replyToId`).

- [ ] Across a gateway restart, a Teams DM still gets a reply (no
      "no cached inbound for chat_id" failure in the gateway log).
- [ ] `conversations.json` carries the same conversation id before
      and after the restart.
- [ ] Cron-driven (or test-driven) `send()` to the same chat **before
      the user sends an inbound this lifetime** lands in Teams via
      `sendToConversation`. POST URL ends `/v3/conversations/<id>/activities`
      (no `/<activity_id>` suffix); the activity body has no
      `replyToId`. Slice 19x-e gate fix verified.

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
hermes-a365 cleanup --agent-name "<display-name>" --tenant-id <tenant-id>
```

The plan output prints `local slug: <slug>` and renders each step as
`a365 cleanup -y <kind> --agent-name "<display-name>"` — confirm the
slug matches the directory you used at `instance create` time. If
they diverge, pass `--slug <your-slug>` to override.

Apply:

```bash
hermes-a365 cleanup --agent-name "<display-name>" \
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
  hermes-a365 cleanup --agent-name "<display-name>" \
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

If every checkbox above is ticked, Path A is verified end-to-end
against your tenant (Hermes-A365 v0.4.0 at last refresh). Open issues
observed during the run
(unexpected error codes, CLI behaviour mismatches with
`references/a365-cli-reference.md`, retry counts that needed bumping)
should land in the repo as a follow-up slice — these are the highest-
signal inputs we can get pre-activity-bridge.

If a step fails, **do not** skip ahead — most downstream steps depend on
the prior step's tenant state. Fix in place or run `cleanup` and start
over.

## Wrapper-bug fix history (rounds 1–6)

Captured during the 2026-05-05 → 2026-05-07 walkthrough sequence.
Each is a discrete, small fix; the table below is the historical
record — every row is now closed (the architectural one too).

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
| 17 | `mutator.py` (architectural) | ~~`subprocess.run(capture_output=True)` blocks until completion, so device-code prompts and admin-consent flows from `a365 setup *` are invisible.~~ **Fixed in slice 18j** — replaced with `_run_streaming` (line-buffered Popen + `select.select` deadline + stderr→stdout merge). Device-code prompts surface in real time; round-6 (2026-05-07) drove `register --apply` end-to-end through the wrapper this way. The 900 s timeout from slice 18i remains as the per-step ceiling. |
| 18 | `setup permissions bot` interaction | **Resolved upstream — intended behaviour with cosmetic logging gap.** Filed with Microsoft as [microsoft/Agent365-devTools#402](https://github.com/microsoft/Agent365-devTools/issues/402); reply on 2026-05-05 from @sellakumaran clarifies: (a) the blueprint SP is **supposed to** receive only the `Agent365Observability` S2S app-role assignment — Messaging Bot API and Power Platform API are configured via delegated OAuth2 grants only, the misleading `Configuring S2S app role assignments...` header will be reworded; (b) the mid-run "non-admin user" message is a real bug but cosmetic — fires on `AppRoleAssignment.ReadWrite.All` consent state, not a role check, and the PowerShell fallback acquires the token interactively, so a run that exits 0 completed correctly; (c) the unconditional `Bot API permissions configured successfully` log will be gated on the actual S2S outcome. **All three fixes shipped in 1.1.174** (verified 2026-05-07 via NuGet changelog scan). **Do not** manually `POST /servicePrincipals/<sp>/appRoleAssignments` for Messaging Bot / Power Platform — that would grant privileges the system doesn't intend. Operator-side query (informational only): `az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals/<blueprint-sp-id>/appRoleAssignments" --query "value[].{resource:resourceDisplayName, role:appRoleId}" -o table` — expect exactly one row (`Agent365Observability`). |
| 19 | `register.py` / GA CLI persistence regression | After `setup blueprint` claims success, `agentBlueprintClientSecret` is `null` on disk on macOS / Linux. **Wrapper-side coverage in slice 19s** — `register.py` detects + warns by default and runs `az ad app credential reset --append` + patches the generated config + `chmod 0600` when `--auto-recover-secret` is set. Layer 1 fix `9e0187e`; live-found follow-up `4b1a2e8` extracts JSON past the `az -o json` credential-protection WARNING that `_run_streaming`'s stderr→stdout merge dumped into stdout. Filed upstream as [microsoft/Agent365-devTools#408](https://github.com/microsoft/Agent365-devTools/issues/408); reproduces 100% across CLI 1.1.171 → 1.1.174. **Issue #35 follow-up:** Microsoft closed #408 in the next build line; doctor now warns below 1.1.178 and keeps `--auto-recover-secret` as an opt-in safety net for older installs / regressions. |

## 11. Path B — Custom Engine Agent + Azure Bot Service (Copilot Chat surfacing)

> ## ⚠️ Inbound shipped; outbound blocked on operator identity registration
>
> §11 was drafted 2026-05-14 from Microsoft's published docs and
> walked the same day against the satscryption Azure GA account
> (sub `Hermes-A365`, `westeurope`). All Microsoft-side steps walk
> green — bot resource provisions, msteams channel enables (with
> the **acceptedTerms PATCH** in §11.4 as a load-bearing finding),
> Custom Engine Agent surfaces in M365 Copilot Chat and Cowork.
>
> - **[#34](https://github.com/satscryption/Hermes-A365/issues/34)
>   closed 2026-05-15** — Path B inbound JWT validator branch
>   shipped + live-verified. The agent loop now runs end-to-end on
>   Path B traffic.
> - **[#33](https://github.com/satscryption/Hermes-A365/issues/33)
>   wrapper code shipped 2026-05-15** — BF S2S `client_credentials`
>   mint + Path A/B dispatcher + path-tag refinement. But the
>   blueprint Entra app is classified Agentic by Microsoft's policy,
>   so the live token mint fails with `AADSTS82001`. Path B outbound
>   is gated on the **#33 follow-up**: register a separate
>   non-agentic Entra app for Path B and rewire the Bot Service
>   `--appid` to it.
>
> Until the follow-up lands, Path B end-to-end shows: inbound
> arrives, agent loop runs, outbound mint fails with the explicit
> AADSTS82001 error pointing at the follow-up issue. See §11.10 for
> the full findings table + the Direct Line probe recipe.
>
> Path A (§§0–10) remains the validated end-to-end path.

Path B layers Azure Bot Service registration on top of Path A's
blueprint work. The blueprint Entra app from §3 + its client secret
(per §3's `--auto-recover-secret`) + its service principal stay
exactly as they are — Path B reuses them as the Bot Service identity.
Operators running both paths share one Hermes install and one
`/api/messages` endpoint.

### 11.1 — What this gets you

- Agent surfaces in M365 Copilot Chat agents picker (`@` mention).
- Agent surfaces in Word / Excel / PowerPoint / Outlook side-panels
  inside Copilot.
- Microsoft Search invokes (`search`) once #18 (invoke handlers) lands.
- Classic Teams reach (DM / group / channel) as a side effect of the
  Microsoft Teams channel on Bot Service.

If plain Teams chat is all you want, the sibling
[`plugins/platforms/teams/`](https://github.com/NousResearch/hermes-agent/tree/main/plugins/platforms/teams)
adapter is the right tool — classic Bot Framework, no Azure / M365
agentic-user setup. Path B's defensible value is the Copilot-fabric
reach that the sibling structurally cannot deliver.

### 11.2 — Prerequisites (additive to Path A)

- ✅ Path A §§3–6 complete: blueprint Entra app registered, client
  secret recovered (`--auto-recover-secret`), service principal in
  place, agent already published once (the secret is required for
  Bot Service Client Secret authentication below).
- **Azure subscription** in the same Entra tenant as your M365
  tenant. Free Bot Service SKU (`F0`) is sufficient — billed at $0/mo
  for the registration-only shape we use here.
- **Contributor** (or higher) RBAC on the subscription or the
  resource group you plan to use. *[Phase 2: confirm minimum-sufficient
  role; Microsoft's manual-deployment doc doesn't state this
  explicitly.]*
- `az` CLI ≥ 2.55 signed in to the same tenant + with the right
  subscription selected:

  ```bash
  az account set --subscription <subscription-id>
  az account show --query "{tenantId:tenantId, name:name}" -o table
  ```

- **`Microsoft.BotService` resource provider registered on the
  subscription.** ⚠️ Fresh Azure subscriptions ship with this
  provider `NotRegistered`; `az bot create` will fail until you
  register it. Confirmed against a fresh sub on 2026-05-14 —
  deterministic blocker, not a quirky propagation issue. One-line
  fix:

  ```bash
  az provider register --namespace Microsoft.BotService --wait
  # Verify:
  az provider show --namespace Microsoft.BotService \
      --query "registrationState" -o tsv
  # Expect: Registered
  ```

  Idempotent + free + ~2 min propagation. Slice 20a's
  `bot-service verify` should probe this; `bot-service create --apply`
  should auto-register before attempting `az bot create`.

Path B authentication shape: Microsoft's
[provision-azure-bot-service-manually](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/provision-azure-bot-service-manually)
doc lists three options — Client Secret (single tenant), User-Assigned
Managed Identity, Federated Credentials. Path A's blueprint app
already has a single-tenant client secret from §3, so **Client Secret
(Single tenant)** is the natural pairing. The other two would require
additional Entra setup beyond Path A.

Record these once, used throughout §11:

| Var | Example | Meaning |
|---|---|---|
| `<azure-rg>` | `hermes-a365-bots` | Resource group for the bot resource |
| `<region>` | `westeurope` | Azure region for the resource group's metadata residency. RG location is required; bot resource itself stays `--location global` regardless |
| `<azure-bot-name>` | `hermes-inbox-helper-bot` | Bot resource name. 4–42 chars, `-`, `a–z`, `A–Z`, `0–9`, `_` only (per [`az bot create` reference](https://learn.microsoft.com/en-us/cli/azure/bot#az-bot-create)) |
| `<tunnel-url>` | `https://apollo-….trycloudflare.com` | Same tunnel URL you used in §9d.4 |
| `<blueprint-app-id>` | `<guid>` | The MSA App ID from §3 (`A365_APP_ID` in `~/.hermes/.env`) |

### 11.3 — Provision the Azure Bot resource

Per the [`az bot create` reference](https://learn.microsoft.com/en-us/cli/azure/bot#az-bot-create):

```bash
# Resource group (skip if you already have one earmarked). The RG
# --location is a regional spec for the group's metadata residency
# (e.g. westeurope, eastus, uksouth); pick a region near you. It is
# NOT "global" — global is reserved for resources, not groups.
az group create --name <azure-rg> --location <region>

# Bot resource. --app-type SingleTenant + --appid + --tenant-id binds
# the bot identity to the blueprint Entra app from Path A's §3. The
# bot itself stays --location global (Bot Service is a global Azure
# resource; the RG's region is just metadata residency).
az bot create \
    --resource-group <azure-rg> \
    --name <azure-bot-name> \
    --app-type SingleTenant \
    --appid <blueprint-app-id> \
    --tenant-id <tenant-id> \
    --endpoint <tunnel-url>/api/messages \
    --sku F0 \
    --location global
```

⚠️ **Phase 1 finding — argv shape differs from #28's anticipated draft.**
Issue #28 anticipated `--kind registration --msa-app-id ...
--msa-app-type SingleTenant`. The actual GA argv per Microsoft's
reference doc is what you see above: no `--kind` parameter exists,
the MSA App ID flag is `--appid` (not `--msa-app-id`), and the
tenant flag is `--tenant-id` (not `--msa-app-tenant-id`). Phase 2
walk: re-confirm against the operator's `az` CLI version and bump
this note if Microsoft changes the surface.

Verify:

```bash
az bot show --resource-group <azure-rg> --name <azure-bot-name>
```

Expect a JSON object with `properties.endpoint` = your tunnel URL +
`/api/messages` and `properties.msaAppId` = `<blueprint-app-id>`.
Also note `properties.enabledChannels` will already contain `webchat`
+ `directline` — Bot Service auto-enables these two on `create`, no
explicit `az bot webchat`/`az bot directline create` needed. Only the
Microsoft Teams channel needs the explicit add step below.

### 11.4 — Enable the Microsoft Teams channel + accept publishing terms

The Microsoft Teams channel on Bot Service is what makes Copilot
Chat (and classic Teams) route activities to `/api/messages`. Per
[Microsoft's manual deployment doc](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/deploy-azure-bot-service-manually#deploy-to-microsoft-365):
"Ensure that your Azure Bot resource has the **Microsoft Teams**
channel added under **Channels**."

```bash
az bot msteams create \
    --resource-group <azure-rg> \
    --name <azure-bot-name>
```

⚠️ **`az bot msteams create` is incomplete on its own.** Phase 2 walk
2026-05-14 surfaced a load-bearing finding: the channel provisions with
`acceptedTerms: false`, and **Microsoft holds all traffic on the channel
until the publishing terms are accepted**. The Azure CLI does NOT expose
an `--accepted-terms` (or equivalent) flag, so terms acceptance is
either a manual Azure Portal step (Portal → your Bot resource →
Channels → Microsoft Teams → check the terms box → Apply) or a direct
ARM PATCH:

```bash
SUB=<subscription-id>
RG=<azure-rg>
BOT=<azure-bot-name>
az rest --method PATCH \
    --url "https://management.azure.com/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.BotService/botServices/${BOT}/channels/MsTeamsChannel?api-version=2022-09-15" \
    --headers "Content-Type=application/json" \
    --body '{"location":"global","properties":{"channelName":"MsTeamsChannel","properties":{"acceptedTerms":true,"isEnabled":true,"deploymentEnvironment":"CommercialDeployment"}}}'
```

Without this PATCH the operator's bot endpoint will never see Path B
traffic and there is **no error surfaced anywhere** — Direct Line
probes silently return `BotError / Failed to send activity / 403` and
Test in Web Chat shows nothing. Slice 20a's `bot-service create
--apply` MUST follow `az bot msteams create` with this PATCH or
operators hit the same dead end.

⚠️ **Preview status.** `az bot msteams` is in Azure CLI **Preview** per
the [reference](https://learn.microsoft.com/en-us/cli/azure/bot/msteams).
Argv is stable across recent releases; behaviour may shift. The portal
path (Azure Portal → your Bot resource → Channels → Microsoft Teams)
is the GA fallback and accepts terms in the same UI gesture as the
channel-add.

Verify (look for `acceptedTerms: true` AND `isEnabled: true`):

```bash
az bot msteams show --resource-group <azure-rg> --name <azure-bot-name>
```

Optional: enable calling on the channel if your agent needs Teams
voice (`--enable-calling --calling-web-hook https://.../calls`). Not
required for Copilot Chat surfacing.

### 11.5 — Re-point the messaging endpoint (when the tunnel URL changes)

Quick tunnels (cloudflared) rotate their URL on every restart. After
your tunnel comes up at a new URL, update both registrations:

```bash
# Azure side (Path B).
az bot update \
    --resource-group <azure-rg> \
    --name <azure-bot-name> \
    --endpoint <new-tunnel-url>/api/messages

# M365 / MCP Platform side (Path A). Run if you're also operating Path A.
hermes-a365 activity-bridge update-endpoint \
    --agent-name "<display-name>" \
    --url <new-tunnel-url>/api/messages --apply
```

The two endpoint registrations are independent — `az bot update`
talks to Azure Resource Manager, `activity-bridge update-endpoint`
talks to M365 MCP Platform via the GA `a365` CLI. Both can stay
active and pointed at the same `/api/messages`; the Hermes plugin
processes inbound traffic from either path identically (`agent365`
adapter sits behind one FastAPI route).

### 11.6 — Publish the Custom Engine Agent manifest

Emit the `manifestVersion: 1.21+` zip using the `--copilot-chat`
flag (slice 19u-a, v0.4.0+):

```bash
hermes-a365 publish \
    --agent-name "<display-name>" \
    --tenant-id <tenant-id> \
    --copilot-chat \
    --apply
```

The wrapper post-processes the GA CLI's emitted zip: sets
`manifestVersion: "1.21"`, populates `bots[]` referencing
`<blueprint-app-id>`, adds the `copilotAgents.customEngineAgents`
block, strips `agenticUserTemplates` from `manifest.json`. The 19r-c
`name.short` truncation to ≤30 chars runs against this zip too.
Output lands at `~/manifest/manifest.copilot-chat.zip` (or
`~/manifest/manifest.zip` when run without `--aiteammate`).

⚠️ **Make sure you're running the right `hermes-a365` binary** —
Phase 2 walk found that `pipx install hermes-a365` lands an older
copy at `~/.local/bin/hermes-a365` that's older than the Hermes-venv
install and lacks `--copilot-chat`. `which hermes-a365` will return
the pipx one first because PATH puts `~/.local/bin` before
`~/.hermes/hermes-agent/venv/bin`. Either invoke the venv binary
explicitly (`~/.hermes/hermes-agent/venv/bin/hermes-a365 publish ...`)
or `pipx upgrade hermes-a365` before running this step.

⚠️ **Minor wrapper drift** — Phase 2 walk also noted that the
`--copilot-chat` transformer strips `agenticUserTemplates` from
`manifest.json` but leaves the sibling `agenticUserTemplateManifest.json`
file inside the zip. Microsoft tolerates the leftover (uploads work),
but the file is dead weight in a Custom Engine Agent zip; small
slice-19u-a polish item. Same goes for the wrapper's post-publish
console message ("Teams Admin Center → Manage apps → Upload") which
doesn't match either Microsoft's docs (Microsoft Admin Portal →
Integrated Apps) or the live MAC UI behaviour (MAC → Agents →
Upload — see §11.7 for the resolved destination).

⚠️ **Running A + B simultaneously hits Teams App Catalog
duplicate-id rejection.** When you publish both the AI Teammate zip
(Path A §6) and the Custom Engine Agent zip (§11.6) against the same
tenant, the second upload is rejected because both zips carry the
same `manifest.id` (= `<blueprint-app-id>` by transformer default).
[#26](https://github.com/satscryption/Hermes-A365/issues/26) tracks
adding `--manifest-id auto|<guid>` to mint a fresh catalog id while
keeping `bots[0].botId` = `<blueprint-app-id>`. Until that ships,
walkthrough workaround: open the zip, edit `manifest.json`'s `id`
field to a fresh UUID (`python -c 'import uuid; print(uuid.uuid4())'`),
re-zip, then upload.

### 11.7 — Upload via Microsoft Admin Portal → Agents

Phase 2 walk (2026-05-14) resolved the destination uncertainty: the
canonical path for Custom Engine Agent zips is **MAC → Agents →
Upload custom agent** — the same destination Path A uses for AI
Teammate uploads. Microsoft's manual-deployment doc directs operators
to **Settings → Integrated Apps → Upload custom apps**, but in
practice the Integrated Apps surface shows an in-UI message at upload
time directing you over to the Agents tab (it doesn't accept the zip
itself). Teams Admin Center's **Manage apps** also accepts the zip
but lives in a different operator surface; MAC Agents is the
load-bearing one for Copilot Chat surfacing.

1. Navigate to the [Microsoft Admin Portal](https://admin.microsoft.com)
   (MAC).
2. Left nav: **Agents** (you may need to expand the side menu).
3. Click **Upload custom agent**.
4. Select the `manifest.copilot-chat.zip` from §11.6.
5. Step through the review screens. When asked who the agent is
   available to, pick **Just me** for the first test (assigns only
   to your account; broader scopes work but pollute the tenant if
   anything goes wrong).
6. Click **Deploy** / **Finish**.

Per-user app permission policy is auto-assigned via the **Just me** /
**Specific users** picker — no separate Teams Admin Center step is
needed for the first walk. For broader deployment (whole tenant /
specific groups) you'd typically set a Teams Admin Center policy
explicitly, but that's out of scope for this runbook.

### 11.8 — Verify in M365 Copilot surfaces

Phase 2 walk found that the Custom Engine Agent surfaces in two
different M365 Copilot affordances, on different propagation timelines:

- **M365 Copilot Cowork** (the collaborative-doc Copilot at
  `m365.cloud.microsoft/cowork` or the Cowork tab inside M365 Copilot):
  the agent appears almost immediately after MAC upload (well under 5
  min on satscryption). **Caveat**: @-mentioning the agent in Cowork
  produces a *templated reply paraphrased from the manifest description*
  WITHOUT invoking the bot endpoint — confirmed during the 2026-05-14
  walk by inspecting gateway + cloudflared traffic during the
  exchange (zero POSTs). So Cowork is good for confirming the upload
  surface registered, but **does not** exercise the actual bot
  routing.
- **M365 Copilot Chat** (standalone at
  [https://m365.cloud.microsoft](https://m365.cloud.microsoft) or the
  side-panel inside Word/Excel/PowerPoint/Outlook): propagation takes
  ~5–15 min after MAC upload. The agent first appears under
  **Agents → Available to add** (not in the picker directly).
  Operator clicks **Add**, then the agent shows in the active agents
  picker, can be opened or @ -mentioned, and the conversation routes
  through Azure Bot Service → bot endpoint.

Steps (assuming Copilot Chat propagation has completed):

1. Open [https://m365.cloud.microsoft](https://m365.cloud.microsoft).
2. Find the agents picker (icon near the prompt box, or
   **Agents** in the side rail).
3. The agent appears under **Available to add** — click **Add**.
4. After it moves to the active picker, click it and send a test
   prompt.

Independent verification path: **Azure Portal → your Bot resource →
Test in Web Chat**. This routes Direct Line → BF service → tunnel →
gateway, bypassing Copilot's UX entirely. Cleaner diagnostic when
something doesn't work because it cuts the orchestrator layer out of
the picture. Same path works programmatically via Direct Line API if
you want to see Microsoft's exact error response (script in §11.10's
finding 11 below).

Acceptance gates — Hermes side:

- [ ] Hermes gateway log shows the inbound activity arriving on
      `agent365`: `Agent365Adapter.handle_message(...)` fires.
- [ ] The activity carries a `channelData` shape consistent with the
      Microsoft Teams channel (Path B inbound semantically matches
      Path A's `msteams` channel — same BF activity protocol).
- [ ] The agent loop runs and the reply lands in Copilot Chat within
      ~10 s. Replies > 2 s exercise BF streaming via
      `Agent365Adapter.edit_message` (slices 19s + 19s-bis, #3 closed).

⚠️ **Phase 2 finding — Path B inbound JWT branch is missing.** The
`agent365` plugin's `validate_inbound_jwt`
(`src/hermes_a365/activity_bridge.py`) is A365-only by design (slice
19f): it expects `iss = https://login.microsoftonline.com/<tenant>/v2.0`
+ `azp = APX_PRODUCTION_APP_ID` (Messaging Bot API SP
`5a807f24-…`). Classic Bot Framework S2S tokens — which Path B
inbound carries — have `iss = https://api.botframework.com` and a
different `azp`. So **every Path B inbound currently fails JWT
validation with HTTP 403** and the agent loop never runs. The 2026-05-14
walk confirmed this end-to-end via a Direct Line probe that returned
`BotError / Failed to send activity / 403`. Path B end-to-end is
blocked on this code branch landing; tracked in
[#34](https://github.com/satscryption/Hermes-A365/issues/34) (sibling
to [#33](https://github.com/satscryption/Hermes-A365/issues/33)
which handles the outbound side).

⚠️ **Plugin emits zero request-level logging.** Even
`hermes gateway run -vv` (DEBUG) doesn't surface inbound POST
requests or their 401/403 rejections — only application-level INFO/DEBUG
from the bridge. Operators debugging Path B routing get no
observability without a tcpdump or middleware shim. Worth a polish
pass: add a structured logger to the FastAPI route in
`src/hermes_a365/plugin/adapter.py:419` so operators can correlate
gateway-side rejections with upstream Microsoft errors.

⚠️ **Phase 2 finding (2026-05-15, #33) — Path B outbound is blocked
on Entra identity shape.** [#34](https://github.com/satscryption/Hermes-A365/issues/34)
closed the inbound JWT branch; agent loop now runs end-to-end on Path
B inbound. The outbound side then mints a classic BF S2S
`client_credentials` bearer per [#33](https://github.com/satscryption/Hermes-A365/issues/33)
— but Microsoft returns `AADSTS82001: Agentic application is not
permitted to request app-only tokens for resource Bot.Connector`. The
blueprint Entra app inherits Microsoft's "Agentic" policy class (the
same policy that bit slice 19e for general BF resources) and **cannot
mint app-only tokens for any BF-family resource**, regardless of
scope. Wrapper code is shape-complete (token mint logic, dispatcher,
caching, tests); the gateway surfaces the failure cleanly with the
operator-actionable error message. Path B end-to-end is blocked on
**registering a separate non-agentic Entra app for Path B outbound** —
filed as the #33 follow-up. Until that lands, Path B inbound replies
hang silently on the Copilot Chat side and the gateway log shows the
AADSTS82001 message.

Acceptance gates — Microsoft side:

- [ ] Bot resource's **Test in Web Chat** affordance (Azure Portal
      → your Bot resource → Test in Web Chat) round-trips a message
      end-to-end. This is an independent verification path that
      bypasses Copilot Chat and exercises just Bot Service →
      `/api/messages` → reply. **#34 closed inbound; outbound
      currently fails with AADSTS82001 — gated on the #33 follow-up
      (separate Entra identity).**

Common failure shapes (encoded from the 2026-05-14 walk):

- **Test in Web Chat shows the message but no reply, no error toast** —
  Microsoft's BF service is calling our bot and getting an HTTP error
  back, but the portal panel hides it. Use the Direct Line probe
  (§11.10 finding 11) to surface Microsoft's actual error message.
- **Direct Line probe returns `BotError / Failed to send activity /
  403`** — bot endpoint is reachable, JWT validation rejected the
  token. Currently the only known cause for Path B is the missing
  BF-issuer branch (#34). After #34 lands, if you still see 403 here,
  capture the JWT's `iss`/`aud`/`azp` claims via a temporary log
  hook in the validator and compare against the BF branch's
  expectations.
- **Agent doesn't appear in Copilot Chat's picker even after 15 min** —
  check `Available to add` first; if it's there, click **Add**. If
  not there at all, confirm the per-user policy includes your test
  user (MAC → Settings → Integrated Apps → click the app → check
  policy assignment).
- **Agent appears but messages drop silently** — check `az bot show`
  for the current `properties.endpoint`. Quick tunnels rotate; if the
  URL drifted, run §11.5 to re-point.

### 11.9 — Tear down (Path B only; leaves Path A intact)

To remove the Azure-side Bot Service registration while keeping the
blueprint Entra app for Path A's continued use:

```bash
# Remove the Microsoft Teams channel first (Bot Service deletes are
# happier when channels are gone first).
az bot msteams delete --resource-group <azure-rg> --name <azure-bot-name>

# Remove the Bot resource itself.
az bot delete --resource-group <azure-rg> --name <azure-bot-name>

# Optionally remove the resource group if it was created solely for
# this bot.
# az group delete --name <azure-rg> --yes --no-wait
```

The Teams App Catalog entry from §11.7 also needs cleanup —
[Microsoft Admin Portal](https://admin.microsoft.com) → **Settings**
→ **Integrated Apps** → select the app → **Remove**.

Path A's blueprint Entra app + service principal + agentic-user
instances remain untouched. To tear down both paths, run §11.9
first, then §10.

Phase 2 walk should capture:

- Whether `az bot delete` propagates immediately or has a soft-delete
  retention window (Path A's AI Teammate Admin-Centre Delete is
  30-day soft-delete).
- Whether removing the Teams App Catalog entry is sufficient to
  remove the agent from per-user pickers, or if a per-user policy
  update is also needed.
- Whether the Bot Service `--purge-orphans` concept (analogous to
  Path A's `cleanup --purge-orphans`) applies to anything Azure-side
  — channel registrations, App Insights resources, etc.

### 11.10 — Phase 2 walkthrough log

First walk completed 2026-05-14 against the satscryption Azure GA
account (sub `Hermes-A365`, region `westeurope`). Microsoft-side
steps walk green end-to-end — bot resource reachable, BF service
correctly invokes `/api/messages`, manifest surfaces in Copilot
Cowork and (after Add) M365 Copilot Chat. The end-to-end Copilot
Chat round-trip is blocked on a **plugin-side gap** ([#34](https://github.com/satscryption/Hermes-A365/issues/34)):
the inbound JWT validator is A365-only and rejects BF-shaped tokens
with HTTP 403. Once #34 lands, re-walk §§11.6–11.8 to close
[#16](https://github.com/satscryption/Hermes-A365/issues/16) (Path B
Copilot Chat surfacing, sibling to this validation work).

| # | Section | Finding | Resolution |
|---|---|---|---|
| 1 | §11.2 | `Microsoft.BotService` resource provider ships `NotRegistered` on fresh Azure subscriptions; `az bot create` fails until it's registered. No upstream error message guides operators toward the fix. | §11.2 patched with `az provider register --namespace Microsoft.BotService --wait` as a prereq. Slice 20a `bot-service verify` should probe; `bot-service create --apply` should auto-register. |
| 2 | §11.3 | `az group create --location global` (anticipated by #28's draft) is invalid; resource-group `--location` is a regional spec (`westeurope`, `eastus`, `uksouth`, …). The bot resource itself stays `--location global` per Bot Service convention; RG region is metadata-only. | §11.3 patched: `--location <region>` on the group, `--location global` on the bot. `<region>` added as a recorded variable in §11.2. |
| 3 | §11.3 | `az bot create` auto-enables `webchat` + `directline` channels at creation; only `msteams` needs an explicit `az bot msteams create`. Useful operationally — Test in Web Chat works the moment the bot exists. | §11.3 verify-step note added. |
| 4 | §11.4 | **`az bot msteams create` is incomplete.** Channel provisions with `acceptedTerms: false`, and Microsoft holds **all** traffic on terms-unaccepted channels with no surfaced error. The CLI exposes no `--accepted-terms`-shaped flag; only the Azure portal UI or an ARM PATCH on `properties.properties.acceptedTerms` sets it. Confirmed root cause of "no traffic to gateway despite correct endpoint" during the 2026-05-14 walk. | §11.4 patched with the ARM PATCH recipe. Slice 20a `bot-service create --apply` MUST follow `az bot msteams create` with this PATCH or operators hit the same dead end. Filed as the headline operator-ergonomics finding. |
| 5 | §11.6 | `pipx install hermes-a365` lands an older binary at `~/.local/bin/hermes-a365` (no `--copilot-chat` flag) that PATH-shadows the Hermes venv install (v0.5.2+). `which hermes-a365` returns the wrong one. | §11.6 patched with explicit venv-binary invocation note. Small wrapper packaging clarification worth folding into README's install section. |
| 6 | §11.6 | `--copilot-chat` transformer strips `agenticUserTemplates` from `manifest.json` but leaves the sibling `agenticUserTemplateManifest.json` file inside the zip. Microsoft tolerates the leftover (uploads work), but it's dead weight in a Custom Engine Agent zip. | Small slice-19u-a polish item; note added inline in §11.6. |
| 7 | §11.6 | Wrapper's post-publish console message says "Teams Admin Center → Manage apps → Upload" but the actual destination (resolved in §11.7 below) is MAC → Agents → Upload custom agent. Three-way doc drift: GA CLI says one thing, our wrapper another, Microsoft's transcribed doc a third. | §11.7 resolved the canonical destination; small slice-19u-a polish item to update the wrapper's post-publish text. |
| 8 | §11.7 | The MAC upload destination uncertainty resolves to **MAC → Agents → Upload custom agent**. Attempting upload via Settings → Integrated Apps surfaces an in-UI message at upload time directing you over to Agents tab — the Integrated Apps surface doesn't accept the Custom Engine Agent zip itself, but it does signpost. Teams Admin Center → Manage apps is a third surface that exists but lives in a separate operator lane. | §11.7 rewritten with the resolved destination. Banner removed. |
| 9 | §11.8 | Custom Engine Agent surfaces in **Microsoft 365 Copilot Cowork** within a few minutes of MAC upload. Surfacing in main **M365 Copilot Chat** (`m365.cloud.microsoft`) takes longer (~5–15 min on satscryption) and shows up under **Available to add** rather than the active agents picker; operator must click **Add** before it becomes usable. | §11.8 rewritten with both surfaces + propagation timing. |
| 10 | §11.8 | Cowork @-mention produces a templated reply paraphrased from the manifest description WITHOUT calling the bot endpoint. Zero POSTs hit the gateway or cloudflared during the exchange. So Cowork is good for confirming the manifest registered, but **does not** exercise the actual bot routing — debugging Path B requires Copilot Chat (`m365.cloud.microsoft`) or Azure Portal **Test in Web Chat** / a Direct Line probe. | §11.8 flagged the Cowork-orchestrator distinction. |
| 11 | §11.8 | **HEADLINE.** `agent365` plugin's `validate_inbound_jwt` in `src/hermes_a365/activity_bridge.py:1063` is A365-only by design (slice 19f): it expects `iss = https://login.microsoftonline.com/<tenant>/v2.0` and `azp ∈ {APX_PRODUCTION_APP_ID}`. Classic Bot Framework S2S tokens (which Path B inbound carries) have `iss = https://api.botframework.com` and a different `azp`, so every Path B request fails JWT validation with HTTP 403 and the agent loop never runs. Confirmed by Direct Line probe returning `BotError / Failed to send activity / 403` via Microsoft's BF service. **Path B end-to-end blocked on this code branch landing.** | Filed as [#34](https://github.com/satscryption/Hermes-A365/issues/34) — sibling to [#33](https://github.com/satscryption/Hermes-A365/issues/33) (Path B outbound S2S). Needs a Path-A-vs-B detection branch on the route handler (likely keyed on token `iss` or activity `serviceUrl`) and a BF-shaped validator path that uses BF's JWKS + a BF-azp allowlist. |
| 12 | §11.8 | `agent365` plugin emits **zero request-level logging** for `/api/messages`. Even `hermes gateway run -vv` (DEBUG) doesn't surface inbound POSTs or their 401/403 rejections — only application-level bridge INFO/DEBUG. Operators debugging Path B routing get no observability without a tcpdump or middleware shim. Would have shortened the §11.8 walk from ~60 min to ~10 min if it existed. | §11.8 flagged. Worth a polish pass: add a structured logger to the FastAPI route in `src/hermes_a365/plugin/adapter.py:419` that logs `(method, path, source_ip, response_status, latency_ms)` at INFO and the rejection reason at WARNING. |
| 13 | §11.9 | Not exercised on the 2026-05-14 walk — bot resource left running for #34 dev cycle (see §11.10 footer below for resume instructions). Phase 2 follow-up: walk the teardown once #34 lands and confirm `az bot delete` retention/soft-delete shape + Teams App Catalog removal propagation. | Open follow-up. Skip until #34 closes. |
| 14 | §11.8 | **HEADLINE for Path B outbound (#33 walk, 2026-05-15).** After #34 closed the inbound JWT branch, the live Direct Line probe showed Microsoft posting a real BF S2S token-mint request… but Microsoft returned `AADSTS82001: Agentic application '2e5e2dea-…' is not permitted to request app-only tokens for resource '8d2d3342-cf29-4959-9577-0e0eafbd16bc' (Bot Framework V4)`. The blueprint Entra app inherits Microsoft's Agentic-application policy class (which also blocked the v0.1 design's app-only chain — slice 19e replaced it with the user-FIC chain for Path A) and **cannot mint app-only tokens for ANY BF-family resource**, regardless of scope. Path B outbound's BF S2S `client_credentials` flow architecturally needs a non-agentic identity, which the blueprint app can't satisfy. | #33 wrapper code shipped (`dddb96b` had #34 inbound; the follow-on commit ships the BF S2S mint + dispatcher + path-tag refinement + AADSTS82001-aware error). Live mint fails AADSTS82001 with an operator-actionable error message in the gateway log pointing at the #33-follow-up issue. **The follow-up tracks the separate-Entra-app registration walk** (operator: `az ad app create` + admin consent for `Bot.Connector` + `az bot update --appid <new>` + republish manifest with new `botId`). |

**Reproducing the Direct Line probe** (for finding 11, when #34 dev
wants to verify the failure mode pre-fix or the success mode post-fix):

```bash
# Set these to match your bot:
SUB=<subscription-id>
RG=<azure-rg>
BOT=<azure-bot-name>
CHANNEL_ID="/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.BotService/botServices/${BOT}/channels/DirectLineChannel"

# 1. Fetch the Direct Line site secret.
DL_SECRET=$(az rest --method POST \
    --url "https://management.azure.com${CHANNEL_ID}/listChannelWithKeys?api-version=2022-09-15" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['properties']['properties']['sites'][0]['key'])")

# 2. Exchange the secret for a conversation token.
TOKEN_RESP=$(curl -sS -X POST -H "Authorization: Bearer $DL_SECRET" \
    https://directline.botframework.com/v3/directline/tokens/generate)
DL_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")

# 3. Open a conversation.
CONV_RESP=$(curl -sS -X POST -H "Authorization: Bearer $DL_TOKEN" \
    https://directline.botframework.com/v3/directline/conversations)
CONV_ID=$(echo "$CONV_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['conversationId'])")

# 4. POST an activity.
curl -sS -X POST -H "Authorization: Bearer $DL_TOKEN" -H "Content-Type: application/json" \
    -d '{"type":"message","from":{"id":"probe"},"text":"hi"}' \
    "https://directline.botframework.com/v3/directline/conversations/${CONV_ID}/activities"

# 5. Read back any error or reply that came from the bot endpoint.
sleep 2
curl -sS -H "Authorization: Bearer $DL_TOKEN" \
    "https://directline.botframework.com/v3/directline/conversations/${CONV_ID}/activities"
```

Pre-#34 expected shape on step 4: `{"error":{"code":"BotError",
"message":"Failed to send activity: bot returned an error"},
"httpStatusCode":403}`. Post-#34 expected: an `id` field (the
activity id Microsoft minted) and step 5 returns the bot's reply.

**State at end of 2026-05-14 session (left running for #34 dev)**:

- Sub `Hermes-A365` (`aede290c-47bb-453d-947d-d146d6998bec`), region
  `westeurope`, Owner RBAC on `sadiq.jaffer@satscryption.io`.
- Resource group `hermes-a365-bots`.
- Bot `hermes-inbox-helper-bot`, `--app-type SingleTenant`, `--appid`
  = R8 blueprint `2e5e2dea-…`, `--sku F0`, endpoint = the cloudflared
  URL from session start (will rotate on cloudflared restart).
- Channels: `webchat` + `directline` auto-enabled, `msteams` enabled
  with `acceptedTerms: true` (PATCH at 2026-05-14T18:01).
- Teams App Catalog entry `Hermes Inbox Helper R8 CC` uploaded via
  MAC Agents tab, manifest.id `98e1cf3b-…`, added to test user with
  **Just me** scope.
- Hermes gateway running on `127.0.0.1:3978` with `-vv` (DEBUG).
- cloudflared quick-tunnel pointed at `localhost:3978`.

**To resume #34 dev**:

```bash
# Verify gateway + tunnel are alive (may have died if session quit).
pgrep -fl "hermes gateway"
pgrep -fl cloudflared

# If dead, restart and capture the new tunnel URL.
/Users/sadiqjaffer/.local/bin/hermes gateway run -vv \
    2>&1 | tee /tmp/hermes-gateway-phase2.log &
cloudflared tunnel --url http://localhost:3978 --no-autoupdate \
    2>&1 | tee /tmp/cloudflared-phase2.log &
NEW_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \
    /tmp/cloudflared-phase2.log | head -1)

# Re-point the bot endpoint.
az bot update --resource-group hermes-a365-bots \
    --name hermes-inbox-helper-bot \
    --endpoint "${NEW_URL}/api/messages"

# After the #34 fix lands, re-test via either Test in Web Chat in the
# Azure Portal or the Direct Line probe recipe above. Watch the
# gateway log for a successful 200 + handle_message line.
```

### Sources

- [Custom Engine Agents for Microsoft 365 (overview)](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-custom-engine-agent)
  — manifest 1.21+ requirement, streaming ordering rules,
  channel list.
- [Create and Deploy a Custom Engine Agent with M365 Agents SDK](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/create-deploy-agents-sdk)
  — links into the deployment chain.
- [Deploy your agent to Azure manually](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/deploy-azure-bot-service-manually)
  — the canonical manual deployment doc; source of §11.4 + §11.7's
  exact wording on channel + upload steps.
- [Provision your Azure Bot Service resources manually](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/provision-azure-bot-service-manually)
  — authentication-type matrix (Client Secret / Managed Identity /
  Federated Credentials).
- [`az bot create` reference](https://learn.microsoft.com/en-us/cli/azure/bot#az-bot-create)
  — canonical argv for §11.3.
- [`az bot msteams` reference](https://learn.microsoft.com/en-us/cli/azure/bot/msteams)
  — canonical argv for §11.4.
- [Custom Engine Agent emitter (slice 19u-a)](https://github.com/satscryption/Hermes-A365/blob/main/src/hermes_a365/publish.py)
  — Hermes-A365 side of the §11.6 manifest emission.
