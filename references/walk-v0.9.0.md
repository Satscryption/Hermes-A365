# v0.9.0 focused hardening walk

Status date: 2026-10-05.

Governing contract: [issue #123](https://github.com/Satscryption/Hermes-A365/issues/123),
including its security-baseline amendment and recorded #121 evidence.
Use [the tenant runbook](live-tenant-test.md) for setup mechanics, but verify
its dated commands against the current CLI before execution.

## Current checkpoint

- Inspected source: `0f566cb7ef3134ece5dec2faee387e19c302f24f` on `main`.
- PR #144 is merged. CodeQL and secret scanning report zero open alerts.
- Prerequisites #19, #100, #118, #121 and #125 are closed. #102 and #105
  remain open; their remaining live acceptance must be reconciled here.
  #107 explicitly requires the positive and negative Entra exchange below.
- State: preflight complete; live candidate admission blocked by dependency
  security findings. No live acceptance row is marked passed.
- User requested starting #123. Preparation, readback and local checks have
  begun. Dependency-PR merge approval is pending. No release publication,
  issue closure, cloud provisioning or destructive operation has occurred.
- Local gateway port 3978 had no listener at preflight. This is a snapshot,
  not permission to start a tunnel later without checking again.
- Azure read access succeeds. The shell's default subscription is Satscrip,
  not Hermes-A365. Use an explicit Hermes-A365 subscription for every walk
  command; do not change the shared default or touch other workloads.

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
| CodeQL | Latest main analyses on 2026-09-30 match inspected SHA and report no analysis errors; zero open alerts | Current source analysis |
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

## Open findings

1. Fifteen open Dependabot alerts affect the current lockfile: AnyIO
   alerts 16-17 and PyJWT alerts 18-30. Existing PRs
   [#145](https://github.com/Satscryption/Hermes-A365/pull/145) and
   [#146](https://github.com/Satscryption/Hermes-A365/pull/146) update to
   AnyIO 4.14.2 and PyJWT 2.15.0. Both modify only `uv.lock`, have passing
   test and dependency-review jobs, and are mergeable at preflight.
   Their CodeQL check is neutral, not a successful new source scan.
   The proposed versions are outside every returned vulnerable range.
   Alert 30 has no first-patched version in its metadata, so confirm the
   alert's actual post-merge state rather than claiming automatic closure.
2. Published dependency requirements still allow PyJWT 2.13.0, and do not
   impose an AnyIO security floor. Consumers do not use this project's
   lockfile when installing its wheel. Raise the bridge/dev requirements
   to the patched floors and verify package metadata as part of remediation.
3. The older tenant runbook starts a tunnel before the bridge in its example
   near section 6. For this walk, require the port-ownership and healthy
   gateway checks below before any tunnel starts.

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
| 1a | Security controls match #125 and every active alert has a supported disposition | Blocked: dependency findings above |
| 1b | Publish approval/rejection evidence remains applicable; rejection creates no package or release | Prior evidence carried forward after workflow/configuration comparison |
| 2a | Copilot Chat receives two coalesced segments promptly, once, in order | Not run |
| 2b | Uninstall/eviction clears active stream and coalescing state; no delayed doomed POST | Not run |
| 2c | Gateway restart restores bounded registry/seen state | Not run |
| 2d | Backed-up non-dict `raw` corruption recovers without breaking another conversation | Not run |
| 3a | Legitimate Path A Teams 1:1 validates inbound, mints user-FIC and replies | Not run |
| 3b | Configured agentic user's real Entra exchange succeeds | Not run |
| 3c | Known tenant user without the required FIC relationship is rejected before token issuance | Not run |
| 3d | Authenticated local harness rejects identity/dedupe negatives before minting; logs contain no credentials | Regression selection passed; final mapping/log review pending |
| 4a | Explicit subscription provisioning lands only in the chosen subscription | Not run |
| 4b | Tenant/subscription mismatch is refused before mutation | Not run |
| 4c | Partial cleanup preserves credentials and sidecars for live resource kinds | Not run |
| 4d | Unrelated sentinel blocks RG purge and is enumerated | Not run |
| 4e | Mismatched sidecar/confirmation target is refused | Not run |
| 4f | Unrelated orphan instance deletion requires verified ownership | Not run |
| 4g | All disposable Azure resources are removed and absence is read back | Not run |
| 5a | Shipped provider stores both Path A and B credentials | Not run |
| 5b | Restart retrieves both credentials and both real messaging paths reply | Not run |
| 5c | Deliberately selected `.env` fallback works | Not run |
| 5d | Evidence/log review finds no secrets, bearer tokens or broad environment dumps | Not run |

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
