# OpenTelemetry config & span schema

Snapshot date: 2026-05-04

A365 auto-instruments registered agents (SPEC §6.8). This document
captures the canonical event vocabulary, span attributes, and sampler
configuration that the activity bridge (when it ships) and the
`telemetry` verifier rely on.

## OTLP endpoint

Inherited from the A365 tenant config and stored in the per-agent .env
as `HERMES_OTLP_ENDPOINT`. Format:

```
https://<tenant>.otel.agent365.microsoft.com
```

Authentication uses the T2 confidential-client secret pulled from the OS
keychain; the endpoint accepts OTLP/HTTP with `Authorization: Bearer
<token>`.

## Canonical event vocabulary

A365 emits five canonical events per agent invocation:

| Event name | Emitter | Span kind | Notes |
|---|---|---|---|
| `agent.received` | A365 (channel adapter) | `SERVER` | Conversation arrived at the bridge endpoint. |
| `agent.responded` | Hermes activity bridge | `CLIENT` | Reply posted back. Set `response_status_code` attribute. |
| `agent.tool_invoked` | Hermes runtime | `INTERNAL` | One per Work IQ tool call. Set `tool_name` attribute. |
| `agent.error` | Either | `INTERNAL` | Any exception that escapes a handler. Sets `exception.*` attributes per OTel conventions. |
| `agent.cost` | A365 (billing) | `INTERNAL` | Token / API call counters. Operator-visible in admin centre. |

## Required span attributes

Every span the bridge emits must include:

| Attribute | Source | Notes |
|---|---|---|
| `agent.identity` | `AGENT_IDENTITY` env var | The slug. |
| `agent.tenant_id` | `A365_TENANT_ID` env var | Tenant that owns the agent. |
| `agent.app_id` | `A365_APP_ID` env var | T2 confidential client. |
| `agent.instance_id` | `AA_INSTANCE_ID` env var | Per-agent instance id. |
| `agent.channel` | Activity payload | `teams` / `outlook` / `m365copilot`. |
| `service.name` | Constant: `hermes-a365` | OTel resource attribute. |
| `service.version` | Skill version | Bumped per SPEC §13. |

## Sampler

A365 ships with `parent_based(root=traceidratio(0.1))` by default; the
activity bridge MUST respect the parent decision so traces span the
A365 ingress + Hermes runtime + outbound channel post.

The `telemetry` verifier (`scripts/telemetry.py`) surfaces the live
sampler from `query-entra --telemetry`; if it differs from the spec
default, that's a tenant policy choice, not drift.

## What `query-entra --telemetry` returns (speculative shape)

```json
{
  "last_span": "2026-05-04T18:31:00Z",
  "sampler": "parent_based(root=traceidratio(0.1))",
  "spans_per_minute": 0.7,
  "errors_per_hour": 0
}
```

Field names use snake_case in this snapshot; the parser also accepts
camelCase aliases (`lastSpan`) as a defensive measure since both CLI
variants have shipped slightly different shapes during preview.

## Drift handling

- New canonical event added by A365 → record here, no code change unless
  the bridge wants to forward it specially.
- Attribute renamed (e.g. `agent.tenant_id` → `agent.tenantId`) → update
  bridge + this file in lockstep; the verifier doesn't yet check
  attribute names.
- Sampler config changed at tenant level → reflected by the verifier;
  **not** a drift event.
