# v0.9.0 focused hardening walk

Status date: 2026-10-05.

Governing contract: [issue #123](https://github.com/Satscryption/Hermes-A365/issues/123),
including its security-baseline amendment and recorded #121 evidence.
Use [the tenant runbook](live-tenant-test.md) for setup mechanics, but verify
its dated commands against the current CLI before execution.

## Current checkpoint

- Live walk source: `ec6c67274fd33ee2d4a77df9419aae8581fe2875` on `main`,
  including merged PRs #145, #146 and #147. Main test and both CodeQL
  analysis jobs passed on this exact revision.
- PR #144 is merged. CodeQL and secret scanning report zero open alerts.
- Prerequisites #19, #100, #118, #121 and #125 are closed. #102 and #105
  remain open; their remaining live acceptance must be reconciled here.
  #107 explicitly requires the positive and negative Entra exchange below.
- State: live walk started; Azure provisioning, wrong-tenant/target refusals
  and sentinel protection passed. Runtime admission is blocked by
  [#148](https://github.com/Satscryption/Hermes-A365/issues/148), a verified
  Hermes dependency conflict. The unauthenticated Agent 365 prerequisite
  process was cancelled; obtain a fresh device code when resuming.
- User requested starting #123. Preparation, readback and local checks have
  begun. The user approved dependency remediation and merging #145/#146;
  both merges and the subsequently approved #147 merge are complete.
  The user then requested starting the live walk. The disposable Azure
  phase below was provisioned and cleaned up. No release was published,
  issue closed, gateway started or tunnel opened.
- Local gateway port 3978 had no listener at preflight. This is a snapshot,
  not permission to start a tunnel later without checking again.
- Azure read access succeeds. The shell's default subscription is Satscrip,
  not Hermes-A365. Use an explicit Hermes-A365 subscription for every walk
  command; do not change the shared default or touch other workloads.
- Dedicated resource names: agent `Hermes v090 Walk`, slug
  `hermes-v090-walk`, resource group `hermes-v090-walk-rg`. The app-name
  inventory returned no existing matching registration; ARM inventory in
  Hermes-A365 returned no resources. Local config is owner-only and ignored.
- Installed tooling: Hermes v0.20.0 (2026.8.3), source
  `f60f6d534375ccd6ec78d47ff1b5a97371cd8d60`; Agent 365 CLI
  `1.1.181+9a1be7ddd7`; PowerShell 7.6.1. Doctor returns WARN for the
  already-documented CLI secret-persistence issue, with other probes OK.
- The macOS keychain write backend requires explicit insecure-CLI opt-in.
  The operator decision is pending; no write opt-in has been enabled.
- Cleanup readback: resource group absent; dedicated Path B app and service
  principal queries return empty lists. Only ignored local configuration and
  sidecar provenance/backups remain. The sentinel's resource provider
  `Microsoft.ManagedIdentity` was registered by Azure CLI and remains
  registered; no managed identity or role grant remains from this phase.

## Evidence already available

| Check | Observed result | Scope |
|---|---|---|
| Main ruleset 21134657 | Active; PR and strict `test` check required; deletion and force-push blocked; administrator bypass exists | Current configuration |
| Tag ruleset 21134661 | Active on `refs/tags/v*`; creation, update and deletion restricted; administrator bypass exists | Current configuration |
| Actions default token | `read`; PR-review approval disabled | Current configuration |
| Workflow action references | All action references in tracked workflows use full commit SHAs | Inspected source |
| PyPI environment 20718252432 | Required reviewer `SadiqJaf`; administrator bypass disabled | Current configuration |
| Dependency security controls | Vulnerability-alert endpoint returns 204; automatic security updates enabled | Current configuration |
| Secret controls | Secret scanning and push protection enabled; zero open secret alerts | Current configuration |
| Private reporting | Enabled; tracked `SECURITY.md` exists | Current configuration |
| CodeQL | Both analyses passed on the merged walk candidate; initial preflight found zero open alerts | Current source analysis |
| Latest GitHub Release | `v0.8.5` | Current release readback |

Issue #121's approval-boundary evidence remains historical evidence:
[rejected run](https://github.com/Satscryption/Hermes-A365/actions/runs/33082821976)
and [approved dry run](https://github.com/Satscryption/Hermes-A365/actions/runs/33082950052)
both bind to `5cd80753857f128755470b7402eeee26f036db55`. Their current
conclusions are failure and success respectively. The issue records that
rejection executed zero publish steps, the approved dry run skipped PyPI,
and neither created a release. A source comparison confirms the publish
workflow and its requirements file are unchanged between that revision and
the inspected source. The current environment readback retains the same
reviewer and administrator-bypass restrictions. Carry forward this gate's
evidence unless those inputs change before final acceptance.

Local regression command on the inspected source:

```sh
uv run --frozen pytest tests/test_activity_bridge.py tests/test_agent365_plugin.py \
  -k 'mismatch or dedup or user_fic or coalesc or evict or corrupt_raw or recursionerror or bounded' -q
```

Result: 56 tests passed. This is local regression evidence, not proof of
Microsoft policy, live UI delivery, restart behavior or Azure deletion safety.

The following local prerequisite checks also passed (266 tests):

```sh
uv run --frozen pytest tests/test_cleanup.py tests/test_bot_service.py \
  tests/test_keychain.py tests/test_secrets_provider.py tests/test_publish_workflow.py
```

The secret-store tests use mocks and do not establish real credential-store
or process-restart evidence. Total for these two disjoint selections: 322 tests.

## Findings and remediation

1. Initial preflight found fifteen Dependabot alerts: AnyIO
   alerts 16-17 and PyJWT alerts 18-30. PRs
   [#145](https://github.com/Satscryption/Hermes-A365/pull/145) and
   [#146](https://github.com/Satscryption/Hermes-A365/pull/146) update to
   AnyIO 4.14.2 and PyJWT 2.15.0. Both merged with passing test and
   dependency-review jobs. Their PR CodeQL check was neutral, not a
   successful new source scan. A subsequent GitHub API readback returned
   zero open Dependabot alerts, including resolution of alert 30 whose
   advisory metadata omitted a first-patched version.
2. Published requirements previously allowed PyJWT 2.13.0 and lacked an
   AnyIO floor. This branch requires `anyio>=4.14.2` and
   `pyjwt[crypto]>=2.15.0` independently in both `bridge` and `dev` extras,
   with regenerated lock metadata. Wheel and sdist metadata enforce both
   floors; base requirements, the crypto extra and Python >=3.11 are
   preserved. PR #147 is merged; the change needs a future release to protect new
   public installations; it cannot update already-installed environments.
3. The tenant runbook previously started its tunnel before the bridge in
   section 9c. This branch requires free-port checks, foreground responder
   and bridge startup, listener-ownership verification and `/healthz`
   success before starting the tunnel. No tunnel was launched for validation.
4. **Open, #148:** Hermes Agent 0.20.0 pins `PyJWT[crypto]==2.13.0`, while
   this candidate requires `PyJWT[crypto]>=2.15.0`. Combined local-source
   `uv pip compile --offline --python-version 3.11 --no-header -` returns
   `No solution found` and explicitly identifies those incompatible pins.
   The installed Hermes runtime contains AnyIO 4.12.1, PyJWT 2.13.0 and
   FastAPI 0.133.1, and no A365 distribution. The optional Hermes `web`
   extra also pins the older FastAPI. Its checkout has unrelated local
   changes; none were modified. Prepare and verify a compatible isolated
   runtime before the live gateway cases. Do not weaken the patched floors.

Remediation verification on the branch integrating the merged dependencies:

- `uv lock`: no resolved package-version changes beyond the already merged
  dependency PRs; only project dependency metadata changed.
- `uv build`: wheel successfully built from the source distribution.
- Structured inspection of wheel `METADATA` and sdist `PKG-INFO`: both
  extras exclude AnyIO 4.13.0/4.14.1 and PyJWT 2.13.0/2.14.0, and admit
  AnyIO 4.14.2/PyJWT 2.15.0.
- Offline wheel-only `uv pip compile --python-version 3.11`, independent of
  this repository's lock: both extras resolve with the patched pair;
  pinning either AnyIO 4.14.1 or PyJWT 2.14.0 fails with a dependency
  conflict for each extra (four negative controls).
- `uv run --locked --all-extras pytest`: 1,645 passed, five existing warnings.
- Ruff: all checks passed.

These are release-preflight findings, not a claim that all upstream
advisories are exploitable in Hermes. JWT verification pins RS256, the
issuer-routing peek creates a fresh options dict and catches parse errors,
and key retrieval uses the project's HTTP client rather than PyJWKClient.
Those constraints reduce several reported attack paths but do not justify
retaining old dependency versions in the release candidate.

## Execution order and acceptance ledger

After dependency remediation, freeze the merged source SHA, resolved
dependency versions, gateway build and sanitized configuration identity.
Execute against a dedicated slug and resource group in Hermes-A365.
Record expected/observed results and sanitized evidence for each row.

| Row | Required observation | State |
|---|---|---|
| 1a | Security controls match #125 and every active alert has a supported disposition | Controls verified; zero open dependency alerts; package floors merged in #147 |
| 1b | Publish approval/rejection evidence remains applicable; rejection creates no package or release | Prior evidence carried forward after workflow/configuration comparison |
| 2a | Copilot Chat receives two coalesced segments promptly, once, in order | Not run |
| 2b | Uninstall/eviction clears active stream and coalescing state; no delayed doomed POST | Not run |
| 2c | Gateway restart restores bounded registry/seen state | Not run |
| 2d | Backed-up non-dict `raw` corruption recovers without breaking another conversation | Not run |
| 3a | Legitimate Path A Teams 1:1 validates inbound, mints user-FIC and replies | Not run |
| 3b | Configured agentic user's real Entra exchange succeeds | Not run |
| 3c | Known tenant user without the required FIC relationship is rejected before token issuance | Not run |
| 3d | Authenticated local harness rejects identity/dedupe negatives before minting; logs contain no credentials | Regression selection passed; final mapping/log review pending |
| 4a | Explicit subscription provisioning lands only in the chosen subscription | Passed: Bot Service ARM ID and configuration read back in Hermes-A365 |
| 4b | Tenant/subscription mismatch is refused before mutation | Live wrong-tenant refusal passed; selected-subscription mismatch remains harness-covered |
| 4c | Partial cleanup preserves credentials and sidecars for live resource kinds | Partial: Path B app and pending-purge sidecar survived; live credential/Path A cases still required |
| 4d | Unrelated sentinel blocks RG purge and is enumerated | Passed: sentinel retained and explicitly named after bot deletion |
| 4e | Mismatched sidecar/confirmation target is refused | Passed: wrong ARM target and wrong agent name both refused; bot read back unchanged |
| 4f | Unrelated orphan instance deletion requires verified ownership | Not run |
| 4g | All disposable Azure resources are removed and absence is read back | Passed for this Azure phase; repeat for later live phases |
| 5a | Shipped provider stores both Path A and B credentials | Not run |
| 5b | Restart retrieves both credentials and both real messaging paths reply | Not run |
| 5c | Deliberately selected `.env` fallback works | Not run |
| 5d | Evidence/log review finds no secrets, bearer tokens or broad environment dumps | Not run |

## Live evidence: wrong-tenant refusal

Candidate: `ec6c67274fd33ee2d4a77df9419aae8581fe2875`.
The selected subscription was read back as Hermes-A365, enabled, in the
expected tenant before the probe. The shell default remained Satscrip.

Sanitized command (the subscription placeholder was the explicitly selected
Hermes-A365 UUID; the all-zero-shaped IDs below were deliberately synthetic):

```sh
hermes-a365 bot-service create \
  --agent-name 'Hermes v090 Walk' \
  --resource-group hermes-v090-walk-rg \
  --endpoint https://walk-123.invalid/api/messages \
  --subscription-id <Hermes-A365-subscription> \
  --tenant-id 00000000-0000-0000-0000-000000000001 \
  --appid 00000000-0000-0000-0000-000000000002 \
  --sidecar a365.bot-service.config.json --apply
```

Expected: refuse before provider registration, resource-group/bot creation or
sidecar persistence. Observed: exit 1, `belongs to tenant <actual-tenant>,
not the resolved tenant 00000000-0000-0000-0000-000000000001; refusing before
mutation`. Output enumerated the plan but contained no apply-success entries.
The isolated working directory contained no generated Bot Service sidecar.

After the probe, `az group exists --name hermes-v090-walk-rg --subscription
<Hermes-A365-subscription>` returned `false`. This establishes the live
wrong-tenant refusal and resource-group absence, not the other create/cleanup
acceptance cases. No Microsoft inbound traffic was synthesized.

## Live evidence: Azure provisioning and cleanup

All commands in this phase used candidate
`ec6c67274fd33ee2d4a77df9419aae8581fe2875`. The default Azure subscription
remained Satscrip; every ARM command explicitly selected Hermes-A365.
The selected tenant/subscription were read back before mutation phases.

Created a dedicated single-tenant app, `Hermes v090 Walk Path B`, and its
service principal. No password or role grant was created. Provisioned with:

```sh
hermes-a365 bot-service create --agent-name 'Hermes v090 Walk' \
  --resource-group hermes-v090-walk-rg \
  --endpoint https://walk-123.invalid/api/messages \
  --subscription-id <Hermes-A365-subscription> --tenant-id <expected-tenant> \
  --appid <walk-app-id> --sku F0 --region westeurope \
  --sidecar a365.bot-service.config.json --apply
```

Exit 0. Readback confirmed F0, the expected tenant and app binding, the bot
ARM ID in the chosen subscription, and Teams channel enabled. The endpoint
was deliberately non-routable; this was a provisioning test, not messaging.
The created app ID was `cd9118ee-ac88-4830-9052-cb64ee5244bf`; the app object
was `5d633689-a6a8-4b2e-9de3-e7e14ef3e949`, and its service principal was
`87564529-8856-40ce-b08d-91aa2b48aa46`. All were removed at phase exit.

Two cleanup negative controls returned exit 1:

- Correct agent name with `--confirm-bot-target` ending in `wrong-bot`:
  `cleanup requires exact target acknowledgement before any Azure read or mutation`.
- Correct ARM target with agent and confirmation `Hermes v090 Wrong Agent`:
  `sidecar belongs to agent 'Hermes v090 Walk', not 'Hermes v090 Wrong Agent'`.

A subsequent `az bot show` returned the original bot ID and app binding.

Added a disposable, role-free user-assigned identity named
`hermes-v090-walk-sentinel` to the dedicated group. Backed up the sidecar,
then ran cleanup with the correct agent, exact bot ARM ID,
`--purge-resource-group --apply`. Expected and observed exit 1 (purge pending):

```text
deleted Microsoft Teams channel
deleted bot resource hermes-v090-walk-bot
resource group purge remains pending ... it holds 1 non-Hermes-managed or
unverified resource(s) [Microsoft.ManagedIdentity/userAssignedIdentities/hermes-v090-walk-sentinel]
preserved a365.bot-service.config.json until ... purge is read back as complete
```

Readback listed only the sentinel. The separate Path B app still existed
with zero password credentials. This proves preservation of that app, not
preservation of real Path A credentials, which remains untested.

Removed the sentinel, verified the group inventory was empty, and manually
deleted that dedicated group with explicit subscription selection. Removed
the dedicated app by its exact object ID. Final readback:

- `az group exists` for the dedicated group: `false`.
- App query filtered by the exact walkthrough app ID: `[]`.
- Service principal query filtered by that app ID: `[]`.
- Repeated wrapper cleanup: exit 0, bot absent, managed group absent,
  sidecar backed up and retained as post-cleanup provenance as designed.

No temporary runtime secret, gateway process or public tunnel was created.

## Live execution constraints

- Before each mutation phase, record selected tenant/subscription using an
  explicitly targeted account read. Inventory only walk-owned resources.
- Check the chosen TCP port with `lsof`, then start the isolated gateway.
  Verify the listener PID belongs to that process and its health endpoint
  responds before starting the tunnel. Never start these concurrently.
- Back up the walk's sidecars and registry before corruption, migration or
  cleanup tests. Do not reuse production app registrations or credentials.
- Keep secrets inside the credential provider/process. Evidence records only
  token presence, status, error codes and correlation IDs, never token bodies,
  assertions, federated credentials or complete environment contents.
- Exercise synthetic negative identities only through the authenticated test
  harness; the Entra probes call the real exchange directly with test-owned
  identity configuration. Do not forge Microsoft inbound traffic.
- Bind UI observations to the gateway turn/request correlation evidence.
  Local tests or Direct Line responses alone cannot prove Teams/Copilot UI.
- Read back Azure and local cleanup, record every finding's disposition,
  and review the completed ledger before claiming walked-green. Issue #107
  requires both real Entra results. Do not tag v0.9.0 before #123 closes.
