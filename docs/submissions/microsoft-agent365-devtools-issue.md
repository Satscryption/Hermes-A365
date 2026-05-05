# Microsoft `Agent365-devTools` issue submission

**Target**: <https://github.com/microsoft/Agent365-devTools/issues/new>
**Status**: submitted 2026-05-05.
**Issue**: <https://github.com/microsoft/Agent365-devTools/issues/402>

---

## Update 2026-05-05 — Microsoft response

Same-day reply from @sellakumaran (Microsoft contributor) clarifies the
behaviour described below is **partially intended** and partially a real
but cosmetic bug. Body preserved as-filed for archival accuracy.

- **The "two missing S2S grants" are not missing — they are by design.**
  The blueprint SP is supposed to receive only the
  `Agent365Observability` S2S app-role assignment. Messaging Bot API
  and Power Platform API are configured via delegated OAuth2 grants
  only. The misleading bit is the `Configuring S2S app role
  assignments...` header plus the unconditional success log; both will
  be reworded.
- **The mid-run "non-admin user" line is a real bug.** Microsoft
  confirms it triggers on a consent-not-yet-granted state for
  `AppRoleAssignment.ReadWrite.All`, not on a role check; the
  PowerShell fallback acquires the token interactively, which is why
  exit 0 follows. The wording will be replaced with a role-neutral
  message.
- **`Bot API permissions configured successfully` will be gated** on
  the actual S2S outcome going forward (warning + retry instruction
  on failure rather than a false positive).

All three fixes ship in the next CLI release.

**RETRACTION:** the "Workaround" section below recommends manually
assigning the missing app roles via Microsoft Graph
(`POST /servicePrincipals/<sp>/appRoleAssignments`) — **do not follow
this**. The roles are not missing; doing this would grant privileges
the system does not intend to grant.

---

## Title

`setup permissions bot` silently drops two of three S2S app-role assignments

## Body

### Summary

`a365 setup permissions bot --agent-name <name>` finishes with
`Bot API permissions configured successfully` and exits 0, but only
*one* of the three documented S2S app-role assignments
(`Agent365Observability`) actually lands on the blueprint service
principal. `Messaging Bot API` and `Power Platform API` S2S grants
are silently skipped — no error, no warning. The CLI does configure
the matching delegated OAuth2 grants for all three resources; only
the `appRoleAssignments` list is short.

### Environment

- `a365` CLI: `1.1.171+11c378141d` (`Microsoft.Agents.A365.DevTools.Cli`)
- OS: macOS 26.4.1
- PowerShell: 7.6.1
- Tenant: Frontier Preview Program enrolled, Global Administrator role
- License: `MICROSOFT_AGENT_365_TIER_3` (with `OFFICESUBSCRIPTION` plan
  disabled to coexist with `BUSINESS_PREMIUM_AND_MICROSOFT_365_COPILOT_FOR_BUSINESS`)
- Custom client app: `Agent 365 CLI` (renamed from a previous OpenClaw
  setup; appId stable). `setup requirements` reports
  `Requirements: 2 passed, 0 warnings, 0 failed`.

### Reproduction

This was reproduced **twice** on the same tenant (clean register →
cleanup cycles on 2026-05-04 and 2026-05-05).

```bash
# Fresh blueprint
a365 setup blueprint --agent-name "Test Agent" --tenant-id <guid> --no-endpoint
a365 setup permissions mcp --agent-name "Test Agent" --tenant-id <guid>
a365 setup permissions bot --agent-name "Test Agent" --tenant-id <guid>
```

### Observed CLI output (relevant excerpt)

```
Configuring Messaging Bot API permissions...

Configuring inheritable permissions...
    Messaging Bot API: inheritable permissions configured
    Observability API: inheritable permissions configured
    Power Platform API: inheritable permissions configured
   - OAuth2 grant configured for Messaging Bot API
   - OAuth2 grant configured for Observability API
   - OAuth2 grant configured for Power Platform API

Configuring S2S app role assignments...
   - S2S app role assigned for Observability API   ← only one logged

Admin consent granted.

Bot API permissions configured successfully
```

A single line is logged for the S2S step (`Observability API`),
not three. Only after that does the CLI claim "configured
successfully".

### Confusing intermediate output

A separate confusing line that fires mid-flight even when the
operator IS Global Administrator:

```
Admin consent has not been granted for this application.
You are running as a non-admin user and cannot grant admin consent.
Share this URL with a Global Administrator to grant consent:
  https://login.microsoftonline.com/<tenant>/adminconsent?client_id=<custom-client-app-id>&...
After consent is granted, re-run the command.
MSAL Graph token acquisition failed: Admin consent required. ...
```

It's followed by `S2S app role assigned for Observability API` and
then `Admin consent granted` at the end, so the run still exits 0 —
but operators reasonably interpret "non-admin user" as a real
problem.

### Verification (post-run Graph queries)

After `setup permissions bot` exits 0, query the blueprint SP's
`appRoleAssignments`:

```bash
SP_ID=$(az ad sp list --display-name "Test Agent Blueprint" \
    --query "[0].id" -o tsv)
az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignments" \
    --query "value[].{resource:resourceDisplayName, role:appRoleId}" -o table
```

Output (from the 2026-05-05 reproduction):

```
Resource               RoleId
---------------------  ------------------------------------
Agent365Observability  8f71190c-00c8-461d-a63b-f74abde9ba52
```

Expected: three rows (one per `Configuring inheritable permissions`
log line, one per `OAuth2 grant configured for X` line).

The OAuth2 delegated grants land correctly — querying
`oauth2PermissionGrants` shows all three resources:

```bash
az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/oauth2PermissionGrants" \
    --query "value[].{resource:resourceId, scope:scope}" -o table
```

It's only the `appRoleAssignments` (S2S grants) that are short.

### Impact

For agents that run their messaging path via the GA blueprint, the
missing S2S role on `Messaging Bot API` may block governance-layer
operations (e.g. `Authorization.ReadWrite` against the Messaging
Bot API resource). The `Power Platform API` role gap likely affects
agents that intend to drive Power Platform connectors.

For pure-runtime messaging via the public BF connector
(`https://api.botframework.com`), this gap doesn't appear to break
the synchronous reply path, since outbound replies authenticate
with `https://api.botframework.com/.default` rather than the
Messaging Bot API resource. But operators relying on the documented
"three S2S grants are configured" behaviour will hit this.

### Suggested fixes

In rough priority order:

1. **Surface the failure.** If `Configuring S2S app role
   assignments` only succeeds for some of the three resources, log
   each failure (one line per resource) with the underlying Graph
   API error, and exit non-zero. Today the CLI's success criterion
   appears to be "at least one role assigned", which lets two
   silent failures slide through.
2. **Clarify the "non-admin user" message.** The operator was Global
   Admin in both reproductions. Either drop the message when the
   subsequent grant succeeds via the cached admin token, or
   distinguish "non-admin attempting via PowerShell fallback" from
   "Global Admin via cached MSAL token" in the wording.
3. **Document the dependency**, if "S2S role assignment may legitimately
   skip when X" is the intended behaviour. The current docs at
   <https://learn.microsoft.com/en-us/microsoft-agent-365/developer/registration>
   and the `--m365` agent permissions flow imply all three are
   wired.

### Workaround

Operators currently need to assign the missing app roles by hand via
Microsoft Graph (`POST /servicePrincipals/<sp>/appRoleAssignments`)
or via the Entra portal. Documenting that as a known step would
unblock today.

### Tracking

Reproduced and discussed in the operator-facing runbook for a third-
party Hermes integration:
<https://github.com/satscryption/Hermes-A365/blob/main/references/live-tenant-test.md>
(open-bug table, entry #18). Happy to provide additional logs or
re-run with `--verbose` against a specific commit if useful.
