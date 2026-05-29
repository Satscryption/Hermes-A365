# A365 agent blueprint properties

Snapshot date: 2026-05-04

Microsoft does not publish a JSON Schema for A365 agent blueprints; the
properties below are authored by example from
<https://learn.microsoft.com/en-us/microsoft-agent-365/developer/registration>
and the GA preview docs. The skill renders blueprints via
`templates/blueprint.json.j2`; this file is the canonical "we know about
this property" allowlist consulted by the (TODO) `--allow-unknown` flag
and by `references/error-codes.md` drift checks.

> **Path B needs a *separate* Entra app — not this blueprint app.** The
> blueprint app is an agentic-application class and cannot mint Bot
> Framework S2S tokens (`AADSTS82001`). Path B (Copilot Chat via Azure
> Bot Service) therefore requires a **separate non-agentic Entra app**
> with the `Bot.Connector` scope, surfaced as `A365_BF_APP_ID` /
> `A365_BF_CLIENT_SECRET`. See `references/live-tenant-test.md` §11.2.5
> for the registration walk (#36, shipped v0.6.0).

## Top-level properties (rendered by `blueprint.json.j2`)

| Property | Type | Required | Notes |
|---|---|---|---|
| `agentIdentity` | object | yes | Identity block. Sub-fields: `slug`, `description`, `purpose`. |
| `displayName` | string | yes | Human-readable name; defaults to `slug` if not set. |
| `appRoles` | array&lt;string&gt; | yes | Default: `["User"]`. |
| `optionalClaims` | object | no | OpenID Connect optional claims block. Sub-field: `idToken` (array). |
| `policies` | object | yes | Sub-fields: `dlp`, `externalAccess`, `logging`. |
| `workIqTools` | array&lt;string&gt; | no | Subset of `mail`/`calendar`/`sharepoint`/`teams`/`tasks`/`people`. |
| `functions` | array&lt;string&gt; | no | Skill-defined function names. Default: `[]`. |

## `agentIdentity` sub-properties

| Property | Type | Notes |
|---|---|---|
| `slug` | string | Stable identifier; **renaming requires cleanup-then-recreate** (SPEC §6.13). |
| `description` | string | One-line purpose statement. |
| `purpose` | string | Free-form category (e.g. `productivity`, `compliance`, `support`). |

## `policies` sub-properties

| Property | Allowed values | Notes |
|---|---|---|
| `dlp` | `default-restricted`, `default-strict`, custom policy id | DLP policy applied at message ingress/egress. |
| `externalAccess` | `tenant-only`, `multi-tenant`, `public` | Who can interact with the agent. |
| `logging` | `verbose`, `standard`, `minimal` | Span/event verbosity. `verbose` is the SPEC default. |

## Server-assigned fields (stripped before diffing)

These appear on the **actual** payload returned by `query-entra` but are
*not* rendered by the desired blueprint. `blueprint_create.py` strips
them before handing the actual to `reconcile_blueprint`:

- `blueprintId` / `blueprint_id` / `id`
- `createdAt`
- `lastPatched` / `last_patched`
- `etag`

When Microsoft adds a new server-assigned field, add it to
`_SERVER_ASSIGNED_FIELDS` in `hermes_a365.blueprint_create` so it doesn't
trigger a phantom patch.

## Reconciliation semantics

- Top-level `displayName` mismatch → `patch`.
- `agentIdentity.slug` mismatch → `abort` (rename requires cleanup +
  recreate per SPEC §6.13).
- `workIqTools` is compared positionally (the renderer sorts the list, so
  callers get deterministic diffs).
- `policies` sub-fields compare strictly; a custom `dlp` value diffs as
  itself, not as a normalisation of `default-restricted`.

## Known gaps in this snapshot

These are spec-mentioned but not yet rendered by the template:

- `securityClassification` — referenced in some Microsoft docs but not in
  the GA `query-entra` output as of 2026-05-04.
- `delegationModel` — preview-only; do not encode (SPEC §14.1 risk row).
- `costCenter` — appears in some example payloads; treat as unknown until
  a tenant confirms it's required.

When `query-entra --blueprint=<slug>` returns a property not in this
file, `blueprint_create.py` (currently) treats it as a server-assigned
extra. If the property turns out to be operator-meaningful, add a
template variable + this file row + `BlueprintInputs` field, in that
order.
