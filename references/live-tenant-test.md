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
gitignored both backup patterns; if you've cloned to a fresh checkout,
double-check `git check-ignore -v a365.generated.config.backup-*.json`
returns a hit before running `git add`.

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
| 11 | `cleanup.py` wrapper | ~~Composes `--yes` on each subcommand; the GA CLI only accepts `-y` on the parent `cleanup` verb.~~ **Fixed in slice 18l** — argv now `a365 cleanup -y <kind> --agent-name X`. |
| 12 | `cleanup.py` / `status.py` | ~~Both look up local files using the literal `--agent-name` rather than the slug.~~ **Fixed in slice 18l** — `_common.slugify` derives the slug from the display name; `cleanup.py` adds a `--slug` override; `status.py` falls back to `slugify(agent_name)` if the literal-name dir doesn't exist. |
| 13 | `status.py` `blueprint_scopes` parser | ~~Reports the CLI's progress message in the `detail` field.~~ **Fixed in slice 18q** — `_meaningful_line` skips lines ending in `…`/`...` and lines starting with progress verbs (Querying / Checking / Resolving / Authenticating / Loading / `[DEBUG]` / `[INFO]`) before picking the representative line. Also tightened `_CONSENTED_HINTS` (dropped `"ok"`, which substring-matched inside words like `token` / `broken` / `blocked` and made the classifier falsely report scopes as consented). |
| 14 | `publish.py` wrapper | ~~Doesn't distinguish blueprint-only vs `--aiteammate` flow.~~ **Fixed in slice 18t** — plan output now prints `output:  Graph API instance registration (no zip)` vs `manifest zip for M365 Admin Centre upload`; result extracts the appropriate artefact (instance id vs zip path); post-apply messages branch (no admin-centre prompt for blueprint-only). |
| 15 | SKILL.md / runbook | ~~Claim "T2 client secret lives only in the keychain".~~ **Doc-fixed in slice 18s** — SKILL.md pitfall #7 now describes the macOS / Linux plaintext-on-disk reality and the gitignored backup-file risk. |
| 16 | `references/a365-cli-reference.md:144` | ~~`brew install --cask powershell` is deprecated.~~ **Doc-fixed in slice 18s** — references doc now says `brew install powershell` (the formula) and notes the cask was renamed to `powershell@preview` and flagged for Gatekeeper failures. Snapshot also gained the macOS `DOTNET_ROOT` gotcha that bit the walkthrough. |
| 17 | `mutator.py` (architectural) | `subprocess.run(capture_output=True)` blocks until completion, so device-code prompts and admin-consent flows from `a365 setup *` are invisible. Slice 18i bumped the timeout to 900 s as a stop-gap. The proper fix is line-streamed output via `Popen` with `stdout=PIPE` and a reader thread. |
| 18 | `setup permissions bot` interaction | The CLI prints "Admin consent has not been granted... non-admin user" mid-flight even when run as Global Admin, then claims success at the end with only `Observability API` S2S confirmed. Investigate whether `Messaging Bot API` and `Power Platform API` S2S app role assignments silently skip. |
