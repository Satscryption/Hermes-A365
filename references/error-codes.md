# Error codes

Snapshot date: 2026-05-04

Catalogue of AADSTS / A365 / Bot Framework error codes the skill detects,
the surface that emits each one, and the recovery posture. The
`scripts/register.py::AADSTSError` exception captures any `AADSTS<code>`
token in CLI stderr; specific codes get specific handling in the apply
loops.

## Codes the skill specifically handles

| Code | Surface | Meaning | Skill behaviour |
|---|---|---|---|
| `AADSTS500011` | `register` (T1/T2 setup) | Resource principal `Microsoft.Agent365` not found in tenant — license has not propagated yet. | Retry up to 3× with 30 s backoff (configurable, mockable in tests). |
| `AADSTS90094` | `register` (FIC configure), any post-register call | Admin consent required and not yet granted. | `register` records `consent_deferred=True`, surfaces a follow-up to run `hermes a365 consent`, and exits 0 (the apps are still created). Other commands surface the error. |
| `AADSTS70043` | `activity-bridge` runtime, `fic rotate` reasons | Refresh token expired — user-FIC needs rotation. | Surfaced in spec example with `hermes a365 fic rotate <slug>` remediation hint. (Activity bridge is currently TODO.) |
| `AADSTS65001` | Any delegated-permission call | Scope not consented or not granted. | Surfaced as a fatal error; remediation is `hermes a365 consent` or operator-side consent. |

## Codes the skill surfaces but does not specifically handle

These propagate as generic `AADSTSError`; the operator's first port of
call is the spec §9.1 troubleshooting table:

- `AADSTS50034` — user/principal not found.
- `AADSTS90002` — tenant not found.
- `AADSTS50105` — assignment required (license not assigned to the user).
- `AADSTS50076` / `AADSTS50079` — MFA required (operator-side, not skill's
  fault).

## A365-specific delegated scopes (drift-tracked)

Microsoft has signalled that A365 scope names may evolve during the GA
window. The doctor (`scripts/doctor.py`) calls `a365 query-entra --scopes`
and compares against the snapshot below. When drift is detected, the
**live** name is authoritative; update this file and bump the snapshot.

| Scope (current snapshot) | Type | Used for |
|---|---|---|
| `openid` | OIDC | Sign-in token |
| `profile` | OIDC | Read basic profile |
| `email` | OIDC | Owner email |
| `offline_access` | OIDC | Refresh tokens |
| `User.Read` | Microsoft Graph | FIC subject lookup |
| `AgentIdentity.ReadWrite.All` | A365 (delegated) | Manage agent identity record |
| `AgentBlueprint.ReadWrite.All` | A365 (delegated) | Create/patch blueprints |
| `Mail.Read`, `Mail.Send` | Microsoft Graph | Per-Work-IQ (`mail`) |
| `Calendars.Read`, `Calendars.ReadWrite` | Microsoft Graph | Per-Work-IQ (`calendar`) |
| `Files.Read.All`, `Sites.Read.All` | Microsoft Graph | Per-Work-IQ (`sharepoint`) |
| `Chat.Read`, `Chat.ReadWrite`, `ChannelMessage.Read.All` | Microsoft Graph | Per-Work-IQ (`teams`) |
| `Tasks.ReadWrite` | Microsoft Graph | Per-Work-IQ (`tasks`) |
| `People.Read` | Microsoft Graph | Per-Work-IQ (`people`) |

**Application permissions are explicitly unsupported** by A365 and silently
break the runtime (SPEC §6.2.1). The skill only requests delegated
permissions.

## Bot Framework activity errors

The activity bridge (TODO; blocked on §10 Q1) will need to handle BF-side
errors distinct from AADSTS. Placeholder for the catalogue:

- `BadRequest` on activity post — malformed Adaptive Card.
- `Forbidden` on activity post — instance not deployed to the channel that
  emitted the activity.
- `NotFound` on conversation reference — conversation expired (Teams TTL).

This section will be filled in once the activity bridge ships.
