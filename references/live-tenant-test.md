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
  display name to `Agent 365 CLI` (the appId stays stable). ⚠️ Doctor's
  WARN message currently misreads "app not found" as "az not signed
  in?" — fix queued for slice 18j.
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

**Pass criterion:** prints a recommendation and exits 0. ⚠️ The
recommendation strings refer to "Agent 365 add-on" and "E7" — those
are marketing names that don't exist in `subscribedSkus` (the actual
SKU is `MICROSOFT_AGENT_365_TIER_3`). The reason text also has a
stringification bug ("plan=E5 < E5"). Both queued for slice 18j;
neither blocks. The earlier draft claimed this step writes
`A365_LICENSE_MODEL` into `~/.hermes/.env` — it doesn't.

- [ ] `license` recommendation rendered.

## 3. register — `setup blueprint` + `setup permissions {mcp,bot}`

⚠️ **Major caveat:** the v0.2 `register.py` wrapper currently can't
drive the apply path end-to-end on a real tenant. The `Mutator` uses
`subprocess.run(capture_output=True)`, which buffers stdout — so when
the underlying `a365 setup blueprint` emits a device-code prompt for
its MSAL write-scope auth, the operator never sees it and the
subprocess hangs until the timeout. Slice 18i bumped the timeout from
60 s → 900 s, but the real fix is line-streaming output. Until then,
**run the three steps directly** as below; the wrapper's dry-run is
still useful for reviewing the planned argv.

Dry-run via the wrapper to review the plan:

```bash
uv run python scripts/register.py --agent-name "<display-name>" --tenant-id <tenant-id>
```

Then run each step **directly** through the CLI so you can see prompts:

```bash
a365 setup blueprint     --agent-name "<display-name>" --tenant-id <tenant-id> --no-endpoint
a365 setup permissions mcp --agent-name "<display-name>" --tenant-id <tenant-id>
a365 setup permissions bot --agent-name "<display-name>" --tenant-id <tenant-id>
```

The first call (`setup blueprint`) emits a fresh device-code prompt
for write-scope MSAL bootstrap (separate from the `setup requirements`
auth from step 1) and a follow-up admin-consent browser flow. Subsequent
calls in the same machine reuse the persistent MSAL token cache.

**Pass criterion:** all three steps complete with exit 0. After
`setup blueprint`, `a365.config.json` (operator config) gains the
derived `<display-name> Blueprint` / `<display-name> Identity` names,
and **`a365.generated.config.json`** (gitignored) gains the blueprint
appId, SP id, and the **client secret in plaintext** (DPAPI not
available on macOS / Linux). Treat that file as keychain-equivalent
sensitivity.

Failure modes to watch:

- **AADSTS500011** (license not yet propagated) — wait 5–30 min after
  assigning Tier 3 and re-run.
- **`pwsh` not found** — `a365 setup` errors out citing
  `setup requirements`. Fix the prereq and re-run.
- **"Admin consent has not been granted... non-admin user"** during
  `setup permissions bot` — confusing CLI message that fires even
  when you ARE Global Admin. Observed during the 2026-05-05
  walkthrough: only the `Observability API` S2S app role assignment
  was confirmed; `Messaging Bot API` and `Power Platform API` may
  silently skip. If the bot test message later fails, this is the
  first place to check.

- [ ] `setup blueprint` exits 0; blueprint app + SP visible in Entra.
- [ ] `setup permissions mcp` exits 0.
- [ ] `setup permissions bot` exits 0 (with the S2S caveat above).
- [ ] `a365.generated.config.json` exists and is **gitignored** (verify
      with `git check-ignore -v a365.generated.config.json`).

## 4. consent — admin grant

⚠️ **`consent.py` is currently broken** — it calls
`qs.query_consent(app_id=...)`, a method that doesn't exist on the
v0.2 `QuerySource` protocol (Slice 18f rewrote the protocol around
`query_blueprint_scopes` / `query_instance_scopes` but didn't update
`consent.py`). Fix queued for slice 18j.

In practice, **admin consent for the blueprint app is already granted
by `setup blueprint`** (the second device-code flow opens an
admin-consent URL the operator approves). Verify directly via Graph:

```bash
SP_ID=$(az ad sp list --display-name "<display-name> Blueprint" --query "[0].id" -o tsv)
az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/oauth2PermissionGrants" \
    --query "value[].{resource:resourceId, scope:scope, consentType:consentType}" -o json
```

You should see `consentType: AllPrincipals` grants across Microsoft
Graph (Mail/Chat/Files/Sites/etc.), Connectivity API, Agent 365
Tools, Agent 365 Observability, and MCP Servers Metadata API.

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

⚠️ Two minor warts (queued for slice 18j): the file still includes a
v0.1-leftover `A365_CLI_VARIANT` key, and the dry-run renders a
fresh `AA_INSTANCE_ID` that gets discarded by `--apply` (which
generates its own). Neither blocks.

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

⚠️ Two wrapper bugs noted (queued for slice 18j):
- Pass the **slug** (`<slug>`) not the display name. The wrapper
  doesn't slugify, so `status "Hermes Inbox Helper"` looks for
  `~/.hermes/agents/Hermes Inbox Helper/.env` — wrong.
- The `blueprint_scopes` `detail` field shows the CLI's progress
  message ("Querying Entra ID for…") rather than the result. State
  is correct; only the human-readable detail is misleading.

- [ ] `local_config: ok`
- [ ] `blueprint_scopes: ok`
- [ ] `instance_scopes: ok`

## 10. cleanup — leave the tenant clean

⚠️ **The wrapper composes `--yes` on each subcommand**, but the GA CLI
only accepts `-y` / `--yes` on the **parent** `cleanup` verb — not on
`cleanup azure` / `instance` / `blueprint`. Running the wrapper with
`--apply` will fail at the first cloud step. Queued for slice 18j.

Dry-run via the wrapper to review the plan:

```bash
uv run python scripts/cleanup.py --agent-name "<display-name>"
```

Then apply via the parent CLI verb (cleans azure + instance +
blueprint in one shot):

```bash
a365 cleanup -y --agent-name "<display-name>" --tenant-id <tenant-id>
```

The CLI deletes the blueprint Entra app + SP, removes the local
`a365.config.json` and `a365.generated.config.json` (after backing
both up to `*.backup-<timestamp>.json` in the same directory), and
emits no errors on absent resources (e.g. no Azure App Service in
your test setup is fine — it's a no-op).

⚠️ **Backup files contain the secret.** The
`a365.generated.config.backup-*.json` file the cleanup leaves behind
holds the same plaintext client secret as the original. Slice 18i
gitignored both backup patterns; if you've cloned to a fresh checkout,
double-check `git check-ignore -v a365.generated.config.backup-*.json`
returns a hit before running `git add`.

You'll also need to remove the per-agent local dir manually (the
wrapper's name↔slug bug means cleanup didn't):

```bash
rm -rf ~/.hermes/agents/<slug>
```

- [ ] `cleanup --apply` exits 0.
- [ ] Blueprint app + service principal removed from Entra Portal.
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
| 1 | `_common.py:48` `safe_run` | Returns `None` for empty stdout+stderr success, conflated with failure (the `or None` clause). Affects probes that expect empty as a valid result. |
| 2 | `doctor.py probe_custom_client_app` | When the app isn't found, the WARN message says "az not signed in?" — actual cause is `safe_run` (#1) returning `None`. Misleading. |
| 3 | `doctor.py probe_custom_client_app` | Hard-codes `"Agent 365 CLI"`. Allow operator override via `~/.hermes/.env` or a flag. |
| 4 | `license.py` reason text | Renders nonsensical "users=N < 25 or plan=E5 < E5" stringification. |
| 5 | `license.py` / SKILL.md / runbook | Earlier docs claimed `license` writes `A365_LICENSE_MODEL` to `~/.hermes/.env`. It doesn't. Either implement or drop the claim from docs. |
| 6 | `license.py` SKU naming | Recommends "Agent 365 add-on" / "E7" — the actual GA SKU is `MICROSOFT_AGENT_365_TIER_3`. Update `references/license-cost-table.md` too. |
| 7 | `register.py` rendered argv | Multi-word agent names render unquoted (`--agent-name Hermes Inbox Helper`). Fine for the actual subprocess call (passed as list), misleading if a user copy-pastes. |
| 8 | `consent.py` | Calls `qs.query_consent(app_id=...)`, a method that doesn't exist on the v0.2 `QuerySource` protocol. Slice 18f rewrote `status.py` against the new protocol but missed `consent.py`. |
| 9 | `instance_create.py` | Writes a leftover `A365_CLI_VARIANT` key (v0.1 artefact). |
| 10 | `instance_create.py` | Dry-run renders a fresh `AA_INSTANCE_ID` that `--apply` discards in favour of its own. Surprising. |
| 11 | `cleanup.py` wrapper | Composes `--yes` on each subcommand; the GA CLI only accepts `-y` on the parent `cleanup` verb. Apply path errors immediately. |
| 12 | `cleanup.py` / `status.py` | Both look up local files using the literal `--agent-name` (e.g. `Hermes Inbox Helper`) rather than the slug (`inbox-helper`). Local cleanup misses the dir; status misses the .env. |
| 13 | `status.py` `blueprint_scopes` parser | Reports the CLI's progress message ("Querying Entra ID for…") in the `detail` field instead of the result. State is correct; only the human-readable string is wrong. |
| 14 | `publish.py` wrapper | Doesn't distinguish blueprint-only vs `--aiteammate` flow. The blueprint-only `a365 publish` does a Graph `POST` (no zip); only AI Teammate produces a zip. |
| 15 | SKILL.md / runbook | Claim "T2 client secret lives only in the keychain". On macOS / Linux DPAPI isn't available, so the CLI writes the secret in plaintext to `a365.generated.config.json`. The runbook now reflects this; SKILL.md should too. |
| 16 | `references/a365-cli-reference.md:144` | Says `brew install --cask powershell`. The cask was deprecated 2026-05; use the formula `brew install powershell`. |
| 17 | `mutator.py` (architectural) | `subprocess.run(capture_output=True)` blocks until completion, so device-code prompts and admin-consent flows from `a365 setup *` are invisible. Slice 18i bumped the timeout to 900 s as a stop-gap. The proper fix is line-streamed output via `Popen` with `stdout=PIPE` and a reader thread. |
| 18 | `setup permissions bot` interaction | The CLI prints "Admin consent has not been granted... non-admin user" mid-flight even when run as Global Admin, then claims success at the end with only `Observability API` S2S confirmed. Investigate whether `Messaging Bot API` and `Power Platform API` S2S app role assignments silently skip. |
